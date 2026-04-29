"""W4-T1 阶段 4 · SweepWatch trace 落盘归档器（jsonl 一行一帧）。

目的：
    - 用户原则 8 (回归检查) + 决策 4：trace 必须落盘，未来"效果打分"才有数据基础
    - 每次 build_sweep_watch 调用产出的 trace + 双侧决策都写一行 jsonl
    - 不阻塞主接口（写盘失败不抛出，只 warning）

设计：
    - 路径：data/sweep_watch/{COIN}/{YYYYMMDD}.jsonl（UTC+8 日界）
    - 每行 = 一次 build 的完整记录（含 below/above 决策 + trace_log）
    - 保留 7 天；超过自动删除（trace 量大于普通 snapshot）
    - 单行 ~5-15 KB，单币每天典型量级 < 5 MB；7 天 / 8 币种总量 < 300 MB
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from models.sweep_watch import BrainSweepWatch

logger = logging.getLogger(__name__)


_ROOT = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "sweep_watch",
    )
)
_KEEP_DAYS = 7
_TZ_CN = timezone(timedelta(hours=8))
_WRITE_LOCK = threading.Lock()
# {coin → 最后一次 cleanup 的日期 YYYYMMDD}
# 改为按日幂等：长期运行进程跨日时仍会触发新一轮清理（修复进程级 set 永不再清的 bug）
_CLEANUP_LAST_DATE: dict[str, str] = {}


def _today_str() -> str:
    return datetime.now(tz=_TZ_CN).strftime("%Y%m%d")


def _coin_dir(coin: str) -> str:
    return os.path.join(_ROOT, coin.upper())


def _file_path(coin: str, date_str: Optional[str] = None) -> str:
    return os.path.join(_coin_dir(coin), f"{date_str or _today_str()}.jsonl")


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _cleanup_old_files(coin: str) -> None:
    """删除 _KEEP_DAYS 天之前的 jsonl。

    幂等粒度：每币每天清一次（避免每帧重复 IO，又确保跨日运行的进程不会漏清）。
    """
    today = _today_str()
    if _CLEANUP_LAST_DATE.get(coin) == today:
        return
    _CLEANUP_LAST_DATE[coin] = today
    coin_dir = _coin_dir(coin)
    if not os.path.isdir(coin_dir):
        return
    now = datetime.now(tz=_TZ_CN)
    threshold = now - timedelta(days=_KEEP_DAYS)
    for name in os.listdir(coin_dir):
        if not name.endswith(".jsonl"):
            continue
        try:
            d = datetime.strptime(name[:8], "%Y%m%d").replace(tzinfo=_TZ_CN)
            if d < threshold:
                os.remove(os.path.join(coin_dir, name))
        except Exception:  # pragma: no cover
            continue


def _serialize(snapshot: "BrainSweepWatch") -> dict:
    """直接调用 pydantic model_dump（保留 ts_iso / trace 等所有字段）。"""
    return snapshot.model_dump()


def append_sweep_watch_frame(snapshot: "BrainSweepWatch") -> None:
    """主入口：把一次 build 结果追加到当日 jsonl。

    错误处理：磁盘满 / 权限不足 / 序列化异常都仅 warning，不抛出。
    主接口 (trading_brain) 永远不能因为 trace 落盘失败而失败。
    """
    if snapshot is None:
        return
    coin = (snapshot.coin or "UNKNOWN").upper()
    try:
        line = json.dumps(_serialize(snapshot), ensure_ascii=False, default=str)
    except Exception as exc:  # pragma: no cover
        logger.warning("sweep_watch serialize failed for %s: %s", coin, exc)
        return
    path = _file_path(coin)
    with _WRITE_LOCK:
        try:
            _ensure_dir(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
        except Exception as exc:  # pragma: no cover
            logger.warning("sweep_watch append failed for %s: %s", coin, exc)
            return
    _cleanup_old_files(coin)


def read_sweep_watch_frames(
    coin: str,
    *,
    date_str: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """读取某币某日的 trace 帧（默认今天）；测试 / 后验脚本用。"""
    path = _file_path(coin, date_str)
    if not os.path.isfile(path):
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if limit and len(out) >= limit:
                    break
    except Exception as exc:  # pragma: no cover
        logger.warning("sweep_watch read failed for %s: %s", coin, exc)
    return out
