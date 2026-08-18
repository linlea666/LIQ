"""订单流小时/日桶 SQLite 存储（P2 · 零 Coinglass 配额新功能）。

定位：
    把 CoinState 内存态里"5m×100 点滚动窗口"的 CVD / taker / 大单成交数据
    沉淀为长期小时桶与日桶，回答"每小时/每天大额买卖总和"这类问题，
    供支撑/阻力承接分析与抄底参考。数据源全部复用现有内存态，不发新请求。

复用决策（独立新写的理由）：
    trend_store 服务趋势状态机、bottom_model_store 是日级模型存储，
    复用任何一个都会造成不相关耦合；本模块 schema/生命周期完全独立。

设计：
    - 独立 SQLite：data/orderflow/orderflow.sqlite3（WAL，与 trend_store 同范式）
    - hourly_flows：主键 (coin, market, hour_ts)，taker 列由聚合器整桶重算
      幂等 upsert（自愈）；large_executed / whale 列由增量累加（restart 后
      丢失的增量用 coverage 诚实标记，不伪造）
    - daily_flows：主键 (coin, market, day_key)，UTC+8 日界（与归档目录一致），
      由小时桶汇总 upsert
    - 保留：小时桶 180 天 / 日桶 400 天（config.yaml retention 段可调），
      init 时清理一次 + 聚合器每日触发
    - 线程安全：RLock + check_same_thread=False；写入量极小
      （3 币 × 2 市场 × 每 5 分钟），同步调用不阻塞 event loop
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TZ_CN = timezone(timedelta(hours=8))

_DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "orderflow",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hourly_flows (
    coin TEXT NOT NULL,
    market TEXT NOT NULL,              -- 'spot' | 'futures'
    hour_ts INTEGER NOT NULL,          -- 小时起点 epoch 秒（UTC 整点）
    taker_buy_usd REAL NOT NULL DEFAULT 0,
    taker_sell_usd REAL NOT NULL DEFAULT 0,
    net_usd REAL NOT NULL DEFAULT 0,
    large_executed_bid_usd REAL NOT NULL DEFAULT 0,   -- 大额买单被动成交
    large_executed_ask_usd REAL NOT NULL DEFAULT 0,   -- 大额卖单被动成交
    whale_buy_usd REAL NOT NULL DEFAULT 0,            -- P3: aggTrade 大额主动买
    whale_sell_usd REAL NOT NULL DEFAULT 0,           -- P3: aggTrade 大额主动卖
    whale_buy_qty REAL NOT NULL DEFAULT 0,            -- P4: 鲸鱼买入数量（VWAP=usd/qty）
    whale_sell_qty REAL NOT NULL DEFAULT 0,           -- P4: 鲸鱼卖出数量
    price_high REAL NOT NULL DEFAULT 0,               -- P4: 桶内成交价高点（Binance 单源）
    price_low REAL NOT NULL DEFAULT 0,                -- P4: 桶内成交价低点（0=无数据）
    price_close REAL NOT NULL DEFAULT 0,              -- P4: 桶内最后成交价
    samples INTEGER NOT NULL DEFAULT 0,               -- 观测到的 5m bar 数
    coverage_pct REAL NOT NULL DEFAULT 0,             -- samples/12，诚实标记断档
    updated_ts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (coin, market, hour_ts)
);
CREATE INDEX IF NOT EXISTS idx_hourly_hour ON hourly_flows(hour_ts DESC);

CREATE TABLE IF NOT EXISTS daily_flows (
    coin TEXT NOT NULL,
    market TEXT NOT NULL,
    day_key TEXT NOT NULL,             -- YYYYMMDD（UTC+8 日界，与归档一致）
    taker_buy_usd REAL NOT NULL DEFAULT 0,
    taker_sell_usd REAL NOT NULL DEFAULT 0,
    net_usd REAL NOT NULL DEFAULT 0,
    large_executed_bid_usd REAL NOT NULL DEFAULT 0,
    large_executed_ask_usd REAL NOT NULL DEFAULT 0,
    whale_buy_usd REAL NOT NULL DEFAULT 0,
    whale_sell_usd REAL NOT NULL DEFAULT 0,
    whale_buy_qty REAL NOT NULL DEFAULT 0,
    whale_sell_qty REAL NOT NULL DEFAULT 0,
    price_high REAL NOT NULL DEFAULT 0,
    price_low REAL NOT NULL DEFAULT 0,
    price_close REAL NOT NULL DEFAULT 0,
    hours_covered INTEGER NOT NULL DEFAULT 0,         -- 有数据的小时桶数
    coverage_pct REAL NOT NULL DEFAULT 0,             -- hours_covered/24
    updated_ts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (coin, market, day_key)
);
CREATE INDEX IF NOT EXISTS idx_daily_day ON daily_flows(day_key DESC);
"""

