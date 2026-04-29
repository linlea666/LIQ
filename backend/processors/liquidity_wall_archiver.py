"""流动性墙引擎历史落盘器（W1-T2）。

回应审计 P1-2：
    1-2 周观察期到期后必须能用真实数据校准 trust / break_through / active_attack
    权重；零落盘 = 凭感觉调参。本文件提供轻量摘要落盘（不含原始 bins）。

设计要点（与既有 processors/snapshot_archiver.py 范式对齐）：
    - 同步 IO + threading.RLock，主路径用 run_in_executor 推到 thread pool
    - 不 gzip：纯 JSONL，便于 `jq / rg / pandas.read_json(lines=True)` 直读
    - 按币/按天分文件：data/liquidity_wall_history/{COIN}/{YYYYMMDD}.jsonl
    - 90 天 GC（snapshot_archiver 是 30 天，本模块要更长观察期）
    - 磁盘水位保护：> 80% 跳过落盘 + 日志告警，30s 抑制再检测
    - 写入失败全部 best-effort（吞异常），绝不影响主轮询

落盘 schema（向前兼容 W1-T4 / W2-T1 字段）：
    {
      "ts": 1714327200,
      "coin": "BTC",
      "last_price": 76234.5,
      "atr": 312.0,
      "data_quality": "ok",                 # warming/partial/stale/missing
      "walls_above": [<wall_summary>, ...], # 卖墙
      "walls_below": [<wall_summary>, ...], # 买墙
      "wall_events": [<event_summary>, ...],
      "crowding_global": {...},
      "top_sweep_targets": [...],
    }

每个 <wall_summary> 含（W1-T4/W2-T1 后会被填充更多字段）：
    wall_zone_id (W1-T4)、source、price_mid/low/high、current_usd、max_usd_1h、
    trust_score、raw_trust_score (W2-T1)、trust_components (W2-T1)、
    support_resistance_trust_score (W2-T1)、sweep_attractiveness_score (W2-T1)、
    break_through_risk、active_attack_score、coinbase_spot_confluence、
    coinbase_num_orders、persistence_min、source_age_sec
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────────────────────

_DEFAULT_ROOT = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "liquidity_wall_history",
    )
)
_KEEP_DAYS = 90                       # 1-2 周观察期需更长保留
_TZ_CN = timezone(timedelta(hours=8))

# 磁盘水位保护
_DISK_HIGH_WATERMARK = 0.80           # > 80% 跳过落盘
_DISK_CHECK_INTERVAL_SEC = 30         # 高水位后 30s 内不再检测


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Archiver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LiquidityWallArchiver:
    """流动性墙快照落盘器（process-singleton，线程安全）。

    职责：
      - 接收 OrderbookPressureSnapshot（pydantic model）+ source_age dict
      - 提炼摘要（轻量 schema，不含原始 bins）
      - 按 coin/day 写入 JSONL（同步 + RLock）
      - 90 天 GC（每小时触发一次）
      - 磁盘高水位时跳过 + 日志告警

    使用：
      archiver = get_archiver()
      archiver.append(snapshot, source_age={...})  # 同步快路径，< 1ms
      # 或 async 路径：
      await loop.run_in_executor(None, archiver.append, snapshot, source_age)
    """

    def __init__(self, root: str = _DEFAULT_ROOT, keep_days: int = _KEEP_DAYS) -> None:
        self._lock = threading.RLock()
        self._root = root
        self._keep_days = keep_days
        os.makedirs(self._root, exist_ok=True)
        self._last_gc_ts = 0
        # 磁盘水位状态
        self._disk_full_until_ts: float = 0.0      # 高水位抑制到何时
        self._dropped_count: int = 0               # 高水位 + queue 满计数（运维用）
        self._success_count: int = 0
        self._error_count: int = 0

    # ── 写入 ───────────────────────────────────────────────────────────

    def append(
        self,
        snapshot: Any,
        source_age: Optional[dict[str, int]] = None,
    ) -> bool:
        """提炼并落盘一帧 OrderbookPressureSnapshot。

        参数：
            snapshot: OrderbookPressureSnapshot 实例（pydantic model）
            source_age: dict[endpoint, age_sec]，从 metrics 取，作为冗余审计
        返回：
            True = 落盘成功；False = 跳过（snapshot 无效 / 高水位 / 异常）
        """
        if snapshot is None:
            return False

        # 磁盘水位保护
        if self._is_disk_high():
            self._dropped_count += 1
            return False

        try:
            row = self._build_row(snapshot, source_age or {})
            if row is None:
                return False
            day_key = _day_key(int(row["ts"]))
            path = self._path_for(row["coin"], day_key)

            with self._lock:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                self._success_count += 1
            self._gc_if_due()
            return True
        except Exception as e:
            self._error_count += 1
            logger.debug("[liquidity_wall_archiver] append failed: %s", e, exc_info=True)
            return False

    # ── 读取（后验脚本用，不在主路径调用）───────────────────────────────

    def list_days(self, coin: Optional[str] = None) -> list[str]:
        """列出已有数据的日期（YYYYMMDD）。

        coin=None 返回所有币的并集；指定 coin 返回该币的日期列表。
        """
        days: set[str] = set()
        try:
            if coin:
                d = os.path.join(self._root, coin.upper())
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        if f.endswith(".jsonl"):
                            days.add(f[:-len(".jsonl")])
            else:
                if not os.path.isdir(self._root):
                    return []
                for c in os.listdir(self._root):
                    sub = os.path.join(self._root, c)
                    if not os.path.isdir(sub):
                        continue
                    for f in os.listdir(sub):
                        if f.endswith(".jsonl"):
                            days.add(f[:-len(".jsonl")])
        except OSError:
            return []
        return sorted(days)

    def stats(self) -> dict:
        """运维用统计。"""
        with self._lock:
            return {
                "success_count": self._success_count,
                "error_count": self._error_count,
                "dropped_count": self._dropped_count,
                "disk_high_until_ts": int(self._disk_full_until_ts),
                "keep_days": self._keep_days,
                "root": self._root,
            }

    def reset_for_testing(self) -> None:
        """仅测试用：清空目录 + 计数。"""
        with self._lock:
            if os.path.isdir(self._root):
                for c in os.listdir(self._root):
                    sub = os.path.join(self._root, c)
                    if os.path.isdir(sub):
                        for f in os.listdir(sub):
                            try:
                                os.remove(os.path.join(sub, f))
                            except OSError:
                                pass
                        try:
                            os.rmdir(sub)
                        except OSError:
                            pass
            self._last_gc_ts = 0
            self._disk_full_until_ts = 0.0
            self._dropped_count = 0
            self._success_count = 0
            self._error_count = 0

    # ── 内部 ───────────────────────────────────────────────────────────

    def _path_for(self, coin: str, day: str) -> str:
        return os.path.join(self._root, coin.upper(), f"{day}.jsonl")

    def _is_disk_high(self) -> bool:
        """检查根目录所在分区水位；高位时返回 True 并打印告警（30s 抑制）。"""
        now = time.time()
        if now < self._disk_full_until_ts:
            return True
        try:
            usage = shutil.disk_usage(self._root)
            ratio = (usage.total - usage.free) / max(usage.total, 1)
            if ratio >= _DISK_HIGH_WATERMARK:
                self._disk_full_until_ts = now + _DISK_CHECK_INTERVAL_SEC
                logger.warning(
                    "[liquidity_wall_archiver] disk high | usage=%.1f%% root=%s | "
                    "skipping writes for %ds",
                    ratio * 100, self._root, _DISK_CHECK_INTERVAL_SEC,
                )
                return True
        except Exception:
            # 水位检查失败时不阻塞落盘（退化为不保护）
            return False
        return False

    def _gc_if_due(self) -> None:
        """每小时检查一次，删除 > keep_days 的日文件。"""
        now = int(time.time())
        if now - self._last_gc_ts < 3600:
            return
        self._last_gc_ts = now
        try:
            cutoff_dt = datetime.now(_TZ_CN) - timedelta(days=self._keep_days)
            cutoff_key = cutoff_dt.strftime("%Y%m%d")
            for c in os.listdir(self._root):
                sub = os.path.join(self._root, c)
                if not os.path.isdir(sub):
                    continue
                for f in os.listdir(sub):
                    if not f.endswith(".jsonl"):
                        continue
                    day_key = f[:-len(".jsonl")]
                    if day_key < cutoff_key:
                        try:
                            os.remove(os.path.join(sub, f))
                            logger.info(
                                "[liquidity_wall_archiver] rotated out | coin=%s day=%s",
                                c, day_key,
                            )
                        except OSError:
                            pass
        except Exception:
            logger.debug("[liquidity_wall_archiver] gc failed", exc_info=True)

    def _build_row(self, snapshot: Any, source_age: dict[str, int]) -> Optional[dict]:
        """提炼 OrderbookPressureSnapshot → 落盘 dict（轻量 schema）。"""
        coin = (getattr(snapshot, "coin", None) or "").upper()
        ts_sec = int(getattr(snapshot, "ts_sec", 0) or 0)
        if not coin or ts_sec <= 0:
            return None

        last_price = float(getattr(snapshot, "last_price", 0) or 0)
        atr = getattr(snapshot, "atr", None)
        data_quality = getattr(snapshot, "data_quality", "") or ""

        walls_above_raw = list(getattr(snapshot, "walls_above", None) or [])
        walls_below_raw = list(getattr(snapshot, "walls_below", None) or [])
        events_raw = list(getattr(snapshot, "wall_events", None) or [])
        crowding = getattr(snapshot, "crowding_global", None)
        sweep_targets_raw = list(getattr(snapshot, "top_sweep_targets", None) or [])

        return {
            "ts": ts_sec,
            "coin": coin,
            "last_price": round(last_price, 6) if last_price else 0.0,
            "atr": round(float(atr), 4) if atr else None,
            "data_quality": data_quality,
            "walls_above": [_summarize_wall(w) for w in walls_above_raw],
            "walls_below": [_summarize_wall(w) for w in walls_below_raw],
            "wall_events": [_summarize_event(e) for e in events_raw],
            "crowding_global": _summarize_crowding(crowding),
            "top_sweep_targets": [_summarize_sweep(s) for s in sweep_targets_raw],
            "source_age": dict(source_age) if source_age else {},
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 提炼工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _summarize_wall(z: Any) -> dict:
    """WallZone → 落盘摘要。

    向前兼容：W1-T4 / W2-T1 引入的字段（wall_zone_id / raw_trust_score /
    trust_components / SR/SA）通过 getattr 默认值兼容。落地后自动填充。
    """
    out = {
        "wall_zone_id": getattr(z, "wall_zone_id", "") or "",
        "side": getattr(z, "side", ""),
        "source": getattr(z, "source", ""),
        "dual_source": bool(getattr(z, "dual_source", False)),
        "price_mid": _round(getattr(z, "price_mid", 0), 4),
        "price_low": _round(getattr(z, "price_low", 0), 4),
        "price_high": _round(getattr(z, "price_high", 0), 4),
        "current_usd": _round(getattr(z, "current_usd", 0), 0),
        "max_usd_1h": _round(getattr(z, "max_usd_1h", 0), 0),
        "trust_score": _round(getattr(z, "trust_score", 0), 3),
        "raw_trust_score": _round(getattr(z, "raw_trust_score", 0), 3),
        "trust_components": dict(getattr(z, "trust_components", {}) or {}),
        "support_resistance_trust_score":
            _round(getattr(z, "support_resistance_trust_score", 0), 3),
        "sweep_attractiveness_score":
            _round(getattr(z, "sweep_attractiveness_score", 0), 3),
        "break_through_risk": _round(getattr(z, "break_through_risk", 0), 3),
        "active_attack_score": _round(getattr(z, "active_attack_score", 0), 3),
        "wall_removal_risk": _round(getattr(z, "wall_removal_risk", 0), 3),
        "wall_consumed_confidence": _round(getattr(z, "wall_consumed_confidence", 0), 3),
        "persistence_score": _round(getattr(z, "persistence_score", 0), 3),
        "persistence_min": _round(getattr(z, "visible_minutes", 0), 1),
        "exchange_count": int(getattr(z, "exchange_count", 0) or 0),
        "has_spot_confluence": bool(getattr(z, "has_spot_confluence", False)),
        "spot_current_usd": _round(getattr(z, "spot_current_usd", 0), 0),
        "coinbase_spot_confluence": bool(getattr(z, "coinbase_spot_confluence", False)),
        "coinbase_spot_usd": _round(getattr(z, "coinbase_spot_usd", 0), 0),
        "coinbase_num_orders": int(getattr(z, "coinbase_num_orders", 0) or 0),
        "trend": getattr(z, "trend", ""),
    }
    next_magnet = getattr(z, "next_magnet_price", None)
    if next_magnet is not None:
        out["next_magnet_price"] = _round(next_magnet, 4)
    sweep = getattr(z, "sweep_target", None)
    if sweep is not None:
        out["vacuum_gap_pct"] = _round(getattr(sweep, "vacuum_gap_pct", 0), 2)
    return out


def _summarize_event(e: Any) -> dict:
    return {
        "wall_zone_id": getattr(e, "wall_zone_id", "") or "",
        "event_type": getattr(e, "event_type", ""),
        "side": getattr(e, "side", ""),
        "price_mid": _round(getattr(e, "price_mid", 0), 4),
        "ts": int(getattr(e, "ts_sec", 0) or 0),
        "size_before_usd": _opt_round(getattr(e, "size_before_usd", None), 0),
        "size_after_usd": _opt_round(getattr(e, "size_after_usd", None), 0),
        "executed_usd_value": _opt_round(getattr(e, "executed_usd_value", None), 0),
        "confidence": _round(getattr(e, "confidence", 0), 3),
    }


def _summarize_crowding(cg: Any) -> Optional[dict]:
    if cg is None:
        return None
    return {
        "oi_delta_1h_pct": _opt_round(getattr(cg, "oi_delta_1h_pct", None), 4),
        "oi_delta_24h_pct": _opt_round(getattr(cg, "oi_delta_24h_pct", None), 4),
        "oi_margin_split": getattr(cg, "oi_margin_split", ""),
        "funding_now_pct": _opt_round(getattr(cg, "funding_now_pct", None), 6),
        "funding_avg_8h_pct": _opt_round(getattr(cg, "funding_avg_8h_pct", None), 6),
        "funding_percentile_30d": _opt_round(getattr(cg, "funding_percentile_30d", None), 3),
        "top_position_ls_ratio": _opt_round(getattr(cg, "top_position_ls_ratio", None), 3),
        "global_account_ls_ratio": _opt_round(getattr(cg, "global_account_ls_ratio", None), 3),
        "inferred_position_state": getattr(cg, "inferred_position_state", ""),
        "long_crowding_risk": _round(getattr(cg, "long_crowding_risk", 0), 3),
        "short_crowding_risk": _round(getattr(cg, "short_crowding_risk", 0), 3),
    }


def _summarize_sweep(s: Any) -> dict:
    return {
        "magnet_price": _round(getattr(s, "magnet_price", 0), 4),
        "direction": getattr(s, "direction", ""),
        "magnet_amount_usd": _round(getattr(s, "magnet_amount_usd", 0), 0),
        "vacuum_gap_pct": _round(getattr(s, "vacuum_gap_pct", 0), 2),
        "distance_pct": _round(getattr(s, "distance_pct", 0), 3),
    }


def _round(v: Any, ndigits: int) -> float:
    try:
        return round(float(v or 0), ndigits)
    except (TypeError, ValueError):
        return 0.0


def _opt_round(v: Any, ndigits: int) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _day_key(ts: int) -> str:
    dt = datetime.fromtimestamp(int(ts or time.time()), tz=_TZ_CN)
    return dt.strftime("%Y%m%d")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Process-level singleton
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_instance: Optional[LiquidityWallArchiver] = None
_instance_lock = threading.Lock()


def get_archiver() -> LiquidityWallArchiver:
    """获取全局 archiver 实例（首次调用时懒初始化）。"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = LiquidityWallArchiver()
    return _instance


def reset_archiver_for_test(
    root: Optional[str] = None, keep_days: int = _KEEP_DAYS
) -> LiquidityWallArchiver:
    """仅测试用：重置全局实例，可指定独立 root。"""
    global _instance
    with _instance_lock:
        _instance = LiquidityWallArchiver(
            root=root or _DEFAULT_ROOT, keep_days=keep_days,
        )
    return _instance
