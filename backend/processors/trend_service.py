"""BTC 原生趋势与资金流服务：采集、计算、状态机、事件、outbox。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from typing import Any, Awaitable, Callable, Optional

from models.trend_monitor import (
    ClosedFlowHistory, DataQuality, ExchangeTransferFlowSnapshot, FootprintStatus,
    ModifierBreakdown, TrendMachineContext, TrendSnapshot,
)
from models.hyperliquid_whale import (
    HyperliquidWhaleDistributions,
    pending_hyperliquid_whale_distributions,
)
from notifications.email_alert import send_html_email
from notifications.trend_alert import build_events, render_email
from processors.trend_monitor import (
    build_closed_flow_histories, build_funding_snapshot, calculate_core_direction,
    calculate_timeframe, interpret_flow_behavior, interpret_flow_exhaustion_watch,
    parse_active_flow, parse_closed_klines, parse_cvd_deltas, parse_etf_flow,
    parse_exchange_transfer_flow, parse_oi, parse_wallet_flow,
)
from processors.hyperliquid_whale_stats import (
    build_hyperliquid_whale_distributions,
    refreshed_distribution_quality,
)
from sources.funding_official import fetch_official_pair
from storage.trend_store import TrendStore

logger = logging.getLogger(__name__)


class TrendService:
    """独立只读服务。不会写入交易模块或产出交易参数。"""

    _AUX_MAX_AGE = {
        "spot_net": 900, "fut_net": 900, "fund_hist": 7200,
        "wallet_list": 8 * 3600, "wallet_chart": 8 * 3600,
        "etf": 2 * 3600, "footprint": 1800, "looknode_flow": 8 * 3600,
    }

    def __init__(self, *, coinglass, binance, settings, looknode=None,
                 push_callback: Optional[Callable[[str, dict], Awaitable[None]]] = None):
        self._cg = coinglass
        self._bn = binance
        self._looknode = looknode
        self._looknode_cfg = getattr(settings, "looknode", None)
        self._cfg = settings.trend_monitor
        self._email_cfg = settings.notifications.email
        data_dir = self._cfg.data_dir
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), data_dir)
        self._store = TrendStore(data_dir)
        self._push = push_callback
        from ai.trend_reviewer import TrendAIReviewer
        self._ai_reviewer = (
            TrendAIReviewer(settings.ai)
            if getattr(self._cfg, "ai_review_enabled", True) and hasattr(settings, "ai")
            else None
        )
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._outbox_task: Optional[asyncio.Task] = None
        self._aux_task: Optional[asyncio.Task] = None
        self._aux_data: dict[str, dict[str, Any]] = {}
        self._aux_bootstrapped = False
        self._flow_histories: dict[str, ClosedFlowHistory] = {}
        # 落盘回载最近一次巨鲸快照：重启后立即可用（超过30分钟会按 stale 标注），
        # 消除"重启后 pending 直到下轮辅助刷新"的空窗。
        self._whale_snapshot_path = os.path.join(data_dir, "hl_whale_snapshot.json")
        self._whale_distributions = (
            self._load_whale_snapshot() or pending_hyperliquid_whale_distributions()
        )
        self._latest = self._store.latest_snapshot()
        persisted = self._store.load_machine_context()
        if persisted and persisted.algorithm_version != self._cfg.algorithm_version:
            persisted = None
        self._machine = persisted or TrendMachineContext()
        self._machine.algorithm_version = self._cfg.algorithm_version
        if not persisted and self._latest:
            self._machine.confirmation_direction = (
                self._latest.direction if self._latest.direction in ("bullish", "bearish") else None
            )
            self._machine.confirmation_count = self._latest.consecutive_core_confirmations
            self._machine.last_counted_bar = self._latest.closed_5m_ts
            if self._latest.state in ("bullish_confirmed", "bearish_confirmed", "reversal_confirmed"):
                self._machine.confirmed_direction = self._latest.direction

    @property
    def store(self) -> TrendStore:
        return self._store

    def latest(self) -> Optional[TrendSnapshot]:
        return self._latest

    def flow_history(self, window: str, limit: int) -> ClosedFlowHistory:
        history = self._flow_histories.get(window)
        if history is None:
            return ClosedFlowHistory(
                window=window,
                quality=DataQuality(
                    valid=False, status="pending", reason="等待首轮闭合资金流历史构建",
                ),
            )
        result = history.model_copy(deep=True)
        result.items = result.items[-limit:]
        result.quality.points = len(result.items)
        return result

    def hyperliquid_whale_distributions(self) -> HyperliquidWhaleDistributions:
        """只返回内存聚合结果；API 读取不会触发上游请求。"""
        return refreshed_distribution_quality(self._whale_distributions)

    def _load_whale_snapshot(self) -> Optional[HyperliquidWhaleDistributions]:
        try:
            with open(self._whale_snapshot_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            positions = payload.get("positions")
            fetched_at = payload.get("fetched_at_ts")
            if not isinstance(positions, list) or not isinstance(fetched_at, int):
                return None
            return build_hyperliquid_whale_distributions(
                positions, fetched_at_ts=fetched_at,
            )
        except FileNotFoundError:
            return None
        except Exception as error:  # noqa: BLE001 快照损坏时回退 pending，不影响启动
            logger.warning("whale snapshot load failed | error=%s", error)
            return None

    def _persist_whale_snapshot(self, positions: list, fetched_at: int) -> None:
        try:
            tmp_path = f"{self._whale_snapshot_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"fetched_at_ts": fetched_at, "positions": positions},
                    handle, ensure_ascii=False,
                )
            os.replace(tmp_path, self._whale_snapshot_path)
        except Exception as error:  # noqa: BLE001 落盘失败只损失回载能力，不影响内存结果
            logger.warning("whale snapshot persist failed | error=%s", error)

    def source_diagnostics(self) -> dict[str, Any]:
        diagnostics = self._cg.request_diagnostics()
        if self._looknode is not None:
            diagnostics = {
                **diagnostics,
                "looknode": {
                    **self._looknode.health().model_dump(),
                    "last_error": self._looknode.last_error,
                },
            }
        return diagnostics

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running or not self._cfg.enabled:
            return
        self._running = True
        self._aux_task = asyncio.create_task(
            self._refresh_auxiliary_data(), name="btc-trend-auxiliary-refresh",
        )
        self._task = asyncio.create_task(self._run(), name="btc-trend-monitor")
        self._outbox_task = asyncio.create_task(self._run_outbox(), name="btc-trend-outbox")
        logger.info("BTC trend monitor started | interval=%ds", self._cfg.evaluation_interval_sec)

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._outbox_task, self._aux_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self._task, self._outbox_task, self._aux_task) if t),
                             return_exceptions=True)
        self._store.close()
        logger.info("BTC trend monitor stopped")

    async def _run(self) -> None:
        while self._running:
            started = time.monotonic()
            try:
                await self.evaluate_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BTC trend evaluation failed")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(1.0, self._cfg.evaluation_interval_sec - elapsed))

    async def _run_outbox(self) -> None:
        while self._running:
            try:
                if self._cfg.email_enabled and self._email_cfg.enabled:
                    for item in self._store.claim_due_outbox():
                        try:
                            ok = await send_html_email(
                                item["subject"], item["html"], self._email_cfg,
                                log_context="BTC-trend", idempotency_key=item["dedup_key"],
                            )
                            if ok:
                                self._store.mark_outbox_sent(item["id"])
                            else:
                                self._store.mark_outbox_failed(
                                    item["id"], item["attempts"] + 1, "SMTP send returned false",
                                )
                        except Exception as exc:
                            self._store.mark_outbox_failed(
                                item["id"], item["attempts"] + 1, repr(exc),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("BTC trend outbox worker failed")
            await asyncio.sleep(15)

    async def evaluate_once(self) -> TrendSnapshot:
        now = int(time.time())
        closed_5m_ts = (now // 300) * 300 - 300

        # 首屏只等待 S 级核心和非 CoinGlass 官方源。钱包/ETF/NetFlow/Footprint
        # 在独立 P1/P2 后台刷新，慢接口绝不阻塞 P0 趋势快照。
        jobs = {
            "k15": self._bn.fetch_klines("BTCUSDT", "15m", 200),
            "k1h": self._bn.fetch_klines("BTCUSDT", "1h", 200),
            "k4h": self._bn.fetch_klines("BTCUSDT", "4h", 200),
            "k1d": self._bn.fetch_klines("BTCUSDT", "1d", 200),
            "spot5": self._cg.fetch_spot_aggregated_cvd("BTC", "5m", 100),
            "fut5": self._cg.fetch_aggregated_cvd_history("BTC", "5m", 100),
            "spot1h": self._cg.fetch_spot_aggregated_cvd("BTC", "1h", 720),
            "fut1h": self._cg.fetch_aggregated_cvd_history("BTC", "1h", 720),
            # 与原模块参数完全一致，复用同一完整缓存键。
            "oi5": self._cg.fetch_oi_aggregated_history("BTC", "5m", 50),
            "oi1h": self._cg.fetch_oi_aggregated_history("BTC", "1h", 720),
            "premium": self._bn.fetch_premium_index("BTCUSDT"),
            "official_funding": fetch_official_pair("BTCUSDT"),
        }
        keys = list(jobs)
        values = await asyncio.gather(*jobs.values(), return_exceptions=True)
        data: dict[str, Any] = {}
        aux_fetched_at: dict[str, int] = {}
        for key in self._AUX_MAX_AGE:
            envelope = self._fresh_aux(key, now)
            if envelope is not None:
                data[key] = envelope["value"]
                aux_fetched_at[key] = int(envelope["fetched_at"])
        for key, value in zip(keys, values):
            if isinstance(value, BaseException):
                logger.warning("trend source failed | source=%s error=%s", key, value)
                data[key] = None
            else:
                data[key] = value

        bars = {tf: parse_closed_klines(data.get(key)) for tf, key in (
            ("15m", "k15"), ("1h", "k1h"), ("4h", "k4h"), ("1d", "k1d"),
        )}
        spot5 = parse_cvd_deltas(data.get("spot5"), 300, now)
        fut5 = parse_cvd_deltas(data.get("fut5"), 300, now)
        spot1h = parse_cvd_deltas(data.get("spot1h"), 3600, now)
        fut1h = parse_cvd_deltas(data.get("fut1h"), 3600, now)
        oi5 = parse_oi(data.get("oi5"), 300, now)
        oi1h = parse_oi(data.get("oi1h"), 3600, now)
        built_histories = build_closed_flow_histories(
            bars["1h"], spot1h, fut1h, oi1h, now,
        )
        for name, history in built_histories.items():
            if history.quality.valid:
                self._flow_histories[name] = history
            elif name in self._flow_histories:
                fallback = self._flow_histories[name].model_copy(deep=True)
                fallback.quality.valid = False
                fallback.quality.status = "stale"
                fallback.quality.reason = history.quality.reason
                fallback.quality.age_sec = max(
                    0, now - (fallback.quality.as_of_ts or now),
                )
                self._flow_histories[name] = fallback
            else:
                self._flow_histories[name] = history
        # 1h 告警基线直接由同源逐根 buy/sell 回填；仅存历史分位，不重复进入方向评分。
        for market, rows in (("spot", spot1h), ("futures", fut1h)):
            self._store.record_flows(
                market, "1h", [
                    (row["ts"] + 3600, row["delta"], row["buy"] + row["sell"])
                    for row in rows
                ],
            )

        tf_inputs = {
            "15m": self._aligned_inputs(bars["15m"], spot5, fut5, oi5, 300),
            "1h": self._aligned_inputs(bars["1h"], spot5, fut5, oi5, 300),
            "4h": self._aligned_inputs(bars["4h"], spot1h, fut1h, oi1h, 3600),
            "1d": self._aligned_inputs(bars["1d"], spot1h, fut1h, oi1h, 3600),
        }
        timeframes = {
            tf: calculate_timeframe(
                tf, *inputs, now_sec=now,
                weights=self._cfg.component_weights[tf],
                direction_threshold=self._cfg.direction_threshold,
            )
            for tf, inputs in tf_inputs.items()
        }
        core_score, direction = calculate_core_direction(
            timeframes, self._cfg.core_weights, self._cfg.direction_threshold,
        )

        spot_sign = sum(row["delta"] for row in spot5[-12:])
        fut_sign = sum(row["delta"] for row in fut5[-12:])
        active_flows = {}
        for market, key, sign in (
            ("spot", "spot_net", spot_sign), ("futures", "fut_net", fut_sign),
        ):
            if key in data:
                active_flows[market] = parse_active_flow(
                    data.get(key), market, sign, fetched_at=aux_fetched_at.get(key),
                )
            elif not self._aux_bootstrapped and self._latest and market in self._latest.active_flows:
                active_flows[market] = self._latest.active_flows[market].model_copy(deep=True)
                self._set_pending(
                    active_flows[market].quality, "启动加载主动成交净流",
                )
            elif self._latest and market in self._latest.active_flows:
                active_flows[market] = self._latest.active_flows[market].model_copy(deep=True)
                active_flows[market].quality.valid = False
                active_flows[market].quality.status = "stale"
                active_flows[market].quality.reason = "主动成交净流超过缓存时效，停止告警"
            else:
                active_flows[market] = parse_active_flow(None, market, sign)
                if not self._aux_bootstrapped:
                    self._set_pending(
                        active_flows[market].quality, "启动加载主动成交净流",
                    )
        if "wallet_list" in data and "wallet_chart" in data:
            wallet = parse_wallet_flow(
                data.get("wallet_list"), data.get("wallet_chart"), now,
                self._cfg.wallet_modifier_scale_btc,
            )
            wallet.quality.fetched_at_ts = min(
                aux_fetched_at.get("wallet_list", now), aux_fetched_at.get("wallet_chart", now),
            )
        elif not self._aux_bootstrapped and self._latest:
            wallet = self._latest.wallet_flow.model_copy(deep=True)
            wallet.confidence_modifier = 0.0
            wallet.modifier_reason = "启动加载钱包余额数据，本轮不修正趋势"
            self._set_pending(wallet.quality, "启动加载钱包余额数据")
        elif self._latest:
            wallet = self._latest.wallet_flow.model_copy(deep=True)
            wallet.confidence_modifier = 0.0
            wallet.quality.valid = False
            wallet.quality.status = "stale"
            wallet.quality.reason = "钱包列表或历史超过采集缓存时效，本轮不修正趋势"
            wallet.modifier_reason = wallet.quality.reason
        else:
            wallet = parse_wallet_flow(None, None, now)
            if not self._aux_bootstrapped:
                self._set_pending(wallet.quality, "启动加载钱包余额数据")

        looknode_raw = data.get("looknode_flow")
        if not isinstance(looknode_raw, dict):
            looknode_raw = self._store.load_exchange_transfer_flows(
                int(getattr(self._looknode_cfg, "history_days", 730)),
            )
        exchange_transfer = parse_exchange_transfer_flow(
            looknode_raw,
            now,
            stale_after_sec=int(getattr(self._looknode_cfg, "stale_after_sec", 60 * 3600)),
            history_days=int(getattr(self._looknode_cfg, "history_days", 730)),
        )
        if looknode_raw is None and not self._aux_bootstrapped and self._looknode is not None:
            self._set_pending(
                exchange_transfer.quality, "启动加载Looknode交易所流入流出",
            )

        premium = data.get("premium") if isinstance(data.get("premium"), dict) else {}
        mark = self._safe_float(premium.get("markPrice"))
        index = self._safe_float(premium.get("indexPrice"))
        basis = ((mark - index) / index * 100.0) if mark and index else None
        official = data.get("official_funding")
        bn_rate, okx_rate = official if isinstance(official, (tuple, list)) and len(official) == 2 else (None, None)
        oi_closes = self._depriced_oi_series(oi1h, bars["1h"])
        oi_near_high = bool(oi_closes and oi_closes[-1] >= sorted(oi_closes)[int(0.9 * (len(oi_closes) - 1))])
        previous_basis = (
            self._latest.funding.basis_pct
            if self._latest
            and self._latest.algorithm_version == self._cfg.algorithm_version
            and now - self._latest.ts <= self._cfg.evaluation_interval_sec * 2
            else None
        )
        basis_expanding = bool(
            basis is not None and previous_basis is not None
            and basis * previous_basis > 0 and abs(basis) > abs(previous_basis)
        )
        if "fund_hist" in data:
            funding = build_funding_snapshot(
                bn_rate, okx_rate, data.get("fund_hist"), basis, oi_near_high,
                basis_expanding, direction, now,
            )
        elif not self._aux_bootstrapped and self._latest:
            funding = self._latest.funding.model_copy(deep=True)
            funding.binance_rate, funding.okx_rate, funding.basis_pct = bn_rate, okx_rate, basis
            funding.confidence_modifier = 0.0
            funding.modifier_reason = "启动加载Funding历史，本轮不修正置信度"
            self._set_pending(funding.quality, "启动加载Funding历史")
        elif self._latest:
            funding = self._latest.funding.model_copy(deep=True)
            funding.binance_rate, funding.okx_rate, funding.basis_pct = bn_rate, okx_rate, basis
            funding.confidence_modifier = 0.0
            funding.quality.valid = False
            funding.quality.status = "stale"
            funding.quality.reason = "Funding历史超过缓存时效"
            funding.modifier_reason = "Funding历史过期，本轮不修正置信度"
        else:
            funding = build_funding_snapshot(
                bn_rate, okx_rate, None, basis, oi_near_high,
                core_direction=direction, now_sec=now,
            )
            if not self._aux_bootstrapped:
                self._set_pending(funding.quality, "启动加载Funding历史")
        etf_direction = timeframes["1d"].direction
        etf_modifier_direction = direction if etf_direction == direction else "range"
        if "etf" in data:
            etf = parse_etf_flow(data.get("etf"), etf_modifier_direction, now)
        elif not self._aux_bootstrapped and self._latest:
            etf = self._latest.etf_flow.model_copy(deep=True)
            etf.confidence_modifier = 0.0
            self._set_pending(etf.quality, "启动加载ETF资金流")
        elif self._latest:
            etf = self._latest.etf_flow.model_copy(deep=True)
            etf.confidence_modifier = 0.0
            etf.quality.valid = False
            etf.quality.status = "stale"
            etf.quality.reason = "ETF采集缓存过期，本轮不修正趋势"
        else:
            etf = parse_etf_flow(None, "range", now)
            if not self._aux_bootstrapped:
                self._set_pending(etf.quality, "启动加载ETF资金流")

        qualifies = self._core_qualifies(timeframes, direction)
        state, count, proposed_machine = self._propose_state(direction, qualifies, closed_5m_ts)
        funding_applied = 0.0
        wallet_applied = 0.0
        etf_applied = 0.0
        if direction in ("bullish", "bearish"):
            funding_applied = funding.confidence_modifier
            wallet_allowed = any(
                timeframes[tf].direction == direction for tf in ("4h", "1d")
            )
            if wallet_allowed:
                wallet_applied = (
                    wallet.confidence_modifier
                    if direction == "bullish" else -wallet.confidence_modifier
                )
            etf_applied = etf.confidence_modifier
        wallet_applied = self._apply_wallet_crosscheck(
            wallet, exchange_transfer, wallet_applied,
        )
        modifier_parts = [funding_applied, wallet_applied, etf_applied]
        modifier_total = max(-self._cfg.modifier_cap, min(self._cfg.modifier_cap, sum(modifier_parts)))
        confidence = max(0.0, min(100.0, abs(core_score) + modifier_total))
        footprint_raw = data.get("footprint")
        footprint_available = isinstance(footprint_raw, list) and bool(footprint_raw)
        footprint_effective = bool(self._cfg.footprint_enabled and footprint_available)
        if self._cfg.footprint_enabled:
            self._store.record_source_availability("footprint", closed_5m_ts, footprint_effective)
            footprint_availability = self._store.source_availability_pct("footprint", 14)
        else:
            footprint_availability = None
        core_valid = all(timeframes[tf].quality.valid for tf in ("1h", "4h", "1d"))
        snapshot = TrendSnapshot(
            ts=now, closed_5m_ts=closed_5m_ts,
            algorithm_version=self._cfg.algorithm_version, state=state,
            direction=direction, core_score=core_score, confidence=round(confidence, 2),
            consecutive_core_confirmations=count,
            confirmation_target=self._cfg.confirmation_bars, timeframes=timeframes,
            active_flows=active_flows, wallet_flow=wallet,
            exchange_transfer_flow=exchange_transfer,
            funding=funding, etf_flow=etf,
            footprint=FootprintStatus(
                enabled=self._cfg.footprint_enabled,
                available=footprint_effective,
                availability_14d_pct=footprint_availability,
                ablation_validated=False,
                promotion_eligible=False,
                quality=DataQuality(
                    valid=bool(self._cfg.footprint_enabled and footprint_available),
                    points=len(footprint_raw) if footprint_available else 0,
                    reason="" if footprint_available else
                    "Footprint已关闭" if not self._cfg.footprint_enabled else
                    "Footprint不可用、过期或被限流",
                    as_of_ts=aux_fetched_at.get("footprint"),
                    fetched_at_ts=aux_fetched_at.get("footprint"),
                    status="fresh" if self._cfg.footprint_enabled and footprint_available
                    else "missing" if not self._cfg.footprint_enabled else "stale",
                ),
            ),
            modifier_total=round(modifier_total, 2),
            modifier_breakdown=ModifierBreakdown(
                funding_applied=round(funding_applied, 2),
                wallet_market_bias=round(wallet.confidence_modifier, 2),
                wallet_applied=round(wallet_applied, 2),
                etf_applied=round(etf_applied, 2),
                total=round(modifier_total, 2),
                wallet_cross_source_status=exchange_transfer.cross_source_status,
            ),
            data_quality=DataQuality(
                valid=core_valid, points=sum(len(v) for v in bars.values()),
                reason="" if core_valid else "至少一个核心周期未通过质量门",
                age_sec=max(
                    (timeframes[tf].quality.age_sec or 0) for tf in ("1h", "4h", "1d")
                ),
                as_of_ts=closed_5m_ts + 300, fetched_at_ts=now,
                status="fresh" if core_valid else "missing",
            ),
            source_diagnostics=self.source_diagnostics(),
        )

        if self._ai_reviewer:
            if state not in ("data_invalid", "range"):
                verdict, review_reason = await self._ai_reviewer.review(snapshot)
                snapshot.ai_review = verdict
                snapshot.ai_review_reason = review_reason
                if verdict == "downgrade":
                    snapshot.confidence = max(0.0, snapshot.confidence - 10.0)
                    downgrade = {
                        "bullish_confirmed": "bullish_candidate",
                        "bearish_confirmed": "bearish_candidate",
                        "reversal_confirmed": "reversal_watch",
                        "bullish_candidate": "bullish_watch",
                        "bearish_candidate": "bearish_watch",
                    }
                    snapshot.state = downgrade.get(snapshot.state, snapshot.state)
                elif verdict == "veto":
                    snapshot.confidence = 0.0
                    snapshot.state = f"{snapshot.direction}_watch"
            else:
                snapshot.ai_review_reason = self._ai_reviewer.suspension_message()

        # 解释器必须读取AI复核后的最终展示状态，避免状态被降级后仍显示“反转已确认”。
        snapshot.flow_behavior = interpret_flow_behavior(
            snapshot.state, snapshot.direction, timeframes, tf_inputs, self._cfg,
            active_flows=active_flows, funding=funding,
        )
        snapshot.flow_exhaustion_watch = interpret_flow_exhaustion_watch(
            snapshot.state, snapshot.direction, timeframes, tf_inputs, self._cfg,
            full_1h_inputs=(bars["1h"], spot5, fut5, oi5),
            closed_histories=self._flow_histories,
            funding=funding,
        )

        # 只有AI后的最终状态才允许提交确认方向，避免veto后内部仍记为confirmed。
        self._machine = self._finalize_machine(
            proposed_machine, snapshot.ai_review, snapshot.state,
        )
        snapshot.consecutive_core_confirmations = self._machine.confirmation_count

        previous = (
            self._latest
            if self._latest and self._latest.algorithm_version == self._cfg.algorithm_version
            else None
        )
        events = build_events(
            snapshot, previous, self._store, self._cfg, self._looknode_cfg,
        )
        event_emails = []
        for event in events:
            subject = html_body = None
            if self._cfg.email_enabled and self._email_cfg.enabled:
                subject, html_body = render_email(event, snapshot)
            event_emails.append((event, subject, html_body))
        inserted_events = self._store.commit_evaluation(
            snapshot, self._machine, event_emails,
        )
        self._latest = snapshot
        for event in inserted_events:
            if self._push:
                await self._push("trend_event", event.model_dump(mode="json"))
        if self._push:
            await self._push("trend_update", snapshot.model_dump(mode="json"))
        if self._aux_task is None or self._aux_task.done():
            self._aux_task = asyncio.create_task(
                self._refresh_auxiliary_data(), name="btc-trend-auxiliary-refresh",
            )
        if snapshot.ts % 86400 < self._cfg.evaluation_interval_sec:
            self._store.prune(self._cfg.snapshot_retention_days)
        return snapshot

    async def _refresh_auxiliary_data(self) -> None:
        jobs = {
            "spot_net": self._cg.fetch_spot_taker_netflow_snapshot("BTC"),
            "fut_net": self._cg.fetch_futures_taker_netflow_snapshot("BTC"),
            "fund_hist": self._cg.fetch_fr_oi_weight_history("BTC", "8h", 90),
            "wallet_list": self._cg.fetch_exchange_balance_list("BTC"),
            "wallet_chart": self._cg.fetch_exchange_balance_chart("BTC"),
            "etf": self._cg.fetch_btc_etf_flow_history(),
            # 与 Engine 巨鲸轮询完全相同的请求参数，复用缓存与 single-flight。
            "hl_whale_positions": self._cg.fetch_hyperliquid_whale_position(),
        }
        if self._looknode is not None:
            jobs["looknode_flow"] = self._looknode.fetch_exchange_flows()
        if self._cfg.footprint_enabled:
            fetch_footprint = getattr(
                self._cg, "fetch_trend_futures_footprint_history",
                self._cg.fetch_futures_footprint_history,
            )
            jobs["footprint"] = fetch_footprint(
                "Binance", "BTCUSDT", "1h", 3,
            )
        if self._store.flow_count("spot", "24h") < 180:
            jobs["spot_baseline_1d"] = self._cg.fetch_spot_taker_history_baseline(
                "BTC", "1d", 200,
            )
        if self._store.flow_count("futures", "24h") < 180:
            jobs["fut_baseline_1d"] = self._cg.fetch_futures_taker_history_baseline(
                "BTC", "1d", 200,
            )
        keys = list(jobs)
        values = await asyncio.gather(*jobs.values(), return_exceptions=True)
        refreshed: dict[str, Any] = {}
        fetched_at = int(time.time())
        for key, value in zip(keys, values):
            if isinstance(value, BaseException):
                logger.warning("trend auxiliary source failed | source=%s error=%s", key, value)
                continue
            if value is not None:
                refreshed[key] = value
        for key, value in refreshed.items():
            if key in self._AUX_MAX_AGE:
                self._aux_data[key] = {"value": value, "fetched_at": fetched_at}
        whale_positions = refreshed.get("hl_whale_positions")
        if isinstance(whale_positions, list):
            self._whale_distributions = build_hyperliquid_whale_distributions(
                whale_positions,
                fetched_at_ts=fetched_at,
            )
            self._persist_whale_snapshot(whale_positions, fetched_at)
        if not self._cfg.footprint_enabled:
            self._aux_data.pop("footprint", None)
        for market, key in (("spot", "spot_baseline_1d"), ("futures", "fut_baseline_1d")):
            raw = refreshed.get(key)
            if not isinstance(raw, list):
                continue
            rows = parse_cvd_deltas(raw, 86400)
            self._store.record_flows(
                market, "24h", [
                    (row["ts"] + 86400, row["delta"], row["buy"] + row["sell"])
                    for row in rows
                ],
            )
        looknode_raw = refreshed.get("looknode_flow")
        if isinstance(looknode_raw, dict):
            try:
                history_days = int(getattr(self._looknode_cfg, "history_days", 730))
                inflow = {
                    int(float(row["t"]) // 1000): float(row["v"])
                    for row in looknode_raw.get("inflow", [])[-history_days:]
                    if isinstance(row, dict) and math.isfinite(float(row.get("v", -1)))
                    and float(row.get("v", -1)) >= 0
                }
                outflow = {
                    int(float(row["t"]) // 1000): float(row["v"])
                    for row in looknode_raw.get("outflow", [])[-history_days:]
                    if isinstance(row, dict) and math.isfinite(float(row.get("v", -1)))
                    and float(row.get("v", -1)) >= 0
                }
                if inflow and set(inflow) == set(outflow):
                    timestamps = sorted(inflow)[-history_days:]
                    self._store.upsert_exchange_transfer_flows(
                        [(ts, inflow[ts], outflow[ts], inflow[ts] - outflow[ts])
                         for ts in timestamps],
                        int(looknode_raw.get("fetched_at") or fetched_at),
                    )
            except (KeyError, TypeError, ValueError, OverflowError):
                logger.warning("Looknode flow persistence skipped due to invalid rows", exc_info=True)
        self._aux_bootstrapped = True
        logger.info("trend auxiliary data refreshed | sources=%s", sorted(refreshed))

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _set_pending(quality: DataQuality, reason: str) -> None:
        quality.valid = False
        quality.status = "pending"
        quality.reason = reason

    def _apply_wallet_crosscheck(
        self, wallet, exchange_transfer: ExchangeTransferFlowSnapshot,
        wallet_applied: float,
    ) -> float:
        """Looknode may only veto a material conflicting wallet modifier."""
        exchange_transfer.cross_source_status = "unavailable"
        if not wallet.quality.valid or not exchange_transfer.quality.valid:
            return wallet_applied
        transfer_7d = next(
            (window for window in exchange_transfer.windows if window.window == "7d"), None,
        )
        current_wallet = wallet.change_7d_btc
        if transfer_7d is None or current_wallet is None or current_wallet == 0:
            exchange_transfer.cross_source_status = "neutral"
            return wallet_applied

        historical_wallet: list[float] = []
        points = [point for point in wallet.chart if point.net_change_btc is not None]
        # Keep the current seven days completely outside their own baseline.
        for end in range(7, len(points) - 7 + 1):
            historical_wallet.append(abs(sum(
                float(point.net_change_btc) for point in points[end - 7:end]
            )))
        historical_wallet = historical_wallet[-365:]
        wallet_pct = (
            100.0 * sum(value <= abs(current_wallet) for value in historical_wallet)
            / len(historical_wallet)
            if historical_wallet else None
        )
        exchange_transfer.coinglass_7d_abs_percentile = wallet_pct
        threshold = float(getattr(self._looknode_cfg, "crosscheck_abs_percentile", 75.0))
        min_ratio = float(getattr(self._looknode_cfg, "crosscheck_min_net_ratio", 0.03))
        transfer_pct = transfer_7d.abs_net_percentile_365d
        material = bool(
            wallet_pct is not None and wallet_pct >= threshold
            and transfer_pct is not None and transfer_pct >= threshold
            and abs(transfer_7d.net_ratio) >= min_ratio
        )
        if not material:
            exchange_transfer.cross_source_status = "neutral"
            return wallet_applied
        if current_wallet * transfer_7d.netflow_btc > 0:
            exchange_transfer.cross_source_status = "confirmed"
            return wallet_applied
        exchange_transfer.cross_source_status = "conflict"
        return 0.0

    def _fresh_aux(self, key: str, now: int) -> Optional[dict[str, Any]]:
        envelope = self._aux_data.get(key)
        if not envelope:
            return None
        fetched_at = int(envelope.get("fetched_at", 0))
        if fetched_at <= 0 or now - fetched_at > self._AUX_MAX_AGE[key]:
            return None
        return envelope

    @staticmethod
    def _aligned_inputs(
        bars: list[dict], spot_rows: list[dict], futures_rows: list[dict],
        oi_rows: list[dict], source_interval: int,
    ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        if not bars:
            return bars, [], [], []
        start = int(bars[-1]["ts"] // 1000)
        end = int((bars[-1]["close_ts"] + 1) // 1000)
        spot = [row for row in spot_rows if start <= row["ts"] and row["ts"] + source_interval <= end]
        futures = [row for row in futures_rows if start <= row["ts"] and row["ts"] + source_interval <= end]
        # OI close at bucket end; include the bucket immediately before the target window as anchor.
        oi = [
            row for row in oi_rows
            if start <= row["ts"] + source_interval <= end
        ]
        return bars, spot, futures, oi

    @staticmethod
    def _depriced_oi_series(oi_rows: list[dict], hourly_bars: list[dict]) -> list[float]:
        prices_by_end = {
            int((bar["close_ts"] + 1) // 1000): float(bar["close"])
            for bar in hourly_bars
        }
        values = []
        for row in oi_rows:
            end_ts = int(row["ts"]) + 3600
            price = prices_by_end.get(end_ts)
            if price and price > 0:
                values.append(float(row["close"]) / price)
        return values

    def _core_qualifies(self, timeframes, direction: str) -> bool:
        if direction not in ("bullish", "bearish"):
            return False
        sign = 1 if direction == "bullish" else -1
        tf4, tf1, tfd = timeframes["4h"], timeframes["1h"], timeframes["1d"]
        return (
            tf4.score * sign >= self._cfg.confirm_4h_threshold
            and tf1.score * sign >= self._cfg.confirm_1h_threshold
            and tfd.score * sign > -self._cfg.strong_opposite_1d_threshold
            and tf4.spot_confirms
        )

    def _propose_state(
        self, direction: str, qualifies: bool, closed_ts: int,
    ) -> tuple[str, int, TrendMachineContext]:
        context = self._machine.model_copy(deep=True)
        context.algorithm_version = self._cfg.algorithm_version
        previous_confirmed_direction = context.confirmed_direction
        if direction == "invalid":
            context.confirmation_count = 0
            context.confirmation_direction = None
            return "data_invalid", 0, context
        if direction == "range":
            context.confirmation_count = 0
            context.confirmation_direction = None
            if previous_confirmed_direction:
                return "weakening", 0, context
            return "range", 0, context

        if qualifies and closed_ts != context.last_counted_bar:
            if context.confirmation_direction == direction:
                context.confirmation_count += 1
            else:
                context.confirmation_direction = direction
                context.confirmation_count = 1
            context.last_counted_bar = closed_ts
        elif not qualifies:
            context.confirmation_direction = direction
            context.confirmation_count = 0

        opposite = previous_confirmed_direction and direction != previous_confirmed_direction
        if opposite:
            if qualifies and context.confirmation_count >= getattr(self._cfg, "confirmation_bars", 3):
                context.confirmed_direction = direction
                return "reversal_confirmed", context.confirmation_count, context
            return "reversal_watch", context.confirmation_count, context
        if previous_confirmed_direction == direction and not qualifies:
            return "weakening", context.confirmation_count, context
        if qualifies and context.confirmation_count >= getattr(self._cfg, "confirmation_bars", 3):
            context.confirmed_direction = direction
            return f"{direction}_confirmed", context.confirmation_count, context
        if qualifies:
            return f"{direction}_candidate", context.confirmation_count, context
        return f"{direction}_watch", context.confirmation_count, context

    def _advance_state(self, direction: str, qualifies: bool, closed_ts: int) -> tuple[str, int]:
        """兼容内部测试的无AI提交入口；生产路径使用_propose_state后再过AI。"""
        state, count, context = self._propose_state(direction, qualifies, closed_ts)
        self._machine = context
        return state, count

    def _finalize_machine(
        self, proposed: TrendMachineContext, ai_review: str, final_state: str,
    ) -> TrendMachineContext:
        context = proposed.model_copy(deep=True)
        target = getattr(self._cfg, "confirmation_bars", 3)
        if ai_review == "veto":
            context.confirmation_count = 0
        elif ai_review == "downgrade":
            context.confirmation_count = min(context.confirmation_count, max(0, target - 1))
        if final_state not in ("bullish_confirmed", "bearish_confirmed", "reversal_confirmed"):
            context.confirmed_direction = self._machine.confirmed_direction
        return context
