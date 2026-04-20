"""趋势衰竭模块 · Shadow Logger（P0-A）

设计目标
--------
- 把每次 `compute_trend_exhaustion` 的**完整读数** + 当时的**价格 / ATR / regime**
  原样落盘到 `logs/te_shadow/YYYY-MM-DD/{COIN}.jsonl`。
- **不影响主链路**：异步 queue + 后台 worker 每 3 秒批量 flush；主线程永不阻塞。
- **智能去重**：相同状态组合短时间内压缩为单条；每小时至少 heartbeat 一条，
  避免"慢漂移"导致评估数据稀疏。
- **为 P0-B 事后打标签准备 schema**：字段够全，future_price 查询只需对 ts + coin。

为什么不直接写通用 logging
--------------------------
通用 logging 走 stdout/text buffer，不适合结构化批处理。该模块**专用**于
**可回放、可聚合、可自动化打标**的信号轨迹数据，格式固定为 JSONL。

文件布局
--------
    logs/te_shadow/
        2026-04-20/
            BTC.jsonl
            ETH.jsonl
            SOL.jsonl
        2026-04-21/
            ...

每条记录（JSONL 单行）
------------------------
    {
      "ts": 1699999999,             # 信号产生时刻（unix 秒）
      "coin": "BTC",
      "price": 72345.67,            # ticker.last
      "atr": 450.2,                 # state.atr（小时级 ATR14，作为打标准化基准）
      "regime": "trend_up",
      "regime_vetoed": false,
      "consensus_level": "strong_agree",
      "overall": {
        "state": "healthy_continuation",
        "action": "hold",
        "direction": "up",
        "position_pct": 0.6,
        "plain_cn": "...",
        "tip_cn": "...",
        "reason_cn": "..."
      },
      "data_quality": "ok",
      "missing_inputs": [],
      "tf": {
        "1h": {"state": "...", "direction": "up", "m": 0.42, "p": 0.31,
                "e": -0.10, "c": 0.24, "age_min": 15, "confirmed": 1,
                "sub": [{"k": "m1_macd_2d", "s": 0.6, "n": "..."}]},
        "4h": {...},
        "1d": {...}
      },
      "reason": "state_change" | "heartbeat" | "score_drift"
    }

模块级单例：`get_te_shadow_logger()`。
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

# 北京时间 +08 作为 "日报期望日"——所有日期切片以北京时间 0:00 为界
_BJ_TZ = timezone(timedelta(hours=8))

# 去重窗口与 heartbeat 频率
_HEARTBEAT_SEC = 3600          # 每小时至少一条
_SCORE_DRIFT_THRESHOLD = 0.25  # composite_score 变动超过此值立即记录
_FLUSH_INTERVAL_SEC = 3.0      # 后台 worker flush 节奏
_QUEUE_MAXSIZE = 2000          # 队列上限，满即丢最旧保障不背压主线程


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def shadow_log_root() -> str:
    """Shadow 日志根目录：<repo>/backend/logs/te_shadow"""
    return os.path.join(_repo_root(), "logs", "te_shadow")


def _day_slug(ts: float | int) -> str:
    """把 unix 时间戳映射到北京时间日期（YYYY-MM-DD）。"""
    dt = datetime.fromtimestamp(float(ts), tz=_BJ_TZ)
    return dt.strftime("%Y-%m-%d")


def list_available_dates(max_days: int = 30) -> list[str]:
    """扫描日志目录，返回有记录的日期列表（降序）。"""
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
class _LastSeen:
    """每个 (coin, ts_hour_bucket) 级别的去重状态。"""

    wrote_at: float                  # 上次写入的 unix 时间
    overall_state: str
    overall_action: str
    overall_direction: str
    regime: str
    consensus: str
    composite_avg: float             # 三周期 composite 平均值


def _compact_sub(subs: list[dict] | None) -> list[dict]:
    """把 SubScore list 压成紧凑形式，减少磁盘占用。"""
    if not subs:
        return []
    out = []
    for s in subs:
        out.append({
            "k": s.get("key"),
            "s": round(float(s.get("score", 0.0)), 3),
            "n": s.get("note", "")[:80],  # note 最多 80 字防日志膨胀
        })
    return out


def _compact_tf(tf_state: dict | None) -> Optional[dict]:
    if not tf_state:
        return None
    return {
        "state": tf_state.get("state"),
        "direction": tf_state.get("direction"),
        "m": round(float(tf_state.get("momentum_score", 0.0)), 3),
        "p": round(float(tf_state.get("participation_score", 0.0)), 3),
        "e": round(float(tf_state.get("exhaustion_score", 0.0)), 3),
        "c": round(float(tf_state.get("composite_score", 0.0)), 3),
        "age_min": int(tf_state.get("state_age_min", 0) or 0),
        "confirmed": int(tf_state.get("confirmed_ticks", 0) or 0),
        "triggers": tf_state.get("triggers") or [],
        "sub": _compact_sub(tf_state.get("sub_scores")),
    }


def _avg_composite(signal_dict: dict) -> float:
    """三周期 composite 的平均值（缺失视为 0）。"""
    acc = 0.0
    cnt = 0
    for k in ("tf_1h", "tf_4h", "tf_1d"):
        v = signal_dict.get(k)
        if v:
            acc += float(v.get("composite_score", 0.0) or 0.0)
            cnt += 1
    return acc / cnt if cnt > 0 else 0.0


class TrendExhaustionShadowLogger:
    """趋势衰竭信号影子记录器 · 进程级单例。"""

    def __init__(self, root_dir: Optional[str] = None):
        self._root = root_dir or shadow_log_root()
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._worker_task: Optional[asyncio.Task] = None
        self._last_seen: dict[str, _LastSeen] = {}
        self._open_files: dict[str, Any] = {}  # "YYYY-MM-DD/COIN" -> file handle
        self._dropped_count = 0
        self._written_count = 0
        self._started = False

    def start(self) -> None:
        """在事件循环启动后调用（lifespan startup）。幂等。"""
        if self._started:
            return
        self._started = True
        try:
            os.makedirs(self._root, exist_ok=True)
        except Exception:
            logger.warning("[TE-Shadow] makedirs failed: %s", self._root, exc_info=True)
        try:
            loop = asyncio.get_running_loop()
            self._worker_task = loop.create_task(self._worker(), name="te_shadow_writer")
            logger.info("[TE-Shadow] writer task started, root=%s", self._root)
        except RuntimeError:
            logger.warning("[TE-Shadow] no running loop; will lazy-start on first log")

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

    def record(self, coin: str, signal: Any, price: float, atr: float) -> None:
        """非阻塞投递一条信号。

        Args:
            coin: 币种（大写）
            signal: TrendExhaustionSignal Pydantic 对象，或 model_dump() 得到的 dict
            price: 当时 ticker 价格
            atr: 当时 1h ATR14（作为后续打标的正则化基准）
        """
        if signal is None or not coin:
            return
        # 兼容 pydantic 对象 / dict
        try:
            if hasattr(signal, "model_dump"):
                sig_dict: dict = signal.model_dump()
            elif isinstance(signal, dict):
                sig_dict = signal
            else:
                return
        except Exception:
            logger.debug("[TE-Shadow] model_dump failed", exc_info=True)
            return

        ts_now = time.time()
        composite_avg = _avg_composite(sig_dict)

        # ── 去重判定 ─────────────────────────────────────────────
        overall_state = sig_dict.get("overall_state", "neutral")
        overall_action = sig_dict.get("overall_action", "stand_aside")
        overall_direction = sig_dict.get("overall_direction", "flat")
        regime = sig_dict.get("regime") or "unknown"
        consensus = sig_dict.get("consensus_level", "neutral")

        reason = "state_change"
        last = self._last_seen.get(coin)
        if last is not None:
            key_unchanged = (
                last.overall_state == overall_state
                and last.overall_action == overall_action
                and last.overall_direction == overall_direction
                and last.regime == regime
                and last.consensus == consensus
            )
            drifted = abs(composite_avg - last.composite_avg) >= _SCORE_DRIFT_THRESHOLD
            since_last = ts_now - last.wrote_at
            if key_unchanged and not drifted and since_last < _HEARTBEAT_SEC:
                return  # 跳过
            if key_unchanged and not drifted and since_last >= _HEARTBEAT_SEC:
                reason = "heartbeat"
            elif drifted and key_unchanged:
                reason = "score_drift"

        # ── 组装紧凑 payload ────────────────────────────────────
        record = {
            "ts": int(ts_now),
            "coin": coin.upper(),
            "price": float(price) if price else 0.0,
            "atr": float(atr) if atr else 0.0,
            "regime": regime,
            "regime_vetoed": bool(sig_dict.get("regime_vetoed", False)),
            "consensus_level": consensus,
            "data_quality": sig_dict.get("data_quality", "insufficient"),
            "missing_inputs": sig_dict.get("missing_inputs") or [],
            "overall": {
                "state": overall_state,
                "action": overall_action,
                "direction": overall_direction,
                "position_pct": round(float(sig_dict.get("overall_position_pct", 0.0) or 0.0), 3),
                "plain_cn": (sig_dict.get("overall_plain_cn") or "")[:100],
                "tip_cn": (sig_dict.get("overall_tip_cn") or "")[:100],
                "reason_cn": (sig_dict.get("overall_reason_cn") or "")[:120],
            },
            "tf": {
                "1h": _compact_tf(sig_dict.get("tf_1h")),
                "4h": _compact_tf(sig_dict.get("tf_4h")),
                "1d": _compact_tf(sig_dict.get("tf_1d")),
            },
            "reason": reason,
        }

        # 更新 last_seen 并投递
        self._last_seen[coin] = _LastSeen(
            wrote_at=ts_now,
            overall_state=overall_state,
            overall_action=overall_action,
            overall_direction=overall_direction,
            regime=regime,
            consensus=consensus,
            composite_avg=composite_avg,
        )

        # 懒启动 worker（首次 record 发生在事件循环中）
        if not self._started:
            self.start()

        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            # 队列满则丢最老的（而非阻塞主线程）
            try:
                _ = self._queue.get_nowait()
                self._queue.task_done()
                self._queue.put_nowait(record)
                self._dropped_count += 1
            except Exception:
                self._dropped_count += 1

    # ── 后台 writer ──────────────────────────────────────────
    async def _worker(self) -> None:
        buf: list[dict] = []
        while True:
            try:
                # 阻塞取第一条，再非阻塞批量收尾
                first = await self._queue.get()
                buf.append(first)
                self._queue.task_done()
                deadline = time.monotonic() + _FLUSH_INTERVAL_SEC
                while time.monotonic() < deadline:
                    try:
                        item = self._queue.get_nowait()
                        buf.append(item)
                        self._queue.task_done()
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(0.2)
                self._flush_batch(buf)
                buf.clear()
            except asyncio.CancelledError:
                # 退出前 flush
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
                logger.exception("[TE-Shadow] writer loop error")
                await asyncio.sleep(1.0)

    def _get_file_handle(self, day: str, coin: str):
        key = f"{day}/{coin}"
        fh = self._open_files.get(key)
        if fh is not None:
            return fh
        dir_path = os.path.join(self._root, day)
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception:
            logger.warning("[TE-Shadow] mkdir failed: %s", dir_path, exc_info=True)
            return None
        file_path = os.path.join(dir_path, f"{coin}.jsonl")
        try:
            fh = open(file_path, "a", encoding="utf-8", buffering=1)
        except Exception:
            logger.warning("[TE-Shadow] open failed: %s", file_path, exc_info=True)
            return None
        self._open_files[key] = fh
        return fh

    def _flush_batch(self, batch: list[dict]) -> None:
        if not batch:
            return
        # 按 (day, coin) 分组批量写
        groups: dict[tuple[str, str], list[str]] = {}
        for rec in batch:
            day = _day_slug(rec["ts"])
            coin = rec["coin"]
            try:
                line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                logger.debug("[TE-Shadow] json dump failed", exc_info=True)
                continue
            groups.setdefault((day, coin), []).append(line)

        for (day, coin), lines in groups.items():
            fh = self._get_file_handle(day, coin)
            if fh is None:
                continue
            try:
                fh.write("\n".join(lines) + "\n")
                self._written_count += len(lines)
            except Exception:
                logger.warning("[TE-Shadow] write failed", exc_info=True)
                # 关闭句柄，下次重开
                try:
                    fh.close()
                except Exception:
                    pass
                self._open_files.pop(f"{day}/{coin}", None)

    def stats(self) -> dict:
        return {
            "started": self._started,
            "written": self._written_count,
            "dropped": self._dropped_count,
            "queue_size": self._queue.qsize(),
            "open_files": len(self._open_files),
        }


# ── 单例 ──────────────────────────────────────────────────
_singleton: Optional[TrendExhaustionShadowLogger] = None


def get_te_shadow_logger() -> TrendExhaustionShadowLogger:
    global _singleton
    if _singleton is None:
        _singleton = TrendExhaustionShadowLogger()
    return _singleton
