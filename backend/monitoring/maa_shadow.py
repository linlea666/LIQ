"""Market Action Analyzer · Shadow Logger（Phase 5-A）

设计目标
--------
- 每次 `MarketActionArbiter.analyze` 成功出报告（或 fallback）→ 追加一条
  `kind=report` 的紧凑 JSONL，记录 **报告时刻的完整关键字段 + 价格**，
  作为后续 T+4h/T+8h/T+24h 兑现评估的"信号源"。
- 另外每 ~15 分钟为每个币种落一条 `kind=heartbeat`（仅 ts + coin + price），
  让 evaluator 在任意 target_ts 附近都能找到最近价格快照。
- **不影响主链路**：fire-and-forget 写入；内部 asyncio Queue + 后台 worker。
- **磁盘友好**：每条记录字段裁剪后 <600 字节；按北京时间日期 + 币种切文件。

文件布局
--------
    backend/logs/maa_shadow/
        2026-04-20/
            BTC.jsonl
            ETH.jsonl
            SOL.jsonl

每条记录（JSONL 单行）
------------------------
report 记录 ::
    {
      "ts": 1776920000,
      "coin": "BTC",
      "kind": "report",
      "price": 107345.6,
      "scenario": "exhaustion_top",
      "phase": "distribution",
      "bias": "short",
      "confidence": 65,
      "data_quality": "ok",
      "continuity": "refinement",
      "alt_scenario": "range_bound",
      "alt_prob": 20,
      "parse_ok": true,
      "invalidations": ["price > 78100 持续 15m 且 OI 同步 +1%"]
    }

heartbeat 记录 ::
    { "ts": 1776923600, "coin": "BTC", "kind": "heartbeat", "price": 107500.1 }

使用方式
--------
在 engine 中：
    logger = get_maa_shadow_logger()
    logger.start()                     # lifespan startup
    logger.record_report(coin, report, price)  # 每次分析完成后
    logger.record_heartbeat(coin, price)       # 每 15 分钟一次

在 maa_eval 中 :: `find_nearest_price(coin, target_ts, max_drift_sec=30*60)`
会在最近 3 天的 jsonl 里寻找 ts 距 target_ts 最接近的记录取 price。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BJ_TZ = timezone(timedelta(hours=8))
_FLUSH_INTERVAL_SEC = 3.0
_QUEUE_MAXSIZE = 1000
_HEARTBEAT_MIN_INTERVAL_SEC = 14 * 60  # 15 分钟内不重复写 heartbeat


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def shadow_log_root() -> str:
    """Shadow 日志根目录：<repo>/backend/logs/maa_shadow"""
    return os.path.join(_repo_root(), "logs", "maa_shadow")


def _day_slug(ts: float | int) -> str:
    """unix 时间戳 → 北京时间日期（YYYY-MM-DD）。"""
    dt = datetime.fromtimestamp(float(ts), tz=_BJ_TZ)
    return dt.strftime("%Y-%m-%d")


def list_available_dates(max_days: int = 30) -> list[str]:
    """扫描日志目录，返回有记录的日期（YYYY-MM-DD 降序）。"""
    root = shadow_log_root()
    if not os.path.isdir(root):
        return []
    dates = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isdir(path) and len(name) == 10 and name[4] == "-":
            dates.append(name)
    dates.sort(reverse=True)
    return dates[:max_days]


@dataclass
class _HeartbeatState:
    last_ts: float
    last_price: float


class MarketActionShadowLogger:
    """MAA 信号 + 价格 heartbeat 影子记录器 · 进程级单例。"""

    def __init__(self, root_dir: Optional[str] = None):
        self._root = root_dir or shadow_log_root()
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker_task: Optional[asyncio.Task] = None
        self._open_files: dict[str, Any] = {}  # "YYYY-MM-DD/COIN" -> fh
        self._heartbeats: dict[str, _HeartbeatState] = {}
        self._dropped = 0
        self._written = 0
        self._started = False

    # ── 生命周期 ─────────────────────────────────────────────
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            os.makedirs(self._root, exist_ok=True)
        except Exception:
            logger.warning("[MAA-Shadow] makedirs failed: %s", self._root, exc_info=True)
        try:
            loop = asyncio.get_running_loop()
            self._worker_task = loop.create_task(self._worker(), name="maa_shadow_writer")
            logger.info("[MAA-Shadow] writer started root=%s", self._root)
        except RuntimeError:
            logger.warning("[MAA-Shadow] no running loop; lazy-start on first enqueue")

    async def stop(self) -> None:
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
        for fh in self._open_files.values():
            try:
                fh.close()
            except Exception:
                pass
        self._open_files.clear()
        self._started = False

    # ── 记录入口 ─────────────────────────────────────────────
    def record_report(self, coin: str, report: Any, price: float) -> None:
        """落一条 MAA 报告记录（包含 scenario/bias/confidence 等核心字段）。"""
        if report is None or not coin:
            return
        try:
            if hasattr(report, "model_dump"):
                d = report.model_dump()
            elif isinstance(report, dict):
                d = report
            else:
                return
        except Exception:
            logger.debug("[MAA-Shadow] model_dump failed", exc_info=True)
            return

        ts = int(d.get("timestamp") or time.time())
        ti = d.get("trading_implications") or {}
        alt = d.get("alternative_scenario") or {}
        cont = d.get("continuity") or {}
        pd = d.get("prompt_debug") or {}

        rec = {
            "ts": ts,
            "coin": coin.upper(),
            "kind": "report",
            "price": float(price) if price else 0.0,
            "scenario": d.get("scenario"),
            "phase": d.get("market_phase"),
            "bias": ti.get("bias"),
            "confidence": int(d.get("confidence") or 0),
            "data_quality": d.get("data_quality"),
            "continuity": cont.get("stance"),
            "prev_scenario": cont.get("previous_scenario"),
            "alt_scenario": alt.get("scenario"),
            "alt_prob": alt.get("probability_pct"),
            "parse_ok": bool(pd.get("parse_ok", True)),
            "invalidations": [str(x)[:160] for x in (d.get("invalidation_conditions") or [])][:4],
        }
        self._enqueue(rec)

    def record_heartbeat(self, coin: str, price: float) -> None:
        """落一条价格 heartbeat；内部去重（15 分钟内同币种不重复写）。"""
        if not coin or not price:
            return
        coin_up = coin.upper()
        now = time.time()
        last = self._heartbeats.get(coin_up)
        if last is not None and (now - last.last_ts) < _HEARTBEAT_MIN_INTERVAL_SEC:
            return
        self._heartbeats[coin_up] = _HeartbeatState(last_ts=now, last_price=float(price))
        rec = {
            "ts": int(now),
            "coin": coin_up,
            "kind": "heartbeat",
            "price": float(price),
        }
        self._enqueue(rec)

    # ── 内部入队 ─────────────────────────────────────────────
    def _enqueue(self, rec: dict) -> None:
        if not self._started:
            self.start()
        try:
            self._queue.put_nowait(rec)
        except asyncio.QueueFull:
            try:
                _ = self._queue.get_nowait()
                self._queue.task_done()
                self._queue.put_nowait(rec)
                self._dropped += 1
            except Exception:
                self._dropped += 1

    # ── 后台 writer ─────────────────────────────────────────
    async def _worker(self) -> None:
        buf: list[dict] = []
        while True:
            try:
                first = await self._queue.get()
                buf.append(first)
                self._queue.task_done()
                deadline = time.monotonic() + _FLUSH_INTERVAL_SEC
                while time.monotonic() < deadline:
                    try:
                        buf.append(self._queue.get_nowait())
                        self._queue.task_done()
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(0.2)
                self._flush_batch(buf)
                buf.clear()
            except asyncio.CancelledError:
                while True:
                    try:
                        buf.append(self._queue.get_nowait())
                        self._queue.task_done()
                    except asyncio.QueueEmpty:
                        break
                if buf:
                    self._flush_batch(buf)
                raise
            except Exception:
                logger.exception("[MAA-Shadow] writer loop error")
                await asyncio.sleep(1.0)

    def _get_handle(self, day: str, coin: str):
        key = f"{day}/{coin}"
        fh = self._open_files.get(key)
        if fh is not None:
            return fh
        dir_path = os.path.join(self._root, day)
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception:
            return None
        path = os.path.join(dir_path, f"{coin}.jsonl")
        try:
            fh = open(path, "a", encoding="utf-8", buffering=1)
        except Exception:
            logger.warning("[MAA-Shadow] open failed: %s", path, exc_info=True)
            return None
        self._open_files[key] = fh
        return fh

    def _flush_batch(self, batch: list[dict]) -> None:
        if not batch:
            return
        groups: dict[tuple[str, str], list[str]] = {}
        for rec in batch:
            day = _day_slug(rec["ts"])
            coin = rec["coin"]
            try:
                line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                continue
            groups.setdefault((day, coin), []).append(line)
        for (day, coin), lines in groups.items():
            fh = self._get_handle(day, coin)
            if fh is None:
                continue
            try:
                fh.write("\n".join(lines) + "\n")
                self._written += len(lines)
            except Exception:
                logger.warning("[MAA-Shadow] write failed", exc_info=True)
                try:
                    fh.close()
                except Exception:
                    pass
                self._open_files.pop(f"{day}/{coin}", None)

    def stats(self) -> dict:
        return {
            "started": self._started,
            "written": self._written,
            "dropped": self._dropped,
            "queue_size": self._queue.qsize(),
            "open_files": len(self._open_files),
        }


# ── 读取辅助（供 maa_eval 使用）──────────────────────────────

def iter_records(coin: str, days: int = 7) -> list[dict]:
    """读取最近 N 天的 MAA shadow 记录，按 ts 升序返回。

    Args:
        coin: 币种
        days: 向前回看多少天（含今天）

    Returns:
        list[dict]：JSON 反序列化后的记录列表；读失败单条跳过。
    """
    if days <= 0:
        days = 1
    coin_up = coin.upper()
    root = shadow_log_root()
    if not os.path.isdir(root):
        return []

    now = time.time()
    wanted_days: set[str] = set()
    for offset in range(days + 1):  # 多取一天防跨日
        wanted_days.add(_day_slug(now - offset * 86400))

    records: list[dict] = []
    for day in wanted_days:
        path = os.path.join(root, day, f"{coin_up}.jsonl")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    records.append(rec)
        except Exception:
            logger.debug("[MAA-Shadow] read failed: %s", path, exc_info=True)
    records.sort(key=lambda r: r.get("ts", 0))
    return records


def find_nearest_price(
    coin: str,
    target_ts: float,
    *,
    max_drift_sec: int = 30 * 60,
    records: Optional[list[dict]] = None,
) -> Optional[float]:
    """在 MAA shadow 日志里找 ts 最靠近 target_ts 的一条记录的 price。

    Args:
        coin: 币种
        target_ts: 目标时间戳（秒）
        max_drift_sec: 允许的最大时间漂移（默认 30 分钟）。若最近一条也超过则返回 None。
        records: 可选 · 预先读取的记录列表，避免重复 I/O

    Returns:
        float 或 None
    """
    if records is None:
        records = iter_records(coin, days=2)
    if not records:
        return None
    best: Optional[tuple[float, float]] = None  # (drift, price)
    for rec in records:
        p = rec.get("price")
        if not p:
            continue
        drift = abs(float(rec.get("ts", 0)) - float(target_ts))
        if best is None or drift < best[0]:
            best = (drift, float(p))
    if best is None or best[0] > max_drift_sec:
        return None
    return best[1]


# ── 单例 ──────────────────────────────────────────────────

_singleton: Optional[MarketActionShadowLogger] = None


def get_maa_shadow_logger() -> MarketActionShadowLogger:
    global _singleton
    if _singleton is None:
        _singleton = MarketActionShadowLogger()
    return _singleton
