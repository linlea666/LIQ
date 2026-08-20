"""联合风险 shadow 引擎：固定 tick、PIT 证据、因果根去重与 fail-closed 状态机。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any, Optional

from config.settings import MarketRiskConfig
from models.liquidation import pick_primary_liq_map
from models.market_risk import (
    CalibrationArtifact,
    EstimatedLiquidationDensity,
    EvidenceItem,
    MarketIncidentSnapshot,
    MarketRiskHealth,
    MarketRiskMachineContext,
    MarketRiskTransition,
    PillarSnapshot,
    RealizedLiquidationFlow,
    SourceQuality,
)
from storage.market_risk_store import MarketRiskStore
from storage.onchain_entity_store import OnchainEntityStore
from storage.raw_event_store import RawEventStore, set_raw_event_store

logger = logging.getLogger(__name__)

_ACTIVE_STAGES = {"watch", "warning", "critical"}
_STAGE_RANK = {"normal": 0, "watch": 1, "warning": 2, "critical": 3}


def _seconds(ts: Any) -> int:
    try:
        value = int(float(ts or 0))
    except (TypeError, ValueError):
        return 0
    while value > 10_000_000_000:
        value //= 1000
    return value


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def compute_leverage_refill_ratio(oi: Any, perp_imbalance: float) -> dict[str, Any]:
    """标准化 OI 去杠杆后的回堆比例；不从 USD OI 推断。"""
    points = [
        point for point in list(getattr(oi, "history", []) or [])
        if getattr(point, "decision_valid", False)
        and getattr(point, "oi_contracts", None) is not None
    ]
    if len(points) < 4:
        return {"status": "unavailable", "reason": "insufficient_standardized_oi"}
    values = [float(point.oi_contracts) for point in points]
    # 找最大 peak→trough 释放段；使用 contracts 避免价格机械变化。
    peak_value = values[0]
    peak_index = 0
    best = (0.0, 0, 0)
    for index, value in enumerate(values[1:], 1):
        if peak_value - value > best[0]:
            best = (peak_value - value, peak_index, index)
        if value > peak_value:
            peak_value, peak_index = value, index
    released, anchor_index, trough_index = best
    denominator_floor = max(abs(values[anchor_index]) * 0.002, 1e-9)
    if released <= denominator_floor:
        return {"status": "unavailable", "reason": "release_denominator_too_small"}
    after_trough = values[trough_index:]
    refill_high = max(after_trough)
    current = values[-1]
    refilled = current - values[trough_index]
    ratio = refilled / released
    prior_refill = refill_high - values[trough_index]
    secondary_dip = prior_refill >= released * 0.2 and current < refill_high - released * 0.2
    if secondary_dip:
        status = "secondary_deleveraging"
    elif ratio > 1.25:
        status = "over_refill"
    else:
        status = "refilling"
    direction = (
        "up" if perp_imbalance >= 0.08 else "down" if perp_imbalance <= -0.08 else "unknown"
    )
    return {
        "status": status, "ratio": ratio, "released_contracts": released,
        "refilled_contracts": refilled, "refill_high_contracts": refill_high,
        "secondary_dip": secondary_dip, "direction": direction,
        "anchor_time": int(points[anchor_index].ts),
        "trough_time": int(points[trough_index].ts),
    }


class MarketRiskEngine:
    def __init__(
        self,
        *,
        config: MarketRiskConfig,
        state_getter: Callable[[str], Any],
        backend_root: str,
        email_config: Optional[Any] = None,
    ) -> None:
        self.config = config
        self._state_getter = state_getter
        self._backend_root = backend_root
        self._email_config = email_config
        data_dir = config.data_dir
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(backend_root, data_dir)
        self.store = MarketRiskStore(data_dir)
        self.onchain_store = OnchainEntityStore(data_dir)
        self.raw_event_store: Optional[RawEventStore] = None
        if config.raw_event_store_enabled:
            self.raw_event_store = RawEventStore(
                os.path.join(data_dir, "events"),
                queue_max=config.raw_event_queue_max,
                batch_size=config.raw_event_batch_size,
            )
            set_raw_event_store(self.raw_event_store)
        self.calibration = self._load_calibration(config.calibration_artifact)
        self.store.save_calibration(self.calibration)
        self._contexts: dict[str, MarketRiskMachineContext] = {
            coin: self.store.load_machine_context(coin)
            or MarketRiskMachineContext(coin=coin)
            for coin in config.coins
        }
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._outbox_task: Optional[asyncio.Task] = None
        self._last_tick_at = 0
        self._last_prune_at = 0
        self._last_error = ""

    def _load_calibration(self, path: str) -> CalibrationArtifact:
        resolved = path if os.path.isabs(path) else os.path.join(self._backend_root, path)
        with open(resolved, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        artifact = CalibrationArtifact.model_validate(raw)
        required = {
            "spot_taker_imbalance_early", "spot_taker_imbalance_extreme",
            "spot_min_quote_usd", "oi_change_1h_early_pct", "oi_change_1h_extreme_pct",
            "funding_abs_extreme", "liquidation_1h_early_usd",
            "liquidation_1h_extreme_usd", "liquidation_density_early_usd",
            "liquidation_density_extreme_usd", "wall_attack_early",
            "wall_attack_extreme", "price_move_5m_feedback_pct",
            "warning_confidence", "critical_confidence", "quiet_to_cooldown_sec",
            "cooldown_to_resolved_sec", "resolved_to_normal_sec", "episode_gap_sec",
        }
        missing = sorted(required - set(artifact.thresholds))
        unknown = sorted(set(artifact.thresholds) - required)
        if missing or unknown:
            raise ValueError(
                f"invalid market-risk calibration missing={missing} unknown={unknown}"
            )
        return artifact

    async def start(self) -> None:
        if self._running or not self.config.enabled:
            return
        self._running = True
        if self.raw_event_store is not None:
            self.raw_event_store.start()
        self._task = asyncio.create_task(self._run_loop(), name="market_risk_engine")
        self._outbox_task = asyncio.create_task(
            self._run_outbox(), name="market_risk_email_outbox",
        )
        logger.info(
            "market risk engine started | coins=%s shadow=%s calibration=%s admitted=%s",
            self.config.coins, self.config.shadow_mode,
            self.calibration.calibration_version,
            self.calibration.admitted_for_production,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
            self._outbox_task = None
        if self.raw_event_store is not None:
            self.raw_event_store.stop()
            set_raw_event_store(None)
        self.store.close()
        self.onchain_store.close()

    async def _run_loop(self) -> None:
        while self._running:
            tick_started = time.time()
            for coin in self.config.coins:
                try:
                    await asyncio.to_thread(self.evaluate_coin, coin)
                except Exception as exc:  # noqa: BLE001
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("market risk tick failed | coin=%s", coin)
            self._last_tick_at = int(time.time())
            if self._last_tick_at - self._last_prune_at >= 3600:
                try:
                    await asyncio.to_thread(self.store.prune)
                    if self.raw_event_store is not None:
                        await asyncio.to_thread(self.raw_event_store.prune)
                    self._last_prune_at = self._last_tick_at
                except Exception:
                    logger.exception("market risk retention prune failed")
            elapsed = time.time() - tick_started
            await asyncio.sleep(max(0.25, self.config.tick_interval_sec - elapsed))

    async def _run_outbox(self) -> None:
        """复用现有 SMTP；只有 OOS artifact 已准入且独立开关开启才可能发送。"""
        from notifications.email_alert import send_html_email_result

        while self._running:
            try:
                for item in self.store.due_outbox(limit=10):
                    channel_enabled = bool(
                        self._email_channel_enabled()
                        and not self.config.shadow_mode
                        and self.calibration.admitted_for_production
                    )
                    if not channel_enabled:
                        self.store.suppress_outbox(item["id"], "market-risk email channel disabled")
                        continue
                    complete = bool(
                        getattr(self._email_config, "smtp_host", "")
                        and int(getattr(self._email_config, "smtp_port", 0) or 0) > 0
                        and getattr(self._email_config, "smtp_user", "")
                        and getattr(self._email_config, "smtp_pass", "")
                        and getattr(self._email_config, "to", [])
                    )
                    if not complete:
                        self.store.defer_outbox(item["id"], "SMTP configuration incomplete")
                        continue
                    result = await send_html_email_result(
                        item["subject"], item["html"], self._email_config,
                        log_context="BTC market risk",
                        idempotency_key=item["dedup_key"],
                    )
                    if result.ok:
                        self.store.mark_outbox_sent(item["id"])
                    else:
                        self.store.mark_outbox_failed(
                            item["id"], int(item["attempts"]) + 1,
                            result.error or "SMTP send failed",
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("market risk outbox worker failed")
            await asyncio.sleep(15)

    def _email_channel_enabled(self) -> bool:
        return bool(
            self.config.email_enabled
            and self._email_config is not None
            and getattr(self._email_config, "enabled", False)
            and getattr(self._email_config, "include_market_risk", True)
        )

    def latest(self, coin: str) -> Optional[MarketIncidentSnapshot]:
        return self.store.latest(coin)

    def health(self) -> MarketRiskHealth:
        latest_by_coin = {}
        quality = {}
        for coin in self.config.coins:
            snap = self.store.latest(coin)
            if snap is not None:
                latest_by_coin[coin] = snap.decision_time
                quality[coin] = snap.source_quality
        return MarketRiskHealth(
            enabled=self.config.enabled,
            running=self._running,
            shadow_mode=self.config.shadow_mode,
            config_version=self.config.version_hash,
            calibration_version=self.calibration.calibration_version,
            calibration_admitted=self.calibration.admitted_for_production,
            last_tick_at=self._last_tick_at,
            last_error=self._last_error,
            latest_by_coin=latest_by_coin,
            source_quality=quality,
            raw_event_store=(
                self.raw_event_store.health() if self.raw_event_store is not None
                else {"enabled": False, "status": "disabled"}
            ),
            outbox=self.store.outbox_stats(),
        )

    def _quality(
        self, source_id: str, as_of: int, max_age: int, now: int, *,
        available: bool = True, continuous: Optional[bool] = True,
        valid: bool = True, reasons: Optional[list[str]] = None,
        completeness: float = 1.0,
    ) -> SourceQuality:
        reasons = list(reasons or [])
        age = max(0, now - as_of) if as_of else None
        fresh = bool(as_of and age is not None and age <= max_age)
        if not available:
            reasons.append("source_unavailable")
        if available and not as_of:
            reasons.append("missing_as_of")
        if as_of and not fresh:
            reasons.append(f"stale_age_{age}s")
        if continuous is False:
            reasons.append("source_gap")
        if available and completeness < 0.8:
            reasons.append("window_incomplete")
        decision_usable = bool(available and fresh and continuous is not False and valid)
        return SourceQuality(
            source_id=source_id,
            availability="available" if available else "unavailable",
            freshness="fresh" if fresh else ("stale" if as_of else "unknown"),
            completeness=_clamp(completeness),
            continuity=(
                "continuous" if continuous is True else "gap" if continuous is False
                else "snapshot_only"
            ),
            validity="valid" if valid else "invalid",
            as_of=as_of,
            observed_at=now,
            watermark=as_of,
            decision_usable=decision_usable,
            reasons=list(dict.fromkeys(reasons)),
        )

    def _evidence(
        self, *, coin: str, pillar: str, causal_root: str, name: str,
        direction: str, strength: float, confidence: float, event_time: int,
        now: int, source_id: str, values: dict[str, Any], explanation: str,
        role: str = "scoring", source_sequence: Optional[str] = None,
    ) -> EvidenceItem:
        canonical_values = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
        evidence_id = _stable_id(
            "ev", coin, pillar, causal_root, name, direction, event_time,
            source_id, source_sequence or "", canonical_values,
        )
        return EvidenceItem(
            evidence_id=evidence_id, coin=coin, pillar=pillar,
            causal_root=causal_root, name=name, direction=direction,
            role=role, strength=_clamp(strength), confidence=_clamp(confidence),
            event_time=event_time or now, observed_at=now, decision_time=now,
            watermark=event_time or now, source_sequence=source_sequence,
            source_id=source_id, config_version=self.config.version_hash,
            calibration_version=self.calibration.calibration_version,
            values=values, explanation=explanation,
        )

    def _trade_flow(self, coin: str, market: str) -> Optional[dict[str, Any]]:
        try:
            from sources.binance_trades_ws import get_trades_ws
            source = get_trades_ws()
            result = source.aggressor_flow(coin, market, 300) if source else None
            raw_gap = self.raw_event_store.recent_gap(coin, market, 300) if self.raw_event_store else None
            if result is not None and raw_gap is not None:
                result["continuity"] = "gap"
                result["gap_reason"] = str(raw_gap.get("reason") or "raw_event_gap")
            return result
        except Exception:
            return None

    def _extract(
        self, coin: str, state: Any, now: int,
    ) -> tuple[
        list[EvidenceItem], dict[str, SourceQuality],
        Optional[RealizedLiquidationFlow], list[EstimatedLiquidationDensity], dict[str, Any],
    ]:
        thresholds = self.calibration.thresholds
        evidence: list[EvidenceItem] = []
        qualities: dict[str, SourceQuality] = {}

        # 1) 现货真实主动成交；CVD/taker 只作为同一 causal root 的置信增强。
        spot_flow = self._trade_flow(coin, "spot")
        spot_as_of = int((spot_flow or {}).get("as_of") or 0) + (59 if spot_flow else 0)
        spot_continuous = (spot_flow or {}).get("continuity") == "continuous"
        spot_first = int((spot_flow or {}).get("first_bucket_ts") or 0)
        spot_completeness = _clamp(
            ((spot_as_of - 59) - spot_first + 60) / 300
        ) if spot_as_of and spot_first else 0.0
        qualities["binance_spot_aggtrade"] = self._quality(
            "binance_spot_aggtrade", spot_as_of,
            self.config.source_max_age_sec["spot_demand"], now,
            available=spot_flow is not None,
            continuous=spot_continuous if spot_flow else None,
            valid=bool(
                spot_flow and float(spot_flow.get("total_quote") or 0) > 0
                and spot_completeness >= 0.8
            ),
            reasons=[str((spot_flow or {}).get("gap_reason"))]
            if (spot_flow or {}).get("gap_reason") else [],
            completeness=spot_completeness,
        )
        spot_buy = float((spot_flow or {}).get("aggressor_buy_quote") or 0)
        spot_sell = float((spot_flow or {}).get("aggressor_sell_quote") or 0)
        spot_total = spot_buy + spot_sell
        spot_imbalance = (spot_buy - spot_sell) / spot_total if spot_total > 0 else 0.0
        spot_early = thresholds["spot_taker_imbalance_early"]
        spot_extreme = thresholds["spot_taker_imbalance_extreme"]
        if (
            qualities["binance_spot_aggtrade"].decision_usable
            and spot_total >= thresholds["spot_min_quote_usd"]
            and abs(spot_imbalance) >= spot_early
        ):
            strength = abs(spot_imbalance) / max(spot_extreme, 1e-9)
            direction = "up" if spot_imbalance > 0 else "down"
            confidence = 0.65 if abs(spot_imbalance) < spot_extreme else 0.82
            cvd = getattr(state, "cvd_spot", None)
            cvd_trend = str(getattr(cvd, "trend_1h", "") or "").lower()
            if (direction == "up" and cvd_trend in {"rising", "up"}) or (
                direction == "down" and cvd_trend in {"declining", "falling", "down"}
            ):
                confidence = min(0.95, confidence + 0.08)
            evidence.append(self._evidence(
                coin=coin, pillar="spot_demand", causal_root="spot_demand",
                name="spot_aggressor_imbalance_5m", direction=direction,
                strength=strength, confidence=confidence,
                event_time=spot_as_of, now=now, source_id="binance_spot_aggtrade",
                values={
                    "aggressor_buy_quote": spot_buy,
                    "aggressor_sell_quote": spot_sell,
                    "imbalance": spot_imbalance,
                    "cvd_trend_1h": cvd_trend,
                },
                explanation="现货全量主动成交失衡；CVD 仅增强同一资金根，不重复计票。",
            ))

        # 2) 杠杆发起：全量合约主动成交 + 标准化 contracts/base OI。
        futures_flow = self._trade_flow(coin, "futures")
        futures_as_of = int((futures_flow or {}).get("as_of") or 0) + (59 if futures_flow else 0)
        futures_continuous = (futures_flow or {}).get("continuity") == "continuous"
        futures_first = int((futures_flow or {}).get("first_bucket_ts") or 0)
        futures_completeness = _clamp(
            ((futures_as_of - 59) - futures_first + 60) / 300
        ) if futures_as_of and futures_first else 0.0
        qualities["binance_futures_aggtrade"] = self._quality(
            "binance_futures_aggtrade", futures_as_of,
            self.config.source_max_age_sec["leveraged_positioning"], now,
            available=futures_flow is not None,
            continuous=futures_continuous if futures_flow else None,
            valid=bool(
                futures_flow and float(futures_flow.get("total_quote") or 0) > 0
                and futures_completeness >= 0.8
            ),
            reasons=[str((futures_flow or {}).get("gap_reason"))]
            if (futures_flow or {}).get("gap_reason") else [],
            completeness=futures_completeness,
        )
        oi = getattr(state, "oi", None)
        oi_as_of = _seconds(getattr(oi, "ts", 0))
        oi_valid = bool(getattr(oi, "decision_valid", False))
        qualities["standardized_oi"] = self._quality(
            "standardized_oi", oi_as_of,
            self.config.source_max_age_sec["leveraged_positioning"], now,
            available=oi is not None, continuous=True,
            valid=oi_valid,
            reasons=[] if oi_valid else ["contracts_or_base_change_unavailable"],
        )
        f_buy = float((futures_flow or {}).get("aggressor_buy_quote") or 0)
        f_sell = float((futures_flow or {}).get("aggressor_sell_quote") or 0)
        f_total = f_buy + f_sell
        f_imbalance = (f_buy - f_sell) / f_total if f_total > 0 else 0.0
        oi_change = getattr(oi, "decision_change_1h_pct", None)
        lrr = compute_leverage_refill_ratio(oi, f_imbalance) if oi is not None else {
            "status": "unavailable", "reason": "standardized_oi_unavailable",
        }
        if (
            qualities["standardized_oi"].decision_usable and oi_change is not None
            and abs(float(oi_change)) >= thresholds["oi_change_1h_early_pct"]
        ):
            if float(oi_change) > 0:
                direction = "up" if f_imbalance > 0 else "down" if f_imbalance < 0 else "unknown"
                confidence = 0.58
                if qualities["binance_futures_aggtrade"].decision_usable and abs(f_imbalance) >= spot_early:
                    confidence = 0.75 if abs(float(oi_change)) < thresholds["oi_change_1h_extreme_pct"] else 0.86
                evidence.append(self._evidence(
                    coin=coin, pillar="leveraged_positioning",
                    causal_root="leveraged_initiation", name="standardized_oi_with_perp_flow",
                    direction=direction, strength=abs(float(oi_change)) / thresholds["oi_change_1h_extreme_pct"],
                    confidence=confidence, event_time=max(oi_as_of, futures_as_of), now=now,
                    source_id="binance_fapi",
                    values={
                        "oi_change_1h_pct": float(oi_change),
                        "decision_unit": getattr(oi, "decision_unit", "unavailable"),
                        "perp_imbalance_5m": f_imbalance,
                    },
                    explanation="标准化 OI 增长与合约主动成交共同描述新增杠杆，不使用 USD OI 变化。",
                ))

            else:
                evidence.append(self._evidence(
                    coin=coin, pillar="leveraged_positioning", causal_root="position_unwind",
                    name="standardized_oi_unwind", direction=(
                        "up" if f_imbalance > 0 else "down" if f_imbalance < 0 else "unknown"
                    ), strength=abs(float(oi_change)) / thresholds["oi_change_1h_extreme_pct"],
                    confidence=0.62, event_time=oi_as_of, now=now,
                    source_id="binance_fapi_open_interest_hist",
                    values={"oi_change_1h_pct": float(oi_change)},
                    explanation="OI 下降只表示去杠杆；需与强平或方向成交结合解释。",
                ))

        if lrr.get("status") != "unavailable":
            evidence.append(self._evidence(
                coin=coin, pillar="leveraged_positioning",
                causal_root="leverage_refill", name="leverage_refill_ratio",
                direction=str(lrr.get("direction") or "unknown"),
                strength=min(abs(float(lrr.get("ratio") or 0)), 1.0),
                confidence=0.5 if lrr.get("direction") != "unknown" else 0.25,
                event_time=oi_as_of or now, now=now,
                source_id="standardized_oi_lrr", values=lrr,
                explanation="LRR 首版仅供观察；分母过小、二次下探和超额回堆已显式分型。",
                role="informational",
            ))

        funding = getattr(state, "funding", None)
        funding_as_of = _seconds(getattr(funding, "observed_at", 0) or getattr(funding, "ts", 0))
        funding_rate = getattr(funding, "predicted_rate_observed", None)
        qualities["funding"] = self._quality(
            "funding", funding_as_of,
            self.config.source_max_age_sec["leveraged_positioning"], now,
            available=funding is not None, continuous=True, valid=funding_rate is not None,
        )
        if (
            qualities["funding"].decision_usable and funding_rate is not None
            and abs(float(funding_rate)) >= thresholds["funding_abs_extreme"]
        ):
            evidence.append(self._evidence(
                coin=coin, pillar="leveraged_positioning", causal_root="leveraged_initiation",
                name="predicted_funding_crowding", direction="down" if float(funding_rate) > 0 else "up",
                strength=abs(float(funding_rate)) / thresholds["funding_abs_extreme"],
                confidence=0.55, event_time=funding_as_of, now=now, source_id="official_funding",
                values={
                    "predicted_rate_observed": float(funding_rate),
                    "last_settled_rate": getattr(funding, "last_settled_rate", None),
                    "next_funding_time": getattr(funding, "next_funding_time", 0),
                }, explanation="资金费率只描述拥挤方向，不能单独触发正式 warning。",
            ))

        # 3) 已实现强平和估算密度严格分型。
        global_liq = getattr(state, "global_liq", None)
        liq_as_of = _seconds(getattr(global_liq, "ts", 0))
        qualities["realized_liquidations"] = self._quality(
            "realized_liquidations", liq_as_of,
            self.config.source_max_age_sec["liquidation_risk"], now,
            available=global_liq is not None, continuous=True, valid=global_liq is not None,
        )
        realized = None
        if global_liq is not None:
            long_usd = float(getattr(global_liq, "long_1h_usd", 0) or 0)
            short_usd = float(getattr(global_liq, "short_1h_usd", 0) or 0)
            realized = RealizedLiquidationFlow(
                coin=coin, window_sec=3600,
                long_executed_notional_usd=long_usd,
                short_executed_notional_usd=short_usd,
                executed_notional_usd=long_usd + short_usd,
                event_time=liq_as_of or now, observed_at=now,
                source_id="coinglass_realized_liquidation", quality=qualities["realized_liquidations"],
            )
            dominant = max(long_usd, short_usd)
            if dominant >= thresholds["liquidation_1h_early_usd"]:
                direction = "down" if long_usd > short_usd else "up"
                evidence.append(self._evidence(
                    coin=coin, pillar="liquidation_risk", causal_root="position_unwind",
                    name="realized_liquidation_flow_1h", direction=direction,
                    strength=dominant / thresholds["liquidation_1h_extreme_usd"],
                    confidence=0.7 if dominant < thresholds["liquidation_1h_extreme_usd"] else 0.88,
                    event_time=liq_as_of or now, now=now,
                    source_id="coinglass_realized_liquidation",
                    values={"long_executed_notional_usd": long_usd,
                            "short_executed_notional_usd": short_usd},
                    explanation="已执行强平流；与 OI 下降同属 position_unwind，不重复计票。",
                ))

        liq_map = pick_primary_liq_map(getattr(state, "liq_maps", None))
        density_as_of = _seconds(getattr(liq_map, "ts", 0))
        qualities["estimated_liquidation_density"] = self._quality(
            "estimated_liquidation_density", density_as_of,
            self.config.source_max_age_sec["liquidation_risk"], now,
            available=liq_map is not None, continuous=None, valid=liq_map is not None,
        )
        densities: list[EstimatedLiquidationDensity] = []
        if liq_map is not None:
            for direction, clusters in (
                ("above", liq_map.clusters_above or []),
                ("below", liq_map.clusters_below or []),
            ):
                total = sum(float(cluster.total_usd or 0) for cluster in clusters)
                nearest = min(
                    (float(cluster.price_center) for cluster in clusters),
                    key=lambda price: abs(price - float(getattr(state.ticker, "last", 0) or 0)),
                    default=None,
                )
                item = EstimatedLiquidationDensity(
                    coin=coin, direction=direction, estimated_density_usd=total,
                    nearest_price=nearest, event_time=density_as_of or now,
                    observed_at=now, source_id="coinglass_estimated_liquidation_map",
                    quality=qualities["estimated_liquidation_density"],
                )
                densities.append(item)
                if total >= thresholds["liquidation_density_early_usd"]:
                    evidence.append(self._evidence(
                        coin=coin, pillar="liquidation_risk",
                        causal_root="liquidation_pressure", name=f"estimated_density_{direction}",
                        direction="up" if direction == "above" else "down",
                        strength=total / thresholds["liquidation_density_extreme_usd"],
                        confidence=0.64 if total < thresholds["liquidation_density_extreme_usd"] else 0.82,
                        event_time=density_as_of or now, now=now,
                        source_id="coinglass_estimated_liquidation_map",
                        values={"estimated_density_usd": total, "nearest_price": nearest},
                        explanation="估算清算密度是潜在磁铁，不是已发生爆仓。",
                    ))

        # 4) 被动流动性变化：复用现有 trust/removal/attack，不创建平行分数。
        op = getattr(state, "orderbook_pressure_snapshot", None)
        op_as_of = _seconds(getattr(op, "ts_sec", 0))
        op_quality = str(getattr(op, "data_quality", "missing") or "missing")
        op_valid = op_quality == "ok"
        qualities["liquidity_wall"] = self._quality(
            "liquidity_wall", op_as_of,
            self.config.source_max_age_sec["liquidity_structure"], now,
            available=op is not None, continuous=None, valid=op_valid,
            reasons=(
                ["snapshot_derived_liquidity_structure"]
                if op_valid else [f"liquidity_wall_quality_{op_quality}"]
            ),
        )
        cb_frame = getattr(state, "coinbase_orderbook", None)
        cb_as_of = _seconds(getattr(cb_frame, "ts_sec", 0))
        cb_valid = bool(
            cb_frame is not None and getattr(cb_frame, "validity", "valid") == "valid"
        )
        qualities["coinbase_orderbook"] = self._quality(
            "coinbase_orderbook", cb_as_of,
            self.config.source_max_age_sec["liquidity_structure"], now,
            available=cb_frame is not None, continuous=None, valid=cb_valid,
            reasons=["snapshot_only_public_orderbook"],
        )
        if qualities["liquidity_wall"].decision_usable:
            for zone in list(getattr(op, "wall_zones", []) or []):
                attack = float(getattr(zone, "active_attack_score", 0) or 0)
                removal = float(getattr(zone, "wall_removal_risk", 0) or 0)
                intensity = max(attack, removal)
                if intensity < thresholds["wall_attack_early"]:
                    continue
                direction = "up" if zone.side == "ask" else "down"
                evidence.append(self._evidence(
                    coin=coin, pillar="liquidity_structure",
                    causal_root="passive_liquidity_change",
                    name="wall_attack_or_removal", direction=direction,
                    strength=intensity / thresholds["wall_attack_extreme"],
                    confidence=0.66 if intensity < thresholds["wall_attack_extreme"] else 0.82,
                    event_time=op_as_of, now=now, source_id="liquidity_wall_engine",
                    source_sequence=str(getattr(zone, "wall_zone_id", "")),
                    values={
                        "zone_id": getattr(zone, "wall_zone_id", ""),
                        "active_attack_score": attack,
                        "wall_removal_risk": removal,
                        "trust_score": float(getattr(zone, "trust_score", 0) or 0),
                        "lifecycle": str(getattr(zone, "status", "active") or "active"),
                    }, explanation="墙体可信度、撤单风险和主动攻击复用既有正交字段。",
                ))

        # 5) 价格反馈只展示，不单独触发。首版 Market Response 坚持 INFORMATIONAL。
        candle_rows = list(getattr(state, "candles_15m", []) or [])
        price_event_time = _seconds(getattr(state.ticker, "ts", 0)) if getattr(state, "ticker", None) else 0
        qualities["market_response"] = self._quality(
            "market_response", price_event_time,
            self.config.source_max_age_sec["market_response"], now,
            available=bool(getattr(state, "ticker", None)), continuous=True,
            valid=bool(getattr(state, "ticker", None)),
        )
        if len(candle_rows) >= 2:
            try:
                previous_close = float(candle_rows[-2].get("close") if isinstance(candle_rows[-2], dict) else candle_rows[-2].close)
                current_price = float(getattr(state.ticker, "last", 0) or 0)
                price_change = (current_price - previous_close) / previous_close * 100
                if abs(price_change) >= thresholds["price_move_5m_feedback_pct"]:
                    evidence.append(self._evidence(
                        coin=coin, pillar="market_response", causal_root="price_response",
                        name="price_acceleration_feedback", direction="up" if price_change > 0 else "down",
                        strength=abs(price_change) / thresholds["price_move_5m_feedback_pct"],
                        confidence=0.7, event_time=price_event_time or now, now=now,
                        source_id="binance_price", values={"price_change_pct": price_change},
                        explanation="价格加速是结果反馈，首版仅展示、不计分。", role="informational",
                    ))
            except (TypeError, ValueError, AttributeError, ZeroDivisionError):
                pass

        footprint_contract = list(getattr(state, "footprint_contract", []) or [])
        footprint_spot = list(getattr(state, "footprint_spot", []) or [])
        footprint_ts = max(
            [int(row.get("ts", 0) or 0) for row in footprint_contract + footprint_spot
             if isinstance(row, dict)],
            default=0,
        )
        qualities["executed_absorption"] = self._quality(
            "executed_absorption", footprint_ts,
            self.config.source_max_age_sec["market_response"], now,
            available=bool(footprint_contract or footprint_spot), continuous=None,
            valid=bool(footprint_ts),
        )
        if qualities["executed_absorption"].decision_usable:
            try:
                from processors.absorption_detector import detect_absorption_zones
                absorption = detect_absorption_zones(
                    footprint_contract, footprint_spot,
                    float(getattr(state.ticker, "last", 0) or 0), now_ts=now,
                )
                zones = list(absorption.zones_support or []) + list(absorption.zones_resistance or [])
                if zones:
                    strongest = max(zones, key=lambda zone: zone.taker_volume_usd)
                    evidence.append(self._evidence(
                        coin=coin, pillar="market_response",
                        causal_root="executed_absorption_response",
                        name="footprint_executed_absorption",
                        direction="up" if strongest.side == "support" else "down",
                        strength=min(float(strongest.taker_volume_usd) / 10_000_000, 1.0),
                        confidence=0.55, event_time=footprint_ts, now=now,
                        source_id="footprint_absorption_detector",
                        values={
                            "price": strongest.price, "side": strongest.side,
                            "taker_volume_usd": strongest.taker_volume_usd,
                            "bar_count": strongest.bar_count,
                            "fallback_used": absorption.fallback_used,
                        },
                        explanation="复用已成交 Footprint 吸收解释器；首版 INFORMATIONAL，不参与评分。",
                        role="informational",
                    ))
            except Exception:
                logger.debug("market risk absorption interpreter failed", exc_info=True)

        # 6) Context：ETF/期权/链上/稳定币只展示 known_at，不计分。
        etf = getattr(state, "etf_flow", None)
        options = getattr(state, "option_info", None)
        stablecoin = getattr(state, "stablecoin_mcap", None)
        onchain_events = self.onchain_store.recent_events(
            decision_time=now, since=now - 24 * 3600, limit=50,
        )
        context = {
            "leverage_refill_ratio": lrr,
            "etf": {
                "availability": "available" if etf else "unavailable",
                "trading_day": getattr(etf, "trading_day", ""),
                "published_at": _seconds(getattr(etf, "published_at", 0)),
                "known_at": _seconds(getattr(etf, "known_at", 0)),
                "net_3d": getattr(etf, "net_3d", None),
                "note": "按发布时间 known_at 使用，不倒填为盘中已知。",
            },
            "options": {
                "availability": "available" if options else "unavailable",
                "known_at": _seconds(getattr(options, "known_at", 0)),
                "put_call_oi_ratio": getattr(options, "put_call_oi_ratio", None),
                "iv_atm": getattr(options, "iv_atm", None),
                "iv_skew": getattr(options, "iv_skew", None),
                "term_structure": getattr(options, "term_structure", []),
                "strike_clusters": getattr(options, "strike_clusters", []),
                "expiry_concentration": getattr(options, "expiry_concentration", None),
                "max_pain_weight": "low",
                "gex": getattr(options, "gex_status", "unavailable"),
            },
            "native_btc_onchain": {
                "availability": "available" if onchain_events else "unavailable",
                "events": [event.model_dump() for event in onchain_events],
                "reason": (
                    "" if onchain_events
                    else "暂无 PIT 合格的 BTC 原生链实体事件；WBTC/EVM 不得冒充"
                ),
            },
            "stablecoin": {
                "availability": "available" if stablecoin else "unavailable",
                "known_at": _seconds(getattr(stablecoin, "ts", 0)),
                "exchange_inflow": "unavailable",
                "note": "仅市值慢变量；缺交易所流入时不得解释为即时购买力",
            },
        }
        qualities["context"] = self._quality(
            "context", max(
                _seconds(getattr(etf, "ts", 0)),
                _seconds(getattr(options, "ts", 0)),
                _seconds(getattr(stablecoin, "ts", 0)),
            ), self.config.source_max_age_sec["context"], now,
            available=bool(etf or options or stablecoin), continuous=None,
            valid=True,
        )
        return evidence, qualities, realized, densities, context

    def _summarize_roots(
        self, evidence: list[EvidenceItem], qualities: dict[str, SourceQuality],
    ) -> tuple[dict[str, PillarSnapshot], dict[str, EvidenceItem]]:
        roots: dict[str, EvidenceItem] = {}
        for item in evidence:
            if item.role != "scoring":
                continue
            previous = roots.get(item.causal_root)
            if previous is None or (item.confidence, item.strength) > (
                previous.confidence, previous.strength,
            ):
                roots[item.causal_root] = item
        pillars: dict[str, PillarSnapshot] = {}
        for pillar in (
            "spot_demand", "leveraged_positioning", "liquidation_risk",
            "liquidity_structure", "market_response", "context",
        ):
            items = [item for item in evidence if item.pillar == pillar]
            root_names = sorted({item.causal_root for item in items})
            strongest = max(items, key=lambda item: (item.confidence, item.strength), default=None)
            pillars[pillar] = PillarSnapshot(
                pillar=pillar,
                direction=strongest.direction if strongest else "unknown",
                confidence=strongest.confidence if strongest else 0.0,
                causal_roots=root_names,
                evidence_ids=[item.evidence_id for item in items],
                decision_usable=any(
                    quality.decision_usable for source_id, quality in qualities.items()
                    if (
                        (pillar == "spot_demand" and "spot" in source_id)
                        or (pillar == "leveraged_positioning" and source_id in {"standardized_oi", "binance_futures_aggtrade", "funding"})
                        or (pillar == "liquidation_risk" and "liquidation" in source_id)
                        or (pillar == "liquidity_structure" and source_id == "liquidity_wall")
                        or (pillar == "market_response" and source_id == "market_response")
                        or (pillar == "context" and source_id == "context")
                    )
                ),
                note="同一 causal root 只计一次独立证据。",
            )
        return pillars, roots

    def _desired_stage(
        self, roots: dict[str, EvidenceItem], pillars: dict[str, PillarSnapshot],
    ) -> tuple[str, str, list[str], list[str]]:
        warning_conf = self.calibration.thresholds["warning_confidence"]
        critical_conf = self.calibration.thresholds["critical_confidence"]
        scoring = list(roots.values())
        directional_scores = {
            direction: sum(item.confidence * max(item.strength, 0.25) for item in scoring if item.direction == direction)
            for direction in ("up", "down")
        }
        if not scoring or max(directional_scores.values(), default=0) <= 0:
            direction = "unknown"
        elif abs(directional_scores["up"] - directional_scores["down"]) < 0.15:
            direction = "mixed"
        else:
            direction = max(directional_scores, key=directional_scores.get)
        aligned = [
            item for item in scoring
            if direction in {"up", "down"} and item.direction == direction
        ]
        aligned_roots = sorted({item.causal_root for item in aligned})
        spot = roots.get("spot_demand")
        spot_confirmed = bool(
            spot and spot.direction == direction and spot.confidence >= warning_conf
        )
        research = []
        derivative_extreme = any(
            item.causal_root in {"leveraged_initiation", "position_unwind", "liquidation_pressure"}
            and item.strength >= 1.0 and item.confidence >= warning_conf
            for item in aligned
        )
        if derivative_extreme and not spot_confirmed:
            research.append("derivative_led_watch")
        extreme_count = sum(
            item.strength >= 1.0 and item.confidence >= warning_conf for item in aligned
        )
        if spot_confirmed and len(aligned_roots) >= 3 and any(
            item.confidence >= critical_conf for item in aligned
        ):
            return "critical", direction, aligned_roots, research
        if spot_confirmed and len(aligned_roots) >= 2:
            return "warning", direction, aligned_roots, research
        if extreme_count >= 1 or len(aligned_roots) >= 2:
            return "watch", direction, aligned_roots, research
        return "normal", direction, aligned_roots, research

    def _advance(
        self, context: MarketRiskMachineContext, desired: str, direction: str,
        degraded: bool, now: int,
    ) -> tuple[MarketRiskMachineContext, list[MarketRiskTransition], str]:
        previous = context.model_copy(deep=True)
        transitions: list[MarketRiskTransition] = []
        reason = f"desired={desired}; direction={direction}"
        if degraded:
            return context, transitions, "data_degraded：保留最后阶段，禁止升级、解决和邮件"

        # 活跃 incident 出现明确反向风险，开启新 incident，避免一场行情混两个方向。
        if (
            context.stage in _ACTIVE_STAGES and desired in _ACTIVE_STAGES
            and context.direction in {"up", "down"} and direction in {"up", "down"}
            and context.direction != direction
        ):
            transitions.append(MarketRiskTransition(
                transition_id=_stable_id(
                    "tr", context.coin, context.incident_id, context.episode_id,
                    context.stage, "resolved_direction_flip", now,
                ),
                coin=context.coin, incident_id=context.incident_id,
                episode_id=context.episode_id, from_stage=context.stage,
                to_stage="resolved", direction=context.direction,
                decision_time=now,
                reason=f"opposite_direction_incident_started:{direction}",
                config_version=self.config.version_hash,
                calibration_version=self.calibration.calibration_version,
            ))
            context = MarketRiskMachineContext(coin=context.coin)
            previous = context.model_copy(deep=True)

        if desired in _ACTIVE_STAGES:
            context.last_qualifying_at = now
            if not context.incident_id:
                context.incident_id = _stable_id("inc", context.coin, direction, now)
                context.incident_started_at = now
            if not context.episode_id:
                context.episode_id = _stable_id("ep", context.incident_id, now)
                context.episode_started_at = now
            context.direction = direction

        target = context.stage
        if context.stage == "normal":
            if desired in _ACTIVE_STAGES:
                target = "watch"
        elif context.stage == "watch":
            if desired in {"warning", "critical"}:
                target = "warning"
            elif desired == "normal" and now - context.last_qualifying_at >= int(
                self.calibration.thresholds["quiet_to_cooldown_sec"]
            ):
                target = "cooldown"
        elif context.stage == "warning":
            if desired == "critical":
                target = "critical"
                context.last_critical_at = now
            elif desired == "normal" and now - context.last_qualifying_at >= int(
                self.calibration.thresholds["quiet_to_cooldown_sec"]
            ):
                target = "cooldown"
        elif context.stage == "critical":
            if desired == "critical":
                context.last_critical_at = now
            elif now - context.last_qualifying_at >= int(
                self.calibration.thresholds["quiet_to_cooldown_sec"]
            ):
                target = "cooldown"
        elif context.stage == "cooldown":
            if desired in _ACTIVE_STAGES:
                target = "watch"
                if now - context.episode_started_at >= int(
                    self.calibration.thresholds["episode_gap_sec"]
                ):
                    context.episode_id = _stable_id("ep", context.incident_id, now)
                    context.episode_started_at = now
            elif now - context.stage_since >= int(
                self.calibration.thresholds["cooldown_to_resolved_sec"]
            ):
                target = "resolved"
                context.resolved_at = now
        elif context.stage == "resolved":
            if desired in _ACTIVE_STAGES:
                context = MarketRiskMachineContext(coin=context.coin)
                context.incident_id = _stable_id("inc", context.coin, direction, now)
                context.episode_id = _stable_id("ep", context.incident_id, now)
                context.incident_started_at = context.episode_started_at = now
                context.last_qualifying_at = now
                context.direction = direction
                target = "watch"
            elif now - context.resolved_at >= int(
                self.calibration.thresholds["resolved_to_normal_sec"]
            ):
                target = "normal"

        if target != context.stage:
            from_stage = context.stage
            context.stage = target
            context.stage_since = now
            transitions.append(MarketRiskTransition(
                transition_id=_stable_id(
                    "tr", context.coin, context.incident_id, context.episode_id,
                    from_stage, target, now,
                ),
                coin=context.coin, incident_id=context.incident_id,
                episode_id=context.episode_id, from_stage=from_stage,
                to_stage=target, direction=context.direction,
                decision_time=now, reason=reason,
                config_version=self.config.version_hash,
                calibration_version=self.calibration.calibration_version,
            ))
        elif previous.stage == "normal" and not context.stage_since:
            context.stage_since = now
        return context, transitions, reason

    def evaluate_coin(self, coin: str, decision_time: Optional[int] = None) -> MarketIncidentSnapshot:
        coin = coin.upper()
        if coin not in self.config.coins:
            raise ValueError(f"market risk disabled for coin {coin}")
        now = int(decision_time or time.time())
        existing = self.store.latest(coin)
        if existing is not None and existing.decision_time == now:
            return existing
        state = self._state_getter(coin)
        if state is None:
            raise ValueError(f"CoinState unavailable for {coin}")
        evidence, qualities, realized, densities, context_payload = self._extract(coin, state, now)
        pillars, roots = self._summarize_roots(evidence, qualities)
        desired, direction, aligned_roots, research = self._desired_stage(roots, pillars)

        spot_usable = qualities["binance_spot_aggtrade"].decision_usable
        confirmation_usable = sum((
            qualities["standardized_oi"].decision_usable,
            qualities["realized_liquidations"].decision_usable
            or qualities["estimated_liquidation_density"].decision_usable,
            qualities["liquidity_wall"].decision_usable,
        ))
        hard_core_invalid = any(
            not qualities[source_id].decision_usable
            for source_id in ("binance_spot_aggtrade", "standardized_oi", "liquidity_wall")
        )
        degraded = hard_core_invalid or not spot_usable or confirmation_usable < 2
        machine = self._contexts[coin]
        machine, transitions, transition_reason = self._advance(
            machine, desired, direction, degraded, now,
        )
        self._contexts[coin] = machine
        spot_root = roots.get("spot_demand")
        snapshot = MarketIncidentSnapshot(
            coin=coin,
            event_time=max((item.event_time for item in evidence), default=now),
            observed_at=now, decision_time=now,
            watermark=min(
                (quality.watermark for quality in qualities.values() if quality.watermark > 0),
                default=0,
            ),
            stage=machine.stage,
            quality_layer="data_degraded" if degraded else "normal",
            direction=machine.direction if machine.direction != "unknown" else direction,
            incident_id=machine.incident_id, episode_id=machine.episode_id,
            stage_since=machine.stage_since,
            shadow_mode=self.config.shadow_mode,
            research_signals=research,
            causal_roots=aligned_roots,
            independent_root_count=len(aligned_roots),
            spot_confirmed=bool(
                spot_root and spot_root.direction == direction
                and spot_root.confidence >= self.calibration.thresholds["warning_confidence"]
            ),
            pillars=pillars, evidence=evidence, source_quality=qualities,
            realized_liquidation=realized,
            estimated_liquidation_density=densities,
            context=context_payload,
            transition_reason=transition_reason,
            config_version=self.config.version_hash,
            calibration_version=self.calibration.calibration_version,
            calibration_admitted=self.calibration.admitted_for_production,
            notification_eligible=bool(
                not self.config.shadow_mode
                and self._email_channel_enabled()
                and self.calibration.admitted_for_production
                and machine.stage in {"warning", "critical"}
                and not degraded
            ),
        )
        email = None
        latest_transition = transitions[-1] if transitions else None
        if latest_transition and snapshot.notification_eligible and latest_transition.to_stage in {"warning", "critical"}:
            dedup_key = f"market-risk:{snapshot.episode_id}:{latest_transition.to_stage}"
            subject = f"[LIQ {snapshot.coin}] 联合风险 {latest_transition.to_stage.upper()}"
            html = (
                f"<h3>{subject}</h3><p>方向：{snapshot.direction}</p>"
                f"<p>独立因果根：{', '.join(snapshot.causal_roots)}</p>"
                "<p>仅为风险证据，不是交易或仓位指令。</p>"
            )
            email = (dedup_key, subject, html)
        self.store.commit_evaluation(snapshot, machine, transitions, email)
        try:
            from sources.binance_trades_ws import get_trades_ws
            trade_source = get_trades_ws()
            if trade_source:
                for marker in trade_source.gap_markers(within_sec=3600):
                    self.store.add_gap_marker({**marker, "source_id": f"binance_{marker['market']}_aggtrade"})
        except Exception:
            logger.debug("market risk gap-marker persist failed", exc_info=True)
        return snapshot
