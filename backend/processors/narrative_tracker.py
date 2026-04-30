"""叙事主题追踪器 · Layer 3a（D10）

职责：
  - 把 MarketEventSignal 按 narrative_theme 归档
  - 维护 NarrativeTheme 状态机（emerging → active → fading → dormant）
  - 检测 flip-flop（反复拉扯）
  - 提供 get_active() 供 news_brief / AI prompt 注入

数据持久化：
  - 阶段 1：in-memory（重启丢失）
  - 阶段 2（P1 后期）：序列化到 SQLite / JSON

落实日志锚点：
  - D.D10_FLIP_FLOP：发现 flip_flop_count_24h >= 2 时 status=warn 并上报 theme_id
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from models.narrative import ActiveTheme, NarrativePriceReaction, NarrativeTheme
from models.news_event import MarketEventSignal

logger = logging.getLogger(__name__)


_TRENDING_DIRECTIONS = ("bullish", "bearish", "potential_reversal")

# 生命周期窗口（秒）
_ACTIVE_WINDOW_SEC = 24 * 3600        # 24h 内有事件 → active
_FADING_WINDOW_SEC = 48 * 3600        # 24-48h → fading


class NarrativeTracker:
    """叙事主题追踪（全局单例）"""

    def __init__(
        self,
        *,
        max_themes: int = 50,
        dormant_threshold_hours: float = 72.0,
        max_reactions_per_theme: int = 20,
    ) -> None:
        self._max_themes = int(max_themes)
        self._dormant_threshold_sec = int(dormant_threshold_hours * 3600)
        self._max_reactions = int(max_reactions_per_theme)
        self._themes: dict[str, NarrativeTheme] = {}
        # 方向历史（最近 N 条）— 用于 flip-flop 判断
        self._direction_history: dict[str, list[str]] = {}
        self._lock = threading.RLock()

    # ── 写入 ──
    def ingest(self, event: MarketEventSignal) -> NarrativeTheme:
        """接收一个 AI 结构化事件，更新对应主题状态。"""
        theme_id = (event.narrative_theme or "").strip() or "_misc"
        now = int(time.time())
        direction = event.direction or "neutral"

        with self._lock:
            theme = self._themes.get(theme_id)
            if theme is None:
                theme = NarrativeTheme(
                    theme_id=theme_id,
                    theme_name_cn=_derive_theme_name(event, theme_id),
                    category=_derive_category(event),
                    first_seen_ts=now,
                    last_seen_ts=now,
                    active=True,
                )
                self._themes[theme_id] = theme
                self._direction_history[theme_id] = []

            # 基础字段
            theme.last_seen_ts = now
            theme.active = True
            theme.event_count_24h = _count_recent_events(theme, now, 86400) + 1
            theme.event_count_7d = _count_recent_events(theme, now, 7 * 86400) + 1
            if not theme.theme_name_cn:
                theme.theme_name_cn = _derive_theme_name(event, theme_id)
            if not theme.category:
                theme.category = _derive_category(event)

            # 最近事件快照
            theme.latest_event_id = event.event_id
            theme.latest_event_summary = event.summary_cn or event.first_order_impact[:40]
            theme.latest_event_direction = direction

            # flip-flop 判断（与最近一条"有方向"的事件对比）
            hist = self._direction_history.setdefault(theme_id, [])
            flipped = False
            if direction in _TRENDING_DIRECTIONS:
                last_directional = next(
                    (d for d in reversed(hist) if d in _TRENDING_DIRECTIONS),
                    None,
                )
                if last_directional and _is_opposite(last_directional, direction):
                    flipped = True
            hist.append(direction)
            if len(hist) > 10:
                del hist[:-10]

            if flipped:
                theme.flip_flop_count_24h += 1
                theme.flip_flop_count_7d += 1

            # 当前方向偏向 = 最新一条方向
            if direction in _TRENDING_DIRECTIONS:
                theme.current_direction_bias = direction

            # current_intensity：近 24h impact_score 绝对值均值（含本条）
            reactions_sample = [abs(r.impact_score) for r in theme.price_reactions[-10:]]
            reactions_sample.append(abs(int(event.impact_score)))
            avg_abs = sum(reactions_sample) / max(1, len(reactions_sample))
            theme.current_intensity = max(0, min(5, int(round(avg_abs))))

            # 趋势状态机
            theme.trend = _compute_trend(theme, now)
            theme.current_stage_summary = _compose_stage_summary(theme)

            # LRU 维护
            self._evict_if_over_capacity_locked(now)

            return theme

    def record_price_reaction(
        self,
        event_id: str,
        reaction: NarrativePriceReaction,
    ) -> bool:
        """回填某事件的价格反应（由 price_reaction_backfill 调用）"""
        if not event_id:
            return False
        with self._lock:
            # 逆序找最近包含该 event_id 的主题
            for theme in self._themes.values():
                if theme.latest_event_id == event_id or any(
                    r.event_id == event_id for r in theme.price_reactions
                ):
                    theme.price_reactions.append(reaction)
                    if len(theme.price_reactions) > self._max_reactions:
                        theme.price_reactions = theme.price_reactions[-self._max_reactions :]
                    _recompute_reaction_stats(theme)
                    return True
            # 找不到 → 尝试按 latest_event_id 单独匹配
            for theme in self._themes.values():
                if theme.latest_event_id == event_id:
                    theme.price_reactions.append(reaction)
                    _recompute_reaction_stats(theme)
                    return True
        return False

    # ── 查询 ──
    def get(self, theme_id: str) -> Optional[NarrativeTheme]:
        with self._lock:
            return self._themes.get(theme_id)

    def get_active(self, limit: int = 10) -> list[NarrativeTheme]:
        now = int(time.time())
        with self._lock:
            active = [
                t for t in self._themes.values()
                if (now - t.last_seen_ts) <= _ACTIVE_WINDOW_SEC and t.active
            ]
        active.sort(key=lambda t: (t.current_intensity, t.event_count_24h), reverse=True)
        return active[: max(1, int(limit))]

    def get_active_briefs(self, limit: int = 10) -> list[ActiveTheme]:
        return [_to_active_brief(t) for t in self.get_active(limit=limit)]

    def get_all_ids(self) -> list[str]:
        with self._lock:
            return list(self._themes.keys())

    def get_recent_direction_by_theme(self, theme_id: str) -> list[str]:
        """返回该主题最近 5 条事件的 direction（用于 flip-flop prompt 输入）"""
        with self._lock:
            hist = self._direction_history.get(theme_id, [])
            return list(hist[-5:])

    # ── 维护 ──
    def decay(self, now_ts: Optional[int] = None) -> int:
        """扫描所有 theme，把超过 dormant_threshold_hours 无新事件的改为 dormant

        同时重新计算 event_count_24h / event_count_7d / flip_flop_count_24h。
        返回：变更数量。
        """
        now = int(now_ts if now_ts is not None else time.time())
        changed = 0
        with self._lock:
            for theme in self._themes.values():
                still_active = (now - theme.last_seen_ts) <= _ACTIVE_WINDOW_SEC
                new_trend = _compute_trend(theme, now)
                if theme.active != still_active or theme.trend != new_trend:
                    changed += 1
                theme.active = still_active
                theme.trend = new_trend
        return changed

    def stats(self) -> dict:
        now = int(time.time())
        with self._lock:
            total = len(self._themes)
            active = sum(1 for t in self._themes.values() if t.active)
            dormant = sum(
                1 for t in self._themes.values()
                if (now - t.last_seen_ts) > self._dormant_threshold_sec
            )
            flip_flop = [t for t in self._themes.values() if t.flip_flop_count_24h > 0]
            worst = max(flip_flop, key=lambda t: t.flip_flop_count_24h, default=None)
        return {
            "total": total,
            "active_count": active,
            "dormant_count": dormant,
            "flip_flop_themes_count": len(flip_flop),
            "worst_flip_flop": (
                {"theme_id": worst.theme_id, "count": worst.flip_flop_count_24h}
                if worst else None
            ),
        }

    # ── 内部 ──
    def _evict_if_over_capacity_locked(self, now: int) -> None:
        if len(self._themes) <= self._max_themes:
            return
        # 按 last_seen_ts 升序淘汰最旧的
        victims = sorted(self._themes.values(), key=lambda t: t.last_seen_ts)
        n_drop = len(self._themes) - self._max_themes
        for v in victims[:n_drop]:
            self._themes.pop(v.theme_id, None)
            self._direction_history.pop(v.theme_id, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TRACKER: Optional[NarrativeTracker] = None
_TRACKER_LOCK = threading.Lock()


def get_narrative_tracker() -> NarrativeTracker:
    global _TRACKER
    if _TRACKER is None:
        with _TRACKER_LOCK:
            if _TRACKER is None:
                _TRACKER = NarrativeTracker()
    return _TRACKER


def reset_narrative_tracker() -> None:
    """测试用 —— 重置单例"""
    global _TRACKER
    with _TRACKER_LOCK:
        _TRACKER = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flip-Flop 判定辅助（便于 news_structurer 提前注入）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def detect_flip_flop(
    theme_id: str,
    new_direction: str,
    recent_directions: list[str],
    lookback: int = 5,
) -> bool:
    """简单规则：近 N 条里 direction 来回切换 ≥2 次。

    - 只统计有方向的条目（bullish/bearish/potential_reversal）
    - neutral 跳过
    - 本次 new_direction 计入末尾
    """
    seq = [d for d in recent_directions if d in _TRENDING_DIRECTIONS][-max(1, lookback) :]
    if new_direction in _TRENDING_DIRECTIONS:
        seq = seq + [new_direction]
    if len(seq) < 2:
        return False
    switches = 0
    for i in range(1, len(seq)):
        if _is_opposite(seq[i - 1], seq[i]):
            switches += 1
            if switches >= 2:
                return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部小工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _is_opposite(a: str, b: str) -> bool:
    pair = {a, b}
    if pair == {"bullish", "bearish"}:
        return True
    if pair == {"bullish", "potential_reversal"}:
        return True
    if pair == {"bearish", "potential_reversal"}:
        return True
    return False


def _count_recent_events(theme: NarrativeTheme, now: int, window_sec: int) -> int:
    """用 price_reactions 无法反推历史事件数；保守返回上次窗口内计数 - 1（避免超界）。

    说明：当前实现仅作"滚动累加"，非严格时间窗口。P1.2 接入 SQLite 后改为精确时间过滤。
    """
    if theme.first_seen_ts <= 0:
        return 0
    elapsed = now - theme.first_seen_ts
    if window_sec <= 0 or elapsed >= window_sec:
        # 窗口外则从 0 起记
        if window_sec == 86400:
            return 0
        # 7d 窗口：若已超 7d，也从 0 起
        return 0
    # 窗口内则沿用现有计数
    return (theme.event_count_24h if window_sec == 86400 else theme.event_count_7d)


def _compute_trend(theme: NarrativeTheme, now: int) -> str:
    gap = now - theme.last_seen_ts
    if gap <= _ACTIVE_WINDOW_SEC:
        if theme.event_count_24h >= 3:
            return "active"
        return "emerging"
    if gap <= _FADING_WINDOW_SEC:
        return "fading"
    return "dormant"


def _compose_stage_summary(theme: NarrativeTheme) -> str:
    parts: list[str] = []
    if theme.flip_flop_count_24h >= 1:
        parts.append(f"24h 反复 {theme.flip_flop_count_24h} 次")
    if theme.current_direction_bias != "neutral":
        bias_cn = {
            "bullish": "偏看多",
            "bearish": "偏看空",
            "potential_reversal": "潜在反转",
        }.get(theme.current_direction_bias, "中性")
        parts.append(bias_cn)
    if theme.current_intensity > 0:
        parts.append(f"强度 {theme.current_intensity}/5")
    return "·".join(parts)


def _recompute_reaction_stats(theme: NarrativeTheme) -> None:
    if not theme.price_reactions:
        theme.avg_abs_reaction_pct = 0.0
        theme.latest_reaction_pct = None
        theme.hit_rate = 0.0
        return

    def _preferred_delta(r: NarrativePriceReaction) -> Optional[float]:
        return (
            r.delta_pct_1h if r.delta_pct_1h is not None
            else (r.delta_pct_2h if r.delta_pct_2h is not None else r.delta_pct_24h)
        )

    absolutes = []
    hits = 0
    total_matched = 0
    for r in theme.price_reactions:
        delta = _preferred_delta(r)
        if delta is not None:
            absolutes.append(abs(delta))
        if r.matched_direction is not None:
            total_matched += 1
            if r.matched_direction:
                hits += 1

    theme.avg_abs_reaction_pct = round(sum(absolutes) / max(1, len(absolutes)), 3) if absolutes else 0.0
    last_delta = _preferred_delta(theme.price_reactions[-1])
    theme.latest_reaction_pct = None if last_delta is None else round(last_delta, 3)
    theme.hit_rate = round(hits / total_matched, 3) if total_matched > 0 else 0.0


def _to_active_brief(t: NarrativeTheme) -> ActiveTheme:
    return ActiveTheme(
        theme_id=t.theme_id,
        theme_name_cn=t.theme_name_cn or t.theme_id,
        category=t.category,
        latest_event_summary=t.latest_event_summary,
        latest_event_ts=t.last_seen_ts,
        flip_flop_count_24h=t.flip_flop_count_24h,
        trend=t.trend,
        current_intensity=t.current_intensity,
        current_direction_bias=t.current_direction_bias,
        avg_abs_reaction_pct=t.avg_abs_reaction_pct,
        hit_rate=t.hit_rate,
    )


def _derive_theme_name(event: MarketEventSignal, theme_id: str) -> str:
    # theme_id 常是 "Middle_East_Iran" 这种英文 → 把下划线换空格作兜底中文名
    if not theme_id:
        return ""
    return theme_id.replace("_", " ")


def _derive_category(event: MarketEventSignal) -> str:
    if event.risk_type == "geopolitical":
        return "geopolitical"
    if event.risk_type in {"regulatory"}:
        return "regulatory"
    if event.risk_type in {"macro_economic"}:
        return "macro_policy"
    if event.risk_type == "technical":
        return "tech"
    target = (event.target or "").lower()
    if target.startswith("sector:"):
        return "sector"
    return "macro_policy" if event.target == "macro" else "asset"
