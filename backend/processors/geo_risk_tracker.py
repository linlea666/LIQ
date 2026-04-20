"""地缘风险追踪器 · Layer 3b（D11）

职责：
  - 为每个地缘主题（美伊/俄乌/台海/朝韩）维护 GeoRiskState
  - 从 MarketEventSignal.risk_type=='geopolitical' 的事件驱动
  - 产出 GeoRiskEvent（进 SignalBus）+ GeoRiskOverview（进 UI / SafetyGate）

等级映射（内置默认值，可由 templates 覆盖）：
  外交辞令       → 2 TENSION
  制裁/军事演习  → 3 CRISIS
  空袭/冲突      → 4 ESCALATION
  全面战争       → 5 WAR
  和谈/签协议     → -1 或 -2 等级

flip-flop 特殊逻辑：
  美伊这类主题 24h 内来回升级/降级（拉扯）→ 触发 flip_flop_warning
  AI prompt 侧会提示"反复拉扯，市场已麻木，影响递减"

落实日志锚点：
  - D.D11_GEO_RISK：
    * 每次 ingest 若 level_before != level_after 立即 status=warn 上报 severity
    * 定期刷新 overall_level / active_themes / escalation_24h
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import yaml

from models.common_enums import GeoRiskLabel
from models.geo_risk import (
    GeoRiskEvent, GeoRiskLevelChange, GeoRiskOverview, GeoRiskState,
)
from models.news_event import MarketEventSignal

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 默认 templates（按关键词决定 delta/absolute level）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DEFAULT_TEMPLATES: dict = {
    # 绝对等级关键词（命中即覆盖 current_level，不受其他升降影响）
    "absolute_levels": [
        {"level": 5, "keywords": ["全面战争", "declared war", "all-out war", "nuclear strike", "核打击"]},
        {"level": 4, "keywords": ["airstrike", "空袭", "bombed", "大规模冲突", "ground invasion", "地面入侵"]},
        {"level": 3, "keywords": ["军事演习", "military drill", "sanctions", "制裁", "封锁", "blockade"]},
        {"level": 2, "keywords": ["外交", "diplomatic", "谴责", "抗议", "召回大使", "recalled ambassador"]},
    ],
    # 升降级关键词（在当前等级上做 delta）
    "delta_keywords": [
        {"delta": +2, "keywords": ["导弹袭击", "missile attack", "战争爆发"]},
        {"delta": +1, "keywords": ["升级", "escalate", "威胁", "threat", "紧张升级"]},
        {"delta": -1, "keywords": ["停火", "ceasefire", "和谈", "talks resumed", "重启谈判"]},
        {"delta": -2, "keywords": ["签署协议", "signed agreement", "peace deal", "和平协议"]},
    ],
}

_LABEL_TABLE: list[tuple[int, GeoRiskLabel, str]] = [
    (0, "PEACE", "🟢"),
    (1, "WATCHING", "🟡"),
    (2, "TENSION", "🟠"),
    (3, "CRISIS", "🔴"),
    (4, "ESCALATION", "🟣"),
    (5, "WAR", "⚫"),
]


_FLIP_FLOP_WARN_THRESHOLD = 2


class GeoRiskTracker:
    """地缘风险追踪（全局单例）"""

    def __init__(
        self,
        *,
        flip_flop_window_sec: int = 86400,
        templates_path: Optional[str] = None,
    ) -> None:
        self._flip_flop_window_sec = int(flip_flop_window_sec)
        self._templates = _load_templates(templates_path)
        self._states: dict[str, GeoRiskState] = {}
        self._lock = threading.RLock()

    # ── 写入 ──
    def ingest(self, event: MarketEventSignal) -> Optional[GeoRiskEvent]:
        """接收事件，若 risk_type != geopolitical 则返回 None。"""
        if (event.risk_type or "").lower() != "geopolitical":
            return None

        theme_id = (event.narrative_theme or "").strip() or "_geo_misc"
        now = int(time.time())

        with self._lock:
            state = self._states.get(theme_id)
            if state is None:
                state = GeoRiskState(
                    theme_id=theme_id,
                    theme_name_cn=theme_id.replace("_", " "),
                    current_level=0,
                    level_label="PEACE",
                    level_emoji="🟢",
                    last_updated=now,
                )
                self._states[theme_id] = state

            level_before = int(state.current_level)
            level_after = _derive_level(event, self._templates, current_level=level_before)
            level_after = max(0, min(5, int(level_after)))

            # 更新 flip-flop 计数（清理过期）
            self._prune_flip_flop_locked(state, now)

            if level_after != level_before:
                change = GeoRiskLevelChange(
                    ts=now,
                    level_before=level_before,
                    level_after=level_after,
                    event_id=event.event_id,
                    trigger_summary=(event.summary_cn or event.first_order_impact or "")[:30],
                )
                state.level_history.append(change)
                # flip-flop 判断：相邻两次 level 跃迁方向相反
                if len(state.level_history) >= 2:
                    prev = state.level_history[-2]
                    curr = state.level_history[-1]
                    direction_prev = _severity_of(prev.level_before, prev.level_after)
                    direction_curr = _severity_of(curr.level_before, curr.level_after)
                    if (
                        direction_prev == "escalation" and direction_curr == "de-escalation"
                    ) or (
                        direction_prev == "de-escalation" and direction_curr == "escalation"
                    ):
                        state.flip_flop_count_24h += 1
                        state.flip_flop_count_7d += 1
                state.flip_flop_warning = state.flip_flop_count_24h >= _FLIP_FLOP_WARN_THRESHOLD

            # level 稳定时长
            if level_after == level_before:
                # 累加
                delta_hours = (now - state.last_updated) / 3600.0
                state.level_stable_hours = round(state.level_stable_hours + max(0.0, delta_hours), 3)
            else:
                state.level_stable_hours = 0.0

            # 累积 price 反应（供 AI 看已定价程度）- 这里先占位，price_reaction_backfill 回填

            # 同步 label / emoji
            label, emoji = _label_for_level(level_after)
            state.current_level = level_after
            state.level_label = label
            state.level_emoji = emoji
            state.latest_event_id = event.event_id
            state.latest_event_summary = (event.summary_cn or event.first_order_impact or "")[:60]
            state.latest_event_ts = now
            state.last_updated = now

            # ── 生成 GeoRiskEvent（仅 level 变化时发）──
            if level_after != level_before:
                severity = _severity_of(level_before, level_after)
                geo_event = GeoRiskEvent(
                    event_id=event.event_id,
                    theme_id=theme_id,
                    theme_name_cn=state.theme_name_cn,
                    ts=now,
                    level_before=level_before,
                    level_after=level_after,
                    severity=severity,
                    estimated_crypto_reaction_pct=_estimate_reaction_pct(event, severity),
                    estimated_direction=_estimate_direction(event, severity),
                    confidence=max(0.0, min(1.0, float(event.confidence or 0.5))),
                    flip_flop_warning=state.flip_flop_warning,
                    is_blackswan=(level_after >= 5) or (level_before <= 1 and level_after >= 4),
                    summary_cn=(event.summary_cn or "")[:60],
                )
                _mark_d11_warn(state, severity)
                return geo_event
            else:
                return None

    # ── 查询 ──
    def get_state(self, theme_id: str) -> Optional[GeoRiskState]:
        with self._lock:
            return self._states.get(theme_id)

    def get_all_states(self) -> list[GeoRiskState]:
        with self._lock:
            return list(self._states.values())

    def get_overview(self) -> GeoRiskOverview:
        now = int(time.time())
        with self._lock:
            all_states = list(self._states.values())

        active = [s for s in all_states if s.current_level >= 1]
        active.sort(key=lambda s: s.current_level, reverse=True)

        overall_level = max((s.current_level for s in active), default=0)
        overall_label, overall_emoji = _label_for_level(overall_level)

        summary_parts: list[str] = []
        for s in active[:3]:
            summary_parts.append(f"{s.theme_name_cn} {s.level_emoji}{s.level_label}")
        overall_summary_cn = (
            ("·".join(summary_parts) + f" · 综合 {overall_level}/5")
            if summary_parts
            else f"{overall_emoji} {overall_label}"
        )

        escal = 0
        de_escal = 0
        has_blackswan = False
        for s in all_states:
            for change in s.level_history:
                if (now - change.ts) > 86400:
                    continue
                if change.level_after > change.level_before:
                    escal += 1
                elif change.level_after < change.level_before:
                    de_escal += 1
                if change.level_after >= 4 and change.level_before <= 1:
                    has_blackswan = True
                if change.level_after >= 5:
                    has_blackswan = True

        suggest_block = overall_level >= 4
        if overall_level == 5:
            cap = 0.0
        elif overall_level == 4:
            cap = 15.0
        elif overall_level == 3:
            cap = 30.0
        else:
            cap = None

        return GeoRiskOverview(
            ts=now,
            overall_level=overall_level,
            overall_label=overall_label,
            overall_emoji=overall_emoji,
            overall_summary_cn=overall_summary_cn,
            active_themes=active,
            escalation_count_24h=escal,
            de_escalation_count_24h=de_escal,
            has_blackswan_24h=has_blackswan,
            suggest_safety_gate_block=suggest_block,
            suggest_position_cap_pct=cap,
        )

    # ── 维护 ──
    def decay(self, now_ts: Optional[int] = None) -> int:
        """24h 无事件且 level >= 1 → 降 1 级（自然衰减）。返回变更数量。"""
        now = int(now_ts if now_ts is not None else time.time())
        changed = 0
        with self._lock:
            for state in self._states.values():
                if state.current_level < 1:
                    continue
                gap = now - state.latest_event_ts
                if gap >= 86400:
                    before = state.current_level
                    after = max(0, before - 1)
                    if after != before:
                        change = GeoRiskLevelChange(
                            ts=now,
                            level_before=before,
                            level_after=after,
                            event_id="",
                            trigger_summary="natural decay (24h no event)",
                        )
                        state.level_history.append(change)
                        state.current_level = after
                        state.level_label, state.level_emoji = _label_for_level(after)
                        state.last_updated = now
                        state.level_stable_hours = 0.0
                        changed += 1
                self._prune_flip_flop_locked(state, now)
        return changed

    # ── 内部 ──
    def _prune_flip_flop_locked(self, state: GeoRiskState, now: int) -> None:
        """重新基于 level_history 计算 24h / 7d 内的 flip_flop 次数"""
        hist = state.level_history
        if not hist:
            state.flip_flop_count_24h = 0
            state.flip_flop_count_7d = 0
            state.flip_flop_warning = False
            return

        def _count_window(window_sec: int) -> int:
            flips = 0
            last_severity: Optional[str] = None
            for ch in hist:
                if (now - ch.ts) > window_sec:
                    continue
                sev = _severity_of(ch.level_before, ch.level_after)
                if sev == "stable":
                    continue
                if last_severity and sev != last_severity:
                    flips += 1
                last_severity = sev
            return flips

        state.flip_flop_count_24h = _count_window(86400)
        state.flip_flop_count_7d = _count_window(7 * 86400)
        state.flip_flop_warning = state.flip_flop_count_24h >= _FLIP_FLOP_WARN_THRESHOLD


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 全局单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TRACKER: Optional[GeoRiskTracker] = None
_TRACKER_LOCK = threading.Lock()


def get_geo_risk_tracker() -> GeoRiskTracker:
    global _TRACKER
    if _TRACKER is None:
        with _TRACKER_LOCK:
            if _TRACKER is None:
                _TRACKER = GeoRiskTracker()
    return _TRACKER


def reset_geo_risk_tracker() -> None:
    """测试用"""
    global _TRACKER
    with _TRACKER_LOCK:
        _TRACKER = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 事件 → 等级推断（纯函数，便于单测）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _derive_level(
    event: MarketEventSignal,
    templates: dict,
    *,
    current_level: int = 0,
) -> int:
    """根据事件的 summary_cn + first_order_impact + raw tags 推断等级 (0-5)

    策略：
      1. 绝对等级关键词命中 → 直接覆盖
      2. 升降级关键词 → 在 current_level 上 delta
      3. impact_score 辅助（|score| >= 4 → 至少 level 3）
      4. flip_flop_warning=True 且本次 direction 与最近相反 → 降档 1 级
    """
    text = " ".join(
        [
            event.summary_cn or "",
            event.first_order_impact or "",
            event.second_order_impact or "",
            event.rationale_cn or "",
            event.narrative_theme or "",
            event.trading_insight or "",
        ]
    ).lower()

    # 1. 绝对等级（先覆盖 → 取最高一档命中）
    absolute_hit: Optional[int] = None
    for rule in templates.get("absolute_levels", []):
        lvl = int(rule.get("level", 0))
        for kw in rule.get("keywords", []):
            if kw and kw.lower() in text:
                if absolute_hit is None or lvl > absolute_hit:
                    absolute_hit = lvl
                break
    if absolute_hit is not None:
        level = absolute_hit
    else:
        level = int(current_level)
        # 2. delta 关键词
        for rule in templates.get("delta_keywords", []):
            delta = int(rule.get("delta", 0))
            for kw in rule.get("keywords", []):
                if kw and kw.lower() in text:
                    level += delta
                    break

    # 3. impact_score 辅助下限
    if abs(int(event.impact_score or 0)) >= 4 and level < 3:
        level = 3

    # 4. flip-flop 降档
    if event.flip_flop_warning and abs(level - int(current_level)) >= 1:
        level = max(0, level - 1)

    return max(0, min(5, level))


def _label_for_level(level: int) -> tuple[GeoRiskLabel, str]:
    """level → (label, emoji)

    0 PEACE 🟢 / 1 WATCHING 🟡 / 2 TENSION 🟠 / 3 CRISIS 🔴 / 4 ESCALATION 🟣 / 5 WAR ⚫
    """
    lvl = max(0, min(5, int(level)))
    for l, label, emoji in _LABEL_TABLE:
        if l == lvl:
            return label, emoji
    return "PEACE", "🟢"


def _severity_of(level_before: int, level_after: int) -> str:
    """level_after>before=escalation / <=-de-escalation / ==stable"""
    if level_after > level_before:
        return "escalation"
    if level_after < level_before:
        return "de-escalation"
    return "stable"


def _estimate_reaction_pct(event: MarketEventSignal, severity: str) -> float:
    """粗略估算加密市场反应百分比（绝对值）"""
    base = abs(int(event.impact_score or 0))
    if base == 0:
        return 0.0
    # 每单位 impact_score 约对应 0.5% 反应
    pct = base * 0.5
    if severity == "escalation":
        pct *= 1.2
    elif severity == "de-escalation":
        pct *= 0.8
    return round(pct, 2)


def _estimate_direction(event: MarketEventSignal, severity: str) -> str:
    """根据事件方向 + severity 推方向（地缘事件：升级 → bearish，和谈 → bullish 默认）"""
    if event.direction in {"bullish", "bearish", "neutral", "potential_reversal"}:
        if event.direction != "neutral":
            return event.direction
    if severity == "escalation":
        return "bearish"
    if severity == "de-escalation":
        return "bullish"
    return "neutral"


def _load_templates(templates_path: Optional[str]) -> dict:
    if not templates_path:
        return _deep_copy_templates(_DEFAULT_TEMPLATES)
    if not os.path.exists(templates_path):
        logger.debug("[D11] templates_path not found %s, using defaults", templates_path)
        return _deep_copy_templates(_DEFAULT_TEMPLATES)
    try:
        with open(templates_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        merged = _deep_copy_templates(_DEFAULT_TEMPLATES)
        for k in ("absolute_levels", "delta_keywords"):
            if isinstance(data.get(k), list):
                merged[k] = data[k]
        return merged
    except Exception:  # noqa: BLE001
        logger.warning("[D11] load templates failed, falling back to defaults", exc_info=True)
        return _deep_copy_templates(_DEFAULT_TEMPLATES)


def _deep_copy_templates(src: dict) -> dict:
    return {
        "absolute_levels": [
            {"level": r["level"], "keywords": list(r.get("keywords", []))}
            for r in src.get("absolute_levels", [])
        ],
        "delta_keywords": [
            {"delta": r["delta"], "keywords": list(r.get("keywords", []))}
            for r in src.get("delta_keywords", [])
        ],
    }


def _mark_d11_warn(state: GeoRiskState, severity: str) -> None:
    try:
        from utils.decision_tracker import D, get_tracker
        get_tracker().mark(
            D.D11_GEO_RISK,
            status="warn",
            log=False,
            theme_id=state.theme_id,
            current_level=state.current_level,
            level_label=state.level_label,
            severity=severity,
            flip_flop_count_24h=state.flip_flop_count_24h,
            flip_flop_warning=state.flip_flop_warning,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[D11] mark failed", exc_info=True)
