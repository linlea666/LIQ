"""规则引擎 8 维方向共识聚合器（DirectionVote aggregator）

输入：CoinState（已就绪的各维度数据）
输出：DirectionVoteSummary（供 AISnapshot / Prompt §9k 直接消费）

设计约束
--------
- 纯读，不修改任何 state 字段；无外部 I/O。
- 所有维度均容忍"数据缺失"（missing_inputs 记录），不会因一两维 None 抛错。
- 权重固定（default_weights），后续可通过 config 覆盖；本轮不做动态权重以保守。
- 输出的 direction / strength / note 应当是"AI 一眼能读懂的短语"，避免堆砌数字。

8 维来源
---------
1. structure   ── 1h market_structure.direction  (BOS/CHoCH 结构方向)
2. mtf_align   ── 1w / 1d / 1h 三周期结构一致性
3. momentum    ── MACD 柱 + 零轴 + RSI14  (从 CoinState 的 macd_data / rsi_14)
4. range       ── range_signal.breakout_direction_bias + box_state
5. key_level   ── key_level_snapshot_v2.signals (snipe_long vs snipe_short)
6. flow        ── cvd_contract + taker_flow
7. positioning ── OI 变化 + funding + net_position
8. exhaustion  ── trend_exhaustion.overall_state × overall_direction
"""

from __future__ import annotations

import time
from typing import Optional

from models.direction_vote import (
    ConsensusLevel,
    DirectionVote,
    DirectionVoteSummary,
    VoteDirection,
)


# ── 默认权重（Σ=1.0；按维度"独立性"与"历史稳定性"经验分配）─────
DEFAULT_WEIGHTS: dict[str, float] = {
    "structure":   0.15,
    "mtf_align":   0.18,
    "momentum":    0.12,
    "range":       0.10,
    "key_level":   0.12,
    "flow":        0.13,
    "positioning": 0.10,
    "exhaustion":  0.10,
}

_NAME_CN = {
    "structure":   "1h 结构方向",
    "mtf_align":   "MTF 多周期一致性",
    "momentum":    "MACD/RSI 动能",
    "range":       "箱体突破倾向",
    "key_level":   "关键位信号倾向",
    "flow":        "主动资金流",
    "positioning": "仓位与情绪",
    "exhaustion":  "动能衰竭",
}


# ──────────────────────────────────────────────────────────────
# 单维投票器（每个函数都必须返回 DirectionVote；数据不足时 neutral+note）
# ──────────────────────────────────────────────────────────────

def _vote_structure(state) -> DirectionVote:
    ms = getattr(state, "market_structure", None)
    if ms is None:
        return _vote_missing("structure")
    d = (ms.direction or "").lower()
    conf = float(getattr(ms, "confidence", 0.0) or 0.0)
    if d == "bullish":
        return _mk("structure", "bullish", max(0.3, min(1.0, conf)),
                   f"1h 结构 BOS/CHoCH 偏多，置信 {conf:.2f}")
    if d == "bearish":
        return _mk("structure", "bearish", max(0.3, min(1.0, conf)),
                   f"1h 结构偏空，置信 {conf:.2f}")
    if d == "transitioning":
        return _mk("structure", "neutral", 0.2, "1h 结构过渡中")
    return _mk("structure", "neutral", 0.1, "1h 结构震荡 / 不明")


