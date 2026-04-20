"""滚动新闻简报生成器 · Layer 3c（D09）

职责：
  - 维护一份"24h 新闻简报"，作为主 AI 模块的"记忆锚"
  - 每小时或黑天鹅触发重写
  - 供 ai/snapshot.py 在组装 AI prompt 时注入（而不是塞入原始新闻）

关键特性：
  - AI 维护式增量更新（非全量重写）：把旧 bullets + 新事件一起喂给 AI，让它决定保/删/替换
  - 黑天鹅事件立刻触发重写（不等待定时）
  - char_count 上限 3000（约 token 1200），控制主 AI prompt 占用

落实日志锚点：
  - D.D09_NEWS_BRIEF：每次生成上报 version / char_count / based_on_events / update_trigger
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import threading
import time
from typing import Any, Optional, Protocol

from ai.news_prompts import (
    NEWS_BRIEF_SYSTEM, build_brief_full_user_prompt,
    build_brief_incremental_user_prompt, build_brief_shrink_user_prompt,
)
from config.settings import get_settings
from models.geo_risk import GeoRiskOverview
from models.narrative import NarrativeTheme
from models.news_brief import BriefTrackedTheme, NewsBrief, NewsBriefSection
from models.news_event import EnrichedNewsEvent

logger = logging.getLogger(__name__)


class BaseAIAnalyzer(Protocol):
    """AI 分析器接口（同 news_structurer.BaseAIAnalyzer）"""

    async def call_chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1500,
    ) -> tuple[str, dict]: ...


_SECTION_ORDER = ["macro", "regulatory", "onchain", "risk"]
_SECTION_TITLE_CN = {
    "macro": "宏观",
    "regulatory": "监管",
    "onchain": "链上",
    "risk": "风险",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def generate_brief(
    events_24h: list[EnrichedNewsEvent],
    themes: list[NarrativeTheme],
    geo_overview: GeoRiskOverview,
    *,
    prev_brief: Optional[NewsBrief] = None,
    analyzer: BaseAIAnalyzer,
    trigger: str = "scheduled",  # "scheduled" / "blackswan" / "user"
    max_chars: int = 3000,
) -> NewsBrief:
    """生成/更新滚动简报。

    流程：
      1. 若 prev_brief 存在且 trigger='scheduled'：
         → 增量 prompt（旧简报 + 仅本周期新增事件）
      2. 若 trigger='blackswan' 或首次生成：
         → 全量 prompt（从全部 events 开始）
      3. 调 analyzer.call_chat → JSON → NewsBrief
      4. 校验 char_count <= max_chars，超出则二次调用让 AI 精简
      5. diff_from_prev_version 字段填充（对比旧版本）
      6. mark(D09) 上报
    """
    trigger = trigger if trigger in {"scheduled", "blackswan", "user"} else "scheduled"
    now = int(time.time())
    news_cfg = get_settings().ai.news_agent
    max_tokens = int(news_cfg.max_tokens_brief)

    # ─────────────────────────────────────────────────────────────
    # P0-3 · events=0 严格熔断：没有任何可用事件时，绝不调 AI 产出简报。
    #   旧行为：即使 events=[] 也发 prompt，AI 可能基于 themes/geo 编造
    #   伪"新闻"进入主 AI prompt → 决策污染。
    #   新行为：直接构造一条空简报（based_on_events_count=0），
    #   并通过 snapshot 侧的过滤阻止注入主 AI prompt。
    # ─────────────────────────────────────────────────────────────
    if not events_24h:
        logger.info(
            "[D09] circuit-break · events=0 · skip AI brief generation "
            "(trigger=%s · prev_version=%s)",
            trigger, (prev_brief.version if prev_brief else 0),
        )
        empty = _empty_brief_no_events(prev_brief, trigger, now)
        prev_version = prev_brief.version if prev_brief else 0
        empty.version = prev_version + 1
        empty.prev_version_updated_at = prev_brief.updated_at if prev_brief else None
        empty.update_trigger = trigger
        empty.based_on_events_count = 0
        empty.char_count = _text_len(empty)
        empty.token_estimate = max(1, int(empty.char_count / 2.5))
        empty.diff_from_prev_version = _diff_briefs(prev_brief, empty) if prev_brief else ""
        _mark_d09(empty)
        return empty

    is_incremental = (
        prev_brief is not None
        and trigger == "scheduled"
        and prev_brief.version > 0
    )

    if is_incremental:
        new_events = _select_new_events(events_24h, prev_brief)
        user_prompt = build_brief_incremental_user_prompt(
            prev_brief=prev_brief.model_dump() if prev_brief else {},
            new_events=[_event_to_prompt_dict(e) for e in new_events],
            themes=[_theme_to_prompt_dict(t) for t in themes],
            geo_overview=geo_overview.model_dump(),
            max_chars=max_chars,
        )
    else:
        user_prompt = build_brief_full_user_prompt(
            events=[_event_to_prompt_dict(e) for e in events_24h],
            themes=[_theme_to_prompt_dict(t) for t in themes],
            geo_overview=geo_overview.model_dump(),
            max_chars=max_chars,
        )

    # ── AI 调用 ──
    t0 = time.time()
    model_used = "deepseek-chat"
    try:
        raw_text, meta = await analyzer.call_chat(
            system_prompt=NEWS_BRIEF_SYSTEM,
            user_prompt=user_prompt,
            temperature=news_cfg.temperature,
            max_tokens=max_tokens,
        )
        model_used = str(meta.get("model", model_used))

        # 解析 + 超限精简
        brief = _parse_brief_response(
            raw_text,
            base_events_count=len(events_24h),
            trigger=trigger,
            model_used=model_used,
            now=now,
            coverage_start=_coverage_start(events_24h),
        )

        if _text_len(brief) > max_chars:
            shrink_prompt = build_brief_shrink_user_prompt(_brief_to_json(brief), max_chars)
            try:
                raw2, meta2 = await analyzer.call_chat(
                    system_prompt=NEWS_BRIEF_SYSTEM,
                    user_prompt=shrink_prompt,
                    temperature=0.0,
                    max_tokens=max_tokens,
                )
                brief2 = _parse_brief_response(
                    raw2,
                    base_events_count=len(events_24h),
                    trigger=trigger,
                    model_used=str(meta2.get("model", model_used)),
                    now=now,
                    coverage_start=_coverage_start(events_24h),
                )
                if _text_len(brief2) < _text_len(brief):
                    brief = brief2
            except Exception as e:  # noqa: BLE001
                logger.debug("[D09] shrink pass failed: %s", e)

    except Exception as e:  # noqa: BLE001
        logger.warning("[D09] analyzer call failed: %s", e, exc_info=False)
        # 兜底：回退到 prev_brief（保留 version / 注明 generation_failed）
        brief = _fallback_brief(prev_brief, events_24h, trigger, now)

    # 版本推进 + diff
    prev_version = prev_brief.version if prev_brief else 0
    brief.version = prev_version + 1
    brief.prev_version_updated_at = prev_brief.updated_at if prev_brief else None
    brief.update_trigger = trigger
    brief.based_on_events_count = len(events_24h)
    brief.generation_cost_ms = int((time.time() - t0) * 1000)
    brief.char_count = _text_len(brief)
    brief.token_estimate = max(1, int(brief.char_count / 2.5))
    brief.diff_from_prev_version = _diff_briefs(prev_brief, brief) if prev_brief else ""

    _mark_d09(brief)
    return brief


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助：事件 / 主题 → prompt dict
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _event_to_prompt_dict(e: EnrichedNewsEvent) -> dict:
    """从 EnrichedNewsEvent 抽取精简字段（prompt 用）"""
    s = e.structured
    return {
        "event_id": s.event_id,
        "ts": s.ts,
        "tier": s.tier,
        "risk_type": s.risk_type,
        "direction": s.direction,
        "impact_score": s.impact_score,
        "narrative_theme": s.narrative_theme,
        "summary_cn": s.summary_cn,
        "first_order_impact": s.first_order_impact,
        "impact_on_assets": [a.model_dump() for a in s.impact_on_assets],
    }


def _theme_to_prompt_dict(t: NarrativeTheme) -> dict:
    return {
        "theme_id": t.theme_id,
        "theme_name_cn": t.theme_name_cn,
        "current_direction_bias": t.current_direction_bias,
        "current_intensity": int(t.current_intensity),
        "flip_flop_count_24h": int(t.flip_flop_count_24h),
        "hit_rate": float(t.hit_rate),
        "trend": t.trend,
    }


def _select_new_events(
    events_24h: list[EnrichedNewsEvent],
    prev_brief: NewsBrief,
) -> list[EnrichedNewsEvent]:
    """选出自 prev_brief.updated_at 以后的新事件"""
    cutoff = int(prev_brief.updated_at or 0)
    if cutoff <= 0:
        return list(events_24h)
    return [e for e in events_24h if int(e.structured.ts or 0) > cutoff]


def _coverage_start(events: list[EnrichedNewsEvent]) -> int:
    if not events:
        return int(time.time()) - 86400
    return min(e.structured.ts for e in events if e.structured.ts > 0) or (int(time.time()) - 86400)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JSON 解析
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MD_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _parse_brief_response(
    raw_text: str,
    base_events_count: int,
    trigger: str,
    *,
    model_used: str = "deepseek-chat",
    now: Optional[int] = None,
    coverage_start: Optional[int] = None,
) -> NewsBrief:
    """解析 AI 响应 → NewsBrief"""
    now = int(now if now is not None else time.time())
    obj = _extract_json_object(raw_text)

    sections = _normalize_sections(obj.get("sections") if obj else None)
    tracked = _normalize_themes(obj.get("tracked_themes") if obj else None)
    tldr = str((obj or {}).get("tldr_cn") or "")[:80]

    brief = NewsBrief(
        version=0,  # 外层赋值
        updated_at=now,
        ts_range_start=int(coverage_start or (now - 86400)),
        ts_range_end=now,
        coverage_hours=24.0,
        sections=sections,
        tldr_cn=tldr,
        tracked_themes=tracked,
        char_count=0,         # 外层重算
        token_estimate=0,
        update_trigger=trigger if trigger in {"scheduled", "blackswan", "user"} else "scheduled",
        based_on_events_count=base_events_count,
        model_used=model_used,
        generation_cost_ms=0,
    )
    return brief


def _normalize_sections(raw: Any) -> list[NewsBriefSection]:
    """确保 4 个板块按固定顺序都有，缺失用空板块占位"""
    by_id: dict[str, NewsBriefSection] = {}
    if isinstance(raw, list):
        for s in raw:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("section_id") or "").lower()
            if sid not in _SECTION_ORDER:
                continue
            bullets_raw = s.get("bullets") or []
            bullets = [str(b)[:60] for b in bullets_raw if b][:5]
            by_id[sid] = NewsBriefSection(
                section_id=sid,  # type: ignore[arg-type]
                section_title_cn=str(s.get("section_title_cn") or _SECTION_TITLE_CN[sid]),
                bullets=bullets,
                max_bullets=5,
                last_rewritten_ts=int(time.time()),
            )

    out: list[NewsBriefSection] = []
    for sid in _SECTION_ORDER:
        if sid in by_id:
            out.append(by_id[sid])
        else:
            out.append(NewsBriefSection(
                section_id=sid,  # type: ignore[arg-type]
                section_title_cn=_SECTION_TITLE_CN[sid],
                bullets=[],
                max_bullets=5,
                last_rewritten_ts=int(time.time()),
            ))
    return out


def _normalize_themes(raw: Any) -> list[BriefTrackedTheme]:
    out: list[BriefTrackedTheme] = []
    if isinstance(raw, list):
        for t in raw:
            if not isinstance(t, dict):
                continue
            theme_id = str(t.get("theme_id") or "").strip()
            if not theme_id:
                continue
            try:
                out.append(BriefTrackedTheme(
                    theme_id=theme_id[:60],
                    theme_name_cn=str(t.get("theme_name_cn") or theme_id)[:40],
                    current_stance_cn=str(t.get("current_stance_cn") or "")[:60],
                    flip_flop_count_24h=int(t.get("flip_flop_count_24h") or 0),
                    latest_update_ts=int(t.get("latest_update_ts") or time.time()),
                    relevance_score=max(0.0, min(1.0, float(t.get("relevance_score") or 0.5))),
                ))
            except Exception:  # noqa: BLE001
                continue
    return out[:12]


def _extract_json_object(text: str) -> dict:
    if not text:
        return {}
    s = text.strip()

    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except (json.JSONDecodeError, ValueError):
        pass

    m = _MD_FENCE_RE.search(s)
    if m:
        cand = m.group(1).strip()
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                return v
        except (json.JSONDecodeError, ValueError):
            pass

    lb = s.find("{")
    rb = s.rfind("}")
    if lb >= 0 and rb > lb:
        try:
            v = json.loads(s[lb : rb + 1])
            if isinstance(v, dict):
                return v
        except (json.JSONDecodeError, ValueError):
            pass

    return {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 兜底 / 诊断
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _empty_brief_no_events(
    prev: Optional[NewsBrief],
    trigger: str,
    now: int,
) -> NewsBrief:
    """P0-3 · events=0 熔断专用空简报。

    关键差异（与 _fallback_brief 区别）：
      - _fallback_brief 用于"AI 调用失败"，会保留 prev 内容 → 不适合"根本没有事件"场景
      - _empty_brief_no_events 明确返回空 sections，告诉下游"本轮无可信数据源"
        这样主 AI prompt 层可以据此完全跳过新闻板块
    """
    return NewsBrief(
        version=0,  # 外层 +1
        updated_at=now,
        ts_range_start=now - 86400,
        ts_range_end=now,
        coverage_hours=24.0,
        sections=_normalize_sections(None),  # 4 个空 section 占位
        tldr_cn="",  # 明确留空，禁止任何兜底文本
        tracked_themes=[],
        char_count=0,
        token_estimate=0,
        update_trigger=trigger,
        based_on_events_count=0,
        model_used="skipped_no_events",
        generation_cost_ms=0,
    )


def _fallback_brief(
    prev: Optional[NewsBrief],
    events: list[EnrichedNewsEvent],
    trigger: str,
    now: int,
) -> NewsBrief:
    if prev is not None:
        # 克隆 prev 保持 version 稳定（外层仍会 +1）
        b = prev.model_copy(deep=True)
        b.updated_at = now
        b.tldr_cn = (b.tldr_cn or "") + "（AI 失败·保留上一版本）"
        b.based_on_events_count = len(events)
        b.model_used = "fallback"
        return b
    # 首次失败 → 空简报
    return NewsBrief(
        version=0,
        updated_at=now,
        ts_range_start=_coverage_start(events),
        ts_range_end=now,
        coverage_hours=24.0,
        sections=_normalize_sections(None),
        tldr_cn="新闻简报尚未就绪（AI 首次生成失败）",
        tracked_themes=[],
        char_count=0,
        token_estimate=0,
        update_trigger=trigger,
        based_on_events_count=len(events),
        model_used="fallback",
        generation_cost_ms=0,
    )


def _brief_to_json(b: NewsBrief) -> str:
    return json.dumps(
        {
            "tldr_cn": b.tldr_cn,
            "sections": [s.model_dump() for s in b.sections],
            "tracked_themes": [t.model_dump() for t in b.tracked_themes],
        },
        ensure_ascii=False,
    )


def _text_len(b: NewsBrief) -> int:
    return len(_brief_to_json(b))


def _diff_briefs(old: Optional[NewsBrief], new: NewsBrief) -> str:
    """生成 diff_from_prev_version 字段（便于日志/审计）"""
    if old is None:
        return ""
    try:
        old_lines = _bullet_lines(old)
        new_lines = _bullet_lines(new)
        diff = difflib.unified_diff(old_lines, new_lines, lineterm="", n=0)
        # 取前 12 行摘要（ +/- ）避免日志膨胀
        rows: list[str] = []
        for line in diff:
            if line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
                continue
            if len(rows) >= 12:
                rows.append("…")
                break
            rows.append(line[:120])
        return "\n".join(rows)
    except Exception:  # noqa: BLE001
        return ""


def _bullet_lines(b: NewsBrief) -> list[str]:
    lines: list[str] = [f"tldr: {b.tldr_cn}"]
    for s in b.sections:
        for blt in s.bullets:
            lines.append(f"[{s.section_id}] {blt}")
    for t in b.tracked_themes:
        lines.append(f"[theme] {t.theme_id}: {t.current_stance_cn}")
    return lines


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 简报存取（模块级单例）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_CURRENT: Optional[NewsBrief] = None
_CURRENT_LOCK = threading.Lock()


def get_current_brief() -> Optional[NewsBrief]:
    with _CURRENT_LOCK:
        return _CURRENT


def set_current_brief(brief: NewsBrief) -> None:
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = brief


def reset_current_brief() -> None:
    """测试用"""
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Decision Tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _derive_d09_status(brief: NewsBrief, bullet_total: int) -> tuple[str, str]:
    """根据 brief 状态派生 D09 的 (status, note)。

    语义区分（P0-3 熔断后新增，避免"上游空 = 系统故障"的误报）：
      - skipped_no_events  → ok / note=upstream_empty
        熔断是健康的保护行为，禁止挂 warn（避免告警疲劳）
      - fallback           → warn / note=ai_call_failed
        AI 真的挂了，这才是真正需要关注的 warn
      - 正常生成（bullets>0 或 tldr 非空） → ok / note=""
      - 其他未知情况       → warn / note=unexpected_empty
    """
    model = (brief.model_used or "").strip()
    if model == "skipped_no_events":
        return "ok", "upstream_empty"
    if model == "fallback":
        return "warn", "ai_call_failed"
    if bullet_total > 0 or brief.tldr_cn:
        return "ok", ""
    return "warn", "unexpected_empty"


def _mark_d09(brief: NewsBrief) -> None:
    try:
        from utils.decision_tracker import D, get_tracker
        bullet_total = sum(len(s.bullets) for s in brief.sections)
        status, note = _derive_d09_status(brief, bullet_total)
        get_tracker().mark(
            D.D09_NEWS_BRIEF,
            status=status,
            log=True,
            version=brief.version,
            char_count=brief.char_count,
            based_on_events=brief.based_on_events_count,
            update_trigger=brief.update_trigger,
            bullet_total=bullet_total,
            tracked_themes=len(brief.tracked_themes),
            generation_cost_ms=brief.generation_cost_ms,
            model_used=brief.model_used,
            note=note,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[D09] mark failed", exc_info=True)
