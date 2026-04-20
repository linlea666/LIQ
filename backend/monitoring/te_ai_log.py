"""TE · AI 解读结果 Shadow Log（独立于主 TE Shadow）

文件布局
--------
    logs/te_ai_interpret/
        2026-04-20/
            BTC.jsonl            # 结构化结果（小，每条 ~1KB）
            BTC.thinking.jsonl   # DeepSeek Reasoner 的 reasoning_content（大，几 KB-几十 KB）
            ETH.jsonl
            ETH.thinking.jsonl

为什么分两个文件
----------------
结构化结果用于事后统计（AI 准确率、AI vs 规则 A/B），读取频繁。
reasoning_content 是"调试/教学资产"，只在排查争议信号时偶尔回看，
单独存避免污染 fast path 读取。

写入是同步的（按需触发频率低，不必异步队列）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

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
                "conflict_resolution": result.conflict_resolution,
                "traps": result.traps,
                "triggers_to_watch": result.triggers_to_watch,
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