def _vote_mtf_align(state) -> DirectionVote:
    """P0.4 · MTF 一致性归一化
    旧版 bug：当 1w/1d 缺失、只剩 1h 一个 TF 时，仍走 `bulls == total` 分支
    返回 strength 0.9（和 3/3 同向权重相同），把"MTF"退化成单 TF 重复计数。
    新版：按可用 TF 数量分档打强度，单 TF 硬顶 0.3 并在 note 标注数据不全。
    - total==3：2/3 同向 → 0.55，3/3 → 0.9（保留）
    - total==2：1/2 同向 → 0.25，2/2 → 0.6
    - total==1：无论哪个方向 → ≤0.3（MTF 本应多周期共振，单 TF 不算"共振"）
    - total==0：missing
    """
    tfs = []
    for attr, tag in (("market_structure_1w", "1w"),
                      ("market_structure_1d", "1d"),
                      ("market_structure", "1h")):
        ms = getattr(state, attr, None)
        if ms is None:
            continue
        d = (ms.direction or "").lower()
        if d in ("bullish", "bearish"):
            tfs.append((tag, d))
    if not tfs:
        return _vote_missing("mtf_align")
    bulls = sum(1 for _, d in tfs if d == "bullish")
    bears = sum(1 for _, d in tfs if d == "bearish")
    total = len(tfs)
    joined = "+".join(t for t, _ in tfs)

    if total == 1:
        tag, d = tfs[0]
        # 单 TF 不算 MTF 共振，硬顶 0.3 并标注数据不全
        return _mk(
            "mtf_align", d, 0.3,
            f"仅 {tag} 一个周期 {'多' if d == 'bullish' else '空'}（1w/1d 数据不全，MTF 共振无法判定，权重折半）"
        )

    if total == 2:
        if bulls == 2:
            return _mk("mtf_align", "bullish", 0.6, f"2/2 周期同向做多（{joined}，1 周期数据不全）")
        if bears == 2:
            return _mk("mtf_align", "bearish", 0.6, f"2/2 周期同向做空（{joined}，1 周期数据不全）")
        # 1 多 1 空
        return _mk("mtf_align", "neutral", 0.1, f"MTF 分歧（多{bulls}/空{bears}，{joined}）")

    # total == 3
    if bulls == 3:
        return _mk("mtf_align", "bullish", 0.9, f"3/3 周期同向做多（{joined}）")
    if bears == 3:
        return _mk("mtf_align", "bearish", 0.9, f"3/3 周期同向做空（{joined}）")
    if bulls >= 2 and bears == 0:
        return _mk("mtf_align", "bullish", 0.55, f"{bulls}/3 周期偏多 · 其余中性")
    if bears >= 2 and bulls == 0:
        return _mk("mtf_align", "bearish", 0.55, f"{bears}/3 周期偏空 · 其余中性")
    return _mk("mtf_align", "neutral", 0.1, f"MTF 分歧（多{bulls}/空{bears}）")


def _vote_momentum(state) -> DirectionVote:
    rsi = getattr(state, "rsi_14", None)
    macd = getattr(state, "macd_data", None) or {}
    hist = macd.get("histogram")
    above_zero = macd.get("above_zero")
    if rsi is None and hist is None and above_zero is None:
        return _vote_missing("momentum")
    # 计分：MACD 零轴 ±0.4；柱方向 ±0.3；RSI 区间 ±0.3
    score = 0.0
    parts: list[str] = []
    if above_zero is True:
        score += 0.4
        parts.append("MACD 在零轴上")
    elif above_zero is False:
        score -= 0.4
        parts.append("MACD 在零轴下")
    if hist is not None:
        if hist > 0:
            score += 0.3
            parts.append(f"柱{hist:+.3g}")
        elif hist < 0:
            score -= 0.3
            parts.append(f"柱{hist:+.3g}")
    if rsi is not None:
        if rsi >= 55:
            score += 0.3
            parts.append(f"RSI {rsi:.0f}")
        elif rsi <= 45:
            score -= 0.3
            parts.append(f"RSI {rsi:.0f}")
        else:
            parts.append(f"RSI {rsi:.0f}(中)")
    direction: VoteDirection = (
        "bullish" if score >= 0.25 else "bearish" if score <= -0.25 else "neutral"
    )
    return _mk("momentum", direction, min(1.0, abs(score)), " · ".join(parts) or "动能中性")


def _vote_range(state) -> DirectionVote:
    rs = getattr(state, "range_signal", None)
    if rs is None:
        return _vote_missing("range")
    bias = (getattr(rs, "breakout_direction_bias", "") or "").lower()
    prob = float(getattr(rs, "breakout_probability", 0.0) or 0.0)
    box_state = getattr(rs, "box_state", "") or ""
    position = getattr(rs, "price_position", "") or ""
    # 突破方向倾向作主信号（带概率强度），无倾向时退化到价格位置 + box_state
    if bias == "up" and prob > 0:
        return _mk("range", "bullish", max(0.3, min(1.0, prob)),
                   f"箱体偏向上破（P={prob:.2f}, state={box_state or 'n/a'}）")
    if bias == "down" and prob > 0:
        return _mk("range", "bearish", max(0.3, min(1.0, prob)),
                   f"箱体偏向下破（P={prob:.2f}, state={box_state or 'n/a'}）")
    if box_state == "breaking_up":
        return _mk("range", "bullish", 0.45, "箱体正在向上破位")
    if box_state == "breaking_down":
        return _mk("range", "bearish", 0.45, "箱体正在向下破位")
    if position == "near_upper":
        return _mk("range", "neutral", 0.2, "价格近上沿，尚未选择方向")
    if position == "near_lower":
        return _mk("range", "neutral", 0.2, "价格近下沿，尚未选择方向")
    return _mk("range", "neutral", 0.1, f"箱体中性（state={box_state or 'n/a'}）")


