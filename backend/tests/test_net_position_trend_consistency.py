"""P0.2 回归：净持仓趋势标签与端点差百分比自洽

背景：生产 AI 报告里 §9h 出现"净持仓: 下降(多头减仓) · 24h 变化 +43.7% 显著增持"
这种自相矛盾的同行文本。根因是：
- 趋势标签用"前 4h 滚动均值 vs 后 4h 滚动均值"差值判定
- prompt 侧百分比用"24h 端点差 / 端点较大幅值"计算
两套独立公式在曲线形态特殊时可能方向相反。

本测试锁定修复后性质：趋势标签方向必须与 24h 端点差符号一致。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from polls.derivatives import poll_net_position


class _FakeState:
    def __init__(self) -> None:
        self.net_position_latest: float | None = None
        self.net_position_change_24h: float | None = None
        self.net_position_trend: str = ""
        self.poll_failures: dict[str, str] = {}


def _coin(symbol: str = "BTCUSDT"):
    return SimpleNamespace(symbol_cg_pair=symbol)


def _fake_rows(values: list[float]) -> list[dict]:
    return [{"netPosition": v} for v in values]


async def _run(values: list[float]) -> _FakeState:
    cg = SimpleNamespace(fetch_net_position_v2_history=AsyncMock(return_value=_fake_rows(values)))
    state = _FakeState()
    await poll_net_position(cg, _coin(), state)  # type: ignore[arg-type]
    return state


@pytest.mark.asyncio
async def test_trend_matches_endpoint_diff_sign_when_net_up():
    """P0.2 · 24h 端点差为正（+600）→ 趋势标签必须是'上升'，不能再出现'下降'。"""
    # 构造"前段高、后段反弹走高"的曲线：前 4h 平均 1000、后 4h 平均 600、尾部强反弹到 1500
    vals = [1000, 1050, 1100, 900, 500, 550, 600, 800, 1000, 1200, 1400, 1500] + [1500] * 12
    vals[0] = 900   # 起点
    vals[-1] = 1500  # 终点 → 端点差 = +600 > 0
    state = await _run(vals)
    assert state.net_position_change_24h == pytest.approx(600.0)
    assert "上升" in state.net_position_trend, f"端点差 +600 应标'上升'，实际: {state.net_position_trend}"
    assert "下降" not in state.net_position_trend


@pytest.mark.asyncio
async def test_trend_matches_endpoint_diff_sign_when_net_down():
    """P0.2 · 24h 端点差为负 → 趋势标签必须是'下降'。"""
    vals = [1500, 1400, 1300, 1200] + [1100] * 16 + [900, 850, 800, 700]  # 端点差 = 700 - 1500 = -800
    state = await _run(vals)
    assert state.net_position_change_24h == pytest.approx(-800.0)
    assert "下降" in state.net_position_trend
    assert "上升" not in state.net_position_trend


@pytest.mark.asyncio
async def test_trend_flat_when_small_pct_change():
    """P0.2 · 端点差占比 <5% → 标'持平'。"""
    vals = [10000] * 23 + [10200]  # 变化 2% < 5%
    state = await _run(vals)
    assert state.net_position_trend == "持平"


@pytest.mark.asyncio
async def test_trend_near_zero_base_fallback_to_sign_only():
    """P0.2 · 分母过小（方向翻转期）→ 只按符号定方向，不因分母荒诞被吞。"""
    vals = [-0.3, 0.2, -0.1, 0.4] + [0.1] * 16 + [0.2, 0.3, 0.4, 0.5]  # 尾部为正、端点差 > 0，base < 1
    state = await _run(vals)
    assert state.net_position_trend == "上升(多头增仓)"


@pytest.mark.asyncio
async def test_failure_marks_poll_failures():
    """P0.2 · API 抛错时应记录 poll_failures 而非静默。"""
    cg = SimpleNamespace(fetch_net_position_v2_history=AsyncMock(side_effect=RuntimeError("boom")))
    state = _FakeState()
    await poll_net_position(cg, _coin(), state)  # type: ignore[arg-type]
    assert "net_position" in state.poll_failures
