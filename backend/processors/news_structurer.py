"""新闻 AI 结构化 · Layer 2（D08 Layer 2）

职责：
  - 把 Layer 1 过滤后的 RawNewsItem 通过 LLM（deepseek-v4-flash 非思考模式）转成 MarketEventSignal
  - 采用"三层思维"prompt：翻译 → 解读 → 叙事
  - 支持批量（一次 prompt 处理多条，节省 token）

重点特性（针对地缘事件）：
  - "美伊和谈破裂" / "关闭海峡" 这类"反复拉扯"必须通过 active_narratives 给 flip_flop_warning 提示
  - 同一叙事 24h 内已有反复 → impact_score 自动降档（市场已麻木）
  - 黑天鹅级别（如战争）跳过"已定价"降档

落实日志锚点：
  - D.D08_NEWS_PIPELINE：每次 batch 上报 input / structured / ai_tokens / latency / success_rate
  - D.D12_DS_DUAL_TASK：累计 chat_calls（由 NewsChatAnalyzer 自动上报）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional, Protocol

from ai.news_prompts import (
    NEWS_STRUCTURER_SYSTEM, build_structurer_user_prompt, digest_items_for_prompt,
)
from config.settings import get_settings
from models.common_enums import NewsTier
from models.geo_risk import GeoRiskState
from models.narrative import NarrativeTheme
from models.news_event import AssetImpact, MarketEventSignal, RawNewsItem
from processors.narrative_tracker import detect_flip_flop

logger = logging.getLogger(__name__)


class BaseAIAnalyzer(Protocol):
    """AI 分析器接口（由 ai/news_analyzer.NewsChatAnalyzer 实现）"""

    async def call_chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> tuple[str, dict]: ...


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def structure_news_layer2(
    items: list[RawNewsItem],
    tier_map: dict[str, NewsTier],
    *,
    active_narratives: dict[str, NarrativeTheme],
    geo_states: dict[str, GeoRiskState],
    current_btc_price: float,
    analyzer: BaseAIAnalyzer,
    batch_size: Optional[int] = None,
) -> list[MarketEventSignal]:
    """批量结构化 Layer 1 过滤后的新闻。

    流程：
      1. 按 tier 分桶（blackswan 单条即发；major 3 条/批；normal 5 条/批）
      2. 为每批构造 prompt（注入 active_narratives + geo_states 历史）
      3. 调 analyzer.call_chat → raw_text
      4. 解析 JSON → MarketEventSignal 列表
      5. flip_flop_warning 若未被 AI 判定，由 narrative_tracker 兜底
      6. mark(D08) 上报 success_rate / tokens

    失败策略：
      - 单批失败：该批退化为"纯规则推断"（tier→impact_score 粗估）
      - JSON 解析失败：尝试宽松提取；仍失败则跳过
    """
    if not items:
        _mark_d08_layer2(input_=0, structured=0, tokens=0, latency_ms=0, fallbacks=0)
        return []

    news_cfg = get_settings().ai.news_agent
    batches = _split_into_batches(items, tier_map, news_cfg, forced_batch=batch_size)

    result_map: dict[str, MarketEventSignal] = {}
    total_tokens = 0
    total_latency_ms = 0
    fallbacks = 0
    t0 = time.time()

    # 串行批次（避免瞬间打满小模型 QPS）
    for batch in batches:
        try:
            batch_result, meta = await _run_single_batch(
                batch=batch,
                tier_map=tier_map,
                active_narratives=active_narratives,
                geo_states=geo_states,
                current_btc_price=current_btc_price,
                analyzer=analyzer,
                news_cfg=news_cfg,
            )
            total_tokens += int(meta.get("tokens", 0))
            total_latency_ms += int(meta.get("latency_ms", 0))
            for sig in batch_result:
                result_map[sig.event_id] = sig
        except Exception as e:  # noqa: BLE001
            logger.warning("[D08] batch failed (%d items): %s", len(batch), e, exc_info=False)
            for it in batch:
                tier = tier_map.get(it.external_id, "normal")
                result_map[it.external_id] = _fallback_rule_infer(it, tier)
                fallbacks += 1

    # flip-flop 兜底：若 AI 没标但实际存在反复 → 补
    for sig in result_map.values():
        if sig.flip_flop_warning:
            continue
        if sig.narrative_theme and sig.narrative_theme in active_narratives:
            theme = active_narratives[sig.narrative_theme]
            recent = getattr(theme, "_recent_directions", None) or []
            # 若 active_narratives 里是 NarrativeTheme 且没有 _recent_directions,
            # 则先基于 flip_flop_count_24h 判断（≥1 且本次方向与 bias 相反）
            if detect_flip_flop(sig.narrative_theme, sig.direction, list(recent)):
                sig.flip_flop_warning = True
            elif theme.flip_flop_count_24h >= 1 and sig.direction != theme.current_direction_bias:
                sig.flip_flop_warning = True

    # 按输入顺序返回
    ordered: list[MarketEventSignal] = []
    for it in items:
        sig = result_map.get(it.external_id)
        if sig is not None:
            ordered.append(sig)

    elapsed = int((time.time() - t0) * 1000)
    _mark_d08_layer2(
        input_=len(items),
        structured=len(ordered),
        tokens=total_tokens,
        latency_ms=elapsed,
        fallbacks=fallbacks,
    )
    return ordered


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 批次划分
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _split_into_batches(
    items: list[RawNewsItem],
    tier_map: dict[str, NewsTier],
    news_cfg,
    *,
    forced_batch: Optional[int] = None,
) -> list[list[RawNewsItem]]:
    """
    分桶策略：
      - blackswan: 每批大小 = batch_size_blackswan（默认 1）
      - major: batch_size_major（默认 3）
      - normal/minor: batch_size_normal（默认 5）

    若 forced_batch 指定，则不分 tier，统一按该大小切片。
    """
    if forced_batch and forced_batch > 0:
        chunks: list[list[RawNewsItem]] = []
        for i in range(0, len(items), forced_batch):
            chunks.append(items[i : i + forced_batch])
        return chunks

    buckets: dict[str, list[RawNewsItem]] = {"blackswan": [], "major": [], "normal": [], "minor": []}
    for it in items:
        tier = tier_map.get(it.external_id, "normal")
        if tier not in buckets:
            tier = "normal"
        buckets[tier].append(it)

    sizes = {
        "blackswan": max(1, int(news_cfg.batch_size_blackswan)),
        "major": max(1, int(news_cfg.batch_size_major)),
        "normal": max(1, int(news_cfg.batch_size_normal)),
        "minor": max(1, int(news_cfg.batch_size_normal)),
    }

    chunks: list[list[RawNewsItem]] = []
    for tier, bucket in buckets.items():
        sz = sizes[tier]
        for i in range(0, len(bucket), sz):
            chunks.append(bucket[i : i + sz])
    return chunks


async def _run_single_batch(
    *,
    batch: list[RawNewsItem],
    tier_map: dict[str, NewsTier],
    active_narratives: dict[str, NarrativeTheme],
    geo_states: dict[str, GeoRiskState],
    current_btc_price: float,
    analyzer: BaseAIAnalyzer,
    news_cfg,
) -> tuple[list[MarketEventSignal], dict]:
    digest_items = digest_items_for_prompt(batch)
    # 附带 tier_hint
    for d, it in zip(digest_items, batch):
        d["tier_hint"] = tier_map.get(it.external_id, "normal")

    active_snap = [_narrative_to_prompt_dict(t) for t in list(active_narratives.values())[:12]]
    geo_snap = [_geo_to_prompt_dict(s) for s in list(geo_states.values())[:6]]

    user_prompt = build_structurer_user_prompt(
        items=digest_items,
        active_narratives=active_snap,
        geo_states=geo_snap,
        current_btc_price=float(current_btc_price or 0.0),
    )

    raw, meta = await analyzer.call_chat(
        system_prompt=NEWS_STRUCTURER_SYSTEM,
        user_prompt=user_prompt,
        temperature=news_cfg.temperature,
        max_tokens=int(news_cfg.max_tokens_structurer),
    )
    model_used = str(meta.get("model", "deepseek-v4-flash"))
    parsed = _parse_batch_response(raw, batch, tier_map, model_used=model_used)
    return parsed, meta


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Prompt 输入辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _narrative_to_prompt_dict(theme: NarrativeTheme) -> dict:
    # 避免直接依赖 tracker 内部历史序列，从 latest_event_direction 做粗估
    return {
        "theme_id": theme.theme_id,
        "theme_name_cn": theme.theme_name_cn,
        "recent_directions": [theme.latest_event_direction] if theme.latest_event_direction else [],
        "flip_flop_count_24h": int(theme.flip_flop_count_24h),
        "avg_abs_reaction_pct": float(theme.avg_abs_reaction_pct),
        "current_direction_bias": theme.current_direction_bias,
        "current_intensity": int(theme.current_intensity),
    }


def _geo_to_prompt_dict(state: GeoRiskState) -> dict:
    return {
        "theme_id": state.theme_id,
        "current_level": int(state.current_level),
        "level_label": state.level_label,
        "flip_flop_count_24h": int(state.flip_flop_count_24h),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JSON 解析
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MD_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _parse_batch_response(
    raw_text: str,
    items: list[RawNewsItem],
    tier_map: dict[str, NewsTier],
    *,
    model_used: str = "",
) -> list[MarketEventSignal]:
    """解析 AI 返回的 JSON → MarketEventSignal 列表。

    容错：
      - 模型偶尔会包裹 markdown（```json ... ```）→ 剥离
      - 缺字段 → 使用默认值
      - 条数对不上 → 按 event_id 匹配；缺失的按规则兜底
    """
    obj_list = _extract_json_array(raw_text)
    by_id: dict[str, dict] = {}
    for obj in obj_list:
        if not isinstance(obj, dict):
            continue
        eid = str(obj.get("event_id") or obj.get("external_id") or "").strip()
        if not eid:
            continue
        by_id[eid] = obj

    out: list[MarketEventSignal] = []
    for it in items:
        tier = tier_map.get(it.external_id, "normal")
        obj = by_id.get(it.external_id)
        if obj is None:
            out.append(_fallback_rule_infer(it, tier))
            continue
        try:
            sig = _dict_to_signal(obj, it, tier, model_used=model_used)
            out.append(sig)
        except Exception:  # noqa: BLE001
            logger.debug("[D08] dict_to_signal failed id=%s", it.external_id, exc_info=True)
            out.append(_fallback_rule_infer(it, tier))
    return out


def _extract_json_array(text: str) -> list[Any]:
    """尽最大努力从 text 里抽出 JSON 数组"""
    if not text:
        return []
    s = text.strip()

    # 1) 直接解
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("events"), list):
            return parsed["events"]
    except (json.JSONDecodeError, ValueError):
        pass

    # 2) 去除 markdown 围栏
    m = _MD_FENCE_RE.search(s)
    if m:
        candidate = m.group(1).strip()
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and isinstance(parsed.get("events"), list):
                return parsed["events"]
        except (json.JSONDecodeError, ValueError):
            pass

    # 3) 截取第一个 [ ... ] 片段
    lb = s.find("[")
    rb = s.rfind("]")
    if lb >= 0 and rb > lb:
        try:
            parsed = json.loads(s[lb : rb + 1])
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    logger.debug("[D08] cannot extract JSON array from text (%d chars)", len(s))
    return []


def _dict_to_signal(
    obj: dict,
    item: RawNewsItem,
    tier: NewsTier,
    *,
    model_used: str,
) -> MarketEventSignal:
    now = int(time.time())
    publish_sec = (item.publish_time // 1000) if item.publish_time > 0 else now
    horizon = str(obj.get("horizon") or "short").lower()
    if horizon not in {"immediate", "short", "medium", "lasting"}:
        horizon = "short"
    horizon_seconds = {
        "immediate": 3600, "short": 24 * 3600,
        "medium": 7 * 24 * 3600, "lasting": 30 * 24 * 3600,
    }[horizon]

    direction = str(obj.get("direction") or "neutral").lower()
    if direction not in {"bullish", "bearish", "neutral", "potential_reversal"}:
        direction = "neutral"

    stage = str(obj.get("narrative_stage") or "new").lower()
    if stage not in {"new", "continuing", "reversal", "fading", "escalation", "de-escalation"}:
        stage = "new"

    risk_type = str(obj.get("risk_type") or "none").lower()
    if risk_type not in {"none", "geopolitical", "regulatory", "technical", "macro_economic", "black_swan"}:
        risk_type = "none"

    impact_on_assets: list[AssetImpact] = []
    for a in obj.get("impact_on_assets") or []:
        if not isinstance(a, dict):
            continue
        try:
            impact_on_assets.append(AssetImpact(
                asset=str(a.get("asset", ""))[:20] or "BTC",
                direction=_safe_direction(a.get("direction", "neutral")),
                magnitude=str(a.get("magnitude") or "low").lower(),
            ))
        except Exception:  # noqa: BLE001
            continue

    raw_tier = str(obj.get("tier") or tier).lower()
    if raw_tier not in {"blackswan", "major", "normal", "minor"}:
        raw_tier = tier

    return MarketEventSignal(
        event_id=item.external_id,
        ts=publish_sec,
        expires_at=publish_sec + horizon_seconds,
        target=str(obj.get("target") or "noise")[:20],
        direction=direction,
        first_order_impact=str(obj.get("first_order_impact") or "")[:80],
        second_order_impact=str(obj.get("second_order_impact") or "")[:80],
        impact_score=int(_clamp(obj.get("impact_score"), -5, 5)),
        confidence=float(_clamp(obj.get("confidence", 0.5), 0.0, 1.0)),
        source_credibility=float(_clamp(obj.get("source_credibility", item.source_reliability), 0.0, 1.0)),
        horizon=horizon,
        narrative_theme=str(obj.get("narrative_theme") or "")[:60],
        narrative_stage=stage,
        flip_flop_warning=bool(obj.get("flip_flop_warning")),
        already_priced_in_pct=float(_clamp(obj.get("already_priced_in_pct", 0.0), 0.0, 100.0)),
        risk_type=risk_type,
        impact_on_assets=impact_on_assets,
        rationale_cn=str(obj.get("rationale_cn") or "")[:120],
        summary_cn=str(obj.get("summary_cn") or item.title)[:40],
        trading_insight=str(obj.get("trading_insight") or "")[:80],
        tier=raw_tier,
        processed_by="ai",
        processed_at=now,
        model_used=model_used,
    )


def _safe_direction(v: Any) -> str:
    v = str(v or "neutral").lower()
    if v not in {"bullish", "bearish", "neutral", "potential_reversal"}:
        return "neutral"
    return v


def _clamp(v: Any, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return lo
    if f != f:  # NaN
        return lo
    return max(lo, min(hi, f))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 规则兜底
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _fallback_rule_infer(
    item: RawNewsItem,
    tier: NewsTier,
) -> MarketEventSignal:
    """AI 失败时的规则兜底（极简）

    - blackswan: direction=bearish, impact_score=-4
    - major: direction=neutral, impact_score 按标题情绪粗估 ±2
    - normal/minor: direction=neutral, impact_score=0
    """
    now = int(time.time())
    publish_sec = (item.publish_time // 1000) if item.publish_time > 0 else now

    direction = "neutral"
    impact = 0
    risk_type = "none"

    if tier == "blackswan":
        direction = "bearish"
        impact = -4
        risk_type = "black_swan"
    elif tier == "major":
        impact = _sentiment_score_from_title(item.title or item.content)
        if impact > 0:
            direction = "bullish"
        elif impact < 0:
            direction = "bearish"
        else:
            direction = "neutral"
        # 地缘关键词强制 geopolitical
        low = (item.title + item.content).lower()
        if any(k in low for k in ("war", "ceasefire", "sanction", "战争", "停火", "制裁", "空袭")):
            risk_type = "geopolitical"

    return MarketEventSignal(
        event_id=item.external_id,
        ts=publish_sec,
        expires_at=publish_sec + 24 * 3600,
        target="noise",
        direction=direction,
        first_order_impact=(item.title or "")[:40],
        impact_score=impact,
        confidence=0.3,
        source_credibility=float(item.source_reliability),
        horizon="short",
        narrative_theme="",
        narrative_stage="new",
        flip_flop_warning=False,
        already_priced_in_pct=0.0,
        risk_type=risk_type,
        impact_on_assets=[],
        rationale_cn="AI 未覆盖，规则兜底",
        summary_cn=(item.title or "")[:20],
        trading_insight="",
        tier=tier,
        processed_by="rule",
        processed_at=now,
        model_used="",
    )


def _sentiment_score_from_title(text: str) -> int:
    if not text:
        return 0
    t = text.lower()
    pos = ["利好", "批准", "通过", "降息", "合作", "ceasefire", "resumed", "breakthrough", "approved"]
    neg = ["暴跌", "崩盘", "拒绝", "加息", "war", "制裁", "sanctions", "rejected", "hack", "hacked", "halt"]
    score = 0
    if any(p in t for p in pos):
        score += 2
    if any(n in t for n in neg):
        score -= 2
    return score


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 日志落实
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mark_d08_layer2(
    *, input_: int, structured: int, tokens: int, latency_ms: int, fallbacks: int,
) -> None:
    """PR-3 · decision_tracker 已下线，本函数保留壳避免上游调用失败。"""
    return