def _vote_key_level(state) -> DirectionVote:
    snap = getattr(state, "key_level_snapshot_v2", None)
    if snap is None or not getattr(snap, "signals", None):
        return _vote_missing("key_level")
    longs = 0
    shorts = 0
    long_score = 0.0
    short_score = 0.0
    for sig in snap.signals:
        action = (sig.action or "").lower()
        s = int(getattr(sig, "score", 0) or 0)
        if action in ("snipe_long", "flip_long"):
            longs += 1
            long_score += max(40, s)
        elif action in ("snipe_short", "flip_short"):
            shorts += 1
            short_score += max(40, s)
    if longs == 0 and shorts == 0:
        return _mk("key_level", "neutral", 0.1, "关键位信号多为 wait_*，方向未定")
    if longs > 0 and shorts == 0:
        strength = min(1.0, long_score / 200.0)
        return _mk("key_level", "bullish", max(0.3, strength),
                   f"关键位信号 {longs} 条偏多（合计分 {long_score:.0f}）")
    if shorts > 0 and longs == 0:
        strength = min(1.0, short_score / 200.0)
        return _mk("key_level", "bearish", max(0.3, strength),
                   f"关键位信号 {shorts} 条偏空（合计分 {short_score:.0f}）")
    # 多空同现 → 取分数差
    diff = long_score - short_score
    if abs(diff) < 30:
        return _mk("key_level", "neutral", 0.15,
                   f"关键位多空并存（多{longs}/空{shorts}，分差{diff:+.0f}）")
    if diff > 0:
        return _mk("key_level", "bullish", min(1.0, diff / 200.0),
                   f"关键位偏多（多{longs}/空{shorts}，分差{diff:+.0f}）")
    return _mk("key_level", "bearish", min(1.0, -diff / 200.0),
               f"关键位偏空（多{longs}/空{shorts}，分差{diff:+.0f}）")


def _vote_flow(state) -> DirectionVote:
    cvd = getattr(state, "cvd_contract", None)
    taker = getattr(state, "taker_flow", None)
    if cvd is None and taker is None:
        return _vote_missing("flow")
    score = 0.0
    parts: list[str] = []
    if cvd is not None:
        trend = (getattr(cvd, "trend_1h", "") or "").lower()
        delta = float(getattr(cvd, "delta_1h", 0.0) or 0.0)
        if trend in ("rising", "bullish", "up"):
            score += 0.5
            parts.append(f"合约 CVD↑({delta:+.2g})")
        elif trend in ("falling", "bearish", "down"):
            score -= 0.5
            parts.append(f"合约 CVD↓({delta:+.2g})")
        else:
            parts.append(f"CVD 中性({delta:+.2g})")
    if taker is not None:
        br = getattr(taker, "buy_ratio", None)
        if br is not None:
            if br >= 0.55:
                score += 0.4
                parts.append(f"Taker 买 {br:.2f}")
            elif br <= 0.45:
                score -= 0.4
                parts.append(f"Taker 卖 {br:.2f}")
            else:
                parts.append(f"Taker 均衡 {br:.2f}")
    direction: VoteDirection = (
        "bullish" if score >= 0.3 else "bearish" if score <= -0.3 else "neutral"
    )
    return _mk("flow", direction, min(1.0, abs(score)),
               " · ".join(parts) or "资金流无明显倾向")


