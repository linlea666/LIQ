"""新闻智能 Agent 编排（D13）

职责：
  - 统一编排 D07 → D08 Layer1/Layer2 → D10/D11 ingest → D09 brief → D04+ backfill
  - 单 tick 可独立调用（tests 友好）；带定时循环适配 engine 主循环
  - 黑天鹅即时触发 brief 重写
  - SignalBus 推送 news_event. / geo_risk. 候选信号（保守策略，action=wait）

设计要点：
  - 所有外部依赖（registry / analyzer / trackers / ledger）都可注入 → 单测零网络
  - tick 内部每步都 try/except，子步失败不阻断后续（防止上游网络抖动打断主循环）
  - 每步 mark DecisionTracker D-code，便于 runtime 追踪
  - 黑天鹅阈值：tier=blackswan 或 geo level_after≥4

调度策略：
  - run_forever：
      fetch tick  ─ 每 fetch_interval_sec（默认 600s）
      backfill   ─ 每 backfill_interval_sec（默认 900s）
      brief      ─ 每 brief_interval_sec（默认 3600s）
      decay      ─ 每 decay_interval_sec（默认 1800s）
    使用统一 asyncio.wait_for + 简单时间片调度，省一个 task 一个 sleep。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from models.news_event import EnrichedNewsEvent, MarketEventSignal, RawNewsItem
# PR-3 · utils.decision_tracker 已下线

logger = logging.getLogger(__name__)


@dataclass
class NewsTickStats:
    """单 tick 统计（便于测试断言 + 日志）"""
    fetched: int = 0
    kept_after_filter: int = 0
    structured: int = 0
    blackswan_hit: bool = False
    signals_pushed_news: int = 0
    signals_pushed_geo: int = 0
    brief_version_after: int = 0
    brief_triggered: bool = False
    backfill_processed: int = 0
    backfill_complete: int = 0
    duration_ms: int = 0
    error: str = ""
    extra: dict = field(default_factory=dict)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单 tick 入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def run_news_tick(
    *,
    current_btc_price: float,
    price_history: Optional[list[dict]] = None,
    do_brief: bool = False,
    brief_trigger: str = "scheduled",
    do_backfill: bool = True,
    do_decay: bool = False,
    target_coins: Optional[list[str]] = None,
    # 依赖注入（为单测 & 主循环复用）
    registry_fetch: Optional[Callable[..., Awaitable[list[RawNewsItem]]]] = None,
    filter_fn: Optional[Callable[..., Any]] = None,
    structurer: Optional[Callable[..., Awaitable[list[MarketEventSignal]]]] = None,
    brief_fn: Optional[Callable[..., Awaitable[Any]]] = None,
    backfill_fn: Optional[Callable[..., Any]] = None,
    analyzer: Optional[Any] = None,
    narrative_tracker: Any = None,
    geo_tracker: Any = None,
    ledger: Any = None,
) -> NewsTickStats:
    """执行一次完整新闻流水线 tick。

    所有依赖默认用全局单例；测试可 inject stub 规避网络/AI。
    """
    stats = NewsTickStats()
    t0 = time.time()
    try:
        # ── 解析依赖 ──
        if registry_fetch is None:
            from sources.news.registry import fetch_all as _fetch
            registry_fetch = _fetch
        if filter_fn is None:
            from processors.news_filter import filter_news_layer1
            filter_fn = filter_news_layer1
        if structurer is None:
            from processors.news_structurer import structure_news_layer2
            structurer = structure_news_layer2
        if brief_fn is None:
            from processors.news_brief import generate_brief
            brief_fn = generate_brief
        if backfill_fn is None:
            from processors.price_reaction_backfill import backfill_price_reactions
            backfill_fn = backfill_price_reactions
        if analyzer is None:
            from ai.news_analyzer import get_news_chat_analyzer
            analyzer = get_news_chat_analyzer()
        if narrative_tracker is None:
            from processors.narrative_tracker import get_narrative_tracker
            narrative_tracker = get_narrative_tracker()
        if geo_tracker is None:
            from processors.geo_risk_tracker import get_geo_risk_tracker
            geo_tracker = get_geo_risk_tracker()
        if ledger is None:
            from processors.news_ledger import get_ledger
            ledger = get_ledger()

        # ── Step 1: 拉取 ──
        items: list[RawNewsItem] = []
        try:
            items = await registry_fetch() or []
        except Exception as e:  # noqa: BLE001
            logger.warning("[D13] fetch_all failed: %s", e)
            stats.extra["fetch_error"] = str(e)[:120]
        stats.fetched = len(items)

        # ── Step 2: 规则层滤波 ──
        kept: list[RawNewsItem] = []
        tier_map: dict[str, Any] = {}
        if items:
            try:
                kept, tier_map, _fstats = filter_fn(items)
            except Exception as e:  # noqa: BLE001
                logger.warning("[D13] filter_news_layer1 failed: %s", e)
                stats.extra["filter_error"] = str(e)[:120]
        stats.kept_after_filter = len(kept)

        # ── Step 3: AI 结构化 ──
        structured: list[MarketEventSignal] = []
        if kept and analyzer is not None and getattr(analyzer, "available", False):
            try:
                # Dict{theme_id: NarrativeTheme} ｜ Dict{theme_id: GeoRiskState}
                active_narr = {
                    t.theme_id: t for t in narrative_tracker.get_active(limit=20)
                } if hasattr(narrative_tracker, "get_active") else {}
                geo_states = {
                    s.theme_id: s for s in geo_tracker.get_all_states()
                } if hasattr(geo_tracker, "get_all_states") else {}

                structured = await structurer(
                    kept,
                    tier_map,
                    active_narratives=active_narr,
                    geo_states=geo_states,
                    current_btc_price=current_btc_price,
                    analyzer=analyzer,
                ) or []
            except Exception as e:  # noqa: BLE001
                logger.warning("[D13] structure_news_layer2 failed: %s", e)
                stats.extra["structurer_error"] = str(e)[:120]
        elif kept:
            logger.debug("[D13] analyzer unavailable; skipping structuring of %d items", len(kept))
        stats.structured = len(structured)

        # ── Step 4: ingest 到 narrative / geo tracker ──
        #   geo ingest 可能返回 GeoRiskEvent；记录黑天鹅 & 用于 signal adapter
        geo_events: list[Any] = []
        for sig in structured:
            try:
                if hasattr(narrative_tracker, "ingest"):
                    narrative_tracker.ingest(sig)
            except Exception:
                logger.debug("[D13] narrative ingest failed", exc_info=True)
            try:
                if hasattr(geo_tracker, "ingest") and getattr(sig, "risk_type", "") == "geopolitical":
                    ge = geo_tracker.ingest(sig)
                    if ge is not None:
                        geo_events.append(ge)
                        if int(getattr(ge, "level_after", 0) or 0) >= 4 or bool(getattr(ge, "is_blackswan", False)):
                            stats.blackswan_hit = True
            except Exception:
                logger.debug("[D13] geo ingest failed", exc_info=True)
            if getattr(sig, "tier", "") == "blackswan":
                stats.blackswan_hit = True

        # ── Step 5: 封装为 EnrichedNewsEvent 并写账本 ──
        #   需按 event_id 对齐 RawNewsItem
        raw_by_id = {r.external_id: r for r in kept}
        enriched: list[EnrichedNewsEvent] = []
        for sig in structured:
            raw = raw_by_id.get(sig.event_id)
            if raw is None:
                continue
            enriched.append(EnrichedNewsEvent(raw=raw, structured=sig))
        added = updated = 0
        if enriched:
            try:
                added, updated = ledger.upsert_many(enriched)
            except Exception:
                logger.debug("[D13] ledger upsert failed", exc_info=True)
        stats.extra["ledger_added"] = added
        stats.extra["ledger_updated"] = updated

        # PR-3 · signal_bus 已下线（数学引擎链路被 Strategic AI 取代），
        # 新闻事件不再投放到 SignalBus，narrative_tracker / geo_tracker
        # 仍保留状态用于宏观慢变量章节。

        # ── Step 7: 回填价格反应 ──
        if do_backfill and price_history:
            try:
                pending = ledger.get_pending_backfill()
                if pending:
                    bf_updated, bf_stats = backfill_fn(
                        pending,
                        price_history,
                        int(time.time()),
                        narrative_tracker=narrative_tracker,
                    )
                    if bf_updated:
                        ledger.replace_many(bf_updated)
                    stats.backfill_processed = int(bf_stats.get("processed", 0) or 0)
                    stats.backfill_complete = int(bf_stats.get("newly_complete", 0) or 0)
            except Exception as e:  # noqa: BLE001
                logger.warning("[D13] price_reaction_backfill failed: %s", e)
                stats.extra["backfill_error"] = str(e)[:120]

        # ── Step 8: Rolling Brief（定期 or 黑天鹅） ──
        trigger_brief = bool(do_brief)
        if stats.blackswan_hit:
            try:
                from config.settings import get_settings
                if get_settings().ai.news_agent.blackswan_rewrite_brief:
                    trigger_brief = True
                    brief_trigger = "blackswan"
            except Exception:
                trigger_brief = True
                brief_trigger = "blackswan"
        if trigger_brief and analyzer is not None and getattr(analyzer, "available", False):
            try:
                from processors.news_brief import get_current_brief, set_current_brief
                prev_brief = get_current_brief()
                events_24h = ledger.get_recent(window_sec=24 * 3600)
                themes_list = narrative_tracker.get_active(limit=20) if hasattr(narrative_tracker, "get_active") else []
                overview = geo_tracker.get_overview() if hasattr(geo_tracker, "get_overview") else None
                if overview is None:
                    # brief_fn 需要 GeoRiskOverview，缺席则构造占位
                    from models.geo_risk import GeoRiskOverview
                    overview = GeoRiskOverview(
                        overall_level=0, overall_label="PEACE", overall_emoji="🟢",
                        overall_summary_cn="", updated_at=int(time.time()),
                    )
                new_brief = await brief_fn(
                    events_24h,
                    themes_list,
                    overview,
                    prev_brief=prev_brief,
                    analyzer=analyzer,
                    trigger=brief_trigger,
                )
                if new_brief is not None:
                    set_current_brief(new_brief)
                    stats.brief_triggered = True
                    stats.brief_version_after = int(getattr(new_brief, "version", 0) or 0)
            except Exception as e:  # noqa: BLE001
                logger.warning("[D13] generate_brief failed: %s", e)
                stats.extra["brief_error"] = str(e)[:120]

        # ── Step 9: decay（衰减）──
        if do_decay:
            try:
                if hasattr(narrative_tracker, "decay"):
                    narrative_tracker.decay()
                if hasattr(geo_tracker, "decay"):
                    geo_tracker.decay()
            except Exception:
                logger.debug("[D13] decay failed", exc_info=True)

        # ── Step 10: 过期清理 + 持久化 ──
        try:
            ledger.prune_expired()
            if hasattr(ledger, "persist_to_disk"):
                ledger.persist_to_disk()
        except Exception:
            logger.debug("[D13] ledger maintenance failed", exc_info=True)

    except Exception as e:  # noqa: BLE001
        logger.error("[D13] news_agent_loop tick top-level failed: %s", e, exc_info=True)
        stats.error = str(e)[:200]

    stats.duration_ms = int((time.time() - t0) * 1000)
    return stats


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 长循环：由 engine.start 托管
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def run_forever(get_context: Callable[[], dict]) -> None:
    """持续运行新闻 Agent。

    get_context() 由 engine 提供，返回：
      {
        "running": bool,
        "current_btc_price": float,
        "price_history": list[dict],
        "target_coins": list[str],
      }
    """
    from config.settings import get_settings
    cfg = get_settings().ai.news_agent
    fetch_interval = max(60, int(cfg.fetch_interval_sec))
    brief_interval = max(300, int(cfg.brief_interval_sec))
    backfill_interval = max(60, int(cfg.backfill_interval_sec))
    decay_interval = max(120, int(cfg.decay_interval_sec))

    last_fetch = 0
    last_brief = 0
    last_backfill = 0
    last_decay = 0

    logger.info(
        "[D13] news_agent_loop started fetch=%ss brief=%ss backfill=%ss decay=%ss",
        fetch_interval, brief_interval, backfill_interval, decay_interval,
    )

    while True:
        try:
            ctx = get_context()
            if not ctx.get("running", True):
                return
            now = int(time.time())
            should_fetch = (now - last_fetch) >= fetch_interval
            if not should_fetch:
                await asyncio.sleep(min(30, fetch_interval))
                continue

            do_brief = (now - last_brief) >= brief_interval
            do_backfill = (now - last_backfill) >= backfill_interval
            do_decay = (now - last_decay) >= decay_interval

            stats = await run_news_tick(
                current_btc_price=float(ctx.get("current_btc_price", 0.0) or 0.0),
                price_history=ctx.get("price_history") or [],
                target_coins=ctx.get("target_coins") or None,
                do_brief=do_brief,
                do_backfill=do_backfill,
                do_decay=do_decay,
            )

            last_fetch = now
            if stats.brief_triggered or do_brief:
                last_brief = now
            if do_backfill:
                last_backfill = now
            if do_decay:
                last_decay = now

            if stats.structured or stats.fetched:
                logger.info(
                    "[D13] tick fetched=%d kept=%d structured=%d news_sig=%d geo_sig=%d "
                    "blackswan=%s brief=v%d backfill=%d/%d ms=%d",
                    stats.fetched, stats.kept_after_filter, stats.structured,
                    stats.signals_pushed_news, stats.signals_pushed_geo,
                    stats.blackswan_hit, stats.brief_version_after,
                    stats.backfill_complete, stats.backfill_processed, stats.duration_ms,
                )
        except asyncio.CancelledError:
            logger.info("[D13] news_agent_loop cancelled")
            raise
        except Exception:  # noqa: BLE001
            logger.warning("[D13] news_agent_loop outer exception", exc_info=True)
            await asyncio.sleep(30)