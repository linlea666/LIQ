"""特征分布漂移检测。

评分器对缺失字段静默降级，接口改版不会报错——
漂移检测是唯一能把这种无声失效变成显式告警的机制。
"""

from __future__ import annotations

from radar.obs.metrics import Metrics


def _drift_kwargs(**overrides):
    kwargs = {"min_samples": 10, "max_null_ratio": 0.6, "max_constant_ratio": 0.9}
    kwargs.update(overrides)
    return kwargs


def test_healthy_features_do_not_drift():
    m = Metrics()
    for i in range(20):
        m.observe_feature("price", 0.001 * (i + 1))
    assert m.check_drift(**_drift_kwargs()) == []


def test_null_ratio_breach_is_detected():
    """字段整体消失（解析器对不上新 schema）表现为 NULL 率飙升。"""
    m = Metrics()
    for i in range(20):
        m.observe_feature("holders", None if i < 15 else float(i))
    drifted = m.check_drift(**_drift_kwargs())
    assert [d["name"] for d in drifted] == ["holders"]
    assert drifted[0]["null_ratio"] == 0.75


def test_constant_ratio_breach_is_detected():
    """所有行落到同一个值（典型如解析错误吃掉真实数据后的默认值）。"""
    m = Metrics()
    for _ in range(19):
        m.observe_feature("top10_percent", 0.0)
    m.observe_feature("top10_percent", 55.0)
    drifted = m.check_drift(**_drift_kwargs())
    assert [d["name"] for d in drifted] == ["top10_percent"]


def test_min_samples_gate_prevents_cold_start_noise():
    """样本不足时不判定：冷启动头几分钟的稀疏数据不构成分布证据。"""
    m = Metrics()
    for _ in range(5):
        m.observe_feature("liquidity", None)
    assert m.check_drift(**_drift_kwargs(min_samples=10)) == []
    # 且窗口未被重置，样本继续累积
    assert m.features["liquidity"].total_count == 5


def test_window_resets_after_check_to_avoid_repeat_alerts():
    m = Metrics()
    for _ in range(20):
        m.observe_feature("net_inflow", None)
    assert m.check_drift(**_drift_kwargs())
    # 同一批旧样本不得在下个周期再次触发
    assert m.check_drift(**_drift_kwargs()) == []
    assert m.features["net_inflow"].total_count == 0
