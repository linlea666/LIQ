"""BTC现货动态抄底服务：隔离轮询、事实装配、预算和手工账本。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any, Callable, Optional

from models.spot_accumulation import (
    SpotAccumulationConfig,
    SpotAccumulationFacts,
    SpotAccumulationRuntimeState,
    SpotAccumulationSnapshot,
    SpotDataQuality,
    SpotLedgerEvent,
    SpotOpportunityJournalEvent,
)
from processors.spot_accumulation import (
    build_opportunities,
    build_swing_opportunity,
    build_tail_opportunities,
    score_facts,
)
from storage.spot_accumulation_store import (
    SpotAccumulationStore,
    SpotStorageCorruption,
)

logger = logging.getLogger(__name__)


class SpotAccumulationService:
    def __init__(self, data_dir: str, state_getter: Callable[[], Any]) -> None:
        self.store = SpotAccumulationStore(data_dir)
        self.recovery_errors: list[str] = []
        try:
            self.config = self.store.load_config()
        except SpotStorageCorruption as exc:
            self.config = SpotAccumulationConfig()
            self.recovery_errors.append(str(exc))
        cached_state: Optional[SpotAccumulationRuntimeState] = None
        state_cache_invalid = False
        try:
            cached_state = self.store.load_state()
        except SpotStorageCorruption as exc:
            state_cache_invalid = True
            logger.warning("spot accumulation state cache invalid; rebuilding: %s", exc)
        try:
            journal_state = self.store.latest_journal_runtime()
        except SpotStorageCorruption as exc:
            journal_state = None
            self.recovery_errors.append(str(exc))
        self.runtime = self._merge_recovery_state(cached_state, journal_state)
        try:
            self._reconcile_runtime_from_ledger()
        except (SpotStorageCorruption, ValueError) as exc:
            self.recovery_errors.append(str(exc))
        if not self.store.config_path.exists():
            self.store.save_config(self.config)
        if (state_cache_invalid or not self.store.state_path.exists()) and not self.recovery_errors:
            self.store.save_state(self.runtime)
        if not self.recovery_errors and not self.store.journal_path.exists():
            self.store.backup_legacy_files_once()
            self._journal_runtime("migration", "初始化机会事件日志")
        self.long_term = self.store.load_long_term_facts()
        self._state_getter = state_getter
        self._latest_snapshot: Optional[SpotAccumulationSnapshot] = None
        self._last_snapshot_hash = ""
        self._ai_explanation: Optional[str] = None

    @property
    def recovery_required(self) -> bool:
        return bool(self.recovery_errors)

    def _ensure_operational(self) -> None:
        if self.recovery_errors:
            raise SpotStorageCorruption("; ".join(self.recovery_errors))

    @staticmethod
    def _merge_recovery_state(
        cached: Optional[SpotAccumulationRuntimeState],
        journal: Optional[SpotAccumulationRuntimeState],
    ) -> SpotAccumulationRuntimeState:
        if cached is None and journal is None:
            return SpotAccumulationRuntimeState()
        if cached is None:
            return journal.model_copy(deep=True)  # type: ignore[union-attr]
        result = cached.model_copy(deep=True)
        if journal is None:
            return result
        result.cycle_ath = max(result.cycle_ath, journal.cycle_ath)
        for oid, item in journal.opportunities.items():
            current = result.opportunities.get(oid)
            if current is None or item.updated_at >= current.updated_at:
                result.opportunities[oid] = item.model_copy(deep=True)
        if journal.tail_mode is not None:
            result.tail_mode = journal.tail_mode
        result.updated_at = max(result.updated_at, journal.updated_at)
        return result

    def _reconcile_runtime_from_ledger(self) -> None:
        events = self.store.load_events()
        reversed_ids = {
            event.reverses_event_id for event in events
            if event.event_type == "reversal" and event.reverses_event_id
        }
        active = [
            event for event in events
            if event.event_type == "fill" and event.event_id not in reversed_ids
        ]
        spent_by_opportunity: dict[str, float] = {}
        for event in active:
            if event.side != "buy" or not event.opportunity_id:
                continue
            spent_by_opportunity[event.opportunity_id] = (
                spent_by_opportunity.get(event.opportunity_id, 0.0)
                + event.quantity_btc * event.price_usdt + event.fee_usdt
            )
            if event.opportunity_id not in self.runtime.opportunities:
                raise SpotStorageCorruption(
                    f"账本机会 {event.opportunity_id} 无法从机会日志恢复"
                )
        for oid, item in self.runtime.opportunities.items():
            if oid not in spent_by_opportunity:
                continue
            item.filled_usdt = min(item.allocation_usdt, spent_by_opportunity[oid])
            remaining = max(0.0, item.allocation_usdt - item.filled_usdt)
            item.reserved_usdt = remaining if item.status in {"eligible", "accepted", "filled"} else 0.0
            item.status = "filled" if remaining <= 0.01 else "accepted"
        buys = [
            event for event in active
            if event.side == "buy" and event.bucket in {"core", "tail"}
        ]
        buys.sort(key=lambda event: (event.executed_at, event.sequence or 0))
        self.runtime.last_filled_price = buys[-1].price_usdt if buys else None

    def _journal_runtime(self, event_type: str, note: str = "") -> None:
        now = int(time.time())
        self.runtime.updated_at = now
        self.store.append_journal(SpotOpportunityJournalEvent(
            event_id=uuid.uuid4().hex,
            sequence=1,
            event_type=event_type,  # type: ignore[arg-type]
            created_at=now,
            runtime=self.runtime.model_copy(deep=True),
            note=note[:500],
        ))

    async def poll_fast(self, cg: Any) -> None:
        """5分钟资金面：一份现货净流 + ETF完整窗口；共享CG缓存。"""
        now = int(time.time())
        netflow = await cg.fetch_spot_coin_netflow("BTC")
        if isinstance(netflow, dict):
            self.long_term["spot_netflow"] = netflow
            self.long_term.setdefault("timestamps", {})["spot_netflow"] = now
        etf = await cg.fetch_btc_etf_flow_history()
        if isinstance(etf, list) and etf:
            self.long_term["etf_flow"] = etf[-30:]
            self.long_term.setdefault("timestamps", {})["etf_flow"] = self._payload_ts(etf, now)
        self.store.save_long_term_facts(self.long_term)

    async def poll_slow(self, cg: Any) -> None:
        """低频长期事实；单项失败不覆盖上次成功值。"""
        now = int(time.time())
        calls = (
            ("exchange_balance", lambda: cg.fetch_exchange_balance_chart("BTC")),
            ("nupl", cg.fetch_nupl),
            ("reserve_risk", cg.fetch_reserve_risk),
            ("puell", cg.fetch_puell_multiple),
            ("sth_sopr", cg.fetch_sth_sopr),
            ("sth_supply", cg.fetch_sth_supply),
        )
        for name, fn in calls:
            try:
                data = await fn()
                if data not in (None, [], {}):
                    self.long_term[name] = data
                    self.long_term.setdefault("timestamps", {})[name] = self._payload_ts(data, now)
            except Exception:
                logger.warning("spot accumulation slow poll failed | source=%s", name, exc_info=True)
        self.store.save_long_term_facts(self.long_term)

    def update_config(self, patch: dict) -> SpotAccumulationConfig:
        self._ensure_operational()
        merged = self.config.model_dump()
        merged.update(patch)
        updated = SpotAccumulationConfig.model_validate(merged)
        # 先重放完整账本，确保缩小资金不会让任何分桶出现历史透支。
        portfolio = self.store.build_portfolio(updated)
        runtime = self.runtime.model_copy(deep=True)
        stage_allocations = updated.core_stage_allocations()
        for item in runtime.opportunities.values():
            if item.status not in {"observing", "eligible", "accepted"}:
                # 已结束机会是审计历史，保留当时配置下的原始额度。
                continue
            if item.stage in stage_allocations:
                amount = stage_allocations[item.stage]
            elif item.stage in {"tail_extreme", "tail_catch_up"}:
                amount = updated.tail_tranche_usdt
            elif item.stage == "swing":
                # 波段额度依赖结构止损；总资金变化后必须用新风险额重新计算。
                item.status = "invalidated"
                item.reserved_usdt = 0.0
                continue
            else:
                continue
            if item.filled_usdt > amount + 0.01:
                raise ValueError(
                    f"新资金配置低于机会已成交额 stage={item.stage} "
                    f"filled={item.filled_usdt:.2f} allocation={amount:.2f}"
                )
            item.allocation_usdt = amount
            item.reserved_usdt = (
                max(0.0, amount - item.filled_usdt)
                if item.status in {"eligible", "accepted"} else 0.0
            )

        reserved = {"core": 0.0, "swing": 0.0, "tail": 0.0}
        for item in runtime.opportunities.values():
            if item.status in {"eligible", "accepted"}:
                reserved[item.bucket] += item.reserved_usdt
        for bucket, amount in reserved.items():
            cash = portfolio.buckets[bucket].cash_usdt
            if amount > cash + 0.01:
                raise ValueError(
                    f"新资金配置不足以覆盖已释放额度 bucket={bucket} "
                    f"reserved={amount:.2f} cash={cash:.2f}"
                )

        self.store.save_config(updated)
        self.config = updated
        self.runtime = runtime
        self._journal_runtime("config", "配置变更后重算活动额度")
        self.store.save_state(self.runtime)
        self.evaluate()
        return updated

    def get_events(self) -> list[SpotLedgerEvent]:
        self._ensure_operational()
        return self.store.load_events()

    def record_fill(self, payload: dict) -> SpotLedgerEvent:
        self._ensure_operational()
        client_id = str(payload.get("client_event_id") or "").strip()
        if not client_id:
            raise ValueError("client_event_id 不能为空")
        existing = self.store.get_by_client_event_id(client_id)
        now = int(time.time())
        executed_at = int(
            payload.get("executed_at")
            or (existing.executed_at if existing is not None else now)
        )
        if executed_at <= 0 or executed_at > now + 300:
            raise ValueError("executed_at 必须为有效历史时间，且不能超过当前时间5分钟")
        bucket = str(payload.get("bucket") or "")
        side = str(payload.get("side") or "")
        linked = self.runtime.opportunities.get(str(payload.get("opportunity_id") or ""))
        canonical_payload = {
            "client_event_id": client_id,
            "side": side,
            "bucket": bucket,
            "quantity_btc": float(payload.get("quantity_btc") or 0),
            "price_usdt": float(payload.get("price_usdt") or 0),
            "fee_usdt": float(payload.get("fee_usdt") or 0),
            "executed_at": int(payload["executed_at"]) if payload.get("executed_at") else None,
            "opportunity_id": payload.get("opportunity_id"),
            "note": str(payload.get("note") or "")[:500],
        }
        payload_hash = hashlib.sha256(
            json.dumps(canonical_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        event = SpotLedgerEvent(
            event_id=uuid.uuid4().hex,
            client_event_id=client_id,
            client_payload_hash=payload_hash,
            side=side,
            bucket=bucket,
            quantity_btc=float(payload.get("quantity_btc") or 0),
            price_usdt=float(payload.get("price_usdt") or 0),
            fee_usdt=float(payload.get("fee_usdt") or 0),
            executed_at=executed_at,
            created_at=now,
            opportunity_id=payload.get("opportunity_id"),
            note=str(payload.get("note") or "")[:500],
            policy_override=bucket == "core" and side == "sell",
            policy_version=max(1, int(self.config.version)),
            opportunity_stage=linked.stage if linked else None,
            opportunity_allocation_usdt=linked.allocation_usdt if linked else None,
        )
        event.policy_override = bool(
            (bucket == "core" and side == "sell") or not event.opportunity_id
        )
        if existing is not None:
            # 幂等重试先在存储锁内核对原始业务载荷，不受当前机会状态变化影响。
            return self.store.commit_event(event, self.config)
        portfolio_before = self.store.build_portfolio(self.config)
        if bucket not in portfolio_before.buckets:
            raise ValueError("未知预算分桶")
        if event.opportunity_id and linked is None:
            raise ValueError("关联机会不存在")
        if linked and event.side != "buy":
            raise ValueError("机会只能关联买入成交")
        if linked and linked.status not in {"eligible", "accepted"}:
            raise ValueError("机会尚未达标或已结束，不能关联成交")
        if event.side == "buy":
            spend = event.quantity_btc * event.price_usdt + event.fee_usdt
            reservations = self._reserved_by_bucket()
            own_reserved = linked.reserved_usdt if linked and linked.bucket == bucket else 0.0
            free_cash = portfolio_before.buckets[bucket].cash_usdt - max(
                0.0, reservations.get(bucket, 0.0) - own_reserved
            )
            if spend > free_cash + 0.01:
                raise ValueError(
                    f"成交会占用其他机会预留预算 spend={spend:.2f} free={free_cash:.2f}"
                )
            if linked:
                if linked.bucket != bucket:
                    raise ValueError("成交分桶与机会分桶不一致")
                remaining = linked.allocation_usdt - linked.filled_usdt
                if spend > remaining + 0.01:
                    raise ValueError(
                        f"成交超过机会剩余额度 spend={spend:.2f} remaining={remaining:.2f}"
                    )
        # 先用内存事件集合重放验证，避免把超买/超卖写进账本。
        saved = self.store.commit_event(event, self.config)
        if saved.event_id != event.event_id:
            return saved
        if saved.opportunity_id and saved.opportunity_id in self.runtime.opportunities:
            opportunity = self.runtime.opportunities[saved.opportunity_id]
            if saved.side == "buy":
                spent = saved.quantity_btc * saved.price_usdt + saved.fee_usdt
                opportunity.filled_usdt = min(
                    opportunity.allocation_usdt,
                    opportunity.filled_usdt + spent,
                )
                remaining = max(0.0, opportunity.allocation_usdt - opportunity.filled_usdt)
                opportunity.reserved_usdt = remaining
                opportunity.status = "filled" if remaining <= 0.01 else "accepted"
            opportunity.updated_at = now
            if opportunity.stage == "tail_extreme":
                self.runtime.tail_mode = "extreme"
            elif opportunity.stage == "tail_catch_up":
                self.runtime.tail_mode = "catch_up"
        self._refresh_last_filled_price()
        self._journal_runtime("fill", f"成交 {saved.event_id}")
        self.store.save_state(self.runtime)
        self.evaluate()
        return saved

    def reverse_fill(self, event_id: str, client_event_id: str, note: str = "") -> SpotLedgerEvent:
        self._ensure_operational()
        if not client_event_id.strip():
            raise ValueError("client_event_id 不能为空")
        now = int(time.time())
        payload_hash = hashlib.sha256(json.dumps({
            "client_event_id": client_event_id,
            "event_id": event_id,
            "note": note[:500],
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        reversal = SpotLedgerEvent(
            event_id=uuid.uuid4().hex,
            client_event_id=client_event_id,
            client_payload_hash=payload_hash,
            event_type="reversal",
            reverses_event_id=event_id,
            executed_at=now,
            created_at=now,
            note=note[:500],
            policy_version=max(1, int(self.config.version)),
        )
        saved, target = self.store.commit_reversal(event_id, reversal, self.config)
        if saved.event_id != reversal.event_id:
            return saved
        if target.opportunity_id and target.opportunity_id in self.runtime.opportunities:
            item = self.runtime.opportunities[target.opportunity_id]
            if target.side == "buy":
                spent = target.quantity_btc * target.price_usdt + target.fee_usdt
                item.filled_usdt = max(0.0, item.filled_usdt - spent)
                item.reserved_usdt = max(0.0, item.allocation_usdt - item.filled_usdt)
                item.status = "accepted"
                item.updated_at = now
        self._refresh_last_filled_price()
        self._journal_runtime("reversal", f"冲正 {event_id}")
        self.store.save_state(self.runtime)
        self.evaluate()
        return saved

    def decide_opportunity(self, opportunity_id: str, decision: str) -> Any:
        self._ensure_operational()
        item = self.runtime.opportunities.get(opportunity_id)
        if item is None:
            raise KeyError("机会不存在")
        if decision not in {"accepted", "skipped"}:
            raise ValueError("decision 仅支持 accepted/skipped")
        if item.status not in {"eligible", "accepted"}:
            raise ValueError("只有已达标或已接受的机会可以处理")
        item.status = decision  # type: ignore[assignment]
        item.reserved_usdt = (
            max(0.0, item.allocation_usdt - item.filled_usdt)
            if decision == "accepted" else 0.0
        )
        item.updated_at = int(time.time())
        if decision == "accepted" and item.stage == "tail_extreme":
            self.runtime.tail_mode = "extreme"
        elif decision == "accepted" and item.stage == "tail_catch_up":
            self.runtime.tail_mode = "catch_up"
        self._journal_runtime("decision", f"{opportunity_id}:{decision}")
        self.store.save_state(self.runtime)
        return item

    def set_ai_explanation(self, explanation: Optional[str]) -> None:
        self._ai_explanation = explanation
        if self._latest_snapshot is not None:
            self._latest_snapshot.ai_explanation = explanation

    def get_snapshot(self) -> Optional[SpotAccumulationSnapshot]:
        if self.recovery_required:
            return None
        return self._latest_snapshot or self.evaluate()

    def evaluate(self) -> Optional[SpotAccumulationSnapshot]:
        if self.recovery_required:
            return None
        state = self._state_getter()
        if state is None or state.ticker is None or float(state.ticker.last or 0) <= 0:
            return None
        now = int(time.time())
        price = float(state.ticker.last)
        self._update_cycle_ath(state, price)
        facts = self._build_facts(state, now, price)
        portfolio = self.store.build_portfolio(self.config)
        reserved = self._reserved_by_bucket()
        daily_atr_pct = self._daily_atr_pct(state)
        opportunities = build_opportunities(
            facts,
            self.runtime,
            {name: pos.cash_usdt for name, pos in portfolio.buckets.items()},
            reserved,
            daily_atr_pct=daily_atr_pct,
            capitulation_confirmed=self._capitulation_confirmed(state),
            weekly_reclaim_confirmed=self._weekly_reclaim_confirmed(state),
            stage_allocations=self.config.core_stage_allocations(),
        )
        opportunities.extend(build_tail_opportunities(
            facts,
            self.runtime,
            portfolio.buckets["tail"].cash_usdt,
            reserved["tail"],
            daily_atr_pct=daily_atr_pct,
            capitulation_confirmed=self._capitulation_confirmed(state),
            weekly_reclaim_confirmed=self._weekly_reclaim_confirmed(state),
            tranche_usdt=self.config.tail_tranche_usdt,
        ))
        support, stop, target = self._swing_levels(state, price, daily_atr_pct)
        opportunities.extend(build_swing_opportunity(
            facts,
            self.runtime,
            portfolio.buckets["swing"].cash_usdt,
            reserved["swing"],
            support_price=support,
            stop_price=stop,
            target_price=target,
            has_open_position=portfolio.buckets["swing"].btc_quantity > 0,
            max_loss_usdt=self.config.max_swing_loss_usdt,
            min_rr=self.config.min_swing_rr,
        ))
        self._merge_opportunities(opportunities, now)
        current = sorted(
            self.runtime.opportunities.values(),
            key=lambda item: (item.status != "eligible", item.created_at, item.stage),
        )
        eligible = [item for item in current if item.status == "eligible"]
        next_action = (
            f"可评估 {eligible[0].stage}，上限 {eligible[0].allocation_usdt:.0f} U"
            if eligible else "等待估值、资金和现货承接共同确认"
        )
        snapshot = SpotAccumulationSnapshot(
            timestamp=now,
            facts=facts,
            portfolio=portfolio,
            opportunities=current,
            budget_reserved_usdt=self._reserved_by_bucket(),
            next_action=next_action,
            warnings=list(facts.hard_vetoes) + list(facts.data_quality.notes),
            ai_explanation=self._ai_explanation,
        )
        self._latest_snapshot = snapshot
        self.runtime.updated_at = now
        self.store.save_state(self.runtime)
        self._archive_if_changed(snapshot)
        return snapshot

    def _build_facts(self, state: Any, now: int, price: float) -> SpotAccumulationFacts:
        timestamps = dict(self.long_term.get("timestamps") or {})
        cycle = getattr(state, "cycle_position", None)
        market = getattr(state, "market_index", None)
        latest = lambda key: self._last_dict(self.long_term.get(key))
        nupl = latest("nupl")
        reserve = latest("reserve_risk")
        puell = latest("puell")
        sopr = latest("sth_sopr")
        netflow = self.long_term.get("spot_netflow") or {}
        etf_items = self.long_term.get("etf_flow") or []
        etf_windows = {
            days: sum(self._float(item.get("flow_usd")) or 0 for item in etf_items[-days:])
            for days in (1, 3, 5, 20)
        }
        exchange_windows = {days: self._exchange_balance_change(days) for days in (1, 7, 30)}
        exchange_7d = exchange_windows[7]
        supply_change = self._series_change(self.long_term.get("sth_supply"), 30, "short_term_holder_supply")
        stablecoin_change = self._stablecoin_change(state)
        premium = self._float(getattr(getattr(state, "coinbase_premium", None), "current_premium", None))

        absorption = self._has_absorption(state, price)
        persistent_wall, coinbase_wall = self._wall_evidence(state, price)
        reclaimed = self._key_level_reclaimed(state, price)
        spot_taker = sum(
            self._float(item.get("delta_usd")) or 0
            for item in (getattr(state, "taker_spot_series", None) or [])[-12:]
        )
        cvd_spot = getattr(state, "cvd_spot", None)
        cvd_delta = self._float(getattr(cvd_spot, "delta_1h", None))

        source_ts = {
            **{key: int(value) for key, value in timestamps.items() if value},
            "ticker": self._normal_ts(getattr(state.ticker, "ts", now)),
            "spot_cvd": self._normal_ts(
                getattr(cvd_spot.series[-1], "ts", 0) if cvd_spot and cvd_spot.series else 0
            ),
            "footprint": int(getattr(state, "footprint_last_ts", 0) or 0),
            "orderbook_pressure": int(getattr(getattr(state, "orderbook_pressure_snapshot", None), "ts_sec", 0) or 0),
        }
        quality = self._quality(now, source_ts, state)
        ath = max(price, self.runtime.cycle_ath)
        facts = SpotAccumulationFacts(
            timestamp=now,
            price=price,
            cycle_ath=ath,
            drawdown_pct=max(0.0, (ath - price) / ath * 100),
            valuation_inputs={
                "mvrv": self._float(getattr(market, "btc_mvrv", None)),
                "ahr999": self._float(getattr(cycle, "ahr999_value", None)) or self._float(getattr(market, "ahr999", None)),
                "price_vs_200w": self._ratio(price, getattr(cycle, "sma_200w", None)),
                "price_vs_sth": self._ratio(price, getattr(cycle, "sth_cost_1d", None)),
                "nupl": self._float(nupl.get("net_unpnl")) if nupl else None,
                "reserve_risk": self._float(reserve.get("reserve_risk_index")) if reserve else None,
                "puell": self._float(puell.get("puell_multiple")) if puell else None,
                "sth_sopr": self._float(sopr.get("sth_sopr")) if sopr else None,
                "sth_supply_change_30d_pct": supply_change,
            },
            capital_inputs={
                "etf_flow_1d_usd": etf_windows[1] if etf_items else None,
                "etf_flow_3d_usd": etf_windows[3] if etf_items else None,
                "etf_flow_5d_usd": etf_windows[5] if etf_items else None,
                "etf_flow_20d_usd": etf_windows[20] if etf_items else None,
                "exchange_balance_1d_pct": exchange_windows[1],
                "exchange_balance_7d_pct": exchange_7d,
                "exchange_balance_30d_pct": exchange_windows[30],
                "spot_netflow_1h_usd": self._float(netflow.get("net_flow_usd_1h")),
                "spot_netflow_4h_usd": self._float(netflow.get("net_flow_usd_4h")),
                "spot_netflow_24h_usd": self._float(netflow.get("net_flow_usd_24h")),
                "spot_netflow_7d_usd": self._float(netflow.get("net_flow_usd_7d")),
                "spot_netflow_30d_usd": self._float(netflow.get("net_flow_usd_30d")),
                "stablecoin_change_7d_pct": stablecoin_change,
                "coinbase_premium": premium,
            },
            acceptance_inputs={
                "spot_cvd_delta_1h": cvd_delta,
                "spot_cvd_bottom_divergence": bool(cvd_spot and cvd_spot.has_divergence and "底背离" in cvd_spot.divergence_note),
                "spot_taker_delta_1h": spot_taker if getattr(state, "taker_spot_series", None) else None,
                "footprint_absorption": absorption,
                "persistent_spot_wall": persistent_wall,
                "coinbase_confluence": coinbase_wall,
                "key_level_reclaimed": reclaimed,
            },
            source_timestamps=source_ts,
            data_quality=quality,
        )
        facts.scores = score_facts(facts)
        if cvd_delta is not None and cvd_delta < 0 and spot_taker < 0 and not absorption:
            facts.hard_vetoes.append("现货CVD与主动成交同步恶化且未见吸收")
        if facts.acceptance_inputs.get("spot_cvd_bottom_divergence"):
            facts.evidence.append("现货价格与CVD出现底背离")
        if absorption:
            facts.evidence.append("Footprint存在现货被动吸收")
        if persistent_wall:
            facts.evidence.append("下方存在持续现货买墙")
        if exchange_7d is not None and exchange_7d < 0:
            facts.evidence.append("交易所BTC余额下降")
        if etf_windows[5] > 0:
            facts.evidence.append("ETF近5日净流入")
        if facts.hard_vetoes:
            facts.data_quality.can_open_new_opportunity = False
        return facts

    def _quality(self, now: int, timestamps: dict[str, int], state: Any) -> SpotDataQuality:
        required = {
            "ticker": 180,
            "spot_cvd": 900,
            "footprint": 900,
            "orderbook_pressure": 600,
            "spot_netflow": 900,
            "etf_flow": 7200,
            "nupl": 172800,
            "reserve_risk": 172800,
            "puell": 172800,
            "sth_sopr": 172800,
        }
        missing: list[str] = []
        stale: list[str] = []
        for source, ttl in required.items():
            ts = int(timestamps.get(source, 0) or 0)
            if ts <= 0:
                missing.append(source)
            elif now - ts > ttl:
                stale.append(source)
        complete = (len(required) - len(missing) - len(stale)) / len(required)
        can_open = complete >= 0.8 and "ticker" not in missing + stale and "spot_cvd" not in missing + stale
        notes = [] if can_open else ["主要事实覆盖率不足80%或核心现货源过期"]
        return SpotDataQuality(
            completeness=max(0.0, complete),
            stale_sources=stale,
            missing_sources=missing,
            notes=notes,
            can_open_new_opportunity=can_open,
        )

    def _update_cycle_ath(self, state: Any, price: float) -> None:
        candidates = [price, self.runtime.cycle_ath]
        override = self.config.cycle_ath_override
        if override:
            candidates.append(override)
        for candle in (getattr(state, "candles_daily", None) or []):
            candidates.append(self._float(getattr(candle, "high", None)) or price)
        for candle in (getattr(state, "candles_weekly", None) or []):
            candidates.append(self._float(getattr(candle, "high", None)) or price)
        discovered = max(candidates)
        if discovered > self.runtime.cycle_ath:
            self.runtime.cycle_ath = discovered

    def _merge_opportunities(self, generated: list[Any], now: int) -> None:
        for old in self.runtime.opportunities.values():
            if old.expires_at and old.expires_at < now and old.status in {"observing", "eligible"}:
                old.status = "expired"
                old.reserved_usdt = 0
        for item in generated:
            old = self.runtime.opportunities.get(item.opportunity_id)
            if old and old.status in {"accepted", "filled", "skipped"}:
                continue
            self.runtime.opportunities[item.opportunity_id] = item

    def _reserved_by_bucket(self) -> dict[str, float]:
        result = {"core": 0.0, "swing": 0.0, "tail": 0.0}
        for item in self.runtime.opportunities.values():
            if item.status in {"eligible", "accepted"}:
                result[item.bucket] += item.reserved_usdt
        return result

    def _refresh_last_filled_price(self) -> None:
        events = self.store.load_events()
        reversed_ids = {
            event.reverses_event_id for event in events
            if event.event_type == "reversal" and event.reverses_event_id
        }
        buys = [
            event for event in events
            if event.event_type == "fill" and event.side == "buy"
            and event.event_id not in reversed_ids and event.bucket in {"core", "tail"}
        ]
        buys.sort(key=lambda event: (event.executed_at, event.sequence or 0))
        self.runtime.last_filled_price = buys[-1].price_usdt if buys else None

    def _archive_if_changed(self, snapshot: SpotAccumulationSnapshot) -> None:
        compact = {
            "timestamp": snapshot.timestamp,
            "price": snapshot.facts.price,
            "scores": snapshot.facts.scores.model_dump(),
            "quality": snapshot.facts.data_quality.model_dump(),
            "eligible": [item.stage for item in snapshot.opportunities if item.status == "eligible"],
        }
        digest = hashlib.sha1(json.dumps(compact, sort_keys=True).encode()).hexdigest()
        if digest != self._last_snapshot_hash:
            self.store.append_facts_snapshot(compact, snapshot.timestamp)
            self._last_snapshot_hash = digest

    @staticmethod
    def _last_dict(value: Any) -> dict:
        return value[-1] if isinstance(value, list) and value and isinstance(value[-1], dict) else {}

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _ratio(cls, numerator: float, denominator: Any) -> Optional[float]:
        den = cls._float(denominator)
        return numerator / den if den and den > 0 else None

    @staticmethod
    def _normal_ts(value: Any) -> int:
        try:
            ts = int(value)
            return ts // 1000 if ts > 10_000_000_000 else ts
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _payload_ts(cls, value: Any, fallback: int) -> int:
        if isinstance(value, list) and value:
            item = value[-1]
            if isinstance(item, dict):
                return cls._normal_ts(item.get("timestamp", item.get("time", fallback))) or fallback
        if isinstance(value, dict):
            time_list = value.get("time_list")
            if isinstance(time_list, list) and time_list:
                return cls._normal_ts(time_list[-1]) or fallback
            return cls._normal_ts(value.get("timestamp", value.get("time", fallback))) or fallback
        return fallback

    @classmethod
    def _series_change(cls, value: Any, lookback: int, field: str) -> Optional[float]:
        if not isinstance(value, list) or len(value) < 2:
            return None
        recent = value[-(lookback + 1):]
        first = recent[0] if isinstance(recent[0], dict) else {}
        last = recent[-1] if isinstance(recent[-1], dict) else {}
        old = cls._float(first.get(field))
        new = cls._float(last.get(field))
        return (new - old) / old * 100 if old and new is not None else None

    def _exchange_balance_change(self, days: int) -> Optional[float]:
        data = self.long_term.get("exchange_balance") or {}
        data_map = data.get("data_map") if isinstance(data, dict) else None
        if not isinstance(data_map, dict):
            return None
        series: list[list[float]] = []
        for value in data_map.values():
            if isinstance(value, list):
                numeric = [self._float(item) for item in value]
                if numeric and all(item is not None for item in numeric):
                    series.append([float(item) for item in numeric if item is not None])
        if not series:
            return None
        length = min(len(item) for item in series)
        if length < 2:
            return None
        totals = [sum(item[index] for item in series) for index in range(length)]
        old_index = max(0, length - 1 - days)
        old = totals[old_index]
        return (totals[-1] - old) / old * 100 if old else None

    def _stablecoin_change(self, state: Any) -> Optional[float]:
        stable = getattr(state, "stablecoin_mcap", None)
        history = getattr(stable, "history", None) or []
        if len(history) < 2:
            return None
        first = self._float(getattr(history[0], "total_mcap", None))
        last = self._float(getattr(history[-1], "total_mcap", None))
        return (last - first) / first * 100 if first and last is not None else None

    def _has_absorption(self, state: Any, price: float) -> bool:
        try:
            from processors.market_action.facts_collector import build_absorption_snapshot
            snap = build_absorption_snapshot(state)
            zone = snap.strongest_support
            return bool(zone and 0 <= (price - zone.price) / price * 100 <= 5)
        except Exception:
            return False

    @staticmethod
    def _wall_evidence(state: Any, price: float) -> tuple[bool, bool]:
        snap = getattr(state, "orderbook_pressure_snapshot", None)
        walls = getattr(snap, "walls_below", None) or []
        valid = [
            wall for wall in walls
            if 0 <= (price - wall.price_mid) / price * 100 <= 5
            and wall.persistence_score >= 0.5
            and wall.support_resistance_trust_score >= 0.65
            and (wall.dual_source or wall.has_spot_confluence)
        ]
        return bool(valid), any(wall.coinbase_spot_confluence for wall in valid)

    @staticmethod
    def _key_level_reclaimed(state: Any, price: float) -> bool:
        snap = getattr(state, "key_level_snapshot_v2", None)
        for level in getattr(snap, "levels", None) or []:
            if level.side != "support" or level.price <= 0:
                continue
            if abs(price - level.price) / price * 100 > 5:
                continue
            if level.state in {"bounced", "fake_break", "flipped"} or level.behavior_state in {
                "healthy_retest", "failed_breakout", "confirmed_flip",
            }:
                return True
        return False

    @staticmethod
    def _capitulation_confirmed(state: Any) -> bool:
        snap = getattr(state, "key_level_snapshot_v2", None)
        return any(
            level.side == "support" and level.capitulation_bottom_score >= 0.65
            and level.behavior_state in {"capitulation_flush", "confirmed_flip", "healthy_retest"}
            for level in (getattr(snap, "levels", None) or [])
        )

    @staticmethod
    def _weekly_reclaim_confirmed(state: Any) -> bool:
        candles = list(getattr(state, "candles_weekly", None) or [])
        if len(candles) < 22:
            return False
        closes = [float(c.close) for c in candles]
        sma20_prev = sum(closes[-21:-1]) / 20
        sma20_now = sum(closes[-20:]) / 20
        return closes[-2] > sma20_prev and closes[-1] > sma20_now and closes[-1] >= closes[-2]

    @staticmethod
    def _daily_atr_pct(state: Any) -> float:
        candles = list(getattr(state, "candles_daily", None) or [])[-15:]
        if len(candles) < 2:
            return 5.0
        trs = []
        for prev, cur in zip(candles, candles[1:]):
            trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
        price = float(candles[-1].close or 0)
        return sum(trs) / len(trs) / price * 100 if price > 0 else 5.0

    @staticmethod
    def _swing_levels(state: Any, price: float, daily_atr_pct: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
        snap = getattr(state, "key_level_snapshot_v2", None)
        levels = list(getattr(snap, "levels", None) or [])
        supports = sorted(
            [float(level.price) for level in levels if level.side == "support" and level.price <= price],
            reverse=True,
        )
        resistances = sorted(
            [float(level.price) for level in levels if level.side == "resistance" and level.price > price]
        )
        if not supports or not resistances:
            return None, None, None
        support = supports[0]
        stop = support * (1 - max(0.01, daily_atr_pct / 100.0))
        return support, stop, resistances[0]
