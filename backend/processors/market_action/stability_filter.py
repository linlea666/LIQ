"""MAA 状态机滤波 · Dead Zone + Persistence + Hysteresis + Fast Track

为什么需要：
  AI 在每轮（10 min）独立给出 scenario / bias，震荡市里底层指标只在噪声级波动，
  AI 仍可能在 bull / bear 之间反复换边。本模块对 AI 原始输出做"防抖 + 滞回"裁决，
  让前端拿到的方向稳定（吃鱼身），同时保留 ai_raw_* 在 stability 字段里供复盘。

四层规则（执行顺序）：
  1. 同 scenario        → 直接采纳（细节调整 / confidence / phase 都允许变化）
  2. data_quality 过差  → 维持上轮 accepted（fallback_skipped）
  3. Dead Zone（凌驾）  → 底层 OI 1h / funding / basis 三项全在 dead zone → 强制维持
  4. Fast Track         → confidence ≥ 85 + 已成交硬动作（liq sweep / footprint / absorption）
                          且方向与新场景一致 → 越过 persistence/hysteresis 立即切
  5. 同大类切换         → Persistence：本轮记 pending、accepted 维持；下一轮 AI 仍同向才切
  6. 跨大类反转         → Hysteresis：confidence ≥ 70 + ≥ 3 条 main+high + 旧主证据被反证
                          → 通过；否则维持 + pending

九场景按"交易方向"映射到 3 大类（bull / bear / neutral）；
持续窗口 = 1 轮 pending + 1 轮确认 = 20 min（10 min × 2 轮）。

阈值定义见 `_DEAD_ZONE` / `apply_stability_filter` 文档；调参时务必同步更新
DEVELOPMENT.md 中的"MAA 稳定性规则"段落。
"""

from __future__ import annotations

import logging
from typing import Optional

from models.market_action import (
    MarketActionFacts,
    MarketActionReport,
    StabilityVerdict,
)

logger = logging.getLogger(__name__)


# ── 9 场景 → 3 大类（按"交易方向"分组，不是按"成因"分组）──
_CATEGORY: dict[str, str] = {
    # bull（做多大类）
    "trend_continuation_up": "bull",
    "short_squeeze_up": "bull",
    "fake_breakdown_down": "bull",   # 假跌破回收 = 做多
    "exhaustion_bottom": "bull",     # 底部衰竭 = 做多
    # bear（做空大类）
    "trend_continuation_down": "bear",
    "long_squeeze_down": "bear",
    "fake_breakout_up": "bear",      # 假突破回落 = 做空
    "exhaustion_top": "bear",        # 顶部衰竭 = 做空
    # neutral（等待）
    "range_bound": "neutral",
}


# ── Dead Zone 阈值（按币种价格档位放缩）──
# BTC / ETH 同档（流动性同档）；SOL ×1.3（波动天然大）
_DEAD_ZONE: dict[str, dict[str, float]] = {
    "default": {"oi_1h_pct": 0.5, "funding": 0.0001, "basis_pct": 0.1},
    "BTC":     {"oi_1h_pct": 0.5, "funding": 0.0001, "basis_pct": 0.1},
    "ETH":     {"oi_1h_pct": 0.5, "funding": 0.0001, "basis_pct": 0.1},
    "SOL":     {"oi_1h_pct": 0.65, "funding": 0.00013, "basis_pct": 0.13},
}

# Fast Track 阈值
_FAST_TRACK_CONFIDENCE = 85
_FAST_TRACK_FP_RATIO = 5.0   # footprint stacked imbalance 的 ratio 门槛
_FAST_TRACK_ABS_BARS = 2     # absorption zone bar_count 门槛

# Hysteresis 阈值
_HYSTERESIS_CONFIDENCE = 70
_HYSTERESIS_MAIN_HIGH = 3    # main+high evidence 至少几条


# ────────────────────────────────────────────────────────────────────────────
# 入口
# ────────────────────────────────────────────────────────────────────────────