def _vote_positioning(state) -> DirectionVote:
    """综合 OI 变化 + funding + 净持仓趋势。

    注意：LS 比率不做"越高越多"的简单映射——过度偏多往往是反向信号——
    只在"funding 极端"时作为额外反向修正项（防踩踏）。
    """
    oi_chg = getattr(state, "oi_change_24h_pct", None)
    funding = getattr(state, "funding", None)
    fr = None
    if funding is not None:
        fr = getattr(funding, "binance_rate", None) or getattr(funding, "okx_rate", None)
    np_trend = (getattr(state, "net_position_trend", "") or "").lower()

    if oi_chg is None and fr is None and not np_trend:
        return _vote_missing("positioning")

    score = 0.0
    parts: list[str] = []
    # OI 变化（仅提供"加码强度"，方向需结合价格 → 用 market_structure.direction 近似）
    ms = getattr(state, "market_structure", None)
    price_dir = (ms.direction if ms else "")
    if oi_chg is not None and abs(oi_chg) >= 0.5:
        if price_dir == "bullish" and oi_chg > 0:
            score += 0.3
            parts.append(f"OI+{oi_chg:.1f}% 顺势加码")
        elif price_dir == "bearish" and oi_chg > 0:
            score -= 0.3
            parts.append(f"OI+{oi_chg:.1f}% 空头加仓")
        elif oi_chg < 0:
            parts.append(f"OI{oi_chg:.1f}%（减仓）")
    # funding 极端反向（≥0.05% 倾向回抽 / ≤-0.03% 倾向反弹）
    if fr is not None:
        if fr >= 0.0005:
            score -= 0.25
            parts.append(f"资金费 {fr*100:.3f}% 过热")
        elif fr <= -0.0003:
            score += 0.25
            parts.append(f"资金费 {fr*100:.3f}% 过冷")
        else:
            parts.append(f"资金费 {fr*100:.3f}%（正常）")
    # 净持仓（博主口径："机构净持仓"）
    if np_trend:
        if "increas" in np_trend or "up" in np_trend or "上" in np_trend:
            score += 0.25
            parts.append(f"净持仓 {np_trend}")
        elif "decreas" in np_trend or "down" in np_trend or "下" in np_trend:
            score -= 0.25
            parts.append(f"净持仓 {np_trend}")

    direction: VoteDirection = (
        "bullish" if score >= 0.25 else "bearish" if score <= -0.25 else "neutral"
    )
    return _mk("positioning", direction, min(1.0, abs(score)),
               " · ".join(parts) or "仓位与情绪中性")


def _vote_exhaustion(state) -> DirectionVote:
    te = getattr(state, "trend_exhaustion", None)
    if te is None:
        return _vote_missing("exhaustion")
    # regime 否决：震荡/极端 → 不给方向票
    if getattr(te, "regime_vetoed", False):
        return _mk("exhaustion", "neutral", 0.1, "regime 否决（震荡/极端）")
    state_label = (getattr(te, "overall_state", "") or "").lower()
    overall_dir = (getattr(te, "overall_direction", "") or "").lower()
    consensus = (getattr(te, "consensus_level", "") or "").lower()
    # 续航：同向加权；衰竭：反向加权
    strength = 0.9 if consensus == "strong_agree" else 0.6 if consensus == "partial" else 0.3
    if state_label == "healthy_continuation":
        if overall_dir == "up":
            return _mk("exhaustion", "bullish", strength, "续航健康（MTF 同向做多）")
        if overall_dir == "down":
            return _mk("exhaustion", "bearish", strength, "续航健康（MTF 同向做空）")
    if state_label in ("momentum_fading", "exhaustion_warn"):
        # 动能衰减 → 当前方向反向票
        if overall_dir == "up":
            return _mk("exhaustion", "bearish", strength, f"多头动能{state_label}")
        if overall_dir == "down":
            return _mk("exhaustion", "bullish", strength, f"空头动能{state_label}")
    if state_label == "structural_reversal":
        # 结构反转：给反向大票
        if overall_dir == "up":
            return _mk("exhaustion", "bearish", 0.9, "结构反转（顶部确立）")
        if overall_dir == "down":
            return _mk("exhaustion", "bullish", 0.9, "结构反转（底部确立）")
    return _mk("exhaustion", "neutral", 0.15, f"动能 {state_label or 'neutral'} · 样本不足")


# ──────────────────────────────────────────────────────────────
# 聚合
# ──────────────────────────────────────────────────────────────

