"""状态机。

三重防抖机制，缺任何一个都会产生"同一枚币十分钟内报警五次"的灾难：

  1. **滞回**：进入阈值 ≠ 退出阈值。进 S1 需要 Opportunity ≥ 72，
     但退出 S1 要跌到 62 以下。中间 10 分的死区就是防抖带。
  2. **最短驻留**：状态变更后一段时间内不允许再降级。
  3. **退出确认**：需要连续 N 个评分周期都低于退出阈值才真的降级，
     单次数据抖动（比如某次接口返回缺字段）不会触发降级。

升级刻意**不受最短驻留限制**：发现火箭的速度就是这个系统的全部价值，
而滞回带本身已经足以防止升级方向的抖动。

另一个关键设计：DataQuality / Confidence 是**硬闸门**，
不能被高 Opportunity 覆盖。数据不可信时给出的 91 分毫无意义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .models import ScoreResult, TokenState, TokenView
from .risk_gate import RiskDecision


@dataclass(slots=True)
class Requirement:
    """一条晋升条件的检查结果。"""

    name: str
    label: str
    passed: bool
    actual: float | None
    threshold: float | None
    # 距离达标还差多少（已通过则为 0）。用于 Near-Miss 判定
    gap: float = 0.0
    # 分数型条件（0-100 标度）用绝对差距判 Near-Miss；
    # 量级型条件（流动性美元、地址个数）必须用相对差距，
    # 否则"差 5 美元流动性"和"差 5 分机会分"会被当成同一回事。
    is_score: bool = True

    @property
    def gap_ratio(self) -> float:
        if self.threshold in (None, 0):
            return float("inf") if self.gap else 0.0
        return self.gap / abs(self.threshold)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "passed": self.passed,
            "actual": None if self.actual is None else round(self.actual, 4),
            "threshold": self.threshold,
            "gap": None if self.gap == float("inf") else round(self.gap, 4),
        }


@dataclass
class StateDecision:
    """一次状态评估的完整结论，直接用于落库和邮件解释。"""

    old_state: TokenState
    new_state: TokenState
    changed: bool
    reason: str = ""
    # 目标状态 → 各条件检查结果（含未通过的原因与差距）
    requirements: dict[str, list[Requirement]] = field(default_factory=dict)
    blocked_state: TokenState | None = None       # 差一点就进入的状态
    blocked_by: list[Requirement] = field(default_factory=list)
    near_miss: bool = False
    dwell_held: bool = False                      # 因最短驻留而未降级
    exit_streak: int = 0

    def failing(self, state: TokenState) -> list[Requirement]:
        return [r for r in self.requirements.get(state.value, []) if not r.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "old_state": self.old_state.value,
            "new_state": self.new_state.value,
            "changed": self.changed,
            "reason": self.reason,
            "requirements": {
                state: [r.as_dict() for r in reqs]
                for state, reqs in self.requirements.items()
            },
            "blocked_state": self.blocked_state.value if self.blocked_state else None,
            "blocked_by": [r.as_dict() for r in self.blocked_by],
            "near_miss": self.near_miss,
            "dwell_held": self.dwell_held,
            "exit_streak": self.exit_streak,
        }


class StateMachine:
    def __init__(self, config: Mapping[str, Any], *, near_miss_margin: float = 5.0) -> None:
        self._min_dwell_ms = int(config.get("min_dwell_sec", 180)) * 1000
        self._exit_confirm = max(1, int(config.get("exit_confirm_cycles", 2)))
        transitions = config.get("transitions", {}) or {}
        self._rules: dict[TokenState, dict[str, float]] = {
            TokenState.S0: _rule(transitions.get("s0", {})),
            TokenState.S1: _rule(transitions.get("s1", {})),
            TokenState.S2: _rule(transitions.get("s2", {})),
        }
        dist = config.get("distribution", {}) or {}
        self._dist_enter = float(dist.get("enter_score", 60))
        self._dist_exit = float(dist.get("exit_score", 45))
        self._dormant_after_ms = int(config.get("dormant_after_stale_sec", 21600)) * 1000
        self._dead_min_liquidity = float(config.get("dead_min_liquidity_usd", 500.0))
        self._near_miss_margin = near_miss_margin

    # ═════════════════════════════════════════════════════════════════════
    def evaluate(
        self,
        view: TokenView,
        scores: ScoreResult,
        risk: RiskDecision,
        now_ms: int,
    ) -> StateDecision:
        old = view.state
        decision = StateDecision(old_state=old, new_state=old, changed=False)

        # ── 硬性状态优先（不受滞回与驻留限制）─────────────────────────
        hard = self._hard_state(view, risk, now_ms)
        if hard is not None:
            state, reason = hard
            if state != old:
                decision.new_state = state
                decision.changed = True
                decision.reason = reason
            return decision

        # 从硬性状态恢复：阻断条件消失后回到 WATCHING 重新评估
        if old in (TokenState.BLOCKED, TokenState.DEAD, TokenState.DORMANT):
            decision.new_state = TokenState.WATCHING
            decision.changed = True
            decision.reason = "阻断条件解除，恢复观察"
            return decision

        # ── 派发状态 ───────────────────────────────────────────────────
        if old == TokenState.DISTRIBUTION:
            if scores.distribution < self._dist_exit:
                decision.new_state = TokenState.WATCHING
                decision.changed = True
                decision.reason = f"派发分回落至 {scores.distribution:.0f}，退出派发状态"
            return decision

        if scores.distribution >= self._dist_enter and old.rank >= TokenState.S0.rank:
            decision.new_state = TokenState.DISTRIBUTION
            decision.changed = True
            decision.reason = f"派发分 {scores.distribution:.0f} 超过 {self._dist_enter:.0f}"
            return decision

        # ── 逐级检查晋升条件（从高到低）─────────────────────────────
        target = TokenState.WATCHING
        for candidate in (TokenState.S2, TokenState.S1, TokenState.S0):
            requirements = self._check(candidate, view, scores, risk)
            decision.requirements[candidate.value] = requirements
            if all(r.passed for r in requirements):
                target = candidate
                break

        if target.rank > old.rank:
            decision.new_state = target
            decision.changed = True
            decision.reason = self._promotion_reason(target, decision.requirements)
            return decision

        # ── 降级：需连续多个周期低于退出阈值 + 满足最短驻留 ──────────
        if old.rank >= TokenState.S0.rank and target.rank < old.rank:
            if self._in_hysteresis_band(old, view, scores, risk):
                # 在滞回死区内：不升不降，重置退出计数
                view.exit_streak.pop(old.value, None)
                return decision

            streak = view.exit_streak.get(old.value, 0) + 1
            view.exit_streak[old.value] = streak
            decision.exit_streak = streak

            if streak < self._exit_confirm:
                decision.reason = (
                    f"低于 {old.value} 退出阈值（{streak}/{self._exit_confirm} 次确认）"
                )
                return decision

            if now_ms - view.state_since_ms < self._min_dwell_ms:
                decision.dwell_held = True
                decision.reason = "满足降级条件但仍在最短驻留期内"
                return decision

            view.exit_streak.pop(old.value, None)
            decision.new_state = target
            decision.changed = True
            decision.reason = (
                f"Opportunity {scores.opportunity:.0f} 连续 {streak} 次低于 "
                f"{old.value} 退出阈值 {self._rules[old]['exit_opportunity']:.0f}"
            )
            return decision

        view.exit_streak.pop(old.value, None)

        # ── Near-Miss：差一点就晋升的记录下来供阈值反事实研究 ─────────
        self._detect_near_miss(decision, old)
        return decision

    # ═════════════════════════════════════════════════════════════════════
    def _hard_state(self, view: TokenView, risk: RiskDecision,
                    now_ms: int) -> tuple[TokenState, str] | None:
        if risk.blocked:
            rules = ",".join(v.rule for v in risk.execution_violations)
            return TokenState.BLOCKED, f"命中硬拒规则: {rules}"

        liquidity = view.getf("liquidity")
        if liquidity is not None and liquidity < self._dead_min_liquidity:
            # 流动性被抽干是硬性死亡，不是"暂时不活跃"
            return TokenState.DEAD, f"流动性仅 ${liquidity:,.0f}，视为已死亡"

        if view.last_observed_ms and now_ms - view.last_observed_ms > self._dormant_after_ms:
            hours = (now_ms - view.last_observed_ms) / 3_600_000
            return TokenState.DORMANT, f"已 {hours:.1f} 小时未再出现在任何榜单"

        return None

    def _check(self, target: TokenState, view: TokenView,
               scores: ScoreResult, risk: RiskDecision) -> list[Requirement]:
        rule = self._rules[target]
        requirements: list[Requirement] = [
            _req("opportunity", "机会分", scores.opportunity, rule["enter_opportunity"], "min"),
            _req("rug_risk", "归零风险", scores.rug_risk, rule.get("max_rug_risk"), "max"),
            _req("data_quality", "数据质量", scores.data_quality,
                 rule.get("min_data_quality"), "min"),
        ]
        if rule.get("min_confidence") is not None:
            requirements.append(
                _req("confidence", "把握程度", scores.confidence, rule["min_confidence"], "min")
            )
        if rule.get("min_liquidity_usd") is not None:
            requirements.append(
                _req("liquidity", "流动性", view.getf("liquidity"),
                     rule["min_liquidity_usd"], "min", is_score=False)
            )
        if rule.get("min_smart_money_count") is not None:
            requirements.append(
                _req("smart_money_count", "聪明钱地址数", view.getf("smart_money_count"),
                     rule["min_smart_money_count"], "min", is_score=False)
            )

        # 交易型状态额外要求：研究门未拦截，且审计已有明确结果
        if target.rank >= TokenState.S1.rank:
            requirements.append(Requirement(
                name="research_gate", label="研究风险门",
                passed=not risk.gate_blocked, actual=None, threshold=None,
                gap=0.0 if not risk.gate_blocked else 1.0,
            ))
            requirements.append(Requirement(
                name="audit_known", label="审计结果已知",
                passed=not risk.audit_unknown, actual=None, threshold=None,
                gap=0.0 if not risk.audit_unknown else 1.0,
            ))
        return requirements

    def _in_hysteresis_band(self, state: TokenState, view: TokenView,
                            scores: ScoreResult, risk: RiskDecision) -> bool:
        """是否处于"不升也不降"的滞回死区。

        滞回只作用于 Opportunity 这一个维度。
        风险、数据质量、流动性这些是**硬闸门**：一旦跌破就必须立刻降级，
        绝不能因为机会分还在死区里就继续挂着 S2 的身份——
        那正是"数据已经不可信却还在发交易警报"的成因。
        """
        rule = self._rules.get(state)
        if not rule:
            return False
        if scores.opportunity < rule["exit_opportunity"]:
            return False
        for requirement in self._check(state, view, scores, risk):
            if requirement.name == "opportunity":
                continue
            if not requirement.passed:
                return False
        return True

    def _detect_near_miss(self, decision: StateDecision, old: TokenState) -> None:
        """判断是否"差一点就晋升"。

        只看**当前状态之上紧邻的那一级**，从低到高找到第一个未达标的级别后
        立即停止。这一点很容易搞错：如果从 S2 往下找，一枚连 S1 都没通过的币
        会因为机会分距 S2 只差 3 分而被记成"差一点进 S2"——
        而它其实连 S1 的流动性门槛都没过，这条记录会污染整个阈值反事实研究。

        同时限制"未通过条件不超过 2 项且差距都在 margin 内"，
        否则每枚垃圾币每个周期都会产生一条记录，事件表会被瞬间灌满。
        """
        for candidate in (TokenState.S0, TokenState.S1, TokenState.S2):
            if candidate.rank <= old.rank:
                continue
            requirements = decision.requirements.get(candidate.value)
            if not requirements:
                continue
            failing = [r for r in requirements if not r.passed]
            if not failing:
                continue
            # 紧邻的这一级就是唯一候选，无论结果如何都不再往上找
            if (len(failing) <= 2
                    and not any(r.threshold is None or r.gap == float("inf")
                                for r in failing)
                    and all(self._within_margin(r) for r in failing)):
                decision.blocked_state = candidate
                decision.blocked_by = failing
                decision.near_miss = True
            return

    def _within_margin(self, requirement: Requirement) -> bool:
        if requirement.is_score:
            return requirement.gap <= self._near_miss_margin
        # 量级型条件按相对差距判定：margin=5 视为容许 5% 的差距
        return requirement.gap_ratio <= self._near_miss_margin / 100.0

    @staticmethod
    def _promotion_reason(target: TokenState,
                          requirements: dict[str, list[Requirement]]) -> str:
        reqs = requirements.get(target.value, [])
        opportunity = next((r for r in reqs if r.name == "opportunity"), None)
        if opportunity and opportunity.actual is not None:
            return (
                f"晋升 {target.value}：机会分 {opportunity.actual:.0f} "
                f"≥ {opportunity.threshold:.0f}"
            )
        return f"晋升 {target.value}"


def _rule(raw: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {
        "enter_opportunity": float(raw.get("enter_opportunity", 999)),
        "exit_opportunity": float(raw.get("exit_opportunity", 0)),
    }
    for key in ("max_rug_risk", "min_data_quality", "min_confidence",
                "min_liquidity_usd", "min_smart_money_count"):
        if raw.get(key) is not None:
            out[key] = float(raw[key])
    return out


def _req(name: str, label: str, actual: float | None, threshold: float | None,
         mode: str, *, is_score: bool = True) -> Requirement:
    """构造一条条件检查。

    actual 为 None（数据缺失）时**判定为不通过**，而不是当作通过。
    这是有意的保守设计：拿不到流动性数据就不该晋升到交易型状态。
    此时 gap 记为无穷，从而也不会被误判成 Near-Miss。
    """
    if threshold is None:
        return Requirement(name=name, label=label, passed=True,
                           actual=actual, threshold=None, is_score=is_score)
    if actual is None:
        return Requirement(name=name, label=label, passed=False, actual=None,
                           threshold=threshold, gap=float("inf"), is_score=is_score)
    if mode == "min":
        passed = actual >= threshold
        gap = 0.0 if passed else threshold - actual
    else:
        passed = actual <= threshold
        gap = 0.0 if passed else actual - threshold
    return Requirement(name=name, label=label, passed=passed, actual=actual,
                       threshold=threshold, gap=gap, is_score=is_score)
