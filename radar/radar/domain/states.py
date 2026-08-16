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

        # S2 确认制（V2）：S2 不再由机会分晋升（旧阈值 85 在当前评分模型下
        # 数学上不可达，实测 24,621 条快照最大 75.26——S2 是死档位），
        # 改为对 S1 观察池做"时间 + 行为"确认。
        s2c = config.get("s2_confirmation", {}) or {}
        self._s2c_enabled = bool(s2c.get("enabled", True))
        self._s2c_min_age_ms = int(float(s2c.get("min_age_from_s1_sec", 1200)) * 1000)
        self._s2c_max_drawdown_pct = float(s2c.get("max_drawdown_from_peak_pct", 40.0))
        self._s2c_min_price_ratio = float(s2c.get("min_price_vs_anchor_ratio", 0.7))
        self._s2c_lp_drop_veto_pct = float(s2c.get("hard_veto_lp_drop_pct", 20.0))
        self._s2c_exit_rate_veto = float(s2c.get("hard_veto_exit_rate", 60.0))
        if self._s2c_enabled:
            # S2 的维持/退出闸门继承 S1 规则：确认制下 S2 没有独立的机会分
            # 进入门槛，退出仍需要滞回带，用 S1 的（旧 transitions.s2.* 弃用）
            self._rules[TokenState.S2] = dict(self._rules[TokenState.S1])
        self._dormant_after_ms = int(config.get("dormant_after_stale_sec", 21600)) * 1000
        self._dead_min_liquidity = float(config.get("dead_min_liquidity_usd", 500.0))
        # 价格崩塌判死：拔池后接口常报残余流动性（实盘见过 $12,817），
        # 只靠流动性线永远抓不到最常见的死法（价格瞬间 -99.9%）
        self._dead_collapse_pct = float(config.get("dead_price_collapse_pct", 90.0))
        self._dead_confirm = max(1, int(config.get("dead_confirm_cycles", 2)))
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

        # ── S1/S2 确认期：行为追踪、硬否决、确认晋升 ──────────────────
        if self._s2c_enabled and old.rank >= TokenState.S1.rank:
            self._update_s1_tracking(view)
            veto = self._s1_hard_veto(view)
            if veto:
                decision.new_state = TokenState.DISTRIBUTION
                decision.changed = True
                decision.reason = veto
                return decision

        if self._s2c_enabled and old == TokenState.S1:
            confirm_reqs = self._s2_confirmation_check(view, scores, risk, now_ms)
            # 无论通过与否都记录：落库的 requirements 是前端"为什么还没
            # 晋升 S2"解释与 Near-Miss 反事实研究的唯一数据源
            decision.requirements[TokenState.S2.value] = confirm_reqs
            if all(r.passed for r in confirm_reqs):
                decision.new_state = TokenState.S2
                decision.changed = True
                held_min = (now_ms - view.state_since_ms) / 60_000
                decision.reason = (
                    f"S1 确认期通过：存活 {held_min:.0f} 分钟，"
                    "回撤/结构/二次确认全部达标"
                )
                return decision

        # ── 逐级检查晋升条件（从高到低）─────────────────────────────
        # 确认制下 S2 只能从 S1 池确认晋升，不参与机会分晋升
        candidates = (
            (TokenState.S1, TokenState.S0) if self._s2c_enabled
            else (TokenState.S2, TokenState.S1, TokenState.S0)
        )
        target = TokenState.WATCHING
        for candidate in candidates:
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

        collapse = self._price_collapse(view)
        if collapse is not None:
            return collapse

        if view.last_observed_ms and now_ms - view.last_observed_ms > self._dormant_after_ms:
            hours = (now_ms - view.last_observed_ms) / 3_600_000
            return TokenState.DORMANT, f"已 {hours:.1f} 小时未再出现在任何榜单"

        return None

    # 确认计数在 exit_streak 字典里的专用键。状态变更时会随整个字典清零，
    # 这正是想要的语义：进入新状态后重新开始计数
    _DEAD_STREAK_KEY = "__dead_price_collapse"

    def _price_collapse(self, view: TokenView) -> tuple[TokenState, str] | None:
        """价格较历史窗口内高点崩塌 ≥N% 且连续多次评估确认 → DEAD。

        单次坏数据（接口偶发返回错价）不该判死一枚活币，
        因此复用退出确认的思路：需连续 dead_confirm_cycles 次评估都满足。
        DEAD 本身可复活（崩塌条件消失即回 WATCHING），误判成本有限，
        但确认计数仍能挡住绝大多数数据抖动。
        """
        price = view.getf("price")
        if price is None or price <= 0:
            view.exit_streak.pop(self._DEAD_STREAK_KEY, None)
            return None

        peak = None
        for point in view.history:
            if point.price is not None and (peak is None or point.price > peak):
                peak = point.price
        for point in view.history_coarse:
            if point.price is not None and (peak is None or point.price > peak):
                peak = point.price

        threshold = 1.0 - self._dead_collapse_pct / 100.0
        if peak is None or peak <= 0 or price > peak * threshold:
            view.exit_streak.pop(self._DEAD_STREAK_KEY, None)
            return None

        streak = view.exit_streak.get(self._DEAD_STREAK_KEY, 0) + 1
        view.exit_streak[self._DEAD_STREAK_KEY] = streak
        # 确认计数只约束"进入"DEAD。已是 DEAD 的币条件仍成立时必须直接维持：
        # 进入 DEAD 时 exit_streak 被清零，若维持也要重新数满 N 次，
        # 币会在 DEAD 与 WATCHING 之间来回抖动
        if streak < self._dead_confirm and view.state != TokenState.DEAD:
            return None

        drop_pct = (1.0 - price / peak) * 100.0
        return TokenState.DEAD, (
            f"价格较近期高点崩塌 {drop_pct:.1f}%（连续 {streak} 次确认），视为已死亡"
        )

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

    # ═════════════════════════════════════════════════════════════════════
    # S2 确认制（V2）
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _update_s1_tracking(view: TokenView) -> None:
        """S1/S2 期间的行为追踪：确认期最高价与净流入回落标记。

        只用真实现价更新峰值（不用回看极值——那是 Outcome 污染的教训）。
        """
        price = view.getf("price")
        if price is not None and price > 0:
            if view.s1_peak_price is None or price > view.s1_peak_price:
                view.s1_peak_price = price
        net_inflow = view.getf("net_inflow")
        if net_inflow is not None and net_inflow <= 0:
            view.s1_inflow_dipped = True

    def _s1_hard_veto(self, view: TokenView) -> str | None:
        """确认期硬否决：出现出货行为即时转 DISTRIBUTION，不等确认期走完。

        这些是 V1 实盘 7/7 买在顶部的直接死因——RugRisk 只看静态筹码
        结构（7 条 S1 的 rug 分仅 0-7），对拔池/抛售这类**行为**完全失明。
        """
        anchor = view.s1_anchor or {}

        liquidity = view.getf("liquidity")
        anchor_liq = anchor.get("liquidity")
        if (liquidity is not None and anchor_liq
                and liquidity < anchor_liq * (1.0 - self._s2c_lp_drop_veto_pct / 100.0)):
            drop = (1.0 - liquidity / anchor_liq) * 100.0
            return f"LP 较 S1 锚点抽离 {drop:.0f}%，疑似拔池，转入派发观察"

        exit_rate = view.getf("exit_rate")
        if exit_rate is not None and exit_rate >= self._s2c_exit_rate_veto:
            return f"聪明钱离场率飙升至 {exit_rate:.0f}%，转入派发观察"

        dev_sell = view.getf("dev_sell_percent")
        anchor_dev_sell = anchor.get("dev_sell_percent")
        if dev_sell is not None:
            if anchor_dev_sell is not None and dev_sell - anchor_dev_sell >= 20.0:
                return (f"开发者较 S1 锚点新增卖出 "
                        f"{dev_sell - anchor_dev_sell:.0f}%，转入派发观察")
            if anchor_dev_sell is None and dev_sell >= 50.0:
                return f"开发者已卖出 {dev_sell:.0f}%，转入派发观察"
        return None

    def _s2_confirmation_check(self, view: TokenView, scores: ScoreResult,
                               risk: RiskDecision, now_ms: int) -> list[Requirement]:
        """S1 → S2 的确认条件。全部通过才晋升。

        设计原则：不再依赖不可达的机会分，改问四个可证伪的问题——
        活得够久吗？扛住回撤了吗？结构还在改善吗？有二次确认吗？
        """
        anchor = view.s1_anchor or {}
        price = view.getf("price")
        anchor_price = anchor.get("price")
        peak = view.s1_peak_price

        requirements: list[Requirement] = []

        # 1) 最短存活：进入 S1 后至少存活 N 分钟（用本次 S1 停留时间计）
        held_sec = (now_ms - view.state_since_ms) / 1000.0 if view.state_since_ms else None
        requirements.append(_req(
            "s2_min_age", "确认期时长", held_sec,
            self._s2c_min_age_ms / 1000.0, "min", is_score=False,
        ))

        # 2) 抗回撤：确认期最高价回撤 < N%，且现价不低于锚点价的一定比例
        drawdown = None
        if price is not None and price > 0 and peak and peak > 0:
            drawdown = (1.0 - price / peak) * 100.0
        requirements.append(_req(
            "s2_drawdown", "峰值回撤", drawdown,
            self._s2c_max_drawdown_pct, "max", is_score=False,
        ))
        price_ratio = None
        if price is not None and price > 0 and anchor_price:
            price_ratio = price / anchor_price
        requirements.append(_req(
            "s2_price_vs_anchor", "现价/锚点价", price_ratio,
            self._s2c_min_price_ratio, "min", is_score=False,
        ))

        # 3) 结构持续：持有人连增、流动性未降、Top10 未升、dev 未减仓。
        #    锚点缺某字段（重启恢复的锚点只有价格/市值/流动性/持有人）时
        #    跳过该项对比；当前值缺失则保守判不通过
        requirements.append(self._structure_holders(view, now_ms))
        liquidity = view.getf("liquidity")
        anchor_liq = anchor.get("liquidity")
        if anchor_liq:
            ok = liquidity is not None and liquidity >= anchor_liq * 0.95
            requirements.append(Requirement(
                name="s2_liquidity_hold", label="流动性未降", passed=ok,
                actual=liquidity, threshold=round(anchor_liq * 0.95, 2),
                gap=0.0 if ok else float("inf"), is_score=False,
            ))
        top10 = view.getf("top10_percent")
        anchor_top10 = anchor.get("top10_percent")
        if anchor_top10 is not None:
            ok = top10 is not None and top10 <= anchor_top10 + 2.0
            requirements.append(Requirement(
                name="s2_top10_hold", label="Top10 未升", passed=ok,
                actual=top10, threshold=round(anchor_top10 + 2.0, 2),
                gap=0.0 if ok else float("inf"), is_score=False,
            ))
        dev = view.getf("dev_percent")
        anchor_dev = anchor.get("dev_percent")
        if anchor_dev is not None:
            ok = dev is not None and dev >= anchor_dev - 2.0
            requirements.append(Requirement(
                name="s2_dev_hold", label="dev 未减仓", passed=ok,
                actual=dev, threshold=round(anchor_dev - 2.0, 2),
                gap=0.0 if ok else float("inf"), is_score=False,
            ))

        # 4) 二次确认：S1 后价格创过新高，或净流入回落后二次转正
        new_high = bool(anchor_price and peak and peak > anchor_price * 1.02)
        net_inflow = view.getf("net_inflow")
        inflow_reconfirm = bool(
            view.s1_inflow_dipped and net_inflow is not None and net_inflow > 0
        )
        confirmed = new_high or inflow_reconfirm
        requirements.append(Requirement(
            name="s2_reconfirm", label="二次确认（新高或净流入转正）",
            passed=confirmed, actual=None, threshold=None,
            gap=0.0 if confirmed else float("inf"), is_score=False,
        ))

        # 5) 沿用 S1 级硬闸门（去掉机会分——确认期里动量分自然衰减，
        #    要求它维持在 72 会把所有健康盘整全部挡在门外）
        for requirement in self._check(TokenState.S2, view, scores, risk):
            if requirement.name != "opportunity":
                requirements.append(requirement)
        return requirements

    def _structure_holders(self, view: TokenView, now_ms: int) -> Requirement:
        """持有人两个观察窗口连增：15 分钟净增 > 0 且 5 分钟未减。"""
        holders = view.geti("holders")
        h5 = view.history_at_or_before(now_ms - 300_000)
        h15 = view.history_at_or_before(now_ms - 900_000)
        h5_val = h5.holders if h5 else None
        h15_val = h15.holders if h15 else None
        ok = (holders is not None and h5_val is not None and h15_val is not None
              and holders > h15_val and holders >= h5_val)
        return Requirement(
            name="s2_holders_grow", label="持有人连增",
            passed=ok, actual=holders,
            threshold=float(h15_val) if h15_val is not None else None,
            gap=0.0 if ok else float("inf"), is_score=False,
        )

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