# P4 新增列（生产库已存在旧 schema，init 时按缺列 ALTER TABLE 迁移，幂等）
_MIGRATE_COLUMNS: list[tuple[str, str]] = [
    ("whale_buy_qty", "REAL NOT NULL DEFAULT 0"),
    ("whale_sell_qty", "REAL NOT NULL DEFAULT 0"),
    ("price_high", "REAL NOT NULL DEFAULT 0"),
    ("price_low", "REAL NOT NULL DEFAULT 0"),
    ("price_close", "REAL NOT NULL DEFAULT 0"),
]


def day_key_cn(ts: int) -> str:
    """epoch 秒 → UTC+8 日键 YYYYMMDD。"""
    return datetime.fromtimestamp(int(ts), tz=_TZ_CN).strftime("%Y%m%d")


class OrderflowStore:
    def __init__(
        self,
        data_dir: str = _DEFAULT_DIR,
        hourly_keep_days: int = 180,
        daily_keep_days: int = 400,
    ) -> None:
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "orderflow.sqlite3")
        self._hourly_keep_days = max(7, int(hourly_keep_days))
        self._daily_keep_days = max(30, int(daily_keep_days))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
        self._migrate()
        self.cleanup()

    def _migrate(self) -> None:
        """旧库缺列时 ALTER TABLE 补齐（幂等；新库由 _SCHEMA 直接建全）。"""
        with self._lock, self._conn:
            for table in ("hourly_flows", "daily_flows"):
                existing = {
                    row["name"]
                    for row in self._conn.execute(f"PRAGMA table_info({table})")
                }
                for col, decl in _MIGRATE_COLUMNS:
                    if col not in existing:
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col} {decl}"
                        )
                        logger.info(
                            "[orderflow_store] migrated | table=%s +%s", table, col,
                        )

    # ── 写入 ───────────────────────────────────────────────────────────

    def upsert_taker_hour(
        self,
        coin: str,
        market: str,
        hour_ts: int,
        buy_usd: float,
        sell_usd: float,
        samples: int,
    ) -> None:
        """整桶重算幂等写 taker 列（不触碰 large_executed/whale 增量列）。"""
        now = int(time.time())
        coverage = round(min(1.0, samples / 12.0), 4)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO hourly_flows (
                    coin, market, hour_ts, taker_buy_usd, taker_sell_usd,
                    net_usd, samples, coverage_pct, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(coin, market, hour_ts) DO UPDATE SET
                    taker_buy_usd = excluded.taker_buy_usd,
                    taker_sell_usd = excluded.taker_sell_usd,
                    net_usd = excluded.net_usd,
                    samples = excluded.samples,
                    coverage_pct = excluded.coverage_pct,
                    updated_ts = excluded.updated_ts
                """,
                (
                    coin.upper(), market, int(hour_ts),
                    float(buy_usd), float(sell_usd),
                    float(buy_usd) - float(sell_usd),
                    int(samples), coverage, now,
                ),
            )

    def add_large_executed(
        self, coin: str, market: str, hour_ts: int,
        bid_usd: float = 0.0, ask_usd: float = 0.0,
    ) -> None:
        """大额挂单被动成交增量累加（bid=买单被吃，ask=卖单被吃）。"""
        if bid_usd <= 0 and ask_usd <= 0:
            return
        self._add_incremental(coin, market, hour_ts, {
            "large_executed_bid_usd": float(max(0.0, bid_usd)),
            "large_executed_ask_usd": float(max(0.0, ask_usd)),
        })

    def add_whale_trades(
        self, coin: str, market: str, hour_ts: int,
        buy_usd: float = 0.0, sell_usd: float = 0.0,
        buy_qty: float = 0.0, sell_qty: float = 0.0,
    ) -> None:
        """P3：aggTrade 大额主动成交增量累加（P4 追加 qty，供 VWAP=usd/qty）。"""
        if buy_usd <= 0 and sell_usd <= 0:
            return
        self._add_incremental(coin, market, hour_ts, {
            "whale_buy_usd": float(max(0.0, buy_usd)),
            "whale_sell_usd": float(max(0.0, sell_usd)),
            "whale_buy_qty": float(max(0.0, buy_qty)),
            "whale_sell_qty": float(max(0.0, sell_qty)),
        })

    def merge_price_stats(
        self, coin: str, market: str, hour_ts: int,
        high: float = 0.0, low: float = 0.0, close: float = 0.0,
    ) -> None:
        """P4：桶内成交价统计合并（high 取 MAX、low 取非零 MIN、close 覆盖）。

        调用方（trades_ws flush）按时间顺序冲桶，同一桶后一次 flush 的 close
        即最新成交价，直接覆盖即可（0 视为无数据，不覆盖）。
        """
        if high <= 0 and low <= 0 and close <= 0:
            return
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO hourly_flows (
                    coin, market, hour_ts,
                    price_high, price_low, price_close, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(coin, market, hour_ts) DO UPDATE SET
                    price_high = MAX(price_high, excluded.price_high),
                    price_low = CASE
                        WHEN price_low <= 0 THEN excluded.price_low
                        WHEN excluded.price_low <= 0 THEN price_low
                        ELSE MIN(price_low, excluded.price_low)
                    END,
                    price_close = CASE
                        WHEN excluded.price_close > 0 THEN excluded.price_close
                        ELSE price_close
                    END,
                    updated_ts = excluded.updated_ts
                """,
                (
                    coin.upper(), market, int(hour_ts),
                    float(max(0.0, high)), float(max(0.0, low)),
                    float(max(0.0, close)), now,
                ),
            )

    def _add_incremental(
        self, coin: str, market: str, hour_ts: int, cols: dict[str, float],
    ) -> None:
        """按列名字典做增量累加 upsert（列名为模块内部常量，无注入面）。"""
        names = list(cols.keys())
        col_sql = ", ".join(names)
        update_sql = ", ".join(f"{c} = {c} + excluded.{c}" for c in names)
        placeholders = ", ".join("?" for _ in names)
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                f"""
                INSERT INTO hourly_flows (coin, market, hour_ts, {col_sql}, updated_ts)
                VALUES (?, ?, ?, {placeholders}, ?)
                ON CONFLICT(coin, market, hour_ts) DO UPDATE SET
                    {update_sql},
                    updated_ts = excluded.updated_ts
                """,
                (coin.upper(), market, int(hour_ts), *cols.values(), now),
            )

    def rollup_daily(self, coin: str, market: str, day_key: str) -> None:
        """把某 UTC+8 日的小时桶汇总进 daily_flows（幂等）。"""
        day_start = int(
            datetime.strptime(day_key, "%Y%m%d").replace(tzinfo=_TZ_CN).timestamp()
        )
        day_end = day_start + 86400
        now = int(time.time())
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT
                    COALESCE(SUM(taker_buy_usd), 0) AS buy,
                    COALESCE(SUM(taker_sell_usd), 0) AS sell,
                    COALESCE(SUM(large_executed_bid_usd), 0) AS leb,
                    COALESCE(SUM(large_executed_ask_usd), 0) AS lea,
                    COALESCE(SUM(whale_buy_usd), 0) AS wb,
                    COALESCE(SUM(whale_sell_usd), 0) AS ws,
                    COALESCE(SUM(whale_buy_qty), 0) AS wbq,
                    COALESCE(SUM(whale_sell_qty), 0) AS wsq,
                    COALESCE(MAX(price_high), 0) AS ph,
                    COALESCE(MIN(CASE WHEN price_low > 0 THEN price_low END), 0) AS pl,
                    COUNT(*) AS hours
                FROM hourly_flows
                WHERE coin = ? AND market = ? AND hour_ts >= ? AND hour_ts < ?
                """,
                (coin.upper(), market, day_start, day_end),
            ).fetchone()
            if not row or int(row["hours"]) == 0:
                return
            close_row = self._conn.execute(
                """
                SELECT price_close FROM hourly_flows
                WHERE coin = ? AND market = ? AND hour_ts >= ? AND hour_ts < ?
                  AND price_close > 0
                ORDER BY hour_ts DESC LIMIT 1
                """,
                (coin.upper(), market, day_start, day_end),
            ).fetchone()
            price_close = float(close_row["price_close"]) if close_row else 0.0
            self._conn.execute(
                """
                INSERT INTO daily_flows (
                    coin, market, day_key, taker_buy_usd, taker_sell_usd, net_usd,
                    large_executed_bid_usd, large_executed_ask_usd,
                    whale_buy_usd, whale_sell_usd,
                    whale_buy_qty, whale_sell_qty,
                    price_high, price_low, price_close,
                    hours_covered, coverage_pct, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(coin, market, day_key) DO UPDATE SET
                    taker_buy_usd = excluded.taker_buy_usd,
                    taker_sell_usd = excluded.taker_sell_usd,
                    net_usd = excluded.net_usd,
                    large_executed_bid_usd = excluded.large_executed_bid_usd,
                    large_executed_ask_usd = excluded.large_executed_ask_usd,
                    whale_buy_usd = excluded.whale_buy_usd,
                    whale_sell_usd = excluded.whale_sell_usd,
                    whale_buy_qty = excluded.whale_buy_qty,
                    whale_sell_qty = excluded.whale_sell_qty,
                    price_high = excluded.price_high,
                    price_low = excluded.price_low,
                    price_close = excluded.price_close,
                    hours_covered = excluded.hours_covered,
                    coverage_pct = excluded.coverage_pct,
                    updated_ts = excluded.updated_ts
                """,
                (
                    coin.upper(), market, day_key,
                    float(row["buy"]), float(row["sell"]),
                    float(row["buy"]) - float(row["sell"]),
                    float(row["leb"]), float(row["lea"]),
                    float(row["wb"]), float(row["ws"]),
                    float(row["wbq"]), float(row["wsq"]),
                    float(row["ph"]), float(row["pl"]), price_close,
                    int(row["hours"]),
                    round(min(1.0, int(row["hours"]) / 24.0), 4),
                    now,
                ),
            )

    # ── 查询 ───────────────────────────────────────────────────────────

    def query_hourly(
        self,
        coin: str,
        market: Optional[str] = None,
        start_ts: int = 0,
        end_ts: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM hourly_flows WHERE coin = ?"
        args: list[Any] = [coin.upper()]
        if market:
            sql += " AND market = ?"
            args.append(market)
        if start_ts > 0:
            sql += " AND hour_ts >= ?"
            args.append(int(start_ts))
        if end_ts > 0:
            sql += " AND hour_ts < ?"
            args.append(int(end_ts))
        sql += " ORDER BY hour_ts DESC LIMIT ?"
        args.append(max(1, min(5000, int(limit))))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def query_daily(
        self,
        coin: str,
        market: Optional[str] = None,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM daily_flows WHERE coin = ?"
        args: list[Any] = [coin.upper()]
        if market:
            sql += " AND market = ?"
            args.append(market)
        sql += " ORDER BY day_key DESC LIMIT ?"
        args.append(max(1, min(2000, int(limit))))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ── 维护 ───────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """按保留期删除过期桶（init + 聚合器每日调用）。"""
        now = int(time.time())
        hourly_cutoff = now - self._hourly_keep_days * 86400
        daily_cutoff_key = day_key_cn(now - self._daily_keep_days * 86400)
        try:
            with self._lock, self._conn:
                h = self._conn.execute(
                    "DELETE FROM hourly_flows WHERE hour_ts < ?", (hourly_cutoff,),
                ).rowcount
                d = self._conn.execute(
                    "DELETE FROM daily_flows WHERE day_key < ?", (daily_cutoff_key,),
                ).rowcount
            if h or d:
                logger.info(
                    "[orderflow_store] cleanup | hourly=%d daily=%d removed", h, d,
                )
        except Exception:
            logger.warning("[orderflow_store] cleanup failed", exc_info=True)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            h = self._conn.execute("SELECT COUNT(*) AS n FROM hourly_flows").fetchone()
            d = self._conn.execute("SELECT COUNT(*) AS n FROM daily_flows").fetchone()
        return {
            "hourly_rows": int(h["n"]),
            "daily_rows": int(d["n"]),
            "hourly_keep_days": self._hourly_keep_days,
            "daily_keep_days": self._daily_keep_days,
            "path": self.path,
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
