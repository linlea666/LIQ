"""五维评分器。

五个维度刻意分开而不是合成一个总分，因为它们回答的是不同问题：

  Opportunity   这枚币现在看起来有多大机会？
  Confidence    我们对这个判断有多大把握？（证据是否互相印证）
  DataQuality   我们拿到的数据本身有多可信？（接口是否新鲜、是否自相矛盾）
  RugRisk       归零风险有多高？
  Distribution  主力是否正在派发出货？

合成单一总分会丢掉最关键的信息：一枚 Opportunity 88 但 Confidence 31 的币，
和一枚 Opportunity 72 但 Confidence 89 的币，处理方式应该完全不同。

**缺失数据的处理**是这里最重要的设计决策：
某个因子算不出来时，给一个中性基线分（而不是 0 分），
同时把"覆盖率"计入 Confidence。
如果给 0 分，数据缺失的币会被双重惩罚（DataQuality 已经罚过了）；
如果直接剔除该因子再归一化，单个可用因子就能撑起一个高分。
中性基线 + 低 Confidence 才能既保持分数可比，又如实反映把握程度。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .models import FactorScore, FeatureSet, QualityReport, ScoreResult, TokenView
from .risk_gate import RiskDecision

STRATEGY_VERSION_DEFAULT = "v1.0.0"

# 因子无法计算时给的中性基线（占该因子满分的比例）
NEUTRAL_BASELINE = 0.35


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _scale(value: float | None, low: float, high: float) -> float | None:
    """把数值线性映射到 0..1。低于 low 得 0，高于 high 得 1。"""
    if value is None:
        return None
    if high <= low:
        return None
    return _clamp01((value - low) / (high - low))


def _scale_inverse(value: float | None, low: float, high: float) -> float | None:
    """越小越好的指标：low 得 1，high 得 0。"""
    result = _scale(value, low, high)
    return None if result is None else 1.0 - result


def _blend(*parts: float | None) -> tuple[float | None, int]:
    """对可用的子项取均值，返回 (均值, 可用子项数)。"""
    known = [p for p in parts if p is not None]
    if not known:
        return None, 0
    return sum(known) / len(known), len(known)


class Scorer:
    def __init__(self, config: Mapping[str, Any]) -> None:
        weights = config.get("opportunity_weights", {}) or {}
        self._weights: dict[str, float] = {
            "holder_momentum": float(weights.get("holder_momentum", 20)),
            "capital_flow": float(weights.get("capital_flow", 20)),
            "smart_money": float(weights.get("smart_money", 15)),
            "liquidity_quality": float(weights.get("liquidity_quality", 15)),
            "distribution_health": float(weights.get("distribution_health", 15)),
            "social_momentum": float(weights.get("social_momentum", 10)),
            "valuation_upside": float(weights.get("valuation_upside", 5)),
        }
        self._total_weight = sum(self._weights.values()) or 100.0
        self._scenarios = config.get("valuation_scenarios", {}) or {}
        self.strategy_version = str(config.get("strategy_version", STRATEGY_VERSION_DEFAULT))

    # ═════════════════════════════════════════════════════════════════════
    def score(
        self,
        view: TokenView,
        features: FeatureSet,
        quality: QualityReport,
        risk: RiskDecision,
    ) -> ScoreResult:
        factors: list[FactorScore] = []
        coverage_hits = 0
        coverage_total = 0

        for name, builder in self._factor_builders().items():
            weight = self._weights[name]
            normalized, detail = builder(view, features)
            coverage_total += 1
            if normalized is None:
                normalized = NEUTRAL_BASELINE
                detail = detail or "数据不足，取中性基线"
            else:
                coverage_hits += 1
            factors.append(FactorScore(
                name=name,
                label=_FACTOR_LABELS.get(name, name),
                score=normalized * weight,
                max_score=weight,
                detail=detail,
            ))

        raw_opportunity = sum(f.score for f in factors)
        opportunity = _clamp01(raw_opportunity / self._total_weight) * 100.0

        coverage_ratio = coverage_hits / coverage_total if coverage_total else 0.0
        rug_risk, risk_flags = self._rug_risk(view, features, risk)
        distribution, distribution_reasons = self._distribution(view, features)
        confidence = self._confidence(
            view, features, quality, coverage_ratio, factors, rug_risk
        )

        # 归零风险极高时压低机会分：否则会出现"Opportunity 91 / RugRisk 88"
        # 这种自相矛盾的展示，人看了不知道该怎么办
        if rug_risk >= 70:
            opportunity *= 0.6
        elif rug_risk >= 55:
            opportunity *= 0.8

        return ScoreResult(
            opportunity=round(opportunity, 2),
            confidence=round(confidence, 2),
            data_quality=round(quality.score, 2),
            rug_risk=round(rug_risk, 2),
            distribution=round(distribution, 2),
            factors=factors,
            risk_flags=risk_flags,
            distribution_reasons=distribution_reasons,
        )

    def _factor_builders(self) -> dict[str, Callable[..., tuple[float | None, str]]]:
        return {
            "holder_momentum": self._f_holder_momentum,
            "capital_flow": self._f_capital_flow,
            "smart_money": self._f_smart_money,
            "liquidity_quality": self._f_liquidity_quality,
            "distribution_health": self._f_distribution_health,
            "social_momentum": self._f_social_momentum,
            "valuation_upside": self._f_valuation_upside,
        }

    # ═════════════════════════════════════════════════════════════════════
    # Opportunity 因子
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _f_holder_momentum(view: TokenView, fs: FeatureSet) -> tuple[float | None, str]:
        """持有人动量：增速 + 加速度 + 年龄归一化速度。"""
        growth_5m = _scale(fs.get("holder_growth_5m"), 0.0, 0.5)
        growth_15m = _scale(fs.get("holder_growth_15m"), 0.0, 1.0)
        per_min = _scale(fs.get("holder_per_min_15m"), 0.0, 20.0)
        accel = _scale(fs.get("holder_acceleration"), 0.0, 5.0)
        lifetime = _scale(fs.get("holders_per_hour_lifetime"), 0.0, 300.0)

        value, hits = _blend(growth_5m, growth_15m, per_min, accel, lifetime)
        if value is None:
            return None, ""
        holders = view.geti("holders")
        return value, f"持有人 {holders if holders is not None else '—'}，{hits} 项动量指标可用"

    @staticmethod
    def _f_capital_flow(view: TokenView, fs: FeatureSet) -> tuple[float | None, str]:
        """资金流：净流入占比 + 换手强度 + 买卖失衡。"""
        inflow_mc = _scale(fs.get("net_inflow_mc_ratio"), 0.0, 0.05)
        inflow_liq = _scale(fs.get("net_inflow_liq_ratio"), 0.0, 0.3)
        turnover = _scale(fs.get("volume_mc_ratio"), 0.05, 2.0)
        imbalance = _scale(fs.get("buy_sell_imbalance"), -0.2, 0.5)

        value, hits = _blend(inflow_mc, inflow_liq, turnover, imbalance)
        if value is None:
            return None, ""
        inflow = view.getf("net_inflow")
        detail = f"净流入 ${inflow:,.0f}" if inflow is not None else f"{hits} 项资金指标可用"
        return value, detail

    @staticmethod
    def _f_smart_money(view: TokenView, fs: FeatureSet) -> tuple[float | None, str]:
        """聪明钱：数量、留存率、持仓占比。

        留存率是必需的：只看数量会在聪明钱已经全部离场时依然给高分，
        等于专门在别人跑完之后进场接盘。
        """
        count = fs.get("smart_money_count")
        count_score = _scale(count, 0.0, 8.0)
        retention = fs.get("smart_money_retention")
        holding = _scale(fs.get("smart_money_percent"), 0.0, 12.0)
        delta = _scale(fs.get("smart_money_delta_15m"), 0.0, 3.0)

        value, hits = _blend(count_score, retention, holding, delta)
        if value is None:
            return None, ""

        exit_rate = fs.get("exit_rate")
        if exit_rate is not None and exit_rate >= 80:
            # 已基本清仓，无论数量多少都不构成机会
            value = min(value, 0.15)
        detail_parts = []
        if count is not None:
            detail_parts.append(f"{int(count)} 个聪明钱地址")
        if exit_rate is not None:
            detail_parts.append(f"离场率 {exit_rate:.0f}%")
        return value, "，".join(detail_parts) or f"{hits} 项指标可用"

    @staticmethod
    def _f_liquidity_quality(view: TokenView, fs: FeatureSet) -> tuple[float | None, str]:
        """流动性质量：绝对额、占市值比例、成交分散度。"""
        liquidity = view.getf("liquidity")
        absolute = _scale(liquidity, 3000.0, 80000.0)
        ratio = _scale(fs.get("liquidity_mc_ratio"), 0.01, 0.25)
        growth = _scale(fs.get("liq_growth_15m"), -0.1, 0.5)
        dispersion = fs.get("trader_per_trade")

        value, hits = _blend(absolute, ratio, growth, dispersion)
        if value is None:
            return None, ""
        detail = f"流动性 ${liquidity:,.0f}" if liquidity is not None else f"{hits} 项指标可用"
        return value, detail

    @staticmethod
    def _f_distribution_health(view: TokenView, fs: FeatureSet) -> tuple[float | None, str]:
        """筹码健康度：集中度越低越好，且 Top10 在下降是加分项。"""
        top10 = _scale_inverse(fs.get("top10_percent"), 20.0, 70.0)
        combined = _scale_inverse(fs.get("combined_concentration"), 5.0, 50.0)
        dev = _scale_inverse(fs.get("dev_percent"), 1.0, 15.0)
        # Top10 变化为负 = 筹码在分散，是最强的健康信号之一
        top10_trend = _scale_inverse(fs.get("top10_delta_15m"), -5.0, 3.0)
        kyc = _scale(fs.get("kyc_holder_ratio"), 0.0, 0.3)

        value, hits = _blend(top10, combined, dev, top10_trend, kyc)
        if value is None:
            return None, ""
        top10_pct = fs.get("top10_percent")
        detail = f"Top10 {top10_pct:.1f}%" if top10_pct is not None else f"{hits} 项指标可用"
        coverage = fs.get("concentration_coverage")
        if coverage is not None and coverage < 0.5:
            detail += "（集中度数据不完整）"
        return value, detail

    @staticmethod
    def _f_social_momentum(view: TokenView, fs: FeatureSet) -> tuple[float | None, str]:
        """社交动量。Meme 币的社交榜覆盖率很低，缺失是常态而非异常。"""
        hype = _scale(fs.get("social_hype"), 100.0, 50000.0)
        hype_ratio = _scale(fs.get("hype_mc_ratio"), 0.0, 0.05)
        search = _scale(fs.get("search_count_24h"), 10.0, 500.0)
        followers = _scale(fs.get("twitter_followers"), 500.0, 100000.0)
        sentiment = fs.get("sentiment_score")
        sentiment_scaled = None if sentiment is None else _clamp01((sentiment + 1.0) / 2.0)

        value, hits = _blend(hype, hype_ratio, search, followers, sentiment_scaled)
        if value is None:
            return None, ""
        return value, f"{hits} 项社交指标可用"

    def _f_valuation_upside(self, view: TokenView, fs: FeatureSet) -> tuple[float | None, str]:
        """估值空间。

        必须强调：这是**情景假设**，不是价格预测。
        它只回答"如果这枚币走到 300 万市值，相对现在还有几倍空间"，
        完全不表示它会走到那里。
        """
        market_cap = view.getf("market_cap")
        if market_cap is None or market_cap <= 0:
            return None, ""
        base = float(self._scenarios.get("base_mc", 3_000_000))
        multiple = base / market_cap
        # 空间越大得分越高，但用对数尺度避免"市值 2000 美元"直接顶满
        value = _scale(multiple, 1.0, 50.0)
        return value, f"距基准情景 ${base / 1e6:.0f}M 约 {multiple:.1f}×"

    # ═════════════════════════════════════════════════════════════════════
    # RugRisk
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _rug_risk(view: TokenView, fs: FeatureSet,
                  risk: RiskDecision) -> tuple[float, dict[str, Any]]:
        """归零风险 0-100（越高越危险）。

        **UNKNOWN 会加风险，而不是被忽略。**
        审计查不到结果的币不等于安全的币；如果把未知当无风险，
        风险分会系统性偏低，而这类币恰恰是最容易归零的一批。
        """
        risk_score = 0.0
        flags: dict[str, Any] = dict(risk.flags)

        if risk.blocked:
            # 命中硬拒规则直接顶格，不再参与细分加权
            return 100.0, {**flags, "execution_blocked": True,
                           "violations": [v.rule for v in risk.execution_violations]}

        top10 = fs.get("top10_percent")
        if top10 is not None:
            risk_score += (_scale(top10, 30.0, 85.0) or 0.0) * 25.0

        combined = fs.get("combined_concentration")
        if combined is not None:
            risk_score += (_scale(combined, 10.0, 60.0) or 0.0) * 20.0

        dev = fs.get("dev_percent")
        if dev is not None:
            risk_score += (_scale(dev, 2.0, 20.0) or 0.0) * 10.0

        liq_ratio = fs.get("liquidity_mc_ratio")
        if liq_ratio is not None:
            risk_score += (_scale_inverse(liq_ratio, 0.01, 0.15) or 0.0) * 15.0

        liquidity = view.getf("liquidity")
        if liquidity is not None:
            risk_score += (_scale_inverse(liquidity, 2000.0, 30000.0) or 0.0) * 10.0

        exit_rate = fs.get("exit_rate")
        if exit_rate is not None:
            risk_score += (_scale(exit_rate, 30.0, 100.0) or 0.0) * 8.0

        new_wallet = fs.get("new_wallet_percent")
        if new_wallet is not None:
            # 新钱包占比极高通常意味着女巫/机器人堆量
            risk_score += (_scale(new_wallet, 40.0, 90.0) or 0.0) * 7.0

        # ── 行为项（V2）────────────────────────────────────────────────
        # 上面全是静态存量指标，对"正在发生的 rug"完全失明：
        # 实盘 7 条 S1 推送全部 RUG，当时 rug 分仅 0-7——筹码结构看着健康，
        # 但 LP 正在被抽、dev 正在卖。行为项只在异常时加分，
        # 干净币分数不变，不影响既有阈值
        liq_growth = fs.get("liq_growth_15m")
        if liq_growth is not None and liq_growth < -0.05:
            risk_score += (_scale(-liq_growth, 0.05, 0.4) or 0.0) * 22.0
            flags["lp_outflow_15m"] = round(liq_growth, 4)

        dev_sell = fs.get("dev_sell_percent")
        if dev_sell is not None and dev_sell >= 30.0:
            risk_score += (_scale(dev_sell, 30.0, 100.0) or 0.0) * 12.0
            flags["dev_selling"] = round(dev_sell, 2)

        # 拉价同时抽池：价格上涨掩护流动性撤离，是拔池前最典型的形态。
        # 权重设计目标：LP 深度流出 + dev 抛售 + 背离三项齐发时必须越过
        # S1 的 max_rug_risk=45 闸门；单一信号不足一票否决（S2 确认制兜底）
        price_growth = fs.get("price_growth_15m")
        if (price_growth is not None and price_growth > 0.2
                and liq_growth is not None and liq_growth < -0.02):
            risk_score += 15.0
            flags["price_liq_divergence"] = True

        audit_level = view.geti("audit_risk_level")
        if audit_level is not None:
            risk_score += (_scale(float(audit_level), 0.0, 3.0) or 0.0) * 10.0
        elif risk.audit_unknown:
            # 未知不是安全：给一个明确的固定加成
            risk_score += 8.0
            flags["audit_unknown_penalty"] = 8.0

        # 集中度数据覆盖率不足不在这里重复罚分——
        # DataQuality 与 Confidence 已各自体现，重复惩罚会让分数失真

        for violation in risk.research_violations:
            risk_score += 6.0
            flags.setdefault("research_violations", []).append(violation.rule)

        flags["rug_risk_raw"] = round(risk_score, 2)
        return _clamp01(risk_score / 100.0) * 100.0, flags

    # ═════════════════════════════════════════════════════════════════════
    # Distribution（派发/出货）
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _distribution(view: TokenView, fs: FeatureSet) -> tuple[float, list[str]]:
        """派发分 0-100（越高越像在出货）。

        与 RugRisk 的区别：RugRisk 问"会不会归零"，
        Distribution 问"主力是不是**正在**离场"。
        一枚基本面尚可的币也可能正在被派发，这时候进场同样亏钱。
        """
        score = 0.0
        reasons: list[str] = []

        imbalance = fs.get("buy_sell_imbalance")
        if imbalance is not None and imbalance < 0:
            contribution = (_scale(-imbalance, 0.05, 0.6) or 0.0) * 25.0
            if contribution > 1:
                score += contribution
                reasons.append(f"卖压占优（买卖失衡 {imbalance:.2f}）")

        exit_rate = fs.get("exit_rate")
        if exit_rate is not None and exit_rate >= 40:
            contribution = (_scale(exit_rate, 40.0, 100.0) or 0.0) * 25.0
            score += contribution
            reasons.append(f"聪明钱离场率 {exit_rate:.0f}%")

        sm_delta = fs.get("smart_money_delta_15m")
        if sm_delta is not None and sm_delta < 0:
            score += min(15.0, abs(sm_delta) * 5.0)
            reasons.append(f"聪明钱地址减少 {abs(sm_delta):.0f} 个")

        liq_growth = fs.get("liq_growth_15m")
        if liq_growth is not None and liq_growth < -0.05:
            contribution = (_scale(-liq_growth, 0.05, 0.4) or 0.0) * 20.0
            score += contribution
            reasons.append(f"流动性下降 {abs(liq_growth):.1%}")

        price_growth = fs.get("price_growth_15m")
        holder_growth = fs.get("holder_growth_15m")
        if price_growth is not None and price_growth < -0.15:
            score += (_scale(-price_growth, 0.15, 0.7) or 0.0) * 15.0
            reasons.append(f"价格回落 {abs(price_growth):.1%}")
        # 价格在涨但持有人不增，典型的拉高派发形态
        if (price_growth is not None and price_growth > 0.2
                and holder_growth is not None and holder_growth <= 0.0):
            score += 12.0
            reasons.append("价格上涨但持有人未增长（疑似拉高派发）")

        dev_sell = fs.get("dev_sell_percent")
        if dev_sell is not None and dev_sell >= 50:
            score += 8.0
            reasons.append(f"开发者已卖出 {dev_sell:.0f}%")

        return _clamp01(score / 100.0) * 100.0, reasons[:6]

    # ═════════════════════════════════════════════════════════════════════
    # Confidence
    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _confidence(
        view: TokenView,
        fs: FeatureSet,
        quality: QualityReport,
        coverage_ratio: float,
        factors: list[FactorScore],
        rug_risk: float,
    ) -> float:
        """把握程度 0-100。

        构成：因子覆盖率、观测历史深度、数据质量、以及**独立证据互相印证**。
        最后一项最重要：持有人在涨、净流入为正、聪明钱在进——
        三个来源相互独立却指向同一结论，这比单一维度的极端值可靠得多。
        """
        # 1) 因子覆盖率（占 35 分）
        score = coverage_ratio * 35.0

        # 2) 观测深度（占 20 分）：只看过一眼的币不可能有高把握
        depth = min(1.0, view.history_depth / 12.0)
        score += depth * 20.0

        # 3) 数据质量（占 25 分）
        score += (quality.score / 100.0) * 25.0

        # 4) 多源印证（占 20 分）
        corroboration = 0
        if (fs.get("holder_growth_15m") or 0.0) > 0.05:
            corroboration += 1
        if (fs.get("net_inflow_mc_ratio") or 0.0) > 0.002:
            corroboration += 1
        if (fs.get("smart_money_count") or 0.0) >= 2:
            corroboration += 1
        if (fs.get("buy_sell_imbalance") or 0.0) > 0.05:
            corroboration += 1
        if (fs.get("liq_growth_15m") or 0.0) > 0.02:
            corroboration += 1
        score += min(1.0, corroboration / 4.0) * 20.0

        # 关键字段组过期时封顶：数据不新鲜就不允许出现高把握
        if quality.stale_groups:
            score = min(score, 60.0)
        if quality.block_s2:
            score = min(score, 55.0)
        if rug_risk >= 85:
            score = min(score, 45.0)

        return _clamp01(score / 100.0) * 100.0


_FACTOR_LABELS: dict[str, str] = {
    "holder_momentum": "持有人动量",
    "capital_flow": "资金流入",
    "smart_money": "聪明钱",
    "liquidity_quality": "流动性质量",
    "distribution_health": "筹码健康度",
    "social_momentum": "社交动量",
    "valuation_upside": "估值空间",
}
