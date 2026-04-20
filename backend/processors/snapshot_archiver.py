"""P2.4 · 历史快照归档器

每次 AI Trader pipeline 产出 (snapshot, execution_plan, ai_trader_report,
final_decision) 四件套时，把它们作为一个"帧"写入按天滚动的 JSONL.gz 文件。

目的：
    - 回放历史决策（/replay 页面）
    - 事后复盘：为什么当时做这个判断
    - 迭代 prompt 时有"同一时刻的历史数据 + 新策略"对照基准

设计：
    - 每天一个文件：data/replay/YYYYMMDD.jsonl.gz（UTC+8 日界）
    - 保留最近 30 天；超过自动删除
    - 每帧大小通常 30-100KB；每币每小时 ~1 帧 → 单日 ~72 帧，gz 后约 2-5MB
    - 读取：按 coin + 时间区间；懒加载（只解压需要的文件）
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


_ROOT = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "replay",
    )
)
_KEEP_DAYS = 30
_TZ_CN = timezone(timedelta(hours=8))       # UTC+8 日界


@dataclass
class ReplayFrame:
    ts: int
    coin: str
    snapshot: dict
    execution_plan: Optional[dict]
    ai_trader_report: Optional[dict]
    final_decision: Optional[dict]
    price_at_capture: float = 0.0
    ai_analysis_brief: str = ""             # 简要摘要供列表页渲染，避免再次解压全文

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "coin": self.coin,
            "price_at_capture": self.price_at_capture,
            "ai_analysis_brief": self.ai_analysis_brief,
            "snapshot": self.snapshot,
            "execution_plan": self.execution_plan,
            "ai_trader_report": self.ai_trader_report,
            "final_decision": self.final_decision,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReplayFrame":
        return cls(
            ts=int(d.get("ts", 0) or 0),
            coin=str(d.get("coin", "")).upper(),
            price_at_capture=float(d.get("price_at_capture", 0) or 0),
            ai_analysis_brief=str(d.get("ai_analysis_brief", "")),
            snapshot=d.get("snapshot") or {},
            execution_plan=d.get("execution_plan"),
            ai_trader_report=d.get("ai_trader_report"),
            final_decision=d.get("final_decision"),
        )


class SnapshotArchiver:
    """按天滚动归档 pipeline 快照帧（进程单例）"""

    def __init__(self, root: str = _ROOT, keep_days: int = _KEEP_DAYS) -> None:
        self._lock = threading.RLock()
        self._root = root
        self._keep_days = keep_days
        os.makedirs(self._root, exist_ok=True)
        self._last_gc_ts = 0

    # ── 写入 ───────────────────────────────────────

    def append(
        self,
        coin: str,
        snapshot: Any,
        execution_plan: Any = None,
        ai_trader_report: Any = None,
        final_decision: Any = None,
        price_at_capture: Optional[float] = None,
        ai_analysis_brief: str = "",
    ) -> bool:
        """把一个 pipeline 帧追加到当日归档

        返回 True 表示成功落盘。各字段允许 None / pydantic model / dict。
        pydantic 对象会调用 model_dump()。
        """
        try:
            frame = ReplayFrame(
                ts=int(time.time()),
                coin=str(coin).upper(),
                snapshot=_to_plain(snapshot) or {},
                execution_plan=_to_plain(execution_plan),
                ai_trader_report=_to_plain(ai_trader_report),
                final_decision=_to_plain(final_decision),
                price_at_capture=float(price_at_capture or 0),
                ai_analysis_brief=(ai_analysis_brief or "")[:280],
            )
            path = self._path_for_day(_day_key(frame.ts))
            with self._lock:
                with gzip.open(path, "at", encoding="utf-8") as f:
                    f.write(json.dumps(frame.to_dict(), ensure_ascii=False) + "\n")
            self._gc_if_due()
            return True
        except Exception as e:
            logger.debug("[P2.4] append failed: %s", e, exc_info=True)
            return False

    # ── 读取 ───────────────────────────────────────

    def list_days(self) -> list[str]:
        try:
            return sorted(
                f.replace(".jsonl.gz", "")
                for f in os.listdir(self._root)
                if f.endswith(".jsonl.gz")
            )
        except OSError:
            return []

    def read_range(
        self,
        coin: Optional[str] = None,
        since_ts: Optional[int] = None,
        until_ts: Optional[int] = None,
        limit: int = 200,
    ) -> list[dict]:
        """按时间区间 + coin 查询，最多返回 limit 条（新的在前）"""
        coin_u = coin.upper() if coin else None
        lo = int(since_ts or 0)
        hi = int(until_ts or 0)
        limit = max(1, min(1000, int(limit or 200)))

        # 确定需要扫哪些天
        days = self.list_days()
        if lo or hi:
            lo_day = _day_key(lo) if lo else None
            hi_day = _day_key(hi) if hi else None
            if lo_day:
                days = [d for d in days if d >= lo_day]
            if hi_day:
                days = [d for d in days if d <= hi_day]

        # 顺序语义：
        # - 跨天：从最新天向最老天扫描
        # - 天内：文件是按追加顺序（老→新），所以读完后反转即可得"新→老"
        # - 为保持"同 ts 多帧仍然新在前"，不能只靠 sort（stable sort 会保留原始顺序），
        #   而应在每个天内单独 reverse，再按天外层拼接
        collected: list[dict] = []
        for day in reversed(days):
            day_items: list[dict] = []
            for frame in self._iter_file(self._path_for_day(day)):
                if coin_u and frame.coin != coin_u:
                    continue
                if lo and frame.ts < lo:
                    continue
                if hi and frame.ts > hi:
                    continue
                day_items.append({
                    "ts": frame.ts,
                    "coin": frame.coin,
                    "price_at_capture": frame.price_at_capture,
                    "ai_analysis_brief": frame.ai_analysis_brief,
                    "has_plan": frame.execution_plan is not None,
                    "has_ai_report": frame.ai_trader_report is not None,
                    "has_final": frame.final_decision is not None,
                })
            # 当天内：追加顺序即时间顺序（老在前），反转后新在前
            day_items.reverse()
            collected.extend(day_items)
            if len(collected) >= limit:
                break
        return collected[:limit]

    def read_frame(self, coin: str, ts: int) -> Optional[dict]:
        """按 (coin, ts) 精确读取某一帧全部内容"""
        coin_u = coin.upper()
        day = _day_key(ts)
        path = self._path_for_day(day)
        if not os.path.exists(path):
            return None
        for frame in self._iter_file(path):
            if frame.coin == coin_u and frame.ts == ts:
                return frame.to_dict()
        return None

    # ── 内部 ────────────────────────────────────────

    def _path_for_day(self, day_key: str) -> str:
        return os.path.join(self._root, f"{day_key}.jsonl.gz")

    def _iter_file(self, path: str) -> Iterable[ReplayFrame]:
        if not os.path.exists(path):
            return
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield ReplayFrame.from_dict(json.loads(line))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
        except (OSError, EOFError) as e:
            logger.debug("[P2.4] read file failed path=%s err=%s", path, e)

    def _gc_if_due(self) -> None:
        now = int(time.time())
        if now - self._last_gc_ts < 3600:
            return
        self._last_gc_ts = now
        try:
            days = self.list_days()
            if len(days) <= self._keep_days:
                return
            stale = days[:-self._keep_days]
            for d in stale:
                path = self._path_for_day(d)
                try:
                    os.remove(path)
                    logger.info("[P2.4] archive rotated out | day=%s", d)
                except OSError:
                    pass
        except Exception:
            logger.debug("[P2.4] gc failed", exc_info=True)

    def reset_for_testing(self) -> None:
        with self._lock:
            if os.path.isdir(self._root):
                for f in os.listdir(self._root):
                    try:
                        os.remove(os.path.join(self._root, f))
                    except OSError:
                        pass
            self._last_gc_ts = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _to_plain(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    # pydantic v2
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            pass
    # pydantic v1
    dump1 = getattr(obj, "dict", None)
    if callable(dump1):
        try:
            return dump1()
        except Exception:
            pass
    # dataclass
    try:
        from dataclasses import asdict, is_dataclass
        if is_dataclass(obj):
            return asdict(obj)
    except Exception:
        pass
    # 最后尝试 vars
    try:
        return dict(vars(obj))
    except Exception:
        return None


def _day_key(ts: int) -> str:
    dt = datetime.fromtimestamp(int(ts or time.time()), tz=_TZ_CN)
    return dt.strftime("%Y%m%d")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_instance: Optional[SnapshotArchiver] = None
_instance_lock = threading.Lock()


def get_snapshot_archiver() -> SnapshotArchiver:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = SnapshotArchiver()
    return _instance


def reset_for_testing(
    root: Optional[str] = None, keep_days: int = _KEEP_DAYS
) -> SnapshotArchiver:
    global _instance
    with _instance_lock:
        _instance = SnapshotArchiver(
            root=root or _ROOT, keep_days=keep_days,
        )
    return _instance
