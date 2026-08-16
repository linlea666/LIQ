"""RugRisk 行为项测试。

V1 实盘教训：7 条 S1 推送全部 RUG，当时 rug 分仅 0-7。
静态筹码结构（Top10、dev 持仓、审计）对"正在发生的 rug"完全失明——
LP 正在被抽、dev 正在卖的币，快照看起来和健康币没有区别。
这些测试锁定的是：行为异常必须推高风险分，而干净币分数不受影响。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.domain.models import FeatureSet, TokenView  # noqa: E402
from radar.domain.risk_gate import RiskDecision  # noqa: E402
from radar.domain.scoring import Scorer  # noqa: E402


def make_view(**values) -> TokenView:
    view = TokenView(chain_id="56", contract_address="0xtest", token_id=1)
    view.values.update({"liquidity": 40_000.0, "market_cap": 150_000.0, **values})
    return view


def make_features(**kv) -> FeatureSet:
    fs = FeatureSet()
    # 一组"筹码结构健康"的基线：静态项几乎不产生风险分，
    # 从而隔离出行为项的贡献
    baseline = {
        "top10_percent": 25.0, "combined_concentration": 8.0,
        "dev_percent": 1.0, "liquidity_mc_ratio": 0.25,
        "exit_rate": 10.0, "new_wallet_percent": 20.0,
    }
    baseline.update(kv)
    for name, value in baseline.items():
        fs.set(name, value)
    return fs


def rug(fs: FeatureSet, view: TokenView | None = None) -> tuple[float, dict]:
    return Scorer._rug_risk(view or make_view(), fs, RiskDecision())


def test_clean_token_behavior_terms_add_nothing():
    """行为字段一切正常时，行为项必须是零贡献——
    否则所有既有阈值（S1 max_rug_risk 45）都会被系统性抬高。"""
    without = rug(make_features())[0]
    with_healthy = rug(make_features(
        liq_growth_15m=0.10, dev_sell_percent=0.0, price_growth_15m=0.30,
    ))[0]
    assert with_healthy == without


def test_lp_outflow_raises_rug_risk():
    """15 分钟流动性 -30%：正在被抽池，风险分必须显著抬升。"""
    base, _ = rug(make_features())
    risky, flags = rug(make_features(liq_growth_15m=-0.30))
    assert risky - base > 10.0
    assert flags["lp_outflow_15m"] == -0.30


def test_dev_selling_raises_rug_risk():
    base, _ = rug(make_features())
    risky, flags = rug(make_features(dev_sell_percent=80.0))
    assert risky > base
    assert flags["dev_selling"] == 80.0


def test_price_liquidity_divergence_is_flagged():
    """拉价 +40% 同时抽池 -10%：拔池前最典型的掩护形态。"""
    base, _ = rug(make_features())
    risky, flags = rug(make_features(
        price_growth_15m=0.40, liq_growth_15m=-0.10,
    ))
    assert flags.get("price_liq_divergence") is True
    # 背离 12 分 + LP 流出项的贡献，合计必须超过单纯 LP 流出
    lp_only, _ = rug(make_features(liq_growth_15m=-0.10))
    assert risky - lp_only >= 11.0


def test_combined_behavior_signals_push_past_s1_gate():
    """复现实盘死法：LP -35% + dev 卖 70% + 拉价背离，
    风险分必须越过 S1 的 max_rug_risk=45 闸门，把这类币挡在推送之外。"""
    risky, flags = rug(make_features(
        liq_growth_15m=-0.35, dev_sell_percent=70.0, price_growth_15m=0.50,
    ))
    assert risky > 45.0
    assert {"lp_outflow_15m", "dev_selling", "price_liq_divergence"} <= set(flags)
