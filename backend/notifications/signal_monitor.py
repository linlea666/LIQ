"""S 级信号监控：检测关键位 / 箱体 / MAA 三类高优信号并触发通知。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from models.key_level import KeyLevelSnapshotV2, KeyLevelV2
    from models.flow import RangeSignalData
    from models.market_action import MarketActionReport

logger = logging.getLogger(__name__)

ACTIONABLE_ACTIONS = frozenset({
    "snipe_long", "snipe_short",
    "flip_long", "flip_short",
    "scalp_long", "scalp_short",   # 日内极小止损档
})

# 兜底：scalp 信号用 signal.confidence 映射到 tier（scalp 不依赖 level.strength_tier 的 S/A 阈值，
# 因为 scalp 在引擎层已经做了 S/A 过滤，这里若 _find_level 因浮点误差失败则不应丢信号）
_SCALP_ACTIONS = frozenset({"scalp_long", "scalp_short"})


@dataclass
class AlertEvent:
    """一条待发送的通知事件。

    支持三类来源（source）：
      - "key_level"：关键位 snipe/flip/scalp 信号
      - "range"    ：箱体突破/反弹信号
      - "market_action"：MAA（Market Action Analyzer）方向切换信号
        · 普通通道：accepted_scenario 切换 + bias∈{long,short} + conf≥75
        · 强信号通道：confidence ≥ 85 + continuity.stance="reversal"
        MAA 专属字段以 maa_* 前缀标注，对其他来源默认空值不影响渲染。
    """
    coin: str
    source: str              # "key_level" | "range" | "market_action"
    direction: str           # "long" | "short"
    signal_tier: str         # "S" | "A"
    price: float             # 当前价
    action: str = ""         # "snipe_long" / "scalp_long" / "flip_short" / "" (range 信号)
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    rr_ratio: Optional[float] = None
    reason: str = ""
    level_price: Optional[float] = None
    level_state: str = ""    # "swept" | "flipped" | ...
    cascade_risk: float = 0
    warnings: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    # 1h 市场结构上下文（Commit 6 · 邮件 metadata）
    # 从 RangeSignalData.ms_* 派生，用于邮件标题一眼看出"顺势/逆势"判断
    ms_direction: str = ""     # "bullish" / "bearish" / "ranging" / "transitioning"
    ms_alignment: str = ""     # "aligned" / "conflict" / "neutral" / "unknown"
    # ── MAA 专属字段（source="market_action" 时填充）──
    maa_scenario: str = ""              # accepted_scenario，9 选 1
    maa_phase: str = ""                 # market_phase，5 选 1
    maa_continuity: str = ""            # continuity.stance: continuation/refinement/reversal/first_run
    maa_confidence: int = 0             # report.confidence
    maa_is_strong: bool = False         # 强信号通道命中（confidence≥85 + reversal）
    maa_invalidation_top: str = ""      # 第一条失效条件（截 120 字）
    maa_alternative: str = ""           # 对立场景摘要（≤80 字）
    maa_reasoning_short: str = ""       # analyst_reasoning 前 200 字
    maa_stability_overridden: bool = False   # accepted ≠ ai_raw（被滤波修正过）
    maa_tp_targets: list[float] = field(default_factory=list)  # take_profit_targets 多目标

    @property
    def is_scalp(self) -> bool:
        return self.action in _SCALP_ACTIONS

    @property
    def dedup_key(self) -> str:
        if self.source == "market_action":
            # 强信号走独立 dedup_key，避免被普通通道占用 cooldown 位
            kind = "strong" if self.maa_is_strong else "normal"
            return f"{self.coin}:maa:{kind}:{self.maa_scenario}:{self.direction}"
        if self.source == "key_level":
            # scalp 与 snipe/flip 即使关键位相同也应分别去重（时间尺度不同）
            kind = "scalp" if self.is_scalp else "kl"
            return f"{self.coin}:{kind}:{self.level_price}:{self.level_state}:{self.direction}"
        return f"{self.coin}:range:{self.signal_tier}:{self.direction}"


class AlertDedup:
    """基于冷却时间的去重器。

    拆分 should_send / mark_sent 的原因（P1 可靠性修复）：
    旧实现 should_send 成功时直接把 now 写入 _sent，但调用方 send_alert_email
    若 SMTP 暂时失败（配置缺失 / 网络抖动）会返回 False，此时冷却位已被占用，
    导致同一 dedup_key 静默锁定整个 cooldown 窗口（默认 45 分钟），
    用户观感是"S 级信号有了但收不到邮件"。现在强制调用方必须在发送成功后
    显式 mark_sent，失败时允许下一轮立即重试。
    """

    def __init__(self, cooldown_seconds: int = 1800):
        self.cooldown_seconds = cooldown_seconds
        self._sent: dict[str, float] = {}

    def should_send(self, key: str) -> bool:
        """纯查询：是否超出冷却窗口可以发送。不产生副作用。"""
        last = self._sent.get(key, 0)
        return (time.time() - last) >= self.cooldown_seconds

    def mark_sent(self, key: str) -> None:
        """记录一次"成功送达"的时间戳。调用方必须在 send 返回 True 后调用。"""
        self._sent[key] = time.time()

    def cleanup(self, max_age: int = 7200):
        """清理过期条目，防止内存泄漏。"""
        now = time.time()
        expired = [k for k, ts in self._sent.items() if now - ts > max_age]
        for k in expired:
            del self._sent[k]


def _find_level(price: float, levels: list, max_dist_pct: float = 0.008) -> Optional[KeyLevelV2]:
    """从 V2 关键位列表中找到与信号价格匹配的 level。

    容差从 0.5% 放宽到 0.8%，避免浮点误差 / 动态重算导致 level_price 与 snapshot 中
    对应 level 微小偏差时反查失败而静默丢信号。
    """
    best = None
    best_dist = float("inf")
    for lv in levels:
        dist = abs(lv.price - price)
        if dist < best_dist:
            best_dist = dist
            best = lv
    if best and best_dist / max(price, 1) < max_dist_pct:
        return best
    return None


def scan_alerts(
    coin: str,
    price: float,
    kl_snapshot: Optional[KeyLevelSnapshotV2],
    range_signal: Optional[RangeSignalData],
    min_tier: str = "A",
    include_key_levels: bool = True,
    include_range: bool = True,
) -> list[AlertEvent]:
    """扫描当前状态，返回所有满足条件的告警事件。"""
    allowed_tiers = {"S"} if min_tier == "S" else {"S", "A"}
    events: list[AlertEvent] = []

    # 所有事件共享同一个 1h 市场结构上下文（来自 RangeSignalData）
    ms_direction = range_signal.ms_direction if range_signal else ""
    ms_alignment = range_signal.ms_alignment if range_signal else ""

    if include_key_levels and kl_snapshot and kl_snapshot.signals:
        for sig in kl_snapshot.signals:
            if sig.action not in ACTIONABLE_ACTIONS:
                continue
            level = _find_level(sig.level_price, kl_snapshot.levels)

            # tier 判定：
            #  - scalp：一律以 sig.confidence 为准。scalp 的置信度是生成器按
            #    "S级 level + 强形态(≥0.8) → A，其余 → B" 精算出来的；
            #    若用 level.strength_tier 会夸大强度（如 S 级 level + 弱吞没本应 B，
            #    却被误报为 S），邮件标题就会误导交易员。
            #  - snipe/flip：以 level.strength_tier 为准（保留原语义）。
            if sig.action in _SCALP_ACTIONS:
                if not (sig.entry_price and sig.stop_loss and sig.tp1):
                    continue  # scalp 参数不全，跳过
                tier = sig.confidence if sig.confidence in ("S", "A", "B") else "B"
                cascade = level.cascade_risk if level else 0.0
            elif level:
                tier = level.strength_tier
                cascade = level.cascade_risk
            else:
                continue  # 非 scalp 且找不到 level，放弃以免发错价

            if tier not in allowed_tiers:
                continue

            direction = "long" if "long" in sig.action else "short"
            # key_level 信号的结构对齐度独立计算：以 direction 对比 ms_direction
            if ms_direction == "bullish":
                kl_alignment = "aligned" if direction == "long" else "conflict"
            elif ms_direction == "bearish":
                kl_alignment = "aligned" if direction == "short" else "conflict"
            else:
                kl_alignment = "neutral" if ms_direction else ""

            events.append(AlertEvent(
                coin=coin,
                source="key_level",
                direction=direction,
                signal_tier=tier,
                price=price,
                action=sig.action,
                entry=sig.entry_price,
                stop_loss=sig.stop_loss,
                tp1=sig.tp1,
                rr_ratio=sig.rr_ratio,
                reason=sig.reason,
                level_price=sig.level_price,
                level_state=sig.state,
                cascade_risk=cascade,
                warnings=sig.warnings,
                ms_direction=ms_direction or "",
                ms_alignment=kl_alignment,
            ))

    if include_range and range_signal and range_signal.signal_grade:
        if range_signal.signal_grade in allowed_tiers:
            direction = "long" if range_signal.signal_direction == "long" else "short"
            events.append(AlertEvent(
                coin=coin,
                source="range",
                direction=direction,
                signal_tier=range_signal.signal_grade,
                price=price,
                entry=range_signal.signal_entry,
                stop_loss=range_signal.signal_stop_loss,
                tp1=range_signal.signal_tp1,
                rr_ratio=range_signal.signal_rr_ratio,
                reason=range_signal.signal_reason,
                ms_direction=ms_direction or "",
                ms_alignment=ms_alignment or "",
            ))

    return events


# ─────────────────────────────────────────────────────────────────────────────
# MAA（Market Action Analyzer）扫描器
# ─────────────────────────────────────────────────────────────────────────────

# 与 stability_filter._CATEGORY 同义，但避免反向依赖：本模块只关心 long/short/range
# 触发条件之一（accepted_scenario 切换）天然由 prev_scenario 比较实现。
_MAA_SHORT_REASON_LIMIT = 200       # analyst_reasoning 截取长度
_MAA_INVALIDATION_LIMIT = 120       # 单条 invalidation 截取长度
_MAA_ALTERNATIVE_LIMIT = 80         # 对立场景摘要长度
_MAA_RANGE_SCENARIO = "range_bound"  # 震荡场景 → 不发邮件（与"等待边界"语义一致）


def _maa_truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    s = str(text).strip()
    if len(s) <= limit:
        return s
    return s[: max(1, limit - 1)] + "…"


def scan_maa_alerts(
    coin: str,
    *,
    report: "MarketActionReport",
    prev_scenario: str | None,
    price: float,
    coin_whitelist: list[str],
    min_confidence: int,
    strong_confidence: int,
) -> list[AlertEvent]:
    """从 MAA 报告生成待发送邮件事件。

    判定流程（任一不满足直接返回 []，遵循"严进宽出"避免误报刷屏）：
      1. 币种在白名单内（默认 BTC/ETH）
      2. report.stability 存在（保证滤波层已跑过）
      3. accepted_bias ∈ {"long", "short"}（neutral 不发）
      4. accepted_scenario != "range_bound"（震荡市等边界，不发方向信号）
      5. confidence ≥ min_confidence（默认 75）
      6. report.data_quality == "ok"（partial/insufficient 不发，避免低质数据误导）
      7. accepted_scenario 与 prev_scenario 不同（首次 / 切换才发，避免每 10 分钟发同一方向）

    强信号通道（dedup_key 独立 + 短 cooldown）：
      - confidence ≥ strong_confidence（默认 85）
      - continuity.stance == "reversal"（明确翻转才算"强"）
      两条同时满足 → maa_is_strong=True
    """
    if not report:
        return []

    coin_upper = coin.upper()
    if coin_upper not in {c.upper() for c in (coin_whitelist or [])}:
        return []

    stab = report.stability
    if stab is None:
        # 滤波器未跑（旧报告兼容路径）→ 跳过，避免发未防抖信号
        return []

    bias = (stab.accepted_bias or "").lower()
    if bias not in ("long", "short"):
        return []

    scenario = (stab.accepted_scenario or "").strip()
    if not scenario or scenario == _MAA_RANGE_SCENARIO:
        return []

    confidence = int(report.confidence or 0)
    if confidence < int(min_confidence):
        return []

    # 数据质量门禁：partial/insufficient 时数据本身可能误导，不发邮件
    # （DataQuality Literal: "ok" | "partial" | "insufficient"）
    # data_quality 是 report 顶层字段（AI 评估的总体质量）；
    # FactsDataMeta 仅记录 provisional bars / sources，不含 quality 维度
    dq = str(report.data_quality or "").lower()
    if dq and dq != "ok":
        return []

    # 切换判定：与上一份 accepted_scenario 比较
    prev = (prev_scenario or "").strip()
    if prev and prev == scenario:
        # 同一场景重复 → 不发（避免每 10 分钟发同一方向）
        return []

    continuity_stance = ""
    if report.continuity:
        continuity_stance = (report.continuity.stance or "").lower()

    is_strong = (
        confidence >= int(strong_confidence)
        and continuity_stance == "reversal"
    )

    invalidation_top = ""
    if report.invalidation_conditions:
        invalidation_top = _maa_truncate(
            report.invalidation_conditions[0], _MAA_INVALIDATION_LIMIT
        )

    alternative = ""
    if report.alternative_scenario:
        alt = report.alternative_scenario
        parts = [
            (alt.scenario or "").strip(),
            (alt.trigger or "").strip(),
        ]
        joined = " · ".join(p for p in parts if p)
        alternative = _maa_truncate(joined, _MAA_ALTERNATIVE_LIMIT)

    reasoning_short = _maa_truncate(report.analyst_reasoning, _MAA_SHORT_REASON_LIMIT)

    # 交易计划：entry / SL / 多目标 TP
    tp_targets: list[float] = []
    entry: float | None = None
    sl: float | None = None
    if report.trading_implications:
        ti = report.trading_implications
        if ti.entry_zone and len(ti.entry_zone) >= 1:
            try:
                entry = float(ti.entry_zone[0])
            except (TypeError, ValueError):
                entry = None
        if ti.stop_loss_beyond is not None:
            try:
                sl = float(ti.stop_loss_beyond)
            except (TypeError, ValueError):
                sl = None
        if ti.take_profit_targets:
            for v in ti.take_profit_targets:
                try:
                    tp_targets.append(float(v))
                except (TypeError, ValueError):
                    continue

    tp1 = tp_targets[0] if tp_targets else None
    rr = None
    if entry is not None and sl is not None and tp1 is not None:
        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        if risk > 1e-9:
            rr = round(reward / risk, 2)

    # 滤波是否实际修改了 AI 的 raw 输出（用于邮件正文标注"被防抖压住")
    overridden = (
        (stab.ai_raw_scenario or "") != (stab.accepted_scenario or "")
        or (stab.ai_raw_bias or "") != (stab.accepted_bias or "")
    )

    # MAA 信号统一映射到 signal_tier（仅用于复用现有展示逻辑/日志）
    # 强信号 → "S"，普通 → "A"。MAA 自身不归属 ABC 体系，这里只是占位。
    tier = "S" if is_strong else "A"

    return [AlertEvent(
        coin=coin_upper,
        source="market_action",
        direction="long" if bias == "long" else "short",
        signal_tier=tier,
        price=price,
        entry=entry,
        stop_loss=sl,
        tp1=tp1,
        rr_ratio=rr,
        reason=reasoning_short,
        ms_direction="",
        ms_alignment="",
        maa_scenario=scenario,
        maa_phase=(report.market_phase or ""),
        maa_continuity=continuity_stance,
        maa_confidence=confidence,
        maa_is_strong=is_strong,
        maa_invalidation_top=invalidation_top,
        maa_alternative=alternative,
        maa_reasoning_short=reasoning_short,
        maa_stability_overridden=overridden,
        maa_tp_targets=tp_targets,
    )]
