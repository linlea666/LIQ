"""市场状态检测器 · L2 Regime Filter

职责：
  - 基于 AISnapshot + KeyLevelSnapshotV2 + MarketStructure 识别当前市场状态
  - 输出 RegimeSnapshot，供 Synthesizer 做"权重切换"和 SafetyGate 做"极端行情熔断"

六种 Regime 判据（轮次判定，第一个命中即止）：
  1. extreme        — ATR% > EXTREME 阈值（近似 95 分位）
  2. high_vol_chop  — ATR% > HIGH 阈值 且 结构 ranging/transitioning
  3. squeeze        — BBW < TIGHT 阈值 且 结构 ranging
  4. trend_up       — 结构 bullish + confidence ≥ 0.45
  5. trend_down     — 结构 bearish + confidence ≥ 0.45
  6. range          — 其他（默认）

当前版本属"最小可用规则版 v1"：
  - 特征靠 snapshot.atr_14 / boll_* / market_structure.direction 直接计算
  - 不依赖 ADX / PercentileTracker（避免 P0 阶段引入新外部依赖）
  - 保留 RegimeFeatures.adx / atr_pct_percentile 为 0.0 的占位
  - 后续 P1 可接入更精细的 ADX 与历史分位

落实日志锚点：
  - D.D01_REGIME：每次 compute 上报 regime / confidence / switched / pipeline
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from models.key_level import KeyLevelSnapshotV2
from models.market_structure import MarketStructure
from models.regime import RegimeFeatures, RegimeSnapshot
from models.snapshot import AISnapshot

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 阈值（经验校准值；可随回测结果再调整）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ATR% = ATR(14) / price * 100
_ATR_PCT_EXTREME = 6.0   # 4H ATR 超过价格 6% 视为极端
_ATR_PCT_HIGH    = 3.2   # 4H ATR 超过 3.2% 视为高波动
_BBW_TIGHT       = 3.0   # 布林带宽 < 3% 视为蓄力收敛

# 结构方向可信门槛
_STRUCT_TREND_CONF = 0.45

# Action 权重建议表（1.0=中性）
_ACTION_WEIGHT_TABLE: dict[str, dict[str, float]] = {
    "trend_up": {
        "snipe_long": 1.3, "flip_long": 1.0,
        "snipe_short": 0.5, "flip_short": 0.5,
        "scalp_long": 0.9, "scalp_short": 0.7,
    },
    "trend_down": {
        "snipe_short": 1.3, "flip_short": 1.0,
        "snipe_long": 0.5, "flip_long": 0.5,
        "scalp_short": 0.9, "scalp_long": 0.7,
    },
    "range": {
        "flip_long": 1.2, "flip_short": 1.2,
        "snipe_long": 1.0, "snipe_short": 1.0,
        "scalp_long": 1.1, "scalp_short": 1.1,
    },
    "squeeze": {
        "flip_long": 0.8, "flip_short": 0.8,
        "snipe_long": 0.7, "snipe_short": 0.7,
        "scalp_long": 0.6, "scalp_short": 0.6,
    },
    "high_vol_chop": {
        "flip_long": 0.6, "flip_short": 0.6,
        "snipe_long": 0.6, "snipe_short": 0.6,
        "scalp_long": 0.6, "scalp_short": 0.6,
    },
    "extreme": {
        "flip_long": 0.2, "flip_short": 0.2,
        "snipe_long": 0.2, "snipe_short": 0.2,
        "scalp_long": 0.2, "scalp_short": 0.2,
    },
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_regime(
    coin: str,
    snapshot: Optional[AISnapshot],
    kl_snap: Optional[KeyLevelSnapshotV2] = None,
    structure: Optional[MarketStructure] = None,
    prev: Optional[RegimeSnapshot] = None,
) -> RegimeSnapshot:
    """识别当前市场状态，输出 RegimeSnapshot。

    约束：
      - 幂等：相同输入两次调用结果一致
      - 容错：任一子输入缺失，降级到 'range' 并给低 confidence
      - 日志：mark(D01) 上报 regime + 是否切换
    """
    now = int(time.time())

    # 1. 提取原始特征（容错）
    try:
        features = _extract_features(snapshot, kl_snap, structure)
    except Exception as e:  # noqa: BLE001
        logger.debug("[D01] _extract_features failed: %s", e, exc_info=True)
        features = RegimeFeatures()

    # 2. 分类
    try:
        regime_label, confidence, description_cn = _classify(features)
    except Exception as e:  # noqa: BLE001
        logger.debug("[D01] _classify failed: %s", e, exc_info=True)
        regime_label, confidence, description_cn = "range", 0.2, "降级：分类失败默认震荡"

    # 3. 建议权重
    action_weights = _recommend_action_weights(regime_label)

    # 4. 切换追踪
    prev_regime: Optional[str] = None
    regime_changed_at = now
    stable_duration_sec = 0
    if prev is not None:
        prev_regime = prev.regime
        if prev.regime == regime_label and prev.regime_changed_at > 0:
            regime_changed_at = prev.regime_changed_at
            stable_duration_sec = max(0, now - regime_changed_at)
        else:
            regime_changed_at = now
            stable_duration_sec = 0

    snap = RegimeSnapshot(
        coin=coin,
        ts=now,
        regime=regime_label,  # type: ignore[arg-type]
        confidence=round(confidence, 3),
        features=features,
        description_cn=description_cn,
        action_weights=action_weights,
        prev_regime=prev_regime,  # type: ignore[arg-type]
        regime_changed_at=regime_changed_at,
        stable_duration_sec=stable_duration_sec,
    )

    # PR-3 · D01 decision_tracker 已下线
    return snap


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_regime_from_state(
    coin: str,
    price: float,
    atr_14: float,
    boll_data: Optional[dict] = None,
    structure: Optional[MarketStructure] = None,
    hist_vol_pct: Optional[float] = None,
    ema20: Optional[float] = None,
    prev: Optional[RegimeSnapshot] = None,
) -> RegimeSnapshot:
    """engine._recompute 使用的薄入口（避免构造完整 AISnapshot）。

    只取 regime 真正需要的少数字段，保持 _recompute 轻量。
    """
    now = int(time.time())
    feat = RegimeFeatures()

    if price > 0 and atr_14 > 0:
        feat.atr_pct = (atr_14 / price) * 100.0

    if boll_data:
        try:
            upper = boll_data.get("upper")
            middle = boll_data.get("middle")
            lower = boll_data.get("lower")
            if upper is not None and middle and lower is not None and float(middle) > 0:
                feat.bbw = (float(upper) - float(lower)) / float(middle) * 100.0
        except Exception:  # noqa: BLE001
            pass

    if hist_vol_pct is not None:
        feat.hist_vol_pct = float(hist_vol_pct)

    if structure is not None:
        feat.structure_alignment = structure.direction

    if ema20 and price:
        try:
            feat.trend_slope_pct = (price - ema20) / ema20 * 100.0
        except Exception:  # noqa: BLE001
            pass

    # 分类
    try:
        regime_label, confidence, description_cn = _classify(feat)
    except Exception as e:  # noqa: BLE001
        logger.debug("[D01] _classify(from_state) failed: %s", e, exc_info=True)
        regime_label, confidence, description_cn = "range", 0.2, "降级：分类失败"

    action_weights = _recommend_action_weights(regime_label)

    prev_regime: Optional[str] = None
    regime_changed_at = now
    stable_duration_sec = 0
    if prev is not None:
        prev_regime = prev.regime
        if prev.regime == regime_label and prev.regime_changed_at > 0:
            regime_changed_at = prev.regime_changed_at
            stable_duration_sec = max(0, now - regime_changed_at)

    snap = RegimeSnapshot(
        coin=coin,
        ts=now,
        regime=regime_label,  # type: ignore[arg-type]
        confidence=round(confidence, 3),
        features=feat,
        description_cn=description_cn,
        action_weights=action_weights,
        prev_regime=prev_regime,  # type: ignore[arg-type]
        regime_changed_at=regime_changed_at,
        stable_duration_sec=stable_duration_sec,
    )

    # PR-3 · D01 decision_tracker 已下线
    return snap


def _extract_features(
    snapshot: Optional[AISnapshot],
    kl_snap: Optional[KeyLevelSnapshotV2],
    structure: Optional[MarketStructure],
) -> RegimeFeatures:
    """提取 RegimeFeatures 原始特征。

    snapshot=None 时返回全零 features（调用方会降级到 range）。
    """
    feat = RegimeFeatures()

    if snapshot is not None:
        price = float(snapshot.price or 0.0)
        atr = float(snapshot.atr_14 or 0.0)
        if price > 0 and atr > 0:
            feat.atr_pct = (atr / price) * 100.0

        # 布林带宽（仅当三条线齐备）
        upper = snapshot.boll_upper
        middle = snapshot.boll_middle
        lower = snapshot.boll_lower
        if upper is not None and middle and lower is not None and middle > 0:
            feat.bbw = ((upper - lower) / middle) * 100.0

        # 历史波动率
        if snapshot.btc_hist_vol is not None:
            feat.hist_vol_pct = float(snapshot.btc_hist_vol)

        # CVD 方向持续性：优先读 MAA facts（PR-5 AISnapshot 已移除顶层 cvd_*）
        fcc = getattr(snapshot, "facts_cvd_contract", None)
        if fcc is not None:
            trend = (getattr(fcc, "trend_1h", "") or "").lower()
            delta = float(getattr(fcc, "delta_1h", None) or 0)
            if trend in ("bullish", "up", "long"):
                feat.cvd_persistence = 1.0 if delta > 0 else 0.5
            elif trend in ("bearish", "down", "short"):
                feat.cvd_persistence = 1.0 if delta < 0 else 0.5
            else:
                feat.cvd_persistence = 0.3

        # 24h 爆仓 vs 7d 均值
        liq_24h = (snapshot.global_liq_long_24h or 0) + (snapshot.global_liq_short_24h or 0)
        if liq_24h > 0:
            # 没有 7d 均值接口时用保守近似：> 1e9 USD 视为放量
            feat.liq_24h_vs_7d_avg = 1.0 + min(liq_24h / 1e9, 5.0)

    # 结构对齐（仅显式 structure；AISnapshot 不再带 market_structure dict）
    if structure is not None:
        feat.structure_alignment = structure.direction

    # EMA 斜率（粗估：price vs ema20）
    if snapshot is not None and snapshot.ema20 and snapshot.price:
        try:
            feat.trend_slope_pct = (
                (snapshot.price - snapshot.ema20) / snapshot.ema20 * 100.0
            )
        except Exception:  # noqa: BLE001
            pass

    return feat


def _classify(features: RegimeFeatures) -> tuple[str, float, str]:
    """特征 → (regime_label, confidence, description_cn)。

    判定顺序（第一个命中即止）：
      1. extreme (ATR% > EXTREME)
      2. high_vol_chop (ATR% > HIGH 且 结构 ranging/transitioning)
      3. squeeze (BBW < TIGHT 且 结构 ranging)
      4. trend_up (结构 bullish，ATR% 非极端)
      5. trend_down (结构 bearish，ATR% 非极端)
      6. range (默认)
    """
    atr_pct = features.atr_pct
    bbw = features.bbw
    align = (features.structure_alignment or "").lower()

    # 1. extreme
    if atr_pct > 0 and atr_pct >= _ATR_PCT_EXTREME:
        conf = min(1.0, 0.6 + (atr_pct - _ATR_PCT_EXTREME) * 0.05)
        return (
            "extreme",
            round(conf, 3),
            f"极端波动：ATR 已达价格 {atr_pct:.2f}%（阈值 {_ATR_PCT_EXTREME}%），建议熔断或大幅减仓",
        )

    # 2. high_vol_chop
    if atr_pct > 0 and atr_pct >= _ATR_PCT_HIGH and align in ("ranging", "transitioning", ""):
        return (
            "high_vol_chop",
            0.6,
            f"高波动无序：ATR {atr_pct:.2f}%（>{_ATR_PCT_HIGH}%）且结构无方向 [{align or 'unknown'}]",
        )

    # 3. squeeze
    if bbw > 0 and bbw <= _BBW_TIGHT and align in ("ranging", "transitioning", ""):
        return (
            "squeeze",
            0.6,
            f"蓄力收敛：布林带宽仅 {bbw:.2f}%（<{_BBW_TIGHT}%），等待方向选择",
        )

    # 4/5. trend_up / trend_down（需要结构方向）
    if align == "bullish":
        conf = 0.6 if atr_pct < _ATR_PCT_HIGH else 0.45
        return (
            "trend_up",
            conf,
            f"上升趋势：结构 bullish，ATR {atr_pct:.2f}% 正常",
        )
    if align == "bearish":
        conf = 0.6 if atr_pct < _ATR_PCT_HIGH else 0.45
        return (
            "trend_down",
            conf,
            f"下降趋势：结构 bearish，ATR {atr_pct:.2f}% 正常",
        )

    # 6. range（兜底）
    if atr_pct <= 0 and bbw <= 0:
        return ("range", 0.2, "降级：缺少 ATR/BBW 数据，默认震荡")
    return (
        "range",
        0.4,
        f"箱体震荡：ATR {atr_pct:.2f}% · BBW {bbw:.2f}% · 结构 {align or 'unknown'}",
    )


def _recommend_action_weights(regime: str) -> dict[str, float]:
    """按 regime 返回各 action 的权重乘数（1.0=中性）"""
    return dict(_ACTION_WEIGHT_TABLE.get(regime, {}))
