r"""P1.1 回归：ETF 当日 $0 → pending 标签推断

背景：生产 AI 报告 §9d "ETF 每日明细: 2026-04-21: \$0" 被 AI 误读为
"当日资金面转空"，实际只是美股 ETF 尚未收盘数据未聚合。本测试锁定
`infer_etf_daily_status` 推断结果并验证 prompt 端渲染附带 ⏳ pending 标签。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.prompts import build_user_prompt
from models.data_meta import DataMeta, infer_etf_daily_status


def _today_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _yesterday_str(ts: int) -> str:
    return datetime.fromtimestamp(ts - 86400, tz=timezone.utc).strftime("%Y-%m-%d")


# ─────── DataMeta 推断单测 ───────
def test_today_zero_flagged_as_pending():
    now = int(time.time())
    meta = infer_etf_daily_status(_today_str(now), 0.0, now)
    assert meta.status == "pending"
    assert "尚未收盘" in meta.pending_reason


def test_today_nonzero_still_pending_p0_8():
    """P0.8 HIGH-2 修正：当日非零金额亦须 pending（盘中快照非终值）。

    旧行为（P1.1）：当日 total_net != 0 → fresh。
    问题：ETF 当日盘中会给出预估流入快照（如 $1.5 亿 / $2.5 亿），
    AI 会把盘中快照当收盘终值据此决策。实盘铁律：美股未收盘，
    当日所有 ETF 数据均不可靠。因此无论金额多少，date==today 即 pending。
    """
    now = int(time.time())
    meta = infer_etf_daily_status(_today_str(now), 2.5e8, now)
    assert meta.status == "pending"
    assert "尚未收盘" in meta.pending_reason
    assert "盘中快照非终值" in meta.pending_reason


def test_yesterday_zero_not_flagged_as_pending():
    """P1.1 · 历史日 $0（即便今日已过）不是 pending，维持 fresh。"""
    now = int(time.time())
    meta = infer_etf_daily_status(_yesterday_str(now), 0.0, now)
    assert meta.status == "fresh"


def test_describe_cn_returns_empty_for_fresh():
    meta = DataMeta(status="fresh")
    assert meta.describe_cn() == ""


# ─────── 端到端 prompt 渲染 ───────
def test_prompt_etf_today_zero_marked_with_pending_label():
    now = int(time.time())
    snap = {
        "coin": "BTC", "price": 75000.0, "high_24h": 76000, "low_24h": 74000,
        "etf_recent_days": [
            {"date": _yesterday_str(now), "total_net": 2.5e8},
            {"date": _today_str(now), "total_net": 0},
        ],
    }
    out = build_user_prompt(snap)
    assert "⏳" in out, f"今日 $0 必须带 ⏳ pending 标签:\n{out[out.find('ETF'):out.find('ETF')+400]}"
    assert "今日美股 ETF 尚未收盘" in out


def test_prompt_etf_today_nonzero_also_pending_p0_8():
    """P0.8 HIGH-2 修正：当日非零金额 prompt 仍应标 ⏳ pending。

    旧测断言"⏳ not in"，对应 P1.1 的放行行为；P0.8 后须全部标注。
    """
    now = int(time.time())
    snap = {
        "coin": "BTC", "price": 75000.0, "high_24h": 76000, "low_24h": 74000,
        "etf_recent_days": [
            {"date": _today_str(now), "total_net": 1.5e8},
        ],
    }
    out = build_user_prompt(snap)
    assert "⏳" in out, "P0.8: 当日非零金额也须 ⏳ pending"
    assert "盘中快照非终值" in out