def apply_stability_filter(
    *,
    facts: MarketActionFacts,
    ai_raw_report: MarketActionReport,
    previous_report: Optional[MarketActionReport],
) -> StabilityVerdict:
    """计算稳定性裁决。

    本函数是**纯函数**——只读 facts / ai_raw_report / previous_report，不修改它们。
    调用方（market_action_arbiter）拿到 StabilityVerdict 后自行决定如何应用：
    建议 accepted_scenario != ai_raw 时，把对外的 scenario / market_phase /
    trading_implications 替换为上一份 accepted 报告的对应字段，evidence /
    analyst_reasoning 仍保留 AI 本轮输出。

    Args:
        facts: 本轮 MarketActionFacts（用来读 OI/funding/basis 等数值判断 dead zone，
               liq_sweep / footprint / absorption 判断 fast track）
        ai_raw_report: AI 当前轮原始输出（已通过 _payload_to_report 解析）
        previous_report: 上一份 *已存档* 报告（state.market_action_report）；
                         若它带 stability 则以 stability.accepted_scenario 为基准，
                         否则以 previous_report.scenario 为基准（向后兼容旧历史）。

    Returns:
        StabilityVerdict：包含 accepted/raw 双轨字段 + 滤波解释。
    """
    ai_scenario = ai_raw_report.scenario
    ai_bias = ai_raw_report.trading_implications.bias
    now_ts = int(ai_raw_report.timestamp)

    # ── 首次运行：无前情可对比，直接采纳 ──
    if previous_report is None:
        return _verdict(
            ai_raw_report,
            accepted_scenario=ai_scenario,
            accepted_bias=ai_bias,
            previous_accepted_scenario=None,
            stable_for_runs=1,
            last_change_ts=now_ts,
            override_reason="",
            allow_switch=True,
            pending_switch_to=None,
            pending_runs=0,
            notes="首次运行，无历史可对比，直接采纳。",
        )

    prev_state = _extract_prev_state(previous_report)
    prev_accepted = prev_state["accepted_scenario"]
    prev_bias = prev_state["accepted_bias"]
    prev_pending = prev_state["pending_switch_to"]
    prev_pending_runs = prev_state["pending_runs"]
    prev_stable_for = prev_state["stable_for_runs"]
    prev_last_change = prev_state["last_change_ts"]

    # ── 1. 同 scenario → 直接放行 ──
    if ai_scenario == prev_accepted:
        return _verdict(
            ai_raw_report,
            accepted_scenario=ai_scenario,
            accepted_bias=ai_bias,
            previous_accepted_scenario=prev_accepted,
            stable_for_runs=prev_stable_for + 1,
            last_change_ts=prev_last_change or now_ts,
            override_reason="",
            allow_switch=True,
            pending_switch_to=None,
            pending_runs=0,
            notes=f"主场景与上轮一致（{ai_scenario}），直接采纳细节修正。",
        )

    # ── 2. 数据质量过差 → 维持上轮（避免在 insufficient 下乱切）──
    if facts.data_quality == "insufficient":
        return _verdict(
            ai_raw_report,
            accepted_scenario=prev_accepted,
            accepted_bias=prev_bias,
            previous_accepted_scenario=prev_accepted,
            stable_for_runs=prev_stable_for + 1,
            last_change_ts=prev_last_change,
            override_reason="fallback_skipped",
            allow_switch=False,
            pending_switch_to=None,
            pending_runs=0,
            notes="data_quality=insufficient，本轮跳过滤波并保留上轮 accepted。",
        )

    # ── 3. Dead Zone（最严，凌驾 fast track）──
    dz = _check_dead_zone(facts)
    if dz["in_dead_zone"]:
        return _verdict(
            ai_raw_report,
            accepted_scenario=prev_accepted,
            accepted_bias=prev_bias,
            previous_accepted_scenario=prev_accepted,
            stable_for_runs=prev_stable_for + 1,
            last_change_ts=prev_last_change,
            override_reason="dead_zone",
            allow_switch=False,
            pending_switch_to=None,  # 底层根本没动 → pending 也清
            pending_runs=0,
            notes=f"底层指标全部在 dead zone：{dz['detail']}；AI 想切到 {ai_scenario} 但被压住。",
        )

    # ── 4. Fast Track（强信号越过 persistence/hysteresis）──
    ft = _check_fast_track(facts, ai_raw_report)
    if ft["pass"]:
        return _verdict(
            ai_raw_report,
            accepted_scenario=ai_scenario,
            accepted_bias=ai_bias,
            previous_accepted_scenario=prev_accepted,
            stable_for_runs=1,
            last_change_ts=now_ts,
            override_reason="fast_track",
            allow_switch=True,
            pending_switch_to=None,
            pending_runs=0,
            notes=f"Fast Track 命中（confidence={ai_raw_report.confidence}）：{ft['reason']}；越过 persistence/hysteresis 立即切换。",
        )

    # ── 5/6. 同大类（Persistence）vs 跨大类（Hysteresis）──
    cat_prev = _CATEGORY.get(prev_accepted, "neutral")
    cat_new = _CATEGORY.get(ai_scenario, "neutral")

    if cat_prev == cat_new:
        return _persistence(
            ai_raw_report,
            prev_accepted=prev_accepted,
            prev_bias=prev_bias,
            prev_pending=prev_pending,
            prev_pending_runs=prev_pending_runs,
            prev_stable_for=prev_stable_for,
            prev_last_change=prev_last_change,
            now_ts=now_ts,
        )
    else:
        return _hysteresis(
            facts=facts,
            ai_raw_report=ai_raw_report,
            previous_report=previous_report,
            prev_accepted=prev_accepted,
            prev_bias=prev_bias,
            prev_pending=prev_pending,
            prev_pending_runs=prev_pending_runs,
            prev_stable_for=prev_stable_for,
            prev_last_change=prev_last_change,
            now_ts=now_ts,
        )


