"""双层风险门。

为什么必须分两层，而不是一个"垃圾过滤器"：

  Execution Blocker（硬拒）
    蜜罐、极高卖出税、审计高危。这些币**不可能盈利**，
    连交易型警报都不该产生。

  Research Gate（降级但继续追踪）
    筹码过度集中、流动性太薄、洗盘标签。这些币**大概率归零，
    但历史上确实有一部分变成了百倍币**。
    如果直接删掉不再采集，三个月后就永远无法回答
    "我们的 top10 阈值到底错杀了多少赢家"。
    所以它们降级进拒绝样本池，低频追踪，只是不发交易警报。

第三个关键设计：**UNKNOWN 绝不等于 PASS**。
刚创建几分钟的币，币安审计接口普遍返回 hasResult=false。
如果把"没查到风险"当成"没有风险"，风险门等于完全失效。
因此审计缺失会被显式标记为 unknown，并在晋升 S1 前强制补查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .models import TokenView
from .tags import TAG_DEV_WASH, TAG_INSIDER_WASH

# 风险规则版本：规则或阈值语义变化时递增，写入快照便于回溯
RISK_PARSER_VERSION = "r1.0.0"

GATE_EXECUTION = "execution_blocker"
GATE_RESEARCH = "research_gate"


@dataclass(slots=True)
class Violation:
    """一条被违反的规则。actual/threshold 必须结构化保存以支撑反事实研究。"""

    gate: str
    rule: str
    actual_value: float | None = None
    threshold_value: float | None = None
    actual_text: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "rule": self.rule,
            "actual": self.actual_value,
            "threshold": self.threshold_value,
            "actual_text": self.actual_text,
            "detail": self.detail,
        }


@dataclass
class RiskDecision:
    blocked: bool = False                       # 命中 Execution Blocker
    gate_blocked: bool = False                  # 命中 Research Gate
    violations: list[Violation] = field(default_factory=list)
    audit_unknown: bool = True                  # 审计结果是否缺失
    needs_audit: bool = False                   # 晋升前是否必须补查审计
    flags: dict[str, Any] = field(default_factory=dict)

    @property
    def execution_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.gate == GATE_EXECUTION]

    @property
    def research_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.gate == GATE_RESEARCH]

    def primary_reason(self) -> str:
        if self.violations:
            return self.violations[0].rule
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "gate_blocked": self.gate_blocked,
            "audit_unknown": self.audit_unknown,
            "needs_audit": self.needs_audit,
            "violations": [v.as_dict() for v in self.violations],
            "flags": self.flags,
            "risk_parser_version": RISK_PARSER_VERSION,
        }


class RiskGate:
    def __init__(self, config: Mapping[str, Any]) -> None:
        blocker = config.get("execution_blocker", {}) or {}
        self._audit_risk_min = int(blocker.get("audit_risk_level_min", 4))
        self._sell_tax_max = float(blocker.get("sell_tax_max_pct", 10.0))
        self._buy_tax_max = float(blocker.get("buy_tax_max_pct", 10.0))
        self._honeypot_blocks = bool(blocker.get("honeypot_blocks", True))

        gate = config.get("research_gate", {}) or {}
        # 按年龄分档的 Top10 阈值，从窄到宽排序以便顺序匹配
        self._top10_by_age: list[tuple[float | None, float]] = [
            (
                None if item.get("max_age_min") is None else float(item["max_age_min"]),
                float(item["threshold"]),
            )
            for item in (gate.get("top10_max_pct_by_age") or [])
        ]
        self._combined_max = float(gate.get("combined_concentration_max_pct", 55.0))
        self._dev_max = float(gate.get("dev_max_pct", 15.0))
        self._min_liquidity = float(gate.get("min_liquidity_usd", 5000.0))
        self._min_liq_mc_ratio = float(gate.get("min_liquidity_mc_ratio", 0.01))
        self._wash_tags = set(
            gate.get("wash_trading_tags") or [TAG_DEV_WASH, TAG_INSIDER_WASH]
        )

        audit_cfg = config.get("audit", {}) or {}
        self._audit_min_liquidity = float(audit_cfg.get("min_liquidity_usd", 3000.0))
        self._recheck_before_s1 = bool(audit_cfg.get("recheck_before_s1", True))
        self._audit_ttl_sec = int(audit_cfg.get("ttl_sec", 86400))

    # ── Top10 年龄分档 ──────────────────────────────────────────────────
    def top10_threshold(self, token_age_sec: int | None) -> float:
        """按代币年龄取 Top10 阈值。

        极早期筹码天然集中（第一批买家就是前十名），
        用统一的 50% 阈值会把几乎所有 10 分钟内的新币全部错杀。
        年龄未知时取最宽松档，宁可放进来再观察，也不盲目拒绝。
        """
        if not self._top10_by_age:
            return 50.0
        if token_age_sec is None:
            return max(t for _, t in self._top10_by_age)
        age_min = token_age_sec / 60.0
        for max_age_min, threshold in self._top10_by_age:
            if max_age_min is None or age_min <= max_age_min:
                return threshold
        return self._top10_by_age[-1][1]

    # ── 主评估 ──────────────────────────────────────────────────────────
    def evaluate(self, view: TokenView, now_ms: int) -> RiskDecision:
        decision = RiskDecision()
        self._check_execution_blocker(view, decision)
        self._check_research_gate(view, decision, now_ms)
        self._resolve_audit_need(view, decision, now_ms)
        return decision

    def _check_execution_blocker(self, view: TokenView, decision: RiskDecision) -> None:
        honeypot = view.get("honeypot")
        if self._honeypot_blocks and honeypot is True:
            decision.violations.append(Violation(
                gate=GATE_EXECUTION, rule="honeypot",
                actual_text="honeypot=true",
                detail="合约可买不可卖",
            ))
            decision.blocked = True

        risk_level = view.geti("audit_risk_level")
        if risk_level is not None and risk_level >= self._audit_risk_min:
            decision.violations.append(Violation(
                gate=GATE_EXECUTION, rule="audit_risk_level",
                actual_value=float(risk_level),
                threshold_value=float(self._audit_risk_min),
                detail=f"币安审计风险等级 {risk_level}",
            ))
            decision.blocked = True

        sell_tax = view.getf("sell_tax_pct")
        if sell_tax is not None and sell_tax > self._sell_tax_max:
            decision.violations.append(Violation(
                gate=GATE_EXECUTION, rule="sell_tax_max",
                actual_value=sell_tax, threshold_value=self._sell_tax_max,
                detail=f"卖出税 {sell_tax:.1f}%",
            ))
            decision.blocked = True

        buy_tax = view.getf("buy_tax_pct")
        if buy_tax is not None and buy_tax > self._buy_tax_max:
            decision.violations.append(Violation(
                gate=GATE_EXECUTION, rule="buy_tax_max",
                actual_value=buy_tax, threshold_value=self._buy_tax_max,
                detail=f"买入税 {buy_tax:.1f}%",
            ))
            decision.blocked = True

        decision.flags["honeypot"] = honeypot
        decision.flags["audit_risk_level"] = risk_level
        decision.flags["sell_tax_pct"] = sell_tax
        decision.flags["buy_tax_pct"] = buy_tax

    def _check_research_gate(self, view: TokenView, decision: RiskDecision,
                             now_ms: int) -> None:
        age_sec = view.age_sec(now_ms)
        top10 = view.getf("top10_percent")
        threshold = self.top10_threshold(age_sec)
        if top10 is not None and top10 > threshold:
            decision.violations.append(Violation(
                gate=GATE_RESEARCH, rule="top10_max",
                actual_value=top10, threshold_value=threshold,
                detail=f"Top10 {top10:.1f}%（{_age_label(age_sec)} 档阈值 {threshold:.0f}%）",
            ))
            decision.gate_blocked = True

        # 组合集中度：单看某一项都不超标，但四项叠加已经吃掉大半流通盘
        parts = {
            "dev": view.getf("dev_percent"),
            "sniper": view.getf("sniper_percent"),
            "insider": view.getf("insider_percent"),
            "bundler": view.getf("bundler_percent"),
        }
        known = {k: v for k, v in parts.items() if v is not None}
        if known:
            combined = sum(known.values())
            decision.flags["combined_concentration_pct"] = round(combined, 2)
            decision.flags["combined_parts"] = {k: round(v, 2) for k, v in known.items()}
            if combined > self._combined_max:
                decision.violations.append(Violation(
                    gate=GATE_RESEARCH, rule="combined_concentration_max",
                    actual_value=combined, threshold_value=self._combined_max,
                    detail="+".join(f"{k} {v:.1f}%" for k, v in known.items()),
                ))
                decision.gate_blocked = True

        dev = parts["dev"]
        if dev is not None and dev > self._dev_max:
            decision.violations.append(Violation(
                gate=GATE_RESEARCH, rule="dev_max",
                actual_value=dev, threshold_value=self._dev_max,
                detail=f"开发者持仓 {dev:.1f}%",
            ))
            decision.gate_blocked = True

        liquidity = view.getf("liquidity")
        if liquidity is not None and liquidity < self._min_liquidity:
            decision.violations.append(Violation(
                gate=GATE_RESEARCH, rule="min_liquidity",
                actual_value=liquidity, threshold_value=self._min_liquidity,
                detail=f"流动性 ${liquidity:,.0f}",
            ))
            decision.gate_blocked = True

        market_cap = view.getf("market_cap")
        if liquidity is not None and market_cap and market_cap > 0:
            ratio = liquidity / market_cap
            decision.flags["liquidity_mc_ratio"] = round(ratio, 5)
            if ratio < self._min_liq_mc_ratio:
                # 流动性占市值比例过低 = 账面市值虚高，实际根本卖不出去
                decision.violations.append(Violation(
                    gate=GATE_RESEARCH, rule="min_liquidity_mc_ratio",
                    actual_value=ratio, threshold_value=self._min_liq_mc_ratio,
                    detail=f"流动性/市值 {ratio:.2%}",
                ))
                decision.gate_blocked = True

        hit_wash = sorted(self._wash_tags & view.tags)
        if hit_wash:
            decision.violations.append(Violation(
                gate=GATE_RESEARCH, rule="wash_trading_tag",
                actual_text=",".join(hit_wash),
                detail="命中洗盘标签",
            ))
            decision.gate_blocked = True
        decision.flags["wash_tags"] = hit_wash

    def _resolve_audit_need(self, view: TokenView, decision: RiskDecision,
                            now_ms: int) -> None:
        """判定审计信息是否缺失、是否需要补查。

        审计请求要花配额，因此只在"值得查"时才查：
        流动性太薄的币即使安全也不会交易，没必要为它消耗预算。
        """
        available = view.get("audit_available")
        risk_level = view.geti("audit_risk_level")
        # 列表接口的内联 auditInfo 也算有效审计来源
        decision.audit_unknown = available is not True and risk_level is None

        if not decision.audit_unknown:
            age_ms = now_ms - view.audit_checked_at if view.audit_checked_at else None
            decision.needs_audit = bool(
                self._recheck_before_s1
                and age_ms is not None
                and age_ms > self._audit_ttl_sec * 1000
            )
        else:
            liquidity = view.getf("liquidity")
            worth_checking = liquidity is None or liquidity >= self._audit_min_liquidity
            decision.needs_audit = bool(self._recheck_before_s1 and worth_checking)

        decision.flags["audit_unknown"] = decision.audit_unknown
        decision.flags["audit_checked_at"] = view.audit_checked_at or None


def _age_label(age_sec: int | None) -> str:
    if age_sec is None:
        return "年龄未知"
    minutes = age_sec / 60.0
    if minutes < 60:
        return f"{minutes:.0f}分钟"
    hours = minutes / 60.0
    if hours < 24:
        return f"{hours:.1f}小时"
    return f"{hours / 24:.1f}天"
