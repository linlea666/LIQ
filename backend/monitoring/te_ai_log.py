"""TE · AI 解读结果 Shadow Log（独立于主 TE Shadow）

文件布局
--------
    logs/te_ai_interpret/
        2026-04-20/
            BTC.jsonl            # 结构化结果（小，每条 ~1KB）
            BTC.thinking.jsonl   # reasoning_content 归档（v4-flash 非思考模式下不再产出；R1/reasoner 时代的旧快照仍可存在）
            ETH.jsonl
            ETH.thinking.jsonl

为什么分两个文件
----------------
结构化结果用于事后统计（AI 准确率、AI vs 规则 A/B），读取频繁。
reasoning_content 是"调试/教学资产"，只在排查争议信号时偶尔回看，单独存避免污染 fast path 读取。
v4-flash 非思考模式下 reasoning_content 恒为空，thinking 文件不会新增；保留方案以备未来重启思考模式。

写入是同步的（按需触发频率低，不必异步队列）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from models.te_interpretation import TEAIInterpretation

logger = logging.getLogger(__name__)

_BJ_TZ = timezone(timedelta(hours=8))


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ai_log_root() -> str:
    return os.path.join(_repo_root(), "logs", "te_ai_interpret")


def _day_slug(ts: float | int) -> str:
    return datetime.fromtimestamp(float(ts), tz=_BJ_TZ).strftime("%Y-%m-%d")


def log_interpretation(
    result: "TEAIInterpretation",
    signal_snapshot: dict,
    price: float,
) -> None:
    """写入一条解读记录。

    Args:
        result: AI 解读结果
        signal_snapshot: 触发解读时的 TrendExhaustionSignal.model_dump() 快照
        price: 触发解读时的 ticker 价格（让事后打标能查"未来价格"）
    """
    try:
        day = _day_slug(result.ts)
        dir_path = os.path.join(ai_log_root(), day)
        os.makedirs(dir_path, exist_ok=True)
        coin = (result.coin or "UNKNOWN").upper()

        # ── 主记录（不含 reasoning） ───────────────
        main = {
            "ts": int(result.ts),
            "coin": coin,
            "price": float(price) if price else 0.0,
            "fingerprint": result.signal_fingerprint,
            "model": result.model,
            "cache_hit": result.cache_hit,
            "from_cache_age_sec": result.from_cache_age_sec,
            "latency_ms": result.latency_ms,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "reasoning_tokens": result.reasoning_tokens,
            "ai": {
                "summary_cn": result.summary_cn,
                "scenario": result.scenario,
                "trend_assessment": (
                    result.trend_assessment.model_dump()
                    if result.trend_assessment else None
                ),
                "level_projection": (
                    result.level_projection.model_dump()
                    if result.level_projection else None
                ),
                "trade_bias": (
                    result.trade_bias.model_dump()
                    if result.trade_bias else None
                ),
                "conflict_resolution": result.conflict_resolution,
                "traps": result.traps,
                "triggers_to_watch": result.triggers_to_watch,
                "independent_view": result.independent_view,
                "action_suggestion": result.action_suggestion,
                "confidence": result.confidence,
                "alignment_with_rules": result.alignment_with_rules,
                "alignment_reason": result.alignment_reason,
            },
            # 规则侧快照（压缩版，便于 A/B 对比）
            "rules_snapshot": {
                "overall_state": signal_snapshot.get("overall_state"),
                "overall_action": signal_snapshot.get("overall_action"),
                "overall_direction": signal_snapshot.get("overall_direction"),
                "consensus_level": signal_snapshot.get("consensus_level"),
                "regime": signal_snapshot.get("regime"),
                "regime_vetoed": signal_snapshot.get("regime_vetoed"),
                "overall_position_pct": signal_snapshot.get("overall_position_pct"),
            },
            "error": result.error,
        }

        main_path = os.path.join(dir_path, f"{coin}.jsonl")
        with open(main_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(main, ensure_ascii=False, separators=(",", ":")) + "\n")

        # ── reasoning（单独文件，避免主文件膨胀） ──
        if result.reasoning:
            think_path = os.path.join(dir_path, f"{coin}.thinking.jsonl")
            thinking = {
                "ts": int(result.ts),
                "coin": coin,
                "fingerprint": result.signal_fingerprint,
                "reasoning": result.reasoning,
            }
            with open(think_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(thinking, ensure_ascii=False, separators=(",", ":")) + "\n")

    except Exception:
        logger.warning("[TE-AI-Log] write failed", exc_info=True)


def list_available_dates(max_days: int = 30) -> list[str]:
    root = ai_log_root()
    if not os.path.isdir(root):
        return []
    dates = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isdir(path) and len(name) == 10 and name[4] == "-":
            dates.append(name)
    dates.sort(reverse=True)
    return dates[:max_days]


def read_history(coin: str, limit: int = 20, max_days: int = 30) -> list[dict]:
    """读取某币种最近 N 条 AI 解读记录（从新到旧）。

    只读主 JSONL（不读 thinking，避免大文件 IO）。从最近日期往前扫，
    够 limit 条即止。

    Args:
        coin: 大写币种，如 "BTC"
        limit: 最多返回条数
        max_days: 最多回溯多少天

    Returns:
        list[dict]：每项为 jsonl 行原样 dict，按 ts 降序。
    """
    coin_u = (coin or "").upper()
    if not coin_u:
        return []
    root = ai_log_root()
    if not os.path.isdir(root):
        return []
    results: list[dict] = []
    dates = list_available_dates(max_days)
    for day in dates:
        if len(results) >= limit:
            break
        path = os.path.join(root, day, f"{coin_u}.jsonl")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            results.append(obj)
            if len(results) >= limit:
                break
    results.sort(key=lambda r: int(r.get("ts", 0)), reverse=True)
    return results[:limit]


def read_detail(coin: str, ts: int, with_reasoning: bool = True) -> Optional[dict]:
    """按 ts 查找单条记录（精确匹配）。

    Args:
        coin: 大写币种
        ts: 记录 ts（unix 秒）
        with_reasoning: 是否附带 .thinking.jsonl 中同 fingerprint 的 reasoning

    Returns:
        命中则返回主记录 dict（若 with_reasoning=True 会附加 "reasoning" 字段），
        未命中返回 None。
    """
    coin_u = (coin or "").upper()
    if not coin_u or ts <= 0:
        return None
    root = ai_log_root()
    if not os.path.isdir(root):
        return None

    # ts 通常属于触发当天，但跨天边界容错 → 扫最近 2 天就够
    # 我们不知道 ts 具体对应哪天（可能服务器时区 vs 北京时区差），所以回退到扫所有最近 7 天
    target_fp: Optional[str] = None
    main_record: Optional[dict] = None
    for day in list_available_dates(max_days=7):
        path = os.path.join(root, day, f"{coin_u}.jsonl")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if int(obj.get("ts", 0)) == int(ts):
                        main_record = obj
                        target_fp = obj.get("fingerprint") or None
                        break
        except Exception:
            continue
        if main_record is not None:
            # 同天找 thinking
            if with_reasoning and target_fp:
                think_path = os.path.join(root, day, f"{coin_u}.thinking.jsonl")
                if os.path.isfile(think_path):
                    try:
                        with open(think_path, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    t_obj = json.loads(line)
                                except Exception:
                                    continue
                                if (
                                    t_obj.get("fingerprint") == target_fp
                                    and int(t_obj.get("ts", 0)) == int(ts)
                                ):
                                    main_record["reasoning"] = t_obj.get("reasoning") or ""
                                    break
                    except Exception:
                        pass
            return main_record
    return None


def stats() -> dict:
    """供 /api/te/reports 附带显示 AI 解读的健康度。"""
    root = ai_log_root()
    if not os.path.isdir(root):
        return {"days": 0, "total_records": 0}
    days = 0
    total = 0
    for name in os.listdir(root):
        day_dir = os.path.join(root, name)
        if not os.path.isdir(day_dir):
            continue
        days += 1
        for fname in os.listdir(day_dir):
            if fname.endswith(".jsonl") and not fname.endswith(".thinking.jsonl"):
                try:
                    with open(os.path.join(day_dir, fname), "r", encoding="utf-8") as f:
                        total += sum(1 for _ in f)
                except Exception:
                    pass
    return {"days": days, "total_records": total}