# ────────────────────────────────────────────────────────────────────────────
# 子规则
# ────────────────────────────────────────────────────────────────────────────


def _persistence(
    ai_raw_report: MarketActionReport,
    *,
    prev_accepted: str,
    prev_bias: str,
    prev_pending: Optional[str],
    prev_pending_runs: int,
    prev_stable_for: int,
    prev_last_change: int,
    now_ts: int,
) -> StabilityVerdict:
    """同大类切换：本轮 pending + 下一轮确认（≥ 1 轮 pending）。"""
    ai_scenario = ai_raw_report.scenario
    ai_bias = ai_raw_report.trading_implications.bias

    if prev_pending == ai_scenario and prev_pending_runs >= 1:
        # 上轮已 pending 同方向，本轮确认 → 通过
        return _verdict(
            ai_raw_report,
            accepted_scenario=ai_scenario,
            accepted_bias=ai_bias,
            previous_accepted_scenario=prev_accepted,
            stable_for_runs=1,
            last_change_ts=now_ts,
            override_reason="",
            allow_switch=True,
            pending_switch_to=None,
            pending_runs=0,
            notes=f"persistence 满足（pending {ai_scenario} 已连续 ≥ 2 轮），同大类切换通过。",
        )

    return _verdict(
        ai_raw_report,
        accepted_scenario=prev_accepted,
        accepted_bias=prev_bias,
        previous_accepted_scenario=prev_accepted,
        stable_for_runs=prev_stable_for + 1,
        last_change_ts=prev_last_change,
        override_reason="persistence_pending",
        allow_switch=False,
        pending_switch_to=ai_scenario,
        pending_runs=1,
        notes=f"同大类切换 {prev_accepted}→{ai_scenario}，本轮记 pending；下一轮 AI 仍同向才采纳。",
    )


def _hysteresis(
    *,
    facts: MarketActionFacts,
    ai_raw_report: MarketActionReport,
    previous_report: MarketActionReport,
    prev_accepted: str,
    prev_bias: str,
    prev_pending: Optional[str],
    prev_pending_runs: int,
    prev_stable_for: int,
    prev_last_change: int,
    now_ts: int,
) -> StabilityVerdict:
    """跨大类反转：confidence ≥ 70 + ≥ 3 条 main+high + 旧主证据被反证。"""
    ai_scenario = ai_raw_report.scenario
    ai_bias = ai_raw_report.trading_implications.bias
    h = _check_hysteresis(ai_raw_report, previous_report)
    if h["pass"]:
        return _verdict(
            ai_raw_report,
            accepted_scenario=ai_scenario,
            accepted_bias=ai_bias,
            previous_accepted_scenario=prev_accepted,
            stable_for_runs=1,
            last_change_ts=now_ts,
            override_reason="",
            allow_switch=True,
            pending_switch_to=None,
            pending_runs=0,
            notes=f"hysteresis 三条件满足，跨大类切换通过：{h['detail']}",
        )

    pending_runs = (prev_pending_runs + 1) if prev_pending == ai_scenario else 1
    return _verdict(
        ai_raw_report,
        accepted_scenario=prev_accepted,
        accepted_bias=prev_bias,
        previous_accepted_scenario=prev_accepted,
        stable_for_runs=prev_stable_for + 1,
        last_change_ts=prev_last_change,
        override_reason="hysteresis_block",
        allow_switch=False,
        pending_switch_to=ai_scenario,
        pending_runs=pending_runs,
        notes=f"跨大类反转 {prev_accepted}→{ai_scenario} 证据强度不足：{h['detail']}",
    )


