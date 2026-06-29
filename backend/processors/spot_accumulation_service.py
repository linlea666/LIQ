"""BTC现货动态抄底服务：隔离轮询、事实装配、预算和手工账本。"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from typing import Any, Callable, Optional

from models.spot_accumulation import (
    SpotAccumulationConfig,
    SpotAccumulationFacts,
    SpotAccumulationRuntimeState,
    SpotAccumulationSnapshot,
    SpotDataQuality,
    SpotLayerQuality,
    SpotLedgerEvent,
    SpotMetricFact,
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
    SpotIdempotencyConflict,
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
        elif not self.recovery_errors and self.store.config_needs_v3_migration():
            self.store.backup_config_v2_once()
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
        self._last_archived_opportunity_states: dict[str, str] = {}
        self._ai_explanation: Optional[str] = None
        self.last_evaluation_error = ""
        self.last_evaluation_error_at = 0

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
        # 事件日志一旦存在就是唯一运行时事实源；state.json仅作为可删除缓存。
        if journal is not None:
            return journal.model_copy(deep=True)
        if cached is not None:
            return cached.model_copy(deep=True)
        return SpotAccumulationRuntimeState()

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
            if remaining <= 0.01:
                item.reserved_usdt = 0.0
                item.status = "filled"
            elif item.status in {"eligible", "accepted"}:
                # 账本只重建成交额度，不能把行情已失效的部分成交机会复活。
                item.reserved_usdt = remaining
                item.status = "accepted"
            else:
                item.reserved_usdt = 0.0
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
        parsed_netflow, netflow_status = self._parse_spot_netflow(netflow)
        self.long_term.setdefault("parse_status", {})["spot_netflow"] = netflow_status
        if parsed_netflow is not None:
            self.long_term["spot_netflow"] = parsed_netflow
            self.long_term.setdefault("timestamps", {})["spot_netflow"] = now
        etf = await cg.fetch_btc_etf_flow_history()
        parsed_etf, etf_ts, etf_status = self._parse_timed_series(etf, "flow_usd", now)
        self.long_term.setdefault("parse_status", {})["etf_flow"] = etf_status
        if parsed_etf is not None:
            self.long_term["etf_flow"] = parsed_etf[-30:]
            self.long_term.setdefault("timestamps", {})["etf_flow"] = etf_ts
        self.store.save_long_term_facts(self.long_term)

    async def poll_slow(self, cg: Any) -> None:
        """低频长期事实；单项失败不覆盖上次成功值。"""
        now = int(time.time())
        calls = (
            ("exchange_balance", lambda: cg.fetch_exchange_balance_chart("BTC"), None),
            ("nupl", cg.fetch_nupl, "net_unpnl"),
            ("reserve_risk", cg.fetch_reserve_risk, "reserve_risk_index"),
            ("puell", cg.fetch_puell_multiple, "puell_multiple"),
            ("sth_sopr", cg.fetch_sth_sopr, "sth_sopr"),
            ("sth_supply", cg.fetch_sth_supply, "short_term_holder_supply"),
        )
        for name, fn, field in calls:
            try:
                data = await fn()
                if name == "exchange_balance":
                    parsed, source_ts, status = self._parse_exchange_balance(data, now)
                else:
                    parsed, source_ts, status = self._parse_timed_series(data, str(field), now)
                self.long_term.setdefault("parse_status", {})[name] = status
                if parsed is not None:
                    self.long_term[name] = parsed
                    self.long_term.setdefault("timestamps", {})[name] = source_ts
            except Exception:
                self.long_term.setdefault("parse_status", {})[name] = "request_error"
                logger.warning("spot accumulation slow poll failed | source=%s", name, exc_info=True)
        self.store.save_long_term_facts(self.long_term)

    @classmethod
    def _parse_spot_netflow(cls, payload: Any) -> tuple[Optional[dict], str]:
        if payload is None:
            return None, "request_error"
        if not isinstance(payload, dict):
            return None, "invalid_type"
        if not payload:
            return None, "empty"
        required = cls._float(payload.get("net_flow_usd_24h"))
        if required is None:
            return None, "missing_field"
        parsed: dict[str, Any] = {"net_flow_usd_24h": required}
        if payload.get("symbol") is not None:
            parsed["symbol"] = str(payload["symbol"])
        for name in (
            "net_flow_usd_1h", "net_flow_usd_4h", "net_flow_usd_7d", "net_flow_usd_30d",
        ):
            value = cls._float(payload.get(name))
            if value is not None:
                parsed[name] = value
        return parsed, "ok"

    @classmethod
    def _parse_timed_series(
        cls,
        payload: Any,
        field: str,
        now: int,
    ) -> tuple[Optional[list[dict]], int, str]:
        if payload is None:
            return None, 0, "request_error"
        if not isinstance(payload, list):
            return None, 0, "invalid_type"
        if not payload:
            return None, 0, "empty"
        parsed: list[dict] = []
        saw_field = False
        saw_bad_timestamp = False
        for item in payload:
            if not isinstance(item, dict):
                continue
            value = cls._float(item.get(field))
            if value is None:
                continue
            saw_field = True
            ts = cls._normal_ts(item.get("timestamp", item.get("time", 0)))
            if ts <= 0 or ts > now + 300:
                saw_bad_timestamp = True
                continue
            normalized = dict(item)
            normalized[field] = value
            normalized["timestamp"] = ts
            parsed.append(normalized)
        if not parsed:
            if saw_bad_timestamp:
                return None, 0, "invalid_timestamp"
            return None, 0, "missing_field" if not saw_field else "invalid_type"
        parsed.sort(key=lambda item: int(item["timestamp"]))
        return parsed, int(parsed[-1]["timestamp"]), "ok"

    @classmethod
    def _parse_exchange_balance(
        cls,
        payload: Any,
        now: int,
    ) -> tuple[Optional[dict], int, str]:
        if payload is None:
            return None, 0, "request_error"
        if not isinstance(payload, dict):
            return None, 0, "invalid_type"
        if not payload:
            return None, 0, "empty"
        time_list = payload.get("time_list")
        data_map = payload.get("data_map")
        if not isinstance(time_list, list) or not time_list or not isinstance(data_map, dict):
            return None, 0, "missing_field"
        normalized_ts = [cls._normal_ts(value) for value in time_list]
        if any(ts <= 0 or ts > now + 300 for ts in normalized_ts):
            return None, 0, "invalid_timestamp"
        normalized_map: dict[str, list[float]] = {}
        for exchange, values in data_map.items():
            if not isinstance(values, list) or len(values) != len(normalized_ts):
                continue
            parsed_values = [cls._float(value) for value in values]
            if any(value is None for value in parsed_values):
                continue
            normalized_map[str(exchange)] = [float(value) for value in parsed_values if value is not None]
        if not normalized_map:
            return None, 0, "missing_field"
        return {
            "time_list": normalized_ts,
            "data_map": normalized_map,
            "price_list": payload.get("price_list", []),
        }, normalized_ts[-1], "ok"

    @staticmethod
    def _sanitize_config_patch(patch: dict) -> dict:
        forbidden = {"schema_version", "version", "policy_version"}
        clean = {key: value for key, value in patch.items() if key not in forbidden}
        allowed = set(SpotAccumulationConfig.model_fields) - forbidden
        unknown = sorted(set(clean) - allowed)
        if unknown:
            raise ValueError(f"未知配置项: {', '.join(unknown)}")
        return clean

    def _config_candidate(
        self,
        patch: dict,
        base: Optional[SpotAccumulationConfig] = None,
    ) -> tuple[SpotAccumulationConfig, dict, bool]:
        base = base or self.config
        clean = self._sanitize_config_patch(patch)
        merged = base.model_dump(mode="json")
        if "core_stage_ratios" in clean and "insurance_ratio" not in clean:
            ratios = clean.get("core_stage_ratios")
            if isinstance(ratios, dict) and "insurance" in ratios:
                clean["insurance_ratio"] = ratios["insurance"]
        elif "insurance_ratio" in clean and "core_stage_ratios" not in clean:
            ratios = dict(merged.get("core_stage_ratios") or {})
            ratios["insurance"] = clean["insurance_ratio"]
            clean["core_stage_ratios"] = ratios
        merged.update(clean)
        changed = any(base.model_dump(mode="json").get(key) != value for key, value in clean.items())
        merged["schema_version"] = 3
        merged["policy_version"] = base.policy_version + (1 if changed else 0)
        return SpotAccumulationConfig.model_validate(merged), clean, changed

    def preview_config(
        self,
        patch: dict,
        *,
        base: Optional[SpotAccumulationConfig] = None,
    ) -> dict:
        self._ensure_operational()
        base = base or self.config
        updated, clean, changed = self._config_candidate(patch, base)
        portfolio = self.store.build_portfolio(updated)
        stage_allocations = updated.core_stage_allocations()
        filled_by_stage: dict[str, float] = {}
        for item in self.runtime.opportunities.values():
            if item.stage in stage_allocations:
                filled_by_stage[item.stage] = filled_by_stage.get(item.stage, 0.0) + item.filled_usdt
        for stage, filled in filled_by_stage.items():
            if filled > stage_allocations[stage] + 0.01:
                raise ValueError(
                    f"新配置低于{stage}历史成交额 filled={filled:.2f} "
                    f"allocation={stage_allocations[stage]:.2f}"
                )
        invalidated = [
            item.opportunity_id for item in self.runtime.opportunities.values()
            if item.status in {"observing", "eligible", "accepted"}
        ] if changed else []
        canonical = {
            "expected_policy_version": base.policy_version,
            "patch": clean,
            "candidate": updated.model_dump(mode="json"),
        }
        preview_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        old_budgets = {
            "core": base.core_budget_usdt,
            "swing": base.swing_budget_usdt,
            "tail": base.tail_budget_usdt,
        }
        new_budgets = {
            "core": updated.core_budget_usdt,
            "swing": updated.swing_budget_usdt,
            "tail": updated.tail_budget_usdt,
        }
        return {
            "preview_hash": preview_hash,
            "expected_policy_version": base.policy_version,
            "changed": changed,
            "config": updated.public_dump(),
            "budget_changes": {
                key: {"before": old_budgets[key], "after": new_budgets[key]}
                for key in old_budgets
            },
            "historical_occupancy": {
                key: {
                    "cash_usdt": position.cash_usdt,
                    "btc_quantity": position.btc_quantity,
                    "cost_basis_usdt": position.cost_basis_usdt,
                }
                for key, position in portfolio.buckets.items()
            },
            "invalidated_opportunity_ids": invalidated,
            "errors": [],
        }

    def update_config(
        self,
        patch: dict,
        *,
        expected_policy_version: int,
        preview_hash: str,
    ) -> SpotAccumulationConfig:
        self._ensure_operational()
        with self.store.config_transaction():
            current = self.store.load_config()
            if current.policy_version != expected_policy_version:
                raise SpotIdempotencyConflict(
                    f"策略版本冲突 expected={expected_policy_version} "
                    f"current={current.policy_version}"
                )
            preview = self.preview_config(patch, base=current)
            if preview["preview_hash"] != preview_hash:
                raise SpotIdempotencyConflict("配置预览已过期，请重新预览")
            updated = SpotAccumulationConfig.model_validate(preview["config"])
            if not preview["changed"]:
                self.config = current
                return current
            for item in self.runtime.opportunities.values():
                if item.status in {"observing", "eligible", "accepted"}:
                    item.status = "invalidated"
                    item.reserved_usdt = 0.0
                    item.updated_at = int(time.time())
            self.store.save_config(updated)
            self.config = updated
            self._journal_runtime("config", f"策略版本升级到{updated.policy_version}")
            self.store.save_state(self.runtime)
        self.evaluate_safe()
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
        if (
            linked is not None and linked.status == "accepted"
            and linked.grace_expires_at and now > linked.grace_expires_at
        ):
            self.evaluate_safe()
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
            policy_version=self.config.policy_version,
            opportunity_stage=linked.stage if linked else None,
            opportunity_allocation_usdt=linked.allocation_usdt if linked else None,
            batch_id=linked.batch_id if linked else None,
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
        if linked and linked.status != "accepted":
            raise ValueError("策略机会必须先接受且仍在执行宽限/有效期内才能关联成交")
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
            policy_version=self.config.policy_version,
        )
        saved, target = self.store.commit_reversal(event_id, reversal, self.config)
        if saved.event_id != reversal.event_id:
            return saved
        if target.opportunity_id and target.opportunity_id in self.runtime.opportunities:
            item = self.runtime.opportunities[target.opportunity_id]
            if target.side == "buy":
                spent = target.quantity_btc * target.price_usdt + target.fee_usdt
                item.filled_usdt = max(0.0, item.filled_usdt - spent)
                item.reserved_usdt = 0.0
                item.status = "invalidated"
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
        if item.status == "accepted" and decision == "accepted":
            # 幂等确认不能刷新15分钟宽限，否则可通过重复请求永久续期。
            return item
        item.status = decision  # type: ignore[assignment]
        item.reserved_usdt = (
            max(0.0, item.allocation_usdt - item.filled_usdt)
            if decision == "accepted" else 0.0
        )
        now = int(time.time())
        item.updated_at = now
        if decision == "accepted":
            item.accepted_at = now
            item.grace_expires_at = now + self.config.acceptance_grace_seconds
        else:
            item.accepted_at = None
            item.grace_expires_at = None
        if decision == "accepted" and item.stage == "tail_extreme":
            self.runtime.tail_mode = "extreme"
        elif decision == "accepted" and item.stage == "tail_catch_up":
            self.runtime.tail_mode = "catch_up"
        self._journal_runtime("decision", f"{opportunity_id}:{decision}")
        self.store.save_state(self.runtime)
        self.evaluate_safe()
        return item

    def set_ai_explanation(self, explanation: Optional[str]) -> None:
        self._ai_explanation = explanation
        if self._latest_snapshot is not None:
            self._latest_snapshot.ai_explanation = explanation

    def pending_email_notifications(self) -> list[Any]:
        if not self.config.email_notifications:
            return []
        return [
            item for item in self.runtime.opportunities.values()
            if item.status == "eligible" and item.notification_sent_at is None
            and item.policy_version == self.config.policy_version
        ]

    def mark_email_notification_sent(self, opportunity_id: str) -> None:
        item = self.runtime.opportunities.get(opportunity_id)
        if item is None or item.notification_sent_at is not None:
            return
        item.notification_sent_at = int(time.time())
        item.updated_at = item.notification_sent_at
        self._journal_runtime("market", f"邮件通知 {opportunity_id}")
        self.store.save_state(self.runtime)

    def get_snapshot(self) -> Optional[SpotAccumulationSnapshot]:
        if self.recovery_required or self.last_evaluation_error:
            return None
        return self._latest_snapshot or self.evaluate_safe()

    def evaluate_safe(self) -> Optional[SpotAccumulationSnapshot]:
        """运行时/API安全入口：记录错误并降级，不让单模块异常扩散到Engine。"""
        if self.recovery_required:
            return None
        try:
            snapshot = self.evaluate()
        except Exception as exc:  # noqa: BLE001 - 健康接口需要保留明确运行时原因
            self.last_evaluation_error = f"{type(exc).__name__}: {exc}"
            self.last_evaluation_error_at = int(time.time())
            logger.exception("spot accumulation evaluation failed")
            return None
        self.last_evaluation_error = ""
        self.last_evaluation_error_at = 0
        return snapshot

    def health(self) -> dict:
        if self.recovery_required:
            status = "recovery_required"
        elif self.last_evaluation_error:
            status = "error"
        elif self._latest_snapshot is None:
            status = "warming"
        else:
            status = "ready"
        return {
            "status": status,
            "recovery_required": self.recovery_required,
            "recovery_errors": list(self.recovery_errors),
            "last_evaluation_error": self.last_evaluation_error or None,
            "last_evaluation_error_at": self.last_evaluation_error_at or None,
            "latest_snapshot_at": self._latest_snapshot.timestamp if self._latest_snapshot else None,
            "schema_version": int(self.config.schema_version),
            "policy_version": int(self.config.policy_version),
            "data_quality": (
                self._latest_snapshot.facts.data_quality.model_dump(mode="json")
                if self._latest_snapshot else None
            ),
        }

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
        daily_atr_pct = self._daily_atr_pct(state)
        capitulation_confirmed = self._capitulation_confirmed(state)
        weekly_reclaim_confirmed = self._weekly_reclaim_confirmed(
            state, self.config.weekly_reclaim_weeks,
        )
        market_signature_before = self._runtime_market_signature()
        self._revalidate_opportunities(
            facts,
            now,
            portfolio,
            capitulation_confirmed=capitulation_confirmed,
            weekly_reclaim_confirmed=weekly_reclaim_confirmed,
        )
        reserved = self._reserved_by_bucket()
        opportunities = build_opportunities(
            facts,
            self.runtime,
            {name: pos.cash_usdt for name, pos in portfolio.buckets.items()},
            reserved,
            daily_atr_pct=daily_atr_pct,
            capitulation_confirmed=capitulation_confirmed,
            weekly_reclaim_confirmed=weekly_reclaim_confirmed,
            stage_allocations=self.config.core_stage_allocations(),
            config=self.config,
        )
        opportunities.extend(build_tail_opportunities(
            facts,
            self.runtime,
            portfolio.buckets["tail"].cash_usdt,
            reserved["tail"],
            daily_atr_pct=daily_atr_pct,
            capitulation_confirmed=capitulation_confirmed,
            weekly_reclaim_confirmed=weekly_reclaim_confirmed,
            tranche_usdt=self.config.tail_tranche_usdt,
            config=self.config,
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
            config=self.config,
        ))
        self._merge_opportunities(opportunities, now)
        if self._runtime_market_signature() != market_signature_before:
            self._journal_runtime("market", "市场条件驱动机会状态变化")
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
        raw_timestamps_value = self.long_term.get("timestamps") or {}
        raw_timestamps = raw_timestamps_value if isinstance(raw_timestamps_value, dict) else {}
        timestamps = {
            str(key): self._normal_ts(value)
            for key, value in raw_timestamps.items()
        }
        raw_parse_statuses = self.long_term.get("parse_status") or {}
        parse_statuses = raw_parse_statuses if isinstance(raw_parse_statuses, dict) else {}
        cycle = getattr(state, "cycle_position", None)
        market = getattr(state, "market_index", None)
        latest = lambda key: self._last_dict(self.long_term.get(key))
        nupl = latest("nupl")
        reserve = latest("reserve_risk")
        puell = latest("puell")
        sopr = latest("sth_sopr")
        raw_netflow = self.long_term.get("spot_netflow") or {}
        netflow = raw_netflow if isinstance(raw_netflow, dict) else {}
        raw_etf_items = self.long_term.get("etf_flow") or []
        etf_items = [
            item for item in raw_etf_items
            if isinstance(item, dict)
            and self._float(item.get("flow_usd")) is not None
            and self._normal_ts(item.get("timestamp", item.get("time", 0))) > 0
        ]
        if raw_etf_items and not etf_items and "etf_flow" not in parse_statuses:
            parse_statuses["etf_flow"] = "invalid_timestamp"
        etf_windows = {
            days: sum(self._float(item.get("flow_usd")) or 0 for item in etf_items[-days:])
            for days in (1, 3, 5, 20)
        }
        exchange_windows = {days: self._exchange_balance_change(days) for days in (1, 7, 30)}
        exchange_7d = exchange_windows[7]
        supply_change = self._series_change(self.long_term.get("sth_supply"), 30, "short_term_holder_supply")
        stablecoin_change = self._stablecoin_change(state)
        premium = self._float(getattr(getattr(state, "coinbase_premium", None), "current_premium", None))

        absorption = self._has_spot_absorption(state, price)
        persistent_wall, coinbase_wall = self._wall_evidence(state, price)
        reclaimed = self._key_level_reclaimed(state, price)
        taker_series = list(getattr(state, "taker_spot_series", None) or [])[-12:]
        taker_values = [self._float(item.get("delta_usd")) for item in taker_series]
        spot_taker = (
            sum(value for value in taker_values if value is not None)
            if taker_values and all(value is not None for value in taker_values) else None
        )
        cvd_spot = getattr(state, "cvd_spot", None)
        cvd_delta = self._float(getattr(cvd_spot, "delta_1h", None))
        cvd_divergence = self._spot_cvd_bottom_divergence(state)

        cycle_ts = self._normal_ts(getattr(cycle, "ts", 0))
        market_ts = self._normal_ts(getattr(market, "ts", 0))
        stable_ts = self._normal_ts(getattr(getattr(state, "stablecoin_mcap", None), "ts", 0))
        premium_ts = self._normal_ts(getattr(getattr(state, "coinbase_premium", None), "ts", 0))
        coinbase_book_ts = self._normal_ts(
            getattr(getattr(state, "coinbase_orderbook", None), "ts_sec", 0)
        )
        taker_ts = self._normal_ts(taker_series[-1].get("ts", 0)) if taker_series else 0
        key_level_ts = self._normal_ts(
            getattr(getattr(state, "key_level_snapshot_v2", None), "ts", 0)
        )

        source_ts = {
            **{key: int(value) for key, value in timestamps.items() if value},
            "ticker": self._normal_ts(getattr(state.ticker, "ts", now)),
            "market_index": market_ts,
            "cycle_position": cycle_ts,
            "stablecoin": stable_ts,
            "coinbase_premium": premium_ts,
            "coinbase_orderbook": coinbase_book_ts,
            "spot_cvd": self._normal_ts(
                getattr(cvd_spot.series[-1], "ts", 0) if cvd_spot and cvd_spot.series else 0
            ),
            "spot_taker": taker_ts,
            "footprint": int(getattr(state, "footprint_last_ts", 0) or 0),
            "footprint_spot": int(getattr(state, "footprint_spot_last_ts", 0) or 0),
            "orderbook_pressure": int(getattr(getattr(state, "orderbook_pressure_snapshot", None), "ts_sec", 0) or 0),
            "key_levels": key_level_ts,
        }
        ath = max(price, self.runtime.cycle_ath)
        drawdown = max(0.0, (ath - price) / ath * 100)
        valuation_inputs = {
            "mvrv": self._float(getattr(market, "btc_mvrv", None)),
            "ahr999": self._float(getattr(cycle, "ahr999_value", None))
            or self._float(getattr(market, "ahr999", None)),
            "price_vs_200w": self._ratio(price, getattr(cycle, "sma_200w", None)),
            "price_vs_sth": self._ratio(price, getattr(cycle, "sth_cost_1d", None)),
            "nupl": self._float(nupl.get("net_unpnl")) if nupl else None,
            "reserve_risk": self._float(reserve.get("reserve_risk_index")) if reserve else None,
            "puell": self._float(puell.get("puell_multiple")) if puell else None,
            "sth_sopr": self._float(sopr.get("sth_sopr")) if sopr else None,
            "sth_supply_change_30d_pct": supply_change,
        }
        capital_inputs = {
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
        }
        acceptance_inputs = {
            "spot_cvd_delta_1h": cvd_delta,
            "spot_cvd_bottom_divergence": cvd_divergence,
            "spot_taker_delta_1h": spot_taker,
            "footprint_absorption": absorption,
            "persistent_spot_wall": persistent_wall,
            "coinbase_confluence": coinbase_wall,
            "key_level_reclaimed": reclaimed,
        }
        metric_facts = {
            "drawdown_pct": self._metric_fact(drawdown, source_ts["ticker"], now, 180, "ticker"),
            "mvrv": self._metric_fact(valuation_inputs["mvrv"], market_ts, now, 21_600, "bbx_market_index"),
            "ahr999": self._metric_fact(valuation_inputs["ahr999"], cycle_ts, now, 86_400, "cycle_position"),
            "price_vs_200w": self._metric_fact(valuation_inputs["price_vs_200w"], cycle_ts, now, 86_400, "cycle_position"),
            "price_vs_sth": self._metric_fact(valuation_inputs["price_vs_sth"], cycle_ts, now, 86_400, "cycle_position"),
            "nupl": self._metric_fact(valuation_inputs["nupl"], timestamps.get("nupl", 0), now, 172_800, "coinglass_nupl", parse_statuses.get("nupl")),
            "reserve_risk": self._metric_fact(valuation_inputs["reserve_risk"], timestamps.get("reserve_risk", 0), now, 172_800, "coinglass_reserve_risk", parse_statuses.get("reserve_risk")),
            "puell": self._metric_fact(valuation_inputs["puell"], timestamps.get("puell", 0), now, 172_800, "coinglass_puell", parse_statuses.get("puell")),
            "sth_sopr": self._metric_fact(valuation_inputs["sth_sopr"], timestamps.get("sth_sopr", 0), now, 172_800, "coinglass_sth_sopr", parse_statuses.get("sth_sopr")),
            "sth_supply_change_30d_pct": self._metric_fact(valuation_inputs["sth_supply_change_30d_pct"], timestamps.get("sth_supply", 0), now, 172_800, "coinglass_sth_supply", parse_statuses.get("sth_supply")),
            "etf_flow_5d_usd": self._metric_fact(capital_inputs["etf_flow_5d_usd"], timestamps.get("etf_flow", 0), now, 3 * 86_400, "coinglass_etf", parse_statuses.get("etf_flow")),
            "exchange_balance_7d_pct": self._metric_fact(capital_inputs["exchange_balance_7d_pct"], timestamps.get("exchange_balance", 0), now, 172_800, "coinglass_exchange_balance", parse_statuses.get("exchange_balance")),
            "spot_netflow_24h_usd": self._metric_fact(capital_inputs["spot_netflow_24h_usd"], timestamps.get("spot_netflow", 0), now, 900, "coinglass_spot_netflow", parse_statuses.get("spot_netflow")),
            "stablecoin_change_7d_pct": self._metric_fact(capital_inputs["stablecoin_change_7d_pct"], stable_ts, now, 172_800, "stablecoin_mcap"),
            "coinbase_premium": self._metric_fact(capital_inputs["coinbase_premium"], premium_ts, now, 600, "coinbase_premium"),
            "spot_cvd_delta_1h": self._metric_fact(cvd_delta, source_ts["spot_cvd"], now, 900, "coinglass_spot_cvd"),
            "spot_taker_delta_1h": self._metric_fact(spot_taker, taker_ts, now, 900, "coinglass_spot_taker"),
            "footprint_absorption": self._metric_fact(absorption, source_ts["footprint_spot"], now, 900, "coinglass_spot_footprint"),
            "persistent_spot_wall": self._metric_fact(persistent_wall, source_ts["orderbook_pressure"], now, 600, "spot_orderbook_pressure"),
            "coinbase_confluence": self._metric_fact(coinbase_wall, coinbase_book_ts, now, 600, "coinbase_orderbook"),
            "key_level_reclaimed": self._metric_fact(reclaimed, key_level_ts, now, 900, "key_levels"),
        }
        quality = self._quality(metric_facts)
        facts = SpotAccumulationFacts(
            timestamp=now,
            price=price,
            cycle_ath=ath,
            drawdown_pct=drawdown,
            valuation_inputs=valuation_inputs,
            capital_inputs=capital_inputs,
            acceptance_inputs=acceptance_inputs,
            source_timestamps=source_ts,
            metric_facts=metric_facts,
            data_quality=quality,
        )
        facts.scores = score_facts(facts)
        if (
            metric_facts["spot_cvd_delta_1h"].included_in_score
            and metric_facts["spot_taker_delta_1h"].included_in_score
            and metric_facts["footprint_absorption"].included_in_score
            and cvd_delta is not None and cvd_delta < 0
            and spot_taker is not None and spot_taker < 0 and absorption is False
        ):
            facts.hard_vetoes.append("现货CVD与主动成交同步恶化且未见吸收")
        if cvd_divergence and metric_facts["spot_cvd_delta_1h"].included_in_score:
            facts.evidence.append("现货价格与CVD出现底背离")
        if absorption and metric_facts["footprint_absorption"].included_in_score:
            facts.evidence.append("Footprint存在现货被动吸收")
        if persistent_wall and metric_facts["persistent_spot_wall"].included_in_score:
            facts.evidence.append("下方存在持续现货买墙")
        if (
            exchange_7d is not None and exchange_7d < 0
            and metric_facts["exchange_balance_7d_pct"].included_in_score
        ):
            facts.evidence.append("交易所BTC余额下降")
        if etf_windows[5] > 0 and metric_facts["etf_flow_5d_usd"].included_in_score:
            facts.evidence.append("ETF近5日净流入")
        if facts.hard_vetoes:
            facts.data_quality.can_open_new_opportunity = False
        return facts

    @staticmethod
    def _quality(metrics: dict[str, SpotMetricFact]) -> SpotDataQuality:
        layers = {
            "valuation": [
                "drawdown_pct", "mvrv", "ahr999", "price_vs_200w", "price_vs_sth",
                "nupl", "reserve_risk", "puell", "sth_sopr", "sth_supply_change_30d_pct",
            ],
            "capital_flow": [
                "etf_flow_5d_usd", "exchange_balance_7d_pct", "spot_netflow_24h_usd",
                "stablecoin_change_7d_pct", "coinbase_premium",
            ],
            "acceptance": [
                "spot_cvd_delta_1h", "spot_taker_delta_1h", "footprint_absorption",
                "persistent_spot_wall", "coinbase_confluence", "key_level_reclaimed",
            ],
        }
        requirements = {"valuation": 6, "capital_flow": 3, "acceptance": 4}
        required_metrics = {
            "valuation": ["drawdown_pct"],
            "capital_flow": ["spot_netflow_24h_usd"],
            "acceptance": ["spot_cvd_delta_1h", "spot_taker_delta_1h"],
        }
        quality: dict[str, SpotLayerQuality] = {}
        for layer, names in layers.items():
            fresh = sum(bool(metrics[name].included_in_score) for name in names)
            blocking = [
                f"{name}缺失或过期" for name in required_metrics[layer]
                if not metrics[name].included_in_score
            ]
            if fresh < requirements[layer]:
                blocking.append(f"新鲜指标{fresh}/{len(names)}，至少需要{requirements[layer]}")
            if layer == "acceptance" and not any(
                metrics[name].included_in_score
                for name in ("footprint_absorption", "persistent_spot_wall", "key_level_reclaimed")
            ):
                blocking.append("Footprint、现货墙、关键位收回至少需要一项新鲜")
            quality[layer] = SpotLayerQuality(
                fresh_count=fresh,
                total_count=len(names),
                required_count=requirements[layer],
                required_metrics=required_metrics[layer],
                blocking_reasons=blocking,
                passed=not blocking,
            )
        missing = sorted(name for name, fact in metrics.items() if fact.freshness in {"missing", "invalid"})
        stale = sorted(name for name, fact in metrics.items() if fact.freshness == "stale")
        included = sum(fact.included_in_score for fact in metrics.values())
        can_open = all(item.passed for item in quality.values())
        notes = [reason for item in quality.values() for reason in item.blocking_reasons]
        return SpotDataQuality(
            completeness=included / len(metrics) if metrics else 0.0,
            stale_sources=stale,
            missing_sources=missing,
            notes=notes,
            can_open_new_opportunity=can_open,
            layer_quality=quality,
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

    def _runtime_market_signature(self) -> str:
        payload = [
            (
                item.opportunity_id, item.status, round(item.reserved_usdt, 8),
                round(item.filled_usdt, 8), item.policy_version, item.batch_id,
                item.batch_sequence, item.accepted_at, item.grace_expires_at,
            )
            for item in sorted(
                self.runtime.opportunities.values(),
                key=lambda current: current.opportunity_id,
            )
        ]
        return hashlib.sha1(repr(payload).encode()).hexdigest()

    def _core_stage_valid(
        self,
        stage: str,
        facts: SpotAccumulationFacts,
        *,
        capitulation_confirmed: bool,
        weekly_reclaim_confirmed: bool,
    ) -> bool:
        thresholds = self.config.core_thresholds.get(stage)
        if thresholds is None or facts.hard_vetoes or not facts.data_quality.can_open_new_opportunity:
            return False
        scores = facts.scores
        if (
            scores.valuation < thresholds["v"]
            or scores.capital_flow < thresholds["m"]
            or scores.acceptance < thresholds["a"]
        ):
            return False
        if stage == "capitulation" and not capitulation_confirmed:
            return False
        if stage == "bottom_confirmed" and not weekly_reclaim_confirmed:
            return False
        return True

    def _non_core_opportunity_valid(
        self,
        item: Any,
        facts: SpotAccumulationFacts,
        *,
        capitulation_confirmed: bool,
        weekly_reclaim_confirmed: bool,
    ) -> bool:
        if facts.hard_vetoes or not facts.data_quality.can_open_new_opportunity:
            return False
        scores = facts.scores
        if item.stage == "tail_extreme":
            return (
                scores.valuation >= self.config.tail_extreme_v
                and scores.acceptance >= self.config.tail_extreme_a
                and capitulation_confirmed
            )
        if item.stage == "tail_catch_up":
            return (
                scores.valuation >= self.config.tail_catch_up_v
                and scores.capital_flow >= self.config.tail_catch_up_m
                and scores.acceptance >= self.config.tail_catch_up_a
                and weekly_reclaim_confirmed
            )
        if item.stage == "swing":
            return bool(
                scores.acceptance >= 70
                and item.structural_stop
                and item.target_price
                and item.expected_rr is not None
                and item.expected_rr >= self.config.min_swing_rr
                and item.structural_stop < facts.price < item.target_price
            )
        return False

    def _revalidate_opportunities(
        self,
        facts: SpotAccumulationFacts,
        now: int,
        portfolio: Any,
        *,
        capitulation_confirmed: bool,
        weekly_reclaim_confirmed: bool,
    ) -> None:
        active_statuses = {"observing", "eligible", "accepted"}
        for item in self.runtime.opportunities.values():
            if item.status in active_statuses and item.policy_version != self.config.policy_version:
                item.status = "invalidated"
                item.reserved_usdt = 0.0
                item.updated_at = now

        core_batches: dict[str, list[Any]] = {}
        for item in self.runtime.opportunities.values():
            if item.bucket == "core" and item.batch_id and item.policy_version == self.config.policy_version:
                core_batches.setdefault(item.batch_id, []).append(item)
        for items in core_batches.values():
            items.sort(key=lambda current: current.batch_sequence or 0)
            pending = [item for item in items if item.status in active_statuses]
            if not pending:
                continue
            deepest = max(items, key=lambda current: current.batch_sequence or 0)
            valid = self._core_stage_valid(
                deepest.stage,
                facts,
                capitulation_confirmed=capitulation_confirmed,
                weekly_reclaim_confirmed=weekly_reclaim_confirmed,
            )
            active = next(
                (item for item in items if item.status in {"eligible", "accepted"}),
                None,
            )
            grace_active = bool(
                active and active.status == "accepted"
                and active.grace_expires_at and now < active.grace_expires_at
            )
            expired = bool(active and active.expires_at and active.expires_at < now)
            if expired and not grace_active:
                for item in pending:
                    item.status = "expired"
                    item.reserved_usdt = 0.0
                    item.updated_at = now
                continue
            if not valid and not grace_active:
                for item in pending:
                    item.status = "invalidated"
                    item.reserved_usdt = 0.0
                    item.updated_at = now
                continue
            if active is None and valid:
                next_item = next((item for item in items if item.status == "observing"), None)
                if next_item is not None:
                    other_reserved = sum(
                        item.reserved_usdt for item in self.runtime.opportunities.values()
                        if item.status in {"eligible", "accepted"} and item.opportunity_id != next_item.opportunity_id
                        and item.bucket == "core"
                    )
                    remaining = max(0.0, next_item.allocation_usdt - next_item.filled_usdt)
                    if portfolio.buckets["core"].cash_usdt - other_reserved + 0.01 >= remaining:
                        next_item.status = "eligible"
                        next_item.reserved_usdt = remaining
                        next_item.blocked_by = []
                        next_item.updated_at = now

        for item in self.runtime.opportunities.values():
            if item.bucket == "core" or item.status not in {"eligible", "accepted"}:
                continue
            valid = self._non_core_opportunity_valid(
                item,
                facts,
                capitulation_confirmed=capitulation_confirmed,
                weekly_reclaim_confirmed=weekly_reclaim_confirmed,
            )
            grace_active = bool(
                item.status == "accepted" and item.grace_expires_at
                and now < item.grace_expires_at
            )
            expired = bool(item.expires_at and item.expires_at < now)
            if (not valid or expired) and not grace_active:
                item.status = "expired" if expired else "invalidated"
                item.reserved_usdt = 0.0
                item.updated_at = now

    def _merge_opportunities(self, generated: list[Any], now: int) -> None:
        for old in self.runtime.opportunities.values():
            if old.expires_at and old.expires_at < now and old.status in {"observing", "eligible"}:
                old.status = "expired"
                old.reserved_usdt = 0
        for item in generated:
            if item.batch_id and item.bucket == "core":
                for old in self.runtime.opportunities.values():
                    if (
                        old.bucket == "core" and not old.batch_id
                        and old.policy_version == self.config.policy_version
                        and old.status == "observing"
                    ):
                        old.status = "invalidated"
                        old.reserved_usdt = 0.0
                        old.updated_at = now
            old = self.runtime.opportunities.get(item.opportunity_id)
            if old and old.status in {"accepted", "filled", "skipped"}:
                continue
            self.runtime.opportunities[item.opportunity_id] = item
        terminal = sorted(
            (
                item for item in self.runtime.opportunities.values()
                if item.status in {"skipped", "expired", "invalidated", "filled"}
            ),
            key=lambda current: current.updated_at,
            reverse=True,
        )
        keep_terminal = {item.opportunity_id for item in terminal[:200]}
        self.runtime.opportunities = {
            oid: item for oid, item in self.runtime.opportunities.items()
            if item.status in {"observing", "eligible", "accepted"} or oid in keep_terminal
        }

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
        current_states = {
            item.opportunity_id: item.status for item in snapshot.opportunities
        }
        changes = [
            {
                "opportunity_id": opportunity_id,
                "from": self._last_archived_opportunity_states.get(opportunity_id),
                "to": status,
            }
            for opportunity_id, status in current_states.items()
            if self._last_archived_opportunity_states.get(opportunity_id) != status
        ]
        changes.extend(
            {
                "opportunity_id": opportunity_id,
                "from": status,
                "to": "removed",
            }
            for opportunity_id, status in self._last_archived_opportunity_states.items()
            if opportunity_id not in current_states
        )
        metric_breakdown = {
            name: {
                "value": fact.value,
                "source_timestamp": fact.source_timestamp,
                "freshness": fact.freshness,
                "parse_status": fact.parse_status,
                "included_in_score": fact.included_in_score,
                "score": fact.score,
                "source": fact.source,
            }
            for name, fact in snapshot.facts.metric_facts.items()
        }
        blocking = sorted(set(
            list(snapshot.facts.hard_vetoes)
            + list(snapshot.facts.data_quality.notes)
            + [reason for item in snapshot.opportunities for reason in item.blocked_by]
        ))
        payload = {
            "archive_schema_version": 2,
            "record_type": "spot_accumulation_full_fact_snapshot",
            "capability": "live_full_stack_shadow",
            "timestamp": snapshot.timestamp,
            "coin": snapshot.coin,
            "policy_version": self.config.policy_version,
            "config": self.config.public_dump(),
            "facts": snapshot.facts.model_dump(mode="json"),
            "score_breakdown": metric_breakdown,
            "opportunities": [
                item.model_dump(mode="json") for item in snapshot.opportunities
            ],
            "opportunity_changes": changes,
            "blocking_reasons": blocking,
            "portfolio": snapshot.portfolio.model_dump(mode="json"),
            "budget_reserved_usdt": snapshot.budget_reserved_usdt,
            "next_action": snapshot.next_action,
            "warnings": snapshot.warnings,
        }
        digest_source = {
            "policy_version": payload["policy_version"],
            "price": snapshot.facts.price,
            "metric_breakdown": metric_breakdown,
            "data_quality": snapshot.facts.data_quality.model_dump(mode="json"),
            "opportunities": [
                {
                    "id": item.opportunity_id,
                    "status": item.status,
                    "reserved": item.reserved_usdt,
                    "filled": item.filled_usdt,
                    "blocked_by": item.blocked_by,
                }
                for item in snapshot.opportunities
            ],
        }
        digest = hashlib.sha1(
            json.dumps(digest_source, sort_keys=True, default=str).encode()
        ).hexdigest()
        if digest != self._last_snapshot_hash:
            self.store.append_facts_snapshot(payload, snapshot.timestamp)
            self._last_snapshot_hash = digest
            self._last_archived_opportunity_states = current_states

    @staticmethod
    def _last_dict(value: Any) -> dict:
        return value[-1] if isinstance(value, list) and value and isinstance(value[-1], dict) else {}

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value) if value is not None else None
            return parsed if parsed is not None and math.isfinite(parsed) else None
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def _metric_fact(
        cls,
        value: Any,
        source_timestamp: Any,
        now: int,
        ttl: int,
        source: str,
        parse_status: Optional[str] = None,
    ) -> SpotMetricFact:
        allowed_statuses = {
            "ok", "missing", "empty", "invalid_type", "missing_field",
            "invalid_timestamp", "non_finite", "request_error",
        }
        status = parse_status if parse_status in allowed_statuses else None
        if value is None:
            status = status or "missing_field"
            freshness = "missing" if status in {"missing", "empty", "missing_field"} else "invalid"
            return SpotMetricFact(
                value=None, source_timestamp=0, freshness=freshness,
                parse_status=status, included_in_score=False, source=source,
            )
        if isinstance(value, float) and not math.isfinite(value):
            return SpotMetricFact(
                value=None, source_timestamp=0, freshness="invalid",
                parse_status="non_finite", included_in_score=False, source=source,
            )
        if status and status != "ok":
            return SpotMetricFact(
                value=value, source_timestamp=max(0, cls._normal_ts(source_timestamp)),
                freshness="invalid", parse_status=status,
                included_in_score=False, source=source,
            )
        ts = cls._normal_ts(source_timestamp)
        if ts <= 0 or ts > now + 300:
            return SpotMetricFact(
                value=value, source_timestamp=max(0, ts), freshness="invalid",
                parse_status="invalid_timestamp", included_in_score=False, source=source,
            )
        fresh = now - ts <= ttl
        return SpotMetricFact(
            value=value,
            source_timestamp=ts,
            freshness="fresh" if fresh else "stale",
            parse_status="ok",
            included_in_score=fresh,
            source=source,
        )

    @classmethod
    def _ratio(cls, numerator: float, denominator: Any) -> Optional[float]:
        den = cls._float(denominator)
        return numerator / den if den and den > 0 else None

    @staticmethod
    def _normal_ts(value: Any) -> int:
        try:
            ts = int(value)
            return ts // 1000 if ts > 10_000_000_000 else ts
        except (TypeError, ValueError, OverflowError):
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

    def _has_spot_absorption(self, state: Any, price: float) -> Optional[bool]:
        spot_bars = list(getattr(state, "footprint_spot", None) or [])
        if not spot_bars:
            return None
        try:
            from processors.absorption_detector import detect_absorption_zones
            snap = detect_absorption_zones(
                footprint_contract=None,
                footprint_spot=spot_bars,
                current_price=price,
            )
            zone = snap.strongest_support
            return bool(zone and 0 <= (price - zone.price) / price * 100 <= 5)
        except Exception:
            logger.warning("spot accumulation spot absorption parse failed", exc_info=True)
            return None

    @staticmethod
    def _wall_evidence(state: Any, price: float) -> tuple[Optional[bool], Optional[bool]]:
        snap = getattr(state, "orderbook_pressure_snapshot", None)
        if snap is None:
            return None, None
        coinbase_available = getattr(state, "coinbase_orderbook", None) is not None
        walls = getattr(snap, "walls_below", None) or []
        now = int(time.time())

        def wall_is_fresh(wall: Any) -> bool:
            ts = int(getattr(wall, "last_seen_ts", 0) or 0)
            if ts > 10_000_000_000:
                ts //= 1000
            return ts > 0 and 0 <= now - ts <= 600

        valid = [
            wall for wall in walls
            if 0 <= (price - wall.price_mid) / price * 100 <= 5
            and wall_is_fresh(wall)
            and wall.persistence_score >= 0.5
            and wall.support_resistance_trust_score >= 0.65
            and (
                (getattr(wall, "dual_source", False) and getattr(wall, "spot_current_usd", 0) > 0)
                or (getattr(wall, "has_spot_confluence", False) and bool(getattr(wall, "spot_large_order_ids", [])))
                or (
                    getattr(wall, "coinbase_spot_confluence", False)
                    and getattr(wall, "coinbase_spot_usd", 0) > 0
                    and getattr(wall, "coinbase_num_orders", 0) >= 3
                )
            )
        ]
        coinbase = (
            any(getattr(wall, "coinbase_spot_confluence", False) for wall in valid)
            if coinbase_available else None
        )
        return bool(valid), coinbase

    @staticmethod
    def _key_level_reclaimed(state: Any, price: float) -> Optional[bool]:
        snap = getattr(state, "key_level_snapshot_v2", None)
        if snap is None:
            return None
        for level in getattr(snap, "levels", None) or []:
            if level.side != "support" or level.price <= 0:
                continue
            if abs(price - level.price) / price * 100 > 5:
                continue
            behavior = getattr(level, "behavior", None)
            behavior_state = getattr(behavior, "behavior_state", "")
            if level.state in {"bounced", "fake_break", "flipped"} or behavior_state in {
                "healthy_retest", "failed_breakout", "confirmed_flip",
            }:
                return True
        return False

    @staticmethod
    def _capitulation_confirmed(state: Any) -> bool:
        snap = getattr(state, "key_level_snapshot_v2", None)
        for level in (getattr(snap, "levels", None) or []):
            behavior = getattr(level, "behavior", None)
            if (
                level.side == "support"
                and float(getattr(behavior, "capitulation_bottom_score", 0.0) or 0.0) >= 0.65
                and getattr(behavior, "behavior_state", "")
                in {"capitulation_flush", "confirmed_flip", "healthy_retest"}
            ):
                return True
        return False

    @staticmethod
    def _spot_cvd_bottom_divergence(state: Any) -> bool:
        cvd = getattr(state, "cvd_spot", None)
        if cvd is None or not getattr(cvd, "series", None):
            return False
        prices = list(getattr(state, "candle_prices", None) or [])
        price_ts = list(getattr(state, "candle_ts", None) or [])
        if not prices or len(prices) != len(price_ts):
            candles = list(getattr(state, "candles_1h", None) or [])
            prices = [float(candle.close) for candle in candles]
            price_ts = [int(candle.ts) for candle in candles]
        if len(prices) < 2:
            return False
        try:
            from processors.cvd import compute_cvd_price_divergence
            normalized = cvd.model_copy(deep=True)
            for point in normalized.series:
                if int(point.ts) < 10_000_000_000:
                    point.ts = int(point.ts) * 1000
            normalized_ts = [int(ts) * 1000 if int(ts) < 10_000_000_000 else int(ts) for ts in price_ts]
            result = compute_cvd_price_divergence(normalized, prices, normalized_ts)
            return result.has_divergence and "底背离" in result.note
        except (TypeError, ValueError, AttributeError):
            return False

    @staticmethod
    def _weekly_reclaim_confirmed(state: Any, required_weeks: int = 2) -> bool:
        candles = list(getattr(state, "candles_weekly", None) or [])
        from polls.candles import strip_unclosed_last
        candles = strip_unclosed_last(candles, 7 * 24 * 3600)
        required_weeks = max(1, int(required_weeks))
        if len(candles) < 20 + required_weeks:
            return False
        closes = [float(c.close) for c in candles]
        start = len(closes) - required_weeks
        confirmed: list[float] = []
        for index in range(start, len(closes)):
            window = closes[index - 20:index]
            if len(window) < 20 or closes[index] <= sum(window) / 20:
                return False
            confirmed.append(closes[index])
        return all(current >= previous for previous, current in zip(confirmed, confirmed[1:]))

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
