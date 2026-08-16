"""代币视图与历史缓冲测试。

这里锁住的两个行为都属于"静默失效"类缺陷——出问题时不报错、不告警，
只是特征算错或消失，因此必须由测试而不是运行时日志来守。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.domain.models import (  # noqa: E402
    COARSE_HISTORY_SPACING_MS,
    FieldGroup,
    TokenObservation,
    TokenView,
)

NOW = 1_800_000_000_000


def make_view() -> TokenView:
    return TokenView(chain_id="56", contract_address="0xabc")


# ─────────────────────────────────────────────────────────────────────────
# 年龄：必须由调用方提供时钟，否则回测不可复现
# ─────────────────────────────────────────────────────────────────────────

def test_age_uses_supplied_clock_not_wall_clock():
    view = make_view()
    view.launch_time_ms = NOW - 600_000
    assert view.age_sec(NOW) == 600
    # 用一个"更晚的现在"重放时，年龄必须随之变化，而不是固定看真实时钟
    assert view.age_sec(NOW + 3_600_000) == 4200


def test_age_is_none_without_launch_time():
    assert make_view().age_sec(NOW) is None


# ─────────────────────────────────────────────────────────────────────────
# 历史缓冲：短窗精度 + 长窗覆盖，且绝不用最老的点冒充
# ─────────────────────────────────────────────────────────────────────────

def _fill_history(view: TokenView, points: int, spacing_ms: int) -> None:
    for i in range(points):
        view.values["holders"] = 100 + i
        view.push_history(NOW - (points - 1 - i) * spacing_ms)


def test_dense_buffer_serves_short_window():
    view = make_view()
    _fill_history(view, 32, 30_000)          # 每 30 秒一点，共 16 分钟
    point = view.history_at_or_before(NOW - 300_000)   # 5 分钟前
    assert point is not None
    # 允许一个采样周期的偏差，但不能偏到别的窗口去
    assert 300_000 <= NOW - point.ts < 330_000


def test_coarse_buffer_extends_coverage_beyond_dense_window():
    """密集序列只覆盖 16 分钟，1h 窗口必须由稀疏序列兜住。

    如果没有稀疏序列，热门币的所有 1h 特征会永久为 None，
    而系统不会给出任何提示。
    """
    view = make_view()
    _fill_history(view, 200, 30_000)         # 每 30 秒一点，共 100 分钟
    point = view.history_at_or_before(NOW - 3_600_000)
    assert point is not None, "1 小时窗口应能从稀疏序列取到历史点"
    age_ms = NOW - point.ts
    assert age_ms >= 3_600_000
    # 稀疏序列的采样间隔决定了最大偏差
    assert age_ms < 3_600_000 + COARSE_HISTORY_SPACING_MS


def test_returns_none_when_no_point_is_old_enough():
    """宁可返回 None，也不能拿最老的点冒充。

    否则标着"1h 增长"的特征实际算的是 2 分钟增长，
    这种错误会直接污染评分且完全无法从日志发现。
    """
    view = make_view()
    _fill_history(view, 5, 30_000)           # 只有 2 分钟历史
    assert view.history_at_or_before(NOW - 3_600_000) is None


def test_history_depth_does_not_double_count_overlap():
    view = make_view()
    _fill_history(view, 40, 30_000)
    assert view.history_depth >= len(view.history)
    assert view.history_depth <= len(view.history) + len(view.history_coarse)


# ─────────────────────────────────────────────────────────────────────────
# 观测合并
# ─────────────────────────────────────────────────────────────────────────

def test_sparse_observation_does_not_erase_richer_data():
    """列表接口的稀疏数据不能擦掉详情接口拿到的完整数据。"""
    view = make_view()
    view.apply(TokenObservation(
        chain_id="56", contract_address="0xabc", endpoint="detail",
        observed_at=NOW - 1000, price=0.5, top10_percent=42.0, holders=800,
    ))
    view.apply(TokenObservation(
        chain_id="56", contract_address="0xabc", endpoint="trending",
        observed_at=NOW, price=0.6,
    ))
    assert view.getf("price") == 0.6
    assert view.getf("top10_percent") == 42.0, "稀疏观测不应擦掉筹码数据"
    assert view.geti("holders") == 800


def test_apply_marks_only_touched_groups_fresh():
    view = make_view()
    touched = view.apply(TokenObservation(
        chain_id="56", contract_address="0xabc", endpoint="trending",
        observed_at=NOW, price=0.6, holders=100,
    ))
    assert touched == {FieldGroup.MARKET.value, FieldGroup.HOLDERS.value}
    # 未被本次观测刷新的字段组必须保持"从未有过数据"
    assert view.group_age_sec(FieldGroup.AUDIT, NOW) is None
    assert view.group_age_sec(FieldGroup.MARKET, NOW) == 0.0


def test_apply_stamps_interval_seen_at_only_on_real_refresh():
    """合并视图会永久携带最后一次非空极值，时间戳是唯一能区分
    "刚看到的极值"和"崩盘前旧极值"的依据。"""
    view = make_view()
    view.apply(TokenObservation(
        chain_id="56", contract_address="0xabc", endpoint="trending",
        observed_at=NOW - 60_000, interval_high=0.01, interval_low=0.002,
    ))
    assert view.interval_seen_at == NOW - 60_000

    # 后续观测没有区间极值：值被保留，但时间戳绝不能被推进
    view.apply(TokenObservation(
        chain_id="56", contract_address="0xabc", endpoint="trending",
        observed_at=NOW, price=0.005,
    ))
    assert view.getf("interval_high") == 0.01
    assert view.interval_seen_at == NOW - 60_000


def test_zero_is_preserved_and_distinct_from_unknown():
    """0% dev 持仓 与 未知 dev 持仓 是完全不同的两件事。"""
    view = make_view()
    view.apply(TokenObservation(
        chain_id="56", contract_address="0xabc", endpoint="detail",
        observed_at=NOW, dev_percent=0.0,
    ))
    assert view.getf("dev_percent") == 0.0
    assert view.getf("sniper_percent") is None


def test_stage_advances_but_identity_is_write_once():
    view = make_view()
    view.apply(TokenObservation(
        chain_id="56", contract_address="0xabc", endpoint="meme_rush",
        observed_at=NOW - 1000, symbol="PEPE", stage="new",
    ))
    view.apply(TokenObservation(
        chain_id="56", contract_address="0xabc", endpoint="meme_rush",
        observed_at=NOW, symbol="SCAM", stage="migrated",
    ))
    assert view.stage == "migrated", "生命周期阶段会推进，应允许覆盖"
    assert view.symbol == "PEPE", "身份字段一次写入，避免上游改名污染历史"