def _check_dead_zone(facts: MarketActionFacts) -> dict:
    """三项底层指标全部在 dead zone 内才算"全静"——任一缺失都不能判 dead zone。"""
    coin = (facts.coin or "").upper()
    cfg = _DEAD_ZONE.get(coin, _DEAD_ZONE["default"])

    oi_ch = (
        abs(facts.oi.change_1h_pct)
        if facts.oi and facts.oi.change_1h_pct is not None else None
    )
    funding = (
        abs(facts.funding.avg_current)
        if facts.funding and facts.funding.avg_current is not None else None
    )
    basis = (
        abs(facts.basis.basis_pct)
        if facts.basis and facts.basis.basis_pct is not None else None
    )

    if oi_ch is None or funding is None or basis is None:
        return {
            "in_dead_zone": False,
            "detail": (
                f"底层指标缺失（OI={oi_ch} funding={funding} basis={basis}）"
                "→ 不判 dead zone（保守放行进入 persistence/hysteresis）"
            ),
        }

    in_dz = (
        oi_ch < cfg["oi_1h_pct"]
        and funding < cfg["funding"]
        and basis < cfg["basis_pct"]
    )
    detail = (
        f"|OI 1h|={oi_ch:.3f}% (<{cfg['oi_1h_pct']}%) "
        f"| |funding|={funding:.6f} (<{cfg['funding']}) "
        f"| |basis|={basis:.3f}% (<{cfg['basis_pct']}%)"
    )
    return {"in_dead_zone": in_dz, "detail": detail}


def _check_fast_track(
    facts: MarketActionFacts,
    report: MarketActionReport,
) -> dict:
    """confidence ≥ 85 + 至少一条已成交硬动作证据，且方向与 ai_scenario 大类一致。"""
    if report.confidence < _FAST_TRACK_CONFIDENCE:
        return {"pass": False, "reason": f"confidence={report.confidence} < {_FAST_TRACK_CONFIDENCE}"}

    cat = _CATEGORY.get(report.scenario, "neutral")
    if cat == "neutral":
        return {"pass": False, "reason": "neutral 大类（range_bound）不允许走 fast track"}

    reasons: list[str] = []

    # 1) 连续清算扫单 + 方向对齐
    sw = facts.liq_sweep_recent
    if sw and sw.continuous_trigger:
        if cat == "bull" and sw.last_sweep_side == "short_side":
            reasons.append("liq_sweep continuous (short_side, bull-aligned)")
        elif cat == "bear" and sw.last_sweep_side == "long_side":
            reasons.append("liq_sweep continuous (long_side, bear-aligned)")

    # 2) Footprint 已收盘 bar 出 stacked imbalance（方向对齐）
    if facts.footprint and facts.footprint.contract_latest:
        cl = facts.footprint.contract_latest
        if cl.bar_closed and not cl.low_volume:
            for z in cl.top_imbalance_zones or []:
                try:
                    ratio = float(z.get("ratio") or 0)
                except (TypeError, ValueError):
                    continue
                side = str(z.get("side") or "")
                if ratio < _FAST_TRACK_FP_RATIO:
                    continue
                if cat == "bull" and side == "stacked_buy":
                    reasons.append(f"footprint stacked_buy ratio={ratio:.1f} (closed bar)")
                    break
                if cat == "bear" and side == "stacked_sell":
                    reasons.append(f"footprint stacked_sell ratio={ratio:.1f} (closed bar)")
                    break

    # 3) Absorption · zones 出现且 bar_count ≥ 2（非 fallback）
    if facts.absorption and not facts.absorption.fallback_used:
        if cat == "bull":
            for z in facts.absorption.zones_support or []:
                if z.bar_count >= _FAST_TRACK_ABS_BARS:
                    reasons.append(f"absorption support@{z.price} bar_count={z.bar_count}")
                    break
        elif cat == "bear":
            for z in facts.absorption.zones_resistance or []:
                if z.bar_count >= _FAST_TRACK_ABS_BARS:
                    reasons.append(f"absorption resistance@{z.price} bar_count={z.bar_count}")
                    break

    if reasons:
        return {"pass": True, "reason": " | ".join(reasons)}
    return {"pass": False, "reason": "无方向对齐的硬动作证据"}


