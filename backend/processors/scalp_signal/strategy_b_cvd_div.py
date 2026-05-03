"""策略 B · CVD Divergence（期现 CVD 背离）

策略哲学（dev-constraints #1 根因优先）：
  现货与合约 CVD 方向相反 = 资金面与杠杆面分歧
  - 现强合弱（spot CVD 上升 + contract CVD 下降）：
      现货真金白银买入，合约空头加杠杆 → 空头被挤压风险大 → 看涨
  - 合强现弱（contract CVD 上升 + spot CVD 下降）：
      合约多头加杠杆，现货抛售 → 多头一旦撑不住就爆仓 → 看跌
  + 价格 1h 变动 < 1.5%（分歧未传导到价格）= 信号未被市场消化
  + 附近有关键位（±0.5%）= 流动性催化点

逻辑链路：
  1) state.cvd_contract.trend_1h 与 state.cvd_spot.trend_1h 方向相反
  2) |contract.delta_1h| 与 |spot.delta_1h| 都 ≥ MIN_DIVERGENCE_USD
  3) 价格在最近 1h 变动 < PRICE_DRIFT_MAX_PCT（信号未传导）
  4) 附近有 KL（final_score ≥ 50，距离 ≤ 0.5%）

预测方向：
  - 现强合弱（spot=rising, contract=declining） → up
  - 合强现弱（contract=rising, spot=declining） → down

适用 regime：trend_up / trend_down / range（squeeze 信号弱）
适用 horizon：30 / 60（10 太短，分歧未必传导）

复用决策（dev-constraints #3）：
  - state.cvd_contract / cvd_spot (CVDData) → 直接复用
  - candles_15m → 直接复用做 1h 价格变动判定
  - KeyLevelV2 → 直接复用做催化点判定
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Optional

from models.scalp_signal import EvidenceItem, ScalpDirection, StrategyName

from processors.scalp_signal.base_strategy import (
    BaseStrategy,
    StrategyCandidate,
    StrategyContext,
)

logger = logging.getLogger(__name__)


# 策略参数（暴露为常量，便于单测覆盖）
MIN_DIVERGENCE_USD = 5_000_000              # 单边 CVD 1h 累计 ≥ 500 万 USD 才视为有效信号
PRICE_DRIFT_MAX_PCT = 1.5                    # 1h 价格变动 ≤ 1.5%（分歧未传导）
KL_NEAR_PCT_MAX = 0.5                        # KL 距离 ≤ 0.5%（KL 是可选催化点，非必需）
KL_FINAL_SCORE_MIN = 50.0


class CVDDivergenceStrategy(BaseStrategy):
    """策略 B · 期现 CVD 背离"""

    name: ClassVar[StrategyName] = StrategyName.B_CVD_DIVERGENCE
    display_name: ClassVar[str] = "B 现合 CVD 背离"
    suitable_regimes: ClassVar[set[str]] = {"trend_up", "trend_down", "range"}
    suitable_horizons: ClassVar[set[int]] = {30, 60}

    def detect(self, ctx: StrategyContext) -> Optional[StrategyCandidate]:
        # ── 1) 取参考价 ──
        ref = self.safe_attr(ctx.state, "ticker", "last", default=0.0)
        if not ref or ref <= 0:
            return None

        # ── 2) 取 CVD 现/合 数据 ──
        contract = getattr(ctx.state, "cvd_contract", None)
        spot = getattr(ctx.state, "cvd_spot", None)
        if contract is None or spot is None:
            return None

        c_trend = (getattr(contract, "trend_1h", "") or "").lower()
        s_trend = (getattr(spot, "trend_1h", "") or "").lower()
        c_delta = float(getattr(contract, "delta_1h", 0.0) or 0.0)
        s_delta = float(getattr(spot, "delta_1h", 0.0) or 0.0)

        # ── 3) 检查"方向相反"（仅 rising × declining 算）──
        direction = self._classify_divergence_direction(c_trend, s_trend)
        if direction is None:
            return None

        # ── 4) 幅度门槛（双侧都要够大）──
        if abs(c_delta) < MIN_DIVERGENCE_USD or abs(s_delta) < MIN_DIVERGENCE_USD:
            return None

        # ── 5) 价格 1h 变动 < 阈值（分歧未传导）──
        price_drift = self._compute_1h_price_drift(ctx.state)
        if price_drift is None:
            return None
        if abs(price_drift) > PRICE_DRIFT_MAX_PCT:
            return None

        # ── 6) （可选）附近 KL 催化点 ──
        nearby_kl = self._find_nearby_kl(ctx.state, ref)

        # ── 7) raw_strength ──
        divergence_strength = self._compute_divergence_strength(c_delta, s_delta)
        raw = self._compute_strength(divergence_strength, nearby_kl)

        # ── 8) evidence ──
        c_str_m = abs(c_delta) / 1e6
        s_str_m = abs(s_delta) / 1e6
        scenario = "现强合弱" if direction == "up" else "合强现弱"
        evidence = [
            EvidenceItem(
                dimension="CVD-Divergence",
                observation=(
                    f"{scenario} | spot 1h={s_delta / 1e6:+.1f}M ({s_trend}) "
                    f"vs contract 1h={c_delta / 1e6:+.1f}M ({c_trend})"
                ),
                score_contribution=divergence_strength,
                weight="high",
            ),
            EvidenceItem(
                dimension="PriceQuiet",
                observation=f"近 1h 价格变动 {price_drift:+.2f}%（分歧未传导）",
                score_contribution=1.0 - abs(price_drift) / PRICE_DRIFT_MAX_PCT,
                weight="medium",
            ),
        ]
        if nearby_kl is not None:
            kl_price = float(getattr(nearby_kl, "price", 0.0))
            kl_score = float(getattr(nearby_kl, "final_score", 0.0))
            kl_tier = getattr(nearby_kl, "strength_tier", "")
            evidence.append(EvidenceItem(
                dimension="KeyLevelCatalyst",
                observation=(
                    f"附近 KL @ ${kl_price:.0f}（tier={kl_tier}, score={kl_score:.0f}）"
                    "可作为催化点"
                ),
                score_contribution=kl_score / 100.0,
                weight="medium",
            ))

        return StrategyCandidate(
            direction=direction,
            reference_price=float(ref),
            raw_strength=raw,
            evidence=evidence,
            triggered_conditions=[
                f"divergence={scenario}",
                f"contract_1h={c_str_m:.1f}M",
                f"spot_1h={s_str_m:.1f}M",
                f"price_drift={price_drift:+.2f}%",
                f"has_kl={'yes' if nearby_kl else 'no'}",
            ],
            extra_data={
                "scenario": scenario,
                "contract_delta_1h": c_delta,
                "spot_delta_1h": s_delta,
                "contract_trend_1h": c_trend,
                "spot_trend_1h": s_trend,
                "price_drift_1h_pct": price_drift,
                "divergence_strength": divergence_strength,
                "has_kl_catalyst": nearby_kl is not None,
                "kl_price": float(getattr(nearby_kl, "price", 0.0)) if nearby_kl else None,
            },
        )

    # ── 私有 ──────────────────────────────────────────────────

    @staticmethod
    def _classify_divergence_direction(c_trend: str, s_trend: str) -> Optional[ScalpDirection]:
        """根据 trend_1h 字段判定背离方向

        - spot=rising, contract=declining → up（现强合弱）
        - contract=rising, spot=declining → down（合强现弱）
        - 其他组合 → None（无效背离，包括同向 / flat）
        """
        if s_trend == "rising" and c_trend == "declining":
            return "up"
        if c_trend == "rising" and s_trend == "declining":
            return "down"
        return None

    @staticmethod
    def _compute_1h_price_drift(state: Any) -> Optional[float]:
        """计算最近 1h 价格变动百分比

        用 candles_15m 取最近 4 根（4×15m=1h）的 close 起止变动。
        如样本不足返回 None
        """
        candles = getattr(state, "candles_15m", None) or []
        if len(candles) < 5:
            return None
        # 取倒数 5 根（约 1h 跨度：从 4 根之前的开盘到当前 close）
        # 用 candles[-5].close 作为 1h 前价格，candles[-1].close 作为当前
        try:
            old = float(getattr(candles[-5], "close", 0.0) or 0.0)
            new = float(getattr(candles[-1], "close", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if old <= 0:
            return None
        return (new - old) / old * 100.0

    @staticmethod
    def _find_nearby_kl(state: Any, ref: float) -> Optional[Any]:
        """找 ±0.5% 内 final_score ≥ 50 的最强 KL（可选催化点）"""
        snap = getattr(state, "key_level_snapshot_v2", None)
        if snap is None:
            return None
        levels = getattr(snap, "levels", None) or []
        if not levels or ref <= 0:
            return None
        best: Optional[Any] = None
        best_score = 0.0
        for lv in levels:
            price = float(getattr(lv, "price", 0.0) or 0.0)
            if price <= 0:
                continue
            dist_pct = abs(price - ref) / ref * 100.0
            if dist_pct > KL_NEAR_PCT_MAX:
                continue
            score = float(getattr(lv, "final_score", 0.0) or 0.0)
            if score < KL_FINAL_SCORE_MIN:
                continue
            if score > best_score:
                best_score = score
                best = lv
        return best

    @staticmethod
    def _compute_divergence_strength(c_delta: float, s_delta: float) -> float:
        """背离强度归一 → [0, 1]

        |spot - contract| / max(|spot|, |contract|, 1)
        c_delta、s_delta 方向相反，所以 |c-s| ≈ |c|+|s|
        归一基准用 max 而非 sum，让异常巨大的单边不会人为拉低分数
        """
        diff = abs(c_delta - s_delta)
        denom = max(abs(c_delta), abs(s_delta), 1.0)
        # 当 c=+10M, s=-10M 时 diff=20M, denom=10M → ratio=2.0 → clamp 到 1.0
        # 当 c=+1M, s=-10M 时 diff=11M, denom=10M → ratio=1.1
        # 当 c=+5M, s=-5M 时 diff=10M, denom=5M → ratio=2.0
        ratio = diff / denom
        return float(max(0.0, min(1.0, ratio / 2.0)))  # 归一到 [0, 1]

    @staticmethod
    def _compute_strength(divergence_strength: float, kl: Optional[Any]) -> float:
        """raw_strength = 0.5 + divergence×0.35 + kl×0.15

        - divergence 满分 0.35（背离强度）
        - KL 催化点 0.15（KL final_score / 100 × 0.15）
        - 范围 [0, 1]
        """
        base = 0.5
        d_bonus = divergence_strength * 0.35
        kl_bonus = 0.0
        if kl is not None:
            kl_score = float(getattr(kl, "final_score", 0.0) or 0.0)
            kl_bonus = (kl_score / 100.0) * 0.15
        total = base + d_bonus + kl_bonus
        return float(max(0.0, min(1.0, total)))