def compute_direction_vote(
    state,
    weights: Optional[dict[str, float]] = None,
) -> DirectionVoteSummary:
    """主入口。state 应是 engine.CoinState 或兼容的对象（duck-typed）。"""

    w = dict(DEFAULT_WEIGHTS)
    if weights:
        for k, v in weights.items():
            if k in w and isinstance(v, (int, float)) and v >= 0:
                w[k] = float(v)

    voters = [
        _vote_structure,
        _vote_mtf_align,
        _vote_momentum,
        _vote_range,
        _vote_key_level,
        _vote_flow,
        _vote_positioning,
        _vote_exhaustion,
    ]

    votes: list[DirectionVote] = []
    active_keys: list[str] = []
    missing: list[str] = []
    for fn in voters:
        vote = fn(state)
        vote.weight = w.get(vote.key, 0.0)
        votes.append(vote)
        if vote.direction == "neutral" and vote.strength <= 0.0:
            missing.append(vote.key)
        else:
            active_keys.append(vote.key)

    # ── 加权聚合（权重按"参与投票的非缺失维度"重新归一化，避免缺失维度稀释分值）──
    effective_weight_sum = sum(
        vote.weight for vote in votes if vote.key in active_keys
    ) or 1e-9
    weighted_score = 0.0
    for vote in votes:
        if vote.key not in active_keys:
            continue
        sign = 1.0 if vote.direction == "bullish" else -1.0 if vote.direction == "bearish" else 0.0
        weighted_score += sign * vote.strength * (vote.weight / effective_weight_sum)
    # clamp
    weighted_score = max(-1.0, min(1.0, weighted_score))

    bulls = sum(1 for v in votes if v.direction == "bullish")
    bears = sum(1 for v in votes if v.direction == "bearish")
    neutrals = sum(1 for v in votes if v.direction == "neutral")

    # 主导方向
    if weighted_score >= 0.15 and bulls > bears:
        dominant: VoteDirection = "bullish"
    elif weighted_score <= -0.15 and bears > bulls:
        dominant = "bearish"
    else:
        dominant = "neutral"

    # 共识级别
    consensus: ConsensusLevel = _classify_consensus(
        bulls=bulls, bears=bears, score=weighted_score
    )

    # 贡献最大的 2 维
    def _key_contribution(v: DirectionVote) -> float:
        sign = 1.0 if v.direction == "bullish" else -1.0 if v.direction == "bearish" else 0.0
        return sign * v.strength * v.weight

    sorted_by_contrib = sorted(votes, key=_key_contribution, reverse=True)
    top_bullish = [v.key for v in sorted_by_contrib if v.direction == "bullish"][:2]
    top_bearish = [v.key for v in sorted(
        votes, key=_key_contribution
    ) if v.direction == "bearish"][:2]

    summary_cn = _build_summary_cn(
        dominant=dominant, consensus=consensus,
        bulls=bulls, bears=bears, neutrals=neutrals,
        score=weighted_score,
    )

    return DirectionVoteSummary(
        coin=getattr(state, "coin", "") or "",
        ts=int(time.time()),
        votes=votes,
        bullish_votes=bulls,
        bearish_votes=bears,
        neutral_votes=neutrals,
        weighted_score=round(weighted_score, 4),
        dominant_direction=dominant,
        consensus_level=consensus,
        top_bullish=top_bullish,
        top_bearish=top_bearish,
        active_dimensions=len(active_keys),
        missing_dimensions=missing,
        summary_cn=summary_cn,
    )


# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────

def _mk(key: str, direction: VoteDirection, strength: float, note: str) -> DirectionVote:
    return DirectionVote(
        key=key,
        name_cn=_NAME_CN.get(key, key),
        direction=direction,
        strength=max(0.0, min(1.0, strength)),
        weight=DEFAULT_WEIGHTS.get(key, 0.0),
        note=note,
    )


def _vote_missing(key: str) -> DirectionVote:
    return DirectionVote(
        key=key,
        name_cn=_NAME_CN.get(key, key),
        direction="neutral",
        strength=0.0,
        weight=DEFAULT_WEIGHTS.get(key, 0.0),
        note="数据缺失，弃权",
    )


def _classify_consensus(*, bulls: int, bears: int, score: float) -> ConsensusLevel:
    same_side = max(bulls, bears)
    if bulls >= 2 and bears >= 2:
        return "conflict"
    if same_side >= 5 and abs(score) >= 0.5:
        return "strong_agree"
    if same_side >= 3 and abs(score) >= 0.25:
        return "partial"
    return "low_signal"


def _build_summary_cn(
    *, dominant: VoteDirection, consensus: ConsensusLevel,
    bulls: int, bears: int, neutrals: int, score: float,
) -> str:
    dir_cn = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}[dominant]
    con_cn = {
        "strong_agree": "强共识",
        "partial": "部分共识",
        "conflict": "多空分歧",
        "low_signal": "信号弱",
    }[consensus]
    return (
        f"规则 8 维：{bulls}多 / {bears}空 / {neutrals}中 · "
        f"加权 {score:+.2f} · {dir_cn}（{con_cn}）"
    )
