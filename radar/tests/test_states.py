"""状态机测试。

重点验证的不是"能不能升级"，而是**防抖机制真的生效**：
如果滞回/驻留/退出确认任何一环失效，线上表现就是同一枚币
十分钟内反复 S1→S0→S1，用户收到五封邮件。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.domain.models import ScoreResult, TokenState, TokenView  # noqa: E402
from radar.domain.risk_gate import GATE_EXECUTION, RiskDecision, Violation  # noqa: E402
from radar.domain.states import StateMachine  # noqa: E402

CONFIG = {
    "min_dwell_sec": 180,
    "exit_confirm_cycles": 2,
    "transitions": {
        "s0": {"enter_opportunity": 55, "exit_opportunity": 45,
               "max_rug_risk": 70, "min_data_quality": 40},
        "s1": {"enter_opportunity": 72, "exit_opportunity": 62,
               "max_rug_risk": 45, "min_data_quality": 60,
               "min_liquidity_usd": 12000.0, "min_confidence": 50},
        # V2 起弃用：S2 走确认制，此块不再参与晋升判定
        "s2": {"enter_opportunity": 85, "exit_opportunity": 75,
               "max_rug_risk": 35, "min_data_quality": 70,
               "min_confidence": 65, "min_smart_money_count": 3},
    },
    "s2_confirmation": {
        "enabled": True,
        "min_age_from_s1_sec": 1200,
        "max_drawdown_from_peak_pct": 40.0,
        "min_price_vs_anchor_ratio": 0.7,
        "hard_veto_lp_drop_pct": 20.0,
        "hard_veto_exit_rate": 60.0,
    },
    "distribution": {"enter_score": 60, "exit_score": 45},
    "dormant_after_stale_sec": 21600,
    "dead_min_liquidity_usd": 500.0,
}

NOW = 1_800_000_000_000


def make_view(**values) -> TokenView:
    view = TokenView(chain_id="56", contract_address="0xtest", token_id=1)
    view.values.update({
        "liquidity": 50000.0,
        "smart_money_count": 5,
        "price": 0.001,
        "market_cap": 100000.0,
        **values,
    })
    view.last_observed_ms = NOW
    view.state_since_ms = NOW - 3_600_000
    # 给足历史深度，避免因观测过少影响与状态无关的判断
    for i in range(12):
        view.push_history(NOW - (12 - i) * 60_000)
    return view


def scores(opportunity: float, *, confidence: float = 80, data_quality: float = 90,
           rug_risk: float = 10, distribution: float = 5) -> ScoreResult:
    return ScoreResult(
        opportunity=opportunity, confidence=confidence, data_quality=data_quality,
        rug_risk=rug_risk, distribution=distribution,
    )


def clean_risk() -> RiskDecision:
    return RiskDecision(blocked=False, gate_blocked=False, audit_unknown=False)


@pytest.fixture
def sm() -> StateMachine:
    return StateMachine(CONFIG, near_miss_margin=5.0)


# ─────────────────────────────────────────────────────────────────────────
# 晋升
# ─────────────────────────────────────────────────────────────────────────

def test_promotes_through_levels(sm: StateMachine):
    """V2：机会分晋升到 S1 为止，S2 只能走确认制（85 分数学上不可达，
    V1 的 S2 是死档位）。"""
    view = make_view()
    view.state = TokenState.WATCHING
    assert sm.evaluate(view, scores(60), clean_risk(), NOW).new_state == TokenState.S0
    assert sm.evaluate(view, scores(75), clean_risk(), NOW).new_state == TokenState.S1
    view.state = TokenState.S1
    decision = sm.evaluate(view, scores(90), clean_risk(), NOW)
    assert decision.new_state == TokenState.S1, "再高的机会分也不能跳过 S2 确认期"


def test_promotion_ignores_min_dwell(sm: StateMachine):
    """升级不受最短驻留限制——发现火箭的速度就是系统的全部价值。"""
    view = make_view()
    view.state = TokenState.S0
    view.state_since_ms = NOW - 1000        # 刚刚才进入 S0
    decision = sm.evaluate(view, scores(90), clean_risk(), NOW)
    assert decision.new_state == TokenState.S1
    assert decision.changed


def test_missing_liquidity_blocks_s1(sm: StateMachine):
    """流动性数据缺失时判定为不通过，而不是当作通过。"""
    view = make_view()
    view.values["liquidity"] = None
    view.state = TokenState.S0
    decision = sm.evaluate(view, scores(80), clean_risk(), NOW)
    assert decision.new_state != TokenState.S1
    liquidity_req = next(r for r in decision.failing(TokenState.S1) if r.name == "liquidity")
    assert liquidity_req.gap == float("inf")


def test_unknown_audit_blocks_s1(sm: StateMachine):
    view = make_view()
    view.state = TokenState.S0
    risk = RiskDecision(audit_unknown=True)
    decision = sm.evaluate(view, scores(80), risk, NOW)
    assert decision.new_state != TokenState.S1
    assert "audit_known" in {r.name for r in decision.failing(TokenState.S1)}


# ─────────────────────────────────────────────────────────────────────────
# S2 确认制（V2）
# ─────────────────────────────────────────────────────────────────────────

def confirmable_s1_view() -> TokenView:
    """构造一个满足全部 S2 确认条件的 S1 观察池成员。

    各测试从这个"全通过"基线出发，各自破坏一个条件来验证单点否决。
    """
    view = TokenView(chain_id="56", contract_address="0xtest", token_id=1)
    view.values.update({
        "liquidity": 50000.0, "smart_money_count": 5,
        "price": 0.0012, "market_cap": 100000.0,
        "top10_percent": 28.0, "dev_percent": 5.0,
    })
    view.last_observed_ms = NOW
    view.state = TokenState.S1
    view.state_since_ms = NOW - 1_500_000       # 25 分钟前进入 S1（>20 分钟门槛）
    view.s1_anchor = {
        "price": 0.001, "liquidity": 50000.0, "top10_percent": 30.0,
        "dev_percent": 5.0, "dev_sell_percent": 0.0, "holders": 800,
        "at": NOW - 1_500_000,
    }
    view.s1_peak_price = 0.0015                  # 确认期创过新高（> 锚点价 ×1.02）
    # 持有人连增：20 分钟前 800 → 6 分钟前 900 → 现在 1000
    for ts, holders in ((NOW - 1_200_000, 800), (NOW - 360_000, 900)):
        view.values["holders"] = holders
        view.push_history(ts)
    view.values["holders"] = 1000
    return view


def test_s2_confirmed_by_time_and_behavior_not_score(sm: StateMachine):
    """确认期全部达标即晋升 S2，即使机会分只有 65（低于 S1 进入线 72）。

    这正是 V2 的核心：确认期里动量分自然衰减，能扛住回撤、结构还在
    改善的币才值得确认级推送，而不是分数最高的那一刻。
    """
    view = confirmable_s1_view()
    decision = sm.evaluate(view, scores(65), clean_risk(), NOW)
    assert decision.new_state == TokenState.S2
    assert "确认期通过" in decision.reason


def test_s2_needs_min_age_from_s1(sm: StateMachine):
    """进入 S1 才 5 分钟不许确认——榜单币第一波拉升几分钟内就结束。"""
    view = confirmable_s1_view()
    view.state_since_ms = NOW - 300_000
    decision = sm.evaluate(view, scores(65), clean_risk(), NOW)
    assert decision.new_state != TokenState.S2
    assert "s2_min_age" in {r.name for r in decision.failing(TokenState.S2)}


def test_s2_rejected_on_deep_drawdown(sm: StateMachine):
    """确认期最高价回撤 60% 视为破位，不予确认。"""
    view = confirmable_s1_view()
    view.s1_peak_price = 0.003                   # 现价 0.0012 → 回撤 60%
    decision = sm.evaluate(view, scores(65), clean_risk(), NOW)
    assert decision.new_state != TokenState.S2
    assert "s2_drawdown" in {r.name for r in decision.failing(TokenState.S2)}


def test_s2_requires_holder_growth(sm: StateMachine):
    """持有人不再增长的币结构上已停滞，不予确认。"""
    view = confirmable_s1_view()
    view.values["holders"] = 790                 # 低于 15 分钟前的 800
    decision = sm.evaluate(view, scores(65), clean_risk(), NOW)
    assert decision.new_state != TokenState.S2
    assert "s2_holders_grow" in {r.name for r in decision.failing(TokenState.S2)}


def test_s2_data_quality_gate_survives_v2(sm: StateMachine):
    """S1 级硬闸门（数据质量等）在确认制下仍然生效，
    不能因为行为条件全过就带着不可信数据晋升。"""
    view = confirmable_s1_view()
    decision = sm.evaluate(view, scores(65, data_quality=50), clean_risk(), NOW)
    assert decision.new_state != TokenState.S2
    assert "data_quality" in {r.name for r in decision.failing(TokenState.S2)}


def test_lp_pull_hard_veto_moves_to_distribution(sm: StateMachine):
    """LP 较锚点抽离 40% → 不等确认期走完，即时转派发观察。

    V1 实盘 7/7 买在顶部的直接死因：RugRisk 只看静态筹码结构，
    对拔池这类行为完全失明。"""
    view = confirmable_s1_view()
    view.values["liquidity"] = 30000.0           # 锚点 50000 → -40%
    decision = sm.evaluate(view, scores(65), clean_risk(), NOW)
    assert decision.new_state == TokenState.DISTRIBUTION
    assert "拔池" in decision.reason


def test_smart_money_exodus_hard_veto(sm: StateMachine):
    view = confirmable_s1_view()
    view.values["exit_rate"] = 70.0              # ≥ 60 即否决
    decision = sm.evaluate(view, scores(65), clean_risk(), NOW)
    assert decision.new_state == TokenState.DISTRIBUTION
    assert "离场率" in decision.reason


def test_confirmation_disabled_restores_v1_behavior():
    """关掉确认制后必须完整回退 V1：机会分直升 S2。"""
    config = {**CONFIG, "s2_confirmation": {"enabled": False}}
    sm = StateMachine(config, near_miss_margin=5.0)
    view = make_view()
    view.state = TokenState.S1
    decision = sm.evaluate(view, scores(90), clean_risk(), NOW)
    assert decision.new_state == TokenState.S2


# ─────────────────────────────────────────────────────────────────────────
# 防抖三机制
# ─────────────────────────────────────────────────────────────────────────

def test_hysteresis_band_holds_state(sm: StateMachine):
    """机会分落在 S1 的 [62, 72) 死区内时既不升也不降。"""
    view = make_view()
    view.state = TokenState.S1
    for opportunity in (71, 65, 63, 70):
        decision = sm.evaluate(view, scores(opportunity), clean_risk(), NOW)
        assert not decision.changed, f"机会分 {opportunity} 不应触发状态变更"
        assert decision.exit_streak == 0


def test_demotion_requires_consecutive_confirmations(sm: StateMachine):
    view = make_view()
    view.state = TokenState.S1
    # 58 分：低于 S1 退出阈值 62，但仍达到 S0 的 55
    first = sm.evaluate(view, scores(58), clean_risk(), NOW)
    assert not first.changed
    assert first.exit_streak == 1

    second = sm.evaluate(view, scores(58), clean_risk(), NOW)
    assert second.changed
    assert second.new_state == TokenState.S0


def test_deep_drop_demotes_straight_to_watching(sm: StateMachine):
    """降级落到"最高一个仍然达标的状态"，而不是逐级下降。

    机会分从 75 直接砸到 40 时，逐级下降会让它在 S0 上多挂一个周期，
    而那一个周期里它仍然会被当成候选池成员参与配额分配。
    """
    view = make_view()
    view.state = TokenState.S1
    sm.evaluate(view, scores(40), clean_risk(), NOW)
    decision = sm.evaluate(view, scores(40), clean_risk(), NOW)
    assert decision.new_state == TokenState.WATCHING


def test_single_dip_does_not_demote(sm: StateMachine):
    """单次数据抖动（比如某次接口缺字段）不应导致降级。"""
    view = make_view()
    view.state = TokenState.S1
    sm.evaluate(view, scores(50), clean_risk(), NOW)   # 一次跌破
    recovered = sm.evaluate(view, scores(70), clean_risk(), NOW)  # 回到死区
    assert not recovered.changed
    # 退出计数必须被清零，否则下一次跌破会立刻降级
    assert view.exit_streak.get("S1", 0) == 0


def test_min_dwell_holds_demotion(sm: StateMachine):
    view = make_view()
    view.state = TokenState.S1
    view.state_since_ms = NOW - 10_000          # 才进入 10 秒
    sm.evaluate(view, scores(40), clean_risk(), NOW)
    decision = sm.evaluate(view, scores(40), clean_risk(), NOW)
    assert not decision.changed
    assert decision.dwell_held


def test_hard_gate_failure_bypasses_hysteresis(sm: StateMachine):
    """风险飙升时必须立刻降级，不能因为机会分还在死区内就继续挂 S2。

    这是"数据已不可信却还在发交易警报"的直接成因，必须堵死。
    """
    view = make_view()
    view.state = TokenState.S2
    view.state_since_ms = NOW - 3_600_000
    # 机会分 80 仍在 S2 死区 [75, 85) 内，但归零风险已超过 S2 上限
    sm.evaluate(view, scores(80, rug_risk=60), clean_risk(), NOW)
    decision = sm.evaluate(view, scores(80, rug_risk=60), clean_risk(), NOW)
    assert decision.changed
    assert decision.new_state.rank < TokenState.S2.rank


# ─────────────────────────────────────────────────────────────────────────
# 硬性状态
# ─────────────────────────────────────────────────────────────────────────

def test_execution_blocker_forces_blocked(sm: StateMachine):
    view = make_view()
    view.state = TokenState.S2
    risk = RiskDecision(blocked=True, violations=[
        Violation(gate=GATE_EXECUTION, rule="honeypot", actual_text="honeypot=true"),
    ])
    decision = sm.evaluate(view, scores(95), risk, NOW)
    assert decision.new_state == TokenState.BLOCKED
    assert "honeypot" in decision.reason


def test_drained_liquidity_is_dead(sm: StateMachine):
    view = make_view(liquidity=100.0)
    view.state = TokenState.S1
    decision = sm.evaluate(view, scores(80), clean_risk(), NOW)
    assert decision.new_state == TokenState.DEAD


def test_price_collapse_marks_dead_after_confirmation(sm: StateMachine):
    """拔池后接口仍报残余流动性（实盘 $12,817），必须靠价格崩塌抓死币。"""
    view = make_view()
    view.state = TokenState.S1
    view.values["price"] = 0.00002          # 较历史高点 0.001 跌 98%

    first = sm.evaluate(view, scores(70), clean_risk(), NOW)
    assert first.new_state == TokenState.S1, "首次满足不判死，防单次坏数据误杀"

    second = sm.evaluate(view, scores(70), clean_risk(), NOW + 60_000)
    assert second.new_state == TokenState.DEAD
    assert "崩塌" in second.reason


def test_single_bad_price_does_not_kill(sm: StateMachine):
    """一次接口错价后恢复正常，确认计数必须清零。"""
    view = make_view()
    view.state = TokenState.S1
    view.values["price"] = 0.00002
    sm.evaluate(view, scores(70), clean_risk(), NOW)      # 计数 1

    view.values["price"] = 0.001                           # 价格恢复
    sm.evaluate(view, scores(70), clean_risk(), NOW + 60_000)

    view.values["price"] = 0.00002                         # 再次异常，应重新从 1 数起
    decision = sm.evaluate(view, scores(70), clean_risk(), NOW + 120_000)
    assert decision.new_state == TokenState.S1


def test_dead_by_collapse_stays_dead_and_can_revive(sm: StateMachine):
    """进入 DEAD 后条件仍成立必须维持，不得与 WATCHING 来回抖动；
    但价格真正恢复后要能复活。"""
    view = make_view()
    view.state = TokenState.S1
    view.values["price"] = 0.00002
    sm.evaluate(view, scores(70), clean_risk(), NOW)
    assert sm.evaluate(view, scores(70), clean_risk(),
                       NOW + 60_000).new_state == TokenState.DEAD

    view.state = TokenState.DEAD
    view.exit_streak.clear()                # 模拟 registry 在状态变更后清零
    holding = sm.evaluate(view, scores(70), clean_risk(), NOW + 120_000)
    assert not holding.changed, "条件仍成立时 DEAD 不应复活"

    view.values["price"] = 0.001
    revived = sm.evaluate(view, scores(70), clean_risk(), NOW + 180_000)
    assert revived.new_state == TokenState.WATCHING


def test_stale_token_becomes_dormant_and_can_revive(sm: StateMachine):
    view = make_view()
    view.state = TokenState.S0
    view.last_observed_ms = NOW - 30 * 3_600_000
    decision = sm.evaluate(view, scores(60), clean_risk(), NOW)
    assert decision.new_state == TokenState.DORMANT

    # 重新出现在榜单后必须能复活，而不是永久沉睡
    view.state = TokenState.DORMANT
    view.last_observed_ms = NOW
    revived = sm.evaluate(view, scores(60), clean_risk(), NOW)
    assert revived.new_state == TokenState.WATCHING


def test_distribution_enter_and_exit(sm: StateMachine):
    view = make_view()
    view.state = TokenState.S1
    decision = sm.evaluate(view, scores(70, distribution=75), clean_risk(), NOW)
    assert decision.new_state == TokenState.DISTRIBUTION

    view.state = TokenState.DISTRIBUTION
    holding = sm.evaluate(view, scores(70, distribution=50), clean_risk(), NOW)
    assert not holding.changed, "派发分在 [45,60) 死区内应保持派发状态"

    exiting = sm.evaluate(view, scores(70, distribution=30), clean_risk(), NOW)
    assert exiting.new_state == TokenState.WATCHING


def test_distribution_not_entered_from_watching(sm: StateMachine):
    """还没进入观察池的币谈不上"派发"，避免给垃圾币刷派发警报。"""
    view = make_view()
    view.state = TokenState.WATCHING
    decision = sm.evaluate(view, scores(30, distribution=90), clean_risk(), NOW)
    assert decision.new_state != TokenState.DISTRIBUTION


# ─────────────────────────────────────────────────────────────────────────
# Near-Miss
# ─────────────────────────────────────────────────────────────────────────

def test_near_miss_detected_for_small_score_gap(sm: StateMachine):
    view = make_view()
    view.state = TokenState.S0
    decision = sm.evaluate(view, scores(69), clean_risk(), NOW)   # 差 3 分进 S1
    assert decision.near_miss
    assert decision.blocked_state == TokenState.S1
    assert {r.name for r in decision.blocked_by} == {"opportunity"}


def test_no_near_miss_for_large_gap(sm: StateMachine):
    view = make_view()
    view.state = TokenState.S0
    decision = sm.evaluate(view, scores(58), clean_risk(), NOW)   # 差 14 分
    assert not decision.near_miss


def test_near_miss_uses_relative_gap_for_magnitude_requirements(sm: StateMachine):
    """流动性差 5 美元 和 机会分差 5 分 不能用同一把尺子。"""
    view = make_view(liquidity=11_700.0)     # 差 300，占阈值 2.5%
    view.state = TokenState.S0
    decision = sm.evaluate(view, scores(80), clean_risk(), NOW)
    assert decision.near_miss
    assert {r.name for r in decision.blocked_by} == {"liquidity"}

    far = make_view(liquidity=6_000.0)       # 差 50%
    far.state = TokenState.S0
    assert not sm.evaluate(far, scores(80), clean_risk(), NOW).near_miss


def test_missing_data_is_not_near_miss(sm: StateMachine):
    """数据缺失不算"差一点"，否则 Near-Miss 会被无数缺数据的币灌满。"""
    view = make_view()
    view.values["liquidity"] = None
    view.state = TokenState.S0
    assert not sm.evaluate(view, scores(80), clean_risk(), NOW).near_miss