def _check_hysteresis(
    report: MarketActionReport,
    previous_report: MarketActionReport,
) -> dict:
    """跨大类反转的三条件：confidence ≥ 70 + ≥ 3 条 main+high + 旧主证据被反证。"""
    detail: list[str] = []

    if report.confidence < _HYSTERESIS_CONFIDENCE:
        return {"pass": False, "detail": f"confidence={report.confidence} < {_HYSTERESIS_CONFIDENCE} ✗"}
    detail.append(f"confidence={report.confidence}≥{_HYSTERESIS_CONFIDENCE} ✓")

    main_high = [
        e for e in report.evidence_breakdown
        if e.supports == "main" and e.weight == "high"
    ]
    if len(main_high) < _HYSTERESIS_MAIN_HIGH:
        return {"pass": False, "detail": f"main+high={len(main_high)} < {_HYSTERESIS_MAIN_HIGH} ✗"}
    detail.append(f"main+high={len(main_high)}≥{_HYSTERESIS_MAIN_HIGH} ✓")

    # 旧主证据被反证：上版 main+high 涉及的 dimension，本版有同 dimension 转 contrarian 或 weight=low
    prev_main_high = [
        e for e in (previous_report.evidence_breakdown or [])
        if e.supports == "main" and e.weight == "high"
    ]
    if not prev_main_high:
        # 上版无 main+high（冷启动 / 上版降级）→ 跳过反证检查，保 conservative pass
        detail.append("prev 无 main+high，跳过反证检查 ⚠")
        return {"pass": True, "detail": " | ".join(detail)}

    prev_dims = {e.dimension for e in prev_main_high}
    refuted = 0
    for e in report.evidence_breakdown:
        if e.dimension not in prev_dims:
            continue
        if e.supports == "contrarian" or e.weight == "low":
            refuted += 1
    if refuted < 1:
        return {
            "pass": False,
            "detail": f"prev main+high dims={sorted(prev_dims)} 在新 evidence 中未被反证 ✗",
        }
    detail.append(f"旧主证据被反证 refuted={refuted} ✓")
    return {"pass": True, "detail": " | ".join(detail)}


# ────────────────────────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────────────────────────


def _extract_prev_state(prev_report: MarketActionReport) -> dict:
    """从上一份报告抽取 stability 字段；若无 stability（旧历史）则用 raw scenario 兜底。"""
    s = prev_report.stability
    if s is not None:
        return {
            "accepted_scenario": s.accepted_scenario,
            "accepted_bias": s.accepted_bias,
            "pending_switch_to": s.pending_switch_to,
            "pending_runs": int(s.pending_runs or 0),
            "stable_for_runs": int(s.stable_for_runs or 1),
            "last_change_ts": int(s.last_change_ts or 0),
        }
    return {
        "accepted_scenario": prev_report.scenario,
        "accepted_bias": prev_report.trading_implications.bias,
        "pending_switch_to": None,
        "pending_runs": 0,
        "stable_for_runs": 1,
        "last_change_ts": int(prev_report.timestamp or 0),
    }


def _verdict(
    ai_raw_report: MarketActionReport,
    *,
    accepted_scenario: str,
    accepted_bias: str,
    previous_accepted_scenario: Optional[str],
    stable_for_runs: int,
    last_change_ts: int,
    override_reason: str,
    allow_switch: bool,
    pending_switch_to: Optional[str],
    pending_runs: int,
    notes: str,
) -> StabilityVerdict:
    return StabilityVerdict(
        ai_raw_scenario=ai_raw_report.scenario,
        ai_raw_bias=ai_raw_report.trading_implications.bias,
        accepted_scenario=accepted_scenario,
        accepted_bias=accepted_bias,
        previous_accepted_scenario=previous_accepted_scenario,
        stable_for_runs=int(stable_for_runs),
        last_change_ts=int(last_change_ts),
        override_reason=override_reason,
        allow_switch=allow_switch,
        pending_switch_to=pending_switch_to,
        pending_runs=int(pending_runs),
        notes=notes[:300],
    )


def category_of(scenario: str) -> str:
    """对外暴露的工具函数：把 scenario 映射到 bull/bear/neutral 大类。"""
    return _CATEGORY.get(scenario, "neutral")
