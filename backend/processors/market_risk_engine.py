"""联合风险 shadow 引擎：固定 tick、PIT 证据、因果根去重与 fail-closed 状态机。"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Optional

from config.settings import MarketRiskConfig
from models.liquidation import pick_primary_liq_map
from models.market_risk import (
    CalibrationArtifact,
    ConfirmedIncident,
    DecisionEvidenceSummary,
    DecisionSupport,
    EstimatedLiquidationDensity,
    EvidenceItem,
    MarketIncidentSnapshot,
    MarketFactor,
    MarketRiskHealth,
    MarketRiskIntelligence,
    MarketRiskMachineContext,
    MarketRiskReady,
    MarketRiskTransition,
    LiveObservation,
    PillarSnapshot,
    RealizedLiquidationFlow,
    SourceQuality,
)
from storage.market_risk_store import MarketRiskStore
from storage.onchain_entity_store import OnchainEntityStore
from storage.raw_event_store import RawEventStore, set_raw_event_store
from processors.market_risk_anomaly import RollingPitAnomalyNormalizer

logger = logging.getLogger(__name__)

_ACTIVE_STAGES = {"watch", "warning", "critical"}
_STAGE_RANK = {"normal": 0, "watch": 1, "warning": 2, "critical": 3}


@dataclass(frozen=True)
class DecisionFrame:
    """事件循环内捕获的只读行情帧，工作线程不得再读取可变 CoinState。"""

    coin: str
    captured_at: int
    values: Any

    def __getattr__(self, name: str) -> Any:
        return self.values.get(name)


class _DisabledMarketRiskStore:
    """disabled 模式的无 IO 存根，保持 Engine 既有 checkpoint 调用兼容。"""

    def latest(self, _coin: str) -> None:
        return None

    def history(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    def incident(self, _incident_id: str) -> None:
        return None

    def load_machine_context(self, _coin: str) -> None:
        return None

    def save_checkpoint(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def load_checkpoint(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def add_gap_marker(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def ensure_governance_epoch(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"open": False, "started_at": 0, "payload": {}}

    def close_governance_epoch(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def governance_status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "open": False, "started_at": 0, "identity_hash": "",
            "hard_violations": 0, "last_reset_at": 0,
            "last_reset_reason": "", "payload": {},
        }

    def outbox_stats(self) -> dict[str, Any]:
        return {}

    def due_outbox(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    def prune(self) -> None:
        return None

    def close(self) -> None:
        return None


class _DisabledOnchainStore:
    def recent_events(self, **_kwargs: Any) -> list[Any]:
        return []

    def close(self) -> None:
        return None


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
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._outbox_task: Optional[asyncio.Task] = None
        self._last_tick_at = 0
        self._last_prune_at = 0
        self._last_error = ""
        self._rss_samples: deque[tuple[int, float]] = deque(maxlen=2_880)
        self._governance_scope = "market_risk:" + ",".join(sorted(config.coins))
        self._governance_identity = "disabled"
        self.raw_event_store: Optional[RawEventStore] = None
        if not config.enabled:
            self.store = _DisabledMarketRiskStore()
            self.onchain_store = _DisabledOnchainStore()
            self.calibration = CalibrationArtifact(
                calibration_version="disabled", created_at="", status="disabled",
                admitted_for_production=False, thresholds={},
            )
            self._contexts = {
                coin: MarketRiskMachineContext(coin=coin) for coin in config.coins
            }
            return
        data_dir = config.data_dir
        if not os.path.isabs(data_dir):
            data_dir = os.path.join(backend_root, data_dir)
        self.store = MarketRiskStore(data_dir)
        self.onchain_store = OnchainEntityStore(data_dir)
        if config.raw_event_store_enabled:
            self.raw_event_store = RawEventStore(
                os.path.join(data_dir, "events"),
                queue_max=config.raw_event_queue_max,
                batch_size=config.raw_event_batch_size,
                allowed_coins=config.coins,
                segment_sec=config.raw_event_segment_sec,
                max_lateness_sec=config.raw_event_max_lateness_sec,
                max_total_bytes=config.raw_event_max_total_bytes,
                min_free_bytes=config.raw_event_min_free_bytes,
                min_free_inodes=config.raw_event_min_free_inodes,
                gap_sink=self._handle_raw_gap,
                state_loader=lambda: self.store.load_checkpoint(
                    "raw_event_store_state_v1", "ALL",
                ),
                state_saver=lambda payload: self.store.save_checkpoint(
                    "raw_event_store_state_v1", "ALL", payload,
                ),
            )
            set_raw_event_store(self.raw_event_store)
        self.calibration = self._load_calibration(config.calibration_artifact)
        self.store.save_calibration(self.calibration)
        self._governance_identity = self._compute_governance_identity()
        self._anomaly_normalizer = RollingPitAnomalyNormalizer(self.store)
        self._contexts: dict[str, MarketRiskMachineContext] = {
            coin: self.store.load_machine_context(coin)
            or MarketRiskMachineContext(coin=coin)
            for coin in config.coins
        }

    def _compute_governance_identity(self) -> str:
        digest = hashlib.sha256()
        digest.update(b"market-risk-governance-v3")
        digest.update(self.config.version_hash.encode())
        digest.update(self.calibration.calibration_version.encode())
        files = (
            __file__,
            os.path.join(os.path.dirname(__file__), "market_risk_anomaly.py"),
            os.path.join(os.path.dirname(__file__), "..", "storage", "raw_event_store.py"),
            os.path.join(os.path.dirname(__file__), "..", "storage", "market_risk_store.py"),
            os.path.join(os.path.dirname(__file__), "..", "models", "market_risk.py"),
        )
        for path in files:
            with open(os.path.abspath(path), "rb") as handle:
                digest.update(handle.read())
        return digest.hexdigest()[:24]

    def _raw_epoch_payload(self) -> dict[str, Any]:
        raw = self.raw_event_store.health() if self.raw_event_store else {}
        return {
            "raw_dropped_baseline": int(raw.get("dropped", 0)),
            "raw_late_baseline": int(raw.get("late_events", 0)),
            "raw_writer_failure_baseline": int(raw.get("writer_failures", 0)),
            "code_config_calibration_hash": self._governance_identity,
        }

    def _ensure_governance_epoch(self, now: int) -> dict[str, Any]:
        return self.store.ensure_governance_epoch(
            self._governance_scope, self._governance_identity, now,
            self._raw_epoch_payload(),
        )

    def _handle_raw_gap(self, marker: dict[str, Any]) -> None:
        self.store.add_gap_marker(marker)
        reason = str(marker.get("reason") or "raw_event_integrity_gap")
        if reason in {
            "raw_event_pit_violation", "raw_event_queue_overflow",
            "raw_event_late_beyond_allowance", "raw_event_writer_failure",
            "raw_segment_dedup_read_failure", "pyarrow_unavailable",
        }:
            self.store.close_governance_epoch(
                self._governance_scope, reason,
                int(marker.get("observed_at") or time.time()), marker,
            )

    def _sync_governance(self, now: int) -> dict[str, Any]:
        epoch = self._ensure_governance_epoch(now)
        raw = self.raw_event_store.health() if self.raw_event_store else {}
        baseline = dict(epoch.get("payload") or {})
        violations = (
            ("raw_event_queue_overflow", "dropped", "raw_dropped_baseline"),
            ("raw_event_late_beyond_allowance", "late_events", "raw_late_baseline"),
            ("raw_event_writer_failure", "writer_failures", "raw_writer_failure_baseline"),
        )
        for reason, current_key, baseline_key in violations:
            if int(raw.get(current_key, 0)) > int(baseline.get(baseline_key, 0)):
                self.store.close_governance_epoch(
                    self._governance_scope, reason, now,
                    {"current": raw.get(current_key, 0), "baseline": baseline.get(baseline_key, 0)},
                )
                break
        if raw and not bool(raw.get("resource_admissible", False)):
            self.store.close_governance_epoch(
                self._governance_scope, "raw_event_resource_limit", now,
                {"resource_admissible": False},
            )
        return self.store.governance_status(self._governance_scope, now - 14 * 86_400)

    @property
    def mode(self) -> str:
        requested = str(getattr(self.config, "mode", "shadow"))
        if requested == "shadow":
            return requested
        basic_ready, _ = self._runtime_readiness(int(time.time()))
        if not basic_ready:
            return "shadow"
        if requested == "production_alerting" and not self.calibration.admitted_for_production:
            return "production_read_only"
        return requested

    def _rss_metrics(self, now: int) -> tuple[float, float, int]:
        rss = [(ts, value) for ts, value in self._rss_samples if ts >= now - 86_400]
        rss_values = sorted(value for _, value in rss)
        rss_p95 = (
            rss_values[min(len(rss_values) - 1, int(len(rss_values) * 0.95))]
            if rss_values else 0.0
        )
        rss_slope = 0.0
        observation_age = 0
        if len(rss) >= 2 and rss[-1][0] > rss[0][0]:
            observation_age = rss[-1][0] - rss[0][0]
            rss_slope = (
                (rss[-1][1] - rss[0][1]) * 1024
                / (observation_age / 3600)
            )
        return rss_p95, rss_slope, observation_age

    def _runtime_readiness(self, now: int) -> tuple[bool, list[str]]:
        stats_24h = self.store.readiness_stats(now - 86_400)
        governance = self._sync_governance(now)
        governed_age = max(0, now - int(governance["started_at"])) if governance["open"] else 0
        rss_p95, rss_slope, rss_age = self._rss_metrics(now)
        raw = self.raw_event_store.health() if self.raw_event_store else {
            "enabled": False, "status": "disabled", "dropped": 0,
            "resource_admissible": False, "projected_files_per_day": 0,
        }
        blockers: list[str] = []
        if governed_age < 14 * 86_400:
            blockers.append("修复后 shadow 连续时长不足 14 天")
        if int(governance["hard_violations"]) > 0:
            blockers.append("修复后 shadow 仍存在完整性硬违规")
        if not governance["open"]:
            blockers.append("当前没有开放的完整性质量纪元")
        if not stats_24h["snapshot_count"]:
            blockers.append("尚无受新 PIT 门禁治理的 shadow 快照")
        if stats_24h["core_coverage"] < 0.9:
            blockers.append("24 小时核心数据覆盖率低于 90%")
        if not bool(raw.get("enabled", False)):
            blockers.append("原始事件存储未启用，无法进行全源 PIT 回放")
        elif not bool(raw.get("running", False)) or raw.get("format") == "unavailable":
            blockers.append("原始事件写入器未运行或 Parquet 能力不可用")
        baseline = dict(governance.get("payload") or {})
        if int(raw.get("dropped", 0)) > int(baseline.get("raw_dropped_baseline", 0)):
            blockers.append("当前质量纪元内原始事件队列存在丢弃")
        if float(raw.get("projected_files_per_day", 0)) > 2_000:
            blockers.append("Parquet 新文件预测超过 2,000/日")
        if not bool(raw.get("resource_admissible", False)):
            blockers.append("磁盘、inode 或存储大小触及保留门槛")
        if float(raw.get("projected_90d_bytes", 0)) > float(raw.get("free_bytes", 0)) * 0.5:
            blockers.append("完整保留期预测超过当前空闲磁盘 50%")
        if float(raw.get("projected_90d_files", 0)) > float(raw.get("free_inodes", 0)) * 0.25:
            blockers.append("完整保留期预测超过当前空闲 inode 25%")
        if rss_age < 86_400:
            blockers.append("RSS 连续观察时长不足 24 小时")
        if rss_p95 > 1.3:
            blockers.append("24h RSS p95 超过 1.3 GiB")
        if rss_slope > 2.0:
            blockers.append("RSS 增长斜率超过 2 MiB/h")
        return not blockers, blockers

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
            "critical_to_warning_sec", "warning_to_watch_sec",
            "root_direction_dominance_ratio",
        }
        missing = sorted(required - set(artifact.thresholds))
        unknown = sorted(set(artifact.thresholds) - required)
        if missing or unknown:
            raise ValueError(
                f"invalid market-risk calibration missing={missing} unknown={unknown}"
            )
        if artifact.admitted_for_production:
            metadata = (
                artifact.dataset_hash, artifact.code_hash, artifact.config_hash,
                artifact.admission_report_hash,
            )
            if artifact.status != "production_admitted" or not all(metadata):
                raise ValueError("production calibration missing immutable admission provenance")
        return artifact

    async def start(self) -> None:
        if self._running or not self.config.enabled:
            return
        self._running = True
        self._ensure_governance_epoch(int(time.time()))
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
                    decision_time = int(time.time())
                    frame = self._capture_decision_frame(coin, decision_time)
                    await asyncio.to_thread(
                        self.evaluate_coin, coin, decision_time, frame=frame,
                    )
                    self._last_error = ""
                except Exception as exc:  # noqa: BLE001
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("market risk tick failed | coin=%s", coin)
            self._last_tick_at = int(time.time())
            self._record_rss(self._last_tick_at)
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

    def _record_rss(self, observed_at: int) -> None:
        try:
            with open("/proc/self/statm", "r", encoding="ascii") as handle:
                resident_pages = int(handle.read().split()[1])
            rss_bytes = resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            try:
                import resource
                raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                rss_bytes = raw if raw > 10_000_000 else raw * 1024
            except Exception:
                return
        self._rss_samples.append((int(observed_at), rss_bytes / 1024**3))

    def _capture_decision_frame(self, coin: str, decision_time: int) -> DecisionFrame:
        state = self._state_getter(coin)
        if state is None:
            raise ValueError(f"CoinState unavailable for {coin}")
        names = (
            "ticker", "oi", "oi_exchange_rank", "funding", "multi_funding",
            "global_liq", "liq_maps", "orderbook_pressure_snapshot",
            "coinbase_orderbook", "native_liquidity", "cvd_spot", "cvd_contract", "basis",
            "candles_1m", "candles_5m", "candles_15m", "candles_1h",
            "candles_4h", "candles_daily", "footprint_contract",
            "footprint_spot", "etf_flow", "option_info", "stablecoin_mcap",
            "ibit_official", "cftc_bitcoin_cot",
            "poll_failures", "dependency_failures",
        )
        values = {
            name: copy.deepcopy(getattr(state, name, None)) for name in names
        }
        return DecisionFrame(
            coin=coin, captured_at=decision_time,
            values=MappingProxyType(values),
        )

    async def _run_outbox(self) -> None:
        """复用现有 SMTP；只有 OOS artifact 已准入且独立开关开启才可能发送。"""
        from notifications.email_alert import send_html_email_result

        while self._running:
            try:
                for item in self.store.due_outbox(limit=10):
                    channel_enabled = bool(
                        self._email_channel_enabled()
                        and self.mode == "production_alerting"
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
            shadow_mode=self.mode == "shadow",
            mode=self.mode,
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

    def intelligence(self, coin: str) -> Optional[MarketRiskIntelligence]:
        snapshot = self.latest(coin.upper())
        if snapshot is None:
            return None
        pillar_labels = {
            "spot_demand": "现货主动成交",
            "leveraged_positioning": "杠杆与持仓",
            "liquidation_risk": "清算压力",
            "liquidity_structure": "实时流动性",
            "market_response": "价格反馈",
            "context": "慢周期背景",
        }
        factors: list[MarketFactor] = []
        for pillar_id, label in pillar_labels.items():
            pillar = snapshot.pillars.get(pillar_id)
            items = [item for item in snapshot.evidence if item.pillar == pillar_id]
            strongest = max(
                items,
                key=lambda item: (item.raw_strength, item.confidence, item.evidence_id),
                default=None,
            )
            if pillar is None or not pillar.decision_usable:
                status = "missing"
                band = "unavailable"
            elif pillar.direction == "mixed":
                status = "conflict"
                band = "medium"
            elif strongest is None:
                status = "normal"
                band = "weak"
            elif strongest.raw_strength >= 1:
                status = "extreme"
                band = "strong"
            else:
                status = "unusual"
                band = "medium"
            factors.append(MarketFactor(
                factor_id=pillar_id, label=label,
                direction=pillar.direction if pillar else "unknown",
                status=status, strength_band=band,
                decision_role=(
                    "scoring" if strongest and strongest.role == "scoring"
                    else "blocked" if status == "missing" else "informational"
                ),
                source_ids=sorted({item.source_id for item in items}),
                as_of=max((item.event_time for item in items), default=0),
                decision_usable=bool(pillar and pillar.decision_usable),
                plain_summary=(
                    strongest.explanation if strongest else
                    ("数据缺失或过期，当前不参与判断。" if status == "missing"
                     else "当前未发现达到异常门槛的变化。")
                ),
                values=strongest.values if strongest else {},
            ))
        for key, label in (
            ("etf", "ETF 日度资金"), ("options", "期权波动与偏斜"),
            ("native_btc_onchain", "BTC 原生链实体"), ("stablecoin", "稳定币背景"),
        ):
            payload = snapshot.context.get(key, {})
            available = payload.get("availability") == "available"
            factors.append(MarketFactor(
                factor_id=key, label=label, status="normal" if available else "missing",
                strength_band="weak" if available else "unavailable",
                decision_role="informational", source_ids=[key],
                as_of=int(payload.get("known_at") or payload.get("published_at") or 0),
                decision_usable=False,
                plain_summary=str(
                    payload.get("note") or payload.get("reason")
                    or "当前仅作背景展示，尚未通过持出样本准入。"
                ),
                values=payload,
            ))
        for anomaly in list(snapshot.context.get("ordinary_anomalies", []) or []):
            anomaly_status = str(anomaly.get("status") or "warming")
            factors.append(MarketFactor(
                factor_id=f"anomaly:{anomaly.get('metric', 'unknown')}",
                label=str(anomaly.get("label") or anomaly.get("metric") or "普通异常"),
                direction=str(anomaly.get("direction") or "unknown"),
                status=(
                    anomaly_status if anomaly_status in {"normal", "unusual", "extreme"}
                    else "missing"
                ),
                strength_band=(
                    "strong" if anomaly_status == "extreme"
                    else "medium" if anomaly_status == "unusual"
                    else "weak" if anomaly_status == "normal" else "unavailable"
                ),
                decision_role="informational", source_ids=["rolling_pit_normalizer"],
                as_of=int(anomaly.get("as_of") or 0), decision_usable=False,
                plain_summary=str(anomaly.get("note") or "普通异常观察，不参与事件评分。"),
                values=dict(anomaly),
            ))

        blockers = [
            f"{source_id}：{'、'.join(quality.reasons) or '不可用于决策'}"
            for source_id, quality in snapshot.source_quality.items()
            if not quality.decision_usable and source_id in {
                "binance_spot_aggtrade", "standardized_oi", "realized_liquidations",
                "estimated_liquidation_density", "native_liquidity",
            }
        ]
        blockers.extend(snapshot.pit_violations)
        live_direction = snapshot.live_direction
        root_scores: dict[str, dict[str, float]] = {}
        for item in snapshot.evidence:
            if item.role != "scoring" or item.direction not in {"up", "down"}:
                continue
            scores = root_scores.setdefault(item.causal_root, {"up": 0.0, "down": 0.0})
            scores[item.direction] += item.confidence * max(item.raw_strength, 0.0)
        detail_by_id: dict[str, DecisionEvidenceSummary] = {}
        dominance_required = self.calibration.thresholds["root_direction_dominance_ratio"]
        root_outcomes: dict[str, str] = {}
        root_vote_ids: set[str] = set()
        for root_name, scores in root_scores.items():
            up_score, down_score = scores["up"], scores["down"]
            if up_score > 0 and down_score > 0:
                ratio = max(up_score, down_score) / max(min(up_score, down_score), 1e-12)
                outcome = (
                    "mixed" if ratio < dominance_required
                    else "up" if up_score > down_score else "down"
                )
            else:
                outcome = "up" if up_score > 0 else "down" if down_score > 0 else "unknown"
            root_outcomes[root_name] = outcome
            if outcome in {"up", "down"}:
                candidates = [
                    item for item in snapshot.evidence
                    if item.causal_root == root_name and item.role == "scoring"
                    and item.direction == outcome
                ]
                if candidates:
                    representative = max(
                        candidates,
                        key=lambda item: (
                            item.confidence * item.raw_strength,
                            item.raw_strength, item.evidence_id,
                        ),
                    )
                    root_vote_ids.add(representative.evidence_id)
        for item in snapshot.evidence:
            scores = root_scores.get(item.causal_root, {"up": 0.0, "down": 0.0})
            up_score, down_score = scores["up"], scores["down"]
            ratio = None
            root_outcome = root_outcomes.get(item.causal_root, "unknown")
            if up_score > 0 or down_score > 0:
                if up_score > 0 and down_score > 0:
                    ratio = max(up_score, down_score) / max(min(up_score, down_score), 1e-12)
            counted = bool(
                item.evidence_id in root_vote_ids and item.direction == live_direction
                and item.causal_root in snapshot.live_causal_roots
            )
            counting_reason = (
                "independent_root_vote" if counted
                else "informational_only" if item.role != "scoring"
                else "root_conflict_no_vote" if root_outcome == "mixed"
                else "same_root_confidence_only" if item.direction == root_outcome
                else "opposing_root_evidence"
            )
            detail_by_id[item.evidence_id] = DecisionEvidenceSummary(
                evidence_id=item.evidence_id, label=item.name,
                direction=item.direction, causal_root=item.causal_root,
                role=item.role, source_id=item.source_id, as_of=item.event_time,
                counted_in_direction=counted,
                counting_reason=counting_reason,
                root_outcome=root_outcome,
                root_up_score=up_score, root_down_score=down_score,
                dominance_ratio=ratio, explanation=item.explanation,
                values=dict(item.values),
            )
        supporting = [
            item.explanation for item in snapshot.evidence
            if item.role == "scoring" and item.direction == live_direction
        ]
        opposing_direction = "down" if live_direction == "up" else "up"
        opposing = [
            item.explanation for item in snapshot.evidence
            if item.role == "scoring" and item.direction == opposing_direction
        ]
        supporting_details = [
            detail_by_id[item.evidence_id] for item in snapshot.evidence
            if item.role == "scoring" and item.direction == live_direction
        ]
        opposing_details = [
            detail_by_id[item.evidence_id] for item in snapshot.evidence
            if item.role == "scoring" and item.direction == opposing_direction
        ]
        decision_ready = bool(
            snapshot.quality_layer == "normal"
            and snapshot.spot_confirmed
            and snapshot.independent_root_count >= 1
            and live_direction in {"up", "down"}
        )
        stance = (
            "observe_long" if decision_ready and live_direction == "up"
            else "observe_short" if decision_ready and live_direction == "down"
            else "wait"
        )
        strength_band = (
            "strong" if decision_ready and snapshot.independent_root_count >= 3
            else "medium" if decision_ready and snapshot.independent_root_count >= 2
            else "weak" if decision_ready else "unavailable"
        )
        summary = (
            "现货与多源证据偏多，可列入做多观察，但仍需等入场触发。"
            if stance == "observe_long" else
            "现货与多源证据偏空，可列入做空观察，但仍需等入场触发。"
            if stance == "observe_short" else
            "当前证据冲突、数量不足或数据受阻，优先等待。"
        )
        return MarketRiskIntelligence(
            coin=snapshot.coin, mode=self.mode, decision_time=snapshot.decision_time,
            live_observation=LiveObservation(
                decision_time=snapshot.decision_time, direction=live_direction,
                quality_layer=snapshot.quality_layer,
                spot_confirmed=snapshot.spot_confirmed,
                independent_root_count=snapshot.independent_root_count,
                causal_roots=snapshot.live_causal_roots,
                summary=(
                    "实时证据方向冲突，暂不选边。" if live_direction == "mixed"
                    else f"实时证据为{'上行' if live_direction == 'up' else '下行' if live_direction == 'down' else '中性'}观察。"
                ),
            ),
            confirmed_incident=ConfirmedIncident(
                stage=snapshot.stage, direction=snapshot.direction,
                confirmed_at=snapshot.last_confirmed_at,
                stage_since=snapshot.stage_since, frozen=snapshot.stage_frozen,
                frozen_since=snapshot.frozen_since,
                frozen_age_sec=(
                    max(0, snapshot.decision_time - snapshot.frozen_since)
                    if snapshot.stage_frozen and snapshot.frozen_since else 0
                ),
                incident_id=snapshot.incident_id, episode_id=snapshot.episode_id,
            ),
            decision_support=DecisionSupport(
                stance=stance, strength_band=strength_band, summary=summary,
                supporting_evidence=list(dict.fromkeys(supporting)),
                opposing_evidence=list(dict.fromkeys(opposing)),
                supporting_details=supporting_details,
                opposing_details=opposing_details,
                blockers=list(dict.fromkeys(blockers)),
                invalidation_conditions=[
                    "现货主动成交反向并持续一个闭合 5 分钟窗口",
                    "当前支持方向的独立因果根降为 0 或转为 mixed",
                    "任一必需数据过期、断流或出现 PIT 违规",
                ],
                execution_eligible=False,
            ),
            factors=factors, context=snapshot.context, incident=snapshot,
        )

    def ready(self) -> MarketRiskReady:
        if not self.config.enabled:
            return MarketRiskReady(
                current_mode="shadow", ready_for_mode="shadow",
                blockers=["market_risk_disabled"],
                raw_store={"enabled": False, "status": "disabled"},
            )
        now = int(time.time())
        stats = self.store.readiness_stats(now - 86_400)
        governance = self._sync_governance(now)
        governed_age = max(0, now - int(governance["started_at"])) if governance["open"] else 0
        rss_p95, rss_slope, rss_age = self._rss_metrics(now)
        raw = self.raw_event_store.health() if self.raw_event_store else {
            "enabled": False, "status": "disabled", "dropped": 0,
            "resource_admissible": False, "projected_files_per_day": 0,
        }
        basic_ready, blockers = self._runtime_readiness(now)
        ready_mode = "production_read_only" if basic_ready else "shadow"
        if basic_ready and self.calibration.admitted_for_production:
            ready_mode = "production_alerting"
        frozen = {}
        dependencies: dict[str, Any] = {"ai": "isolated_not_scoring"}
        try:
            from sources.binance_trades_ws import get_trades_ws
            trade_source = get_trades_ws()
            dependencies["binance_trade_stream"] = (
                trade_source.stats() if trade_source else {"status": "unavailable"}
            )
        except Exception:
            dependencies["binance_trade_stream"] = {"status": "unavailable"}
        for coin in self.config.coins:
            snapshot = self.latest(coin)
            if snapshot:
                frozen[coin] = {
                    "frozen": snapshot.stage_frozen,
                    "since": snapshot.frozen_since,
                    "age_sec": max(0, now - snapshot.frozen_since) if snapshot.frozen_since else 0,
                }
                dependencies[f"{coin}_market_data"] = snapshot.context.get(
                    "dependency_degradation", {},
                )
        return MarketRiskReady(
            ready_for_mode=ready_mode, current_mode=self.mode,
            pit_violations_24h=stats["pit_violations"],
            valid_for_calibration_24h=stats["valid_for_calibration"],
            snapshot_count_24h=stats["snapshot_count"],
            core_coverage_24h=stats["core_coverage"],
            governed_shadow_age_sec=governed_age,
            clean_epoch_started_at=int(governance["started_at"]),
            last_epoch_reset_at=int(governance["last_reset_at"]),
            last_epoch_reset_reason=str(governance["last_reset_reason"]),
            hard_violations_14d=int(governance["hard_violations"]),
            governance_identity=str(governance["identity_hash"]),
            rss_observation_age_sec=rss_age,
            rss_p95_gib=rss_p95,
            rss_slope_mib_per_hour=rss_slope,
            frozen_by_coin=frozen, raw_queue_dropped=int(raw.get("dropped", 0)),
            raw_dropped_in_epoch=max(
                0, int(raw.get("dropped", 0))
                - int(dict(governance.get("payload") or {}).get("raw_dropped_baseline", 0)),
            ),
            raw_store=raw, dependencies=dependencies,
            admission={
                "calibration_version": self.calibration.calibration_version,
                "admitted": self.calibration.admitted_for_production,
                "dataset_hash": self.calibration.dataset_hash,
                "code_hash": self.calibration.code_hash,
                "config_hash": self.calibration.config_hash,
                "admission_report_hash": self.calibration.admission_report_hash,
            },
            blockers=blockers,
        )

    def _quality(
        self, source_id: str, as_of: int, max_age: int, now: int, *,
        available: bool = True, continuous: Optional[bool] = True,
        valid: bool = True, reasons: Optional[list[str]] = None,
        completeness: float = 1.0,
    ) -> SourceQuality:
        reasons = list(reasons or [])
        if as_of > now:
            reasons.append(f"pit_future_as_of_{as_of}")
            as_of = now
            valid = False
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
        decision_usable = bool(
            available and fresh and continuous is not False and valid
            and completeness >= 0.8
        )
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
        original_event_time = int(event_time or now)
        if original_event_time > now:
            values = {**values, "rejected_future_event_time": original_event_time}
            explanation = f"{explanation} 原始事件时间晚于决策时间，已拒绝计分。"
            event_time = now
            role = "informational"
            direction = "unknown"
        canonical_values = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
        evidence_id = _stable_id(
            "ev", coin, pillar, causal_root, name, direction, event_time,
            source_id, source_sequence or "", canonical_values,
        )
        return EvidenceItem(
            evidence_id=evidence_id, coin=coin, pillar=pillar,
            causal_root=causal_root, name=name, direction=direction,
            role=role, strength=_clamp(strength), raw_strength=max(0.0, float(strength)),
            confidence=_clamp(confidence),
            event_time=event_time or now, observed_at=now, decision_time=now,
            watermark=event_time or now, source_sequence=source_sequence,
            source_id=source_id, config_version=self.config.version_hash,
            calibration_version=self.calibration.calibration_version,
            values=values, explanation=explanation,
        )

    def _trade_flow(
        self, coin: str, market: str, decision_time: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        try:
            from sources.binance_trades_ws import get_trades_ws
            source = get_trades_ws()
            result = source.aggressor_flow(
                coin, market, 300, decision_time=decision_time,
            ) if source else None
            raw_gap = self.raw_event_store.recent_gap(coin, market, 300) if self.raw_event_store else None
            if result is not None and raw_gap is not None:
                result["continuity"] = "gap"
                result["gap_reason"] = str(raw_gap.get("reason") or "raw_event_gap")
            return result
        except Exception:
            logger.warning(
                "market risk aggressor flow read failed | coin=%s market=%s",
                coin, market, exc_info=True,
            )
            return None

    def _closed_cvd_trend(self, cvd: Any, now: int) -> tuple[str, bool, int]:
        if cvd is None:
            return "", False, 0
        series = list(getattr(cvd, "series", []) or [])
        as_of = _seconds(getattr(cvd, "ts", 0))
        if not as_of and series:
            as_of = _seconds(getattr(series[-1], "ts", 0))
        # Coinglass CVD 是 5m 窗口，ts 表示窗口起点；只有闭合且仍在新鲜窗内才增强。
        closed = bool(as_of and as_of + 300 <= now)
        fresh = bool(as_of and now - (as_of + 300) <= self.config.source_max_age_sec["spot_demand"])
        return str(getattr(cvd, "trend_1h", "") or "").lower(), bool(closed and fresh), as_of

    def _closed_candle_return(
        self, rows: Any, interval_sec: int, now: int,
    ) -> tuple[Optional[float], int]:
        closed: list[tuple[int, float]] = []
        for row in list(rows or []):
            ts = _seconds(row.get("ts", 0) if isinstance(row, dict) else getattr(row, "ts", 0))
            raw_close = (
                row.get("c", row.get("close")) if isinstance(row, dict)
                else getattr(row, "c", getattr(row, "close", None))
            )
            try:
                close = float(raw_close)
            except (TypeError, ValueError):
                continue
            if ts > 0 and close > 0 and ts + interval_sec <= now:
                closed.append((ts, close))
        closed.sort()
        if len(closed) < 2:
            return None, 0
        previous, current = closed[-2], closed[-1]
        return (current[1] - previous[1]) / previous[1] * 100.0, current[0] + interval_sec

    def _ordinary_anomalies(
        self, coin: str, facts: dict[str, dict[str, Any]], now: int,
    ) -> list[dict[str, Any]]:
        return self._anomaly_normalizer.evaluate(coin, facts, now)

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
        spot_flow = self._trade_flow(coin, "spot", now)
        spot_as_of = int((spot_flow or {}).get("as_of") or 0)
        spot_continuous = (spot_flow or {}).get("continuity") == "continuous"
        spot_first = int((spot_flow or {}).get("first_bucket_ts") or 0)
        spot_completeness = _clamp(
            float((spot_flow or {}).get("coverage_sec") or (spot_as_of - spot_first + 1)) / 300
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
            cvd_trend, cvd_usable, cvd_as_of = self._closed_cvd_trend(cvd, now)
            qualities["spot_cvd_closed"] = self._quality(
                "spot_cvd_closed", min(cvd_as_of + 300, now) if cvd_as_of else 0,
                self.config.source_max_age_sec["spot_demand"], now,
                available=cvd is not None, continuous=None, valid=cvd_usable,
                reasons=[] if cvd_usable else ["cvd_window_stale_or_unclosed"],
            )
            if cvd_usable and ((direction == "up" and cvd_trend in {"rising", "up"}) or (
                direction == "down" and cvd_trend in {"declining", "falling", "down"}
            )):
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
        futures_flow = self._trade_flow(coin, "futures", now)
        futures_as_of = int((futures_flow or {}).get("as_of") or 0)
        futures_continuous = (futures_flow or {}).get("continuity") == "continuous"
        futures_first = int((futures_flow or {}).get("first_bucket_ts") or 0)
        futures_completeness = _clamp(
            float((futures_flow or {}).get("coverage_sec") or (futures_as_of - futures_first + 1)) / 300
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
        oi_as_of = _seconds(getattr(oi, "history_as_of", 0) or getattr(oi, "ts", 0))
        oi_valid = bool(getattr(oi, "decision_valid", False))
        qualities["standardized_oi"] = self._quality(
            "standardized_oi", oi_as_of,
            self.config.source_max_age_sec["leveraged_positioning"], now,
            available=oi is not None, continuous=True,
            valid=oi_valid,
            reasons=[] if oi_valid else ["contracts_or_base_change_unavailable"],
        )
        current_oi_as_of = _seconds(getattr(oi, "current_observed_at", 0))
        qualities["current_oi"] = self._quality(
            "current_oi", current_oi_as_of, 90, now,
            available=oi is not None and bool(current_oi_as_of), continuous=True,
            valid=getattr(oi, "current_contracts", None) is not None,
            reasons=[] if current_oi_as_of else ["current_oi_observation_unavailable"],
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
            flow_direction_usable = bool(
                qualities["binance_futures_aggtrade"].decision_usable
                and abs(f_imbalance) >= spot_early
            )
            if float(oi_change) > 0:
                direction = (
                    "up" if flow_direction_usable and f_imbalance > 0
                    else "down" if flow_direction_usable and f_imbalance < 0
                    else "unknown"
                )
                confidence = 0.58
                if flow_direction_usable:
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
                    role="scoring" if flow_direction_usable else "informational",
                ))

            else:
                evidence.append(self._evidence(
                    coin=coin, pillar="leveraged_positioning", causal_root="position_unwind",
                    name="standardized_oi_unwind", direction=(
                        "up" if flow_direction_usable and f_imbalance > 0
                        else "down" if flow_direction_usable and f_imbalance < 0
                        else "unknown"
                    ), strength=abs(float(oi_change)) / thresholds["oi_change_1h_extreme_pct"],
                    confidence=0.62, event_time=oi_as_of, now=now,
                    source_id="binance_fapi_open_interest_hist",
                    values={"oi_change_1h_pct": float(oi_change)},
                    explanation="OI 下降只表示去杠杆；需与强平或方向成交结合解释。",
                    role="scoring" if flow_direction_usable else "informational",
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
            current_price = float(getattr(getattr(state, "ticker", None), "last", 0) or 0)
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
                    distance_pct = (
                        abs(nearest / current_price - 1.0) * 100
                        if nearest and current_price > 0 else None
                    )
                    distance_weight = (
                        1.0 / (1.0 + max(distance_pct, 0.0))
                        if distance_pct is not None else 0.0
                    )
                    weighted_strength = (
                        total / thresholds["liquidation_density_extreme_usd"]
                    ) * distance_weight
                    evidence.append(self._evidence(
                        coin=coin, pillar="liquidation_risk",
                        causal_root="liquidation_pressure", name=f"estimated_density_{direction}",
                        direction="up" if direction == "above" else "down",
                        strength=weighted_strength,
                        confidence=0.64 if total < thresholds["liquidation_density_extreme_usd"] else 0.82,
                        event_time=density_as_of or now, now=now,
                        source_id="coinglass_estimated_liquidation_map",
                        values={
                            "estimated_density_usd": total,
                            "nearest_price": nearest,
                            "distance_pct": distance_pct,
                            "distance_weight": distance_weight,
                            "weighted_density_strength": weighted_strength,
                        },
                        explanation="估算清算密度是潜在磁铁，不是已发生爆仓。",
                    ))

        # 4) 被动流动性变化：复用现有 trust/removal/attack，不创建平行分数。
        op = getattr(state, "orderbook_pressure_snapshot", None)
        op_as_of = _seconds(getattr(op, "ts_sec", 0))
        op_quality = str(getattr(op, "data_quality", "missing") or "missing")
        op_valid = op_quality == "ok"
        max_wall_intensity = 0.0
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
        native_liquidity = getattr(state, "native_liquidity", None) or {}
        native_as_of = _seconds(native_liquidity.get("ts", 0))
        native_count = int(native_liquidity.get("available_count", 0) or 0)
        qualities["native_liquidity"] = self._quality(
            "native_liquidity", native_as_of, 45, now,
            available=native_count > 0, continuous=None, valid=native_count >= 2,
            completeness=min(1.0, native_count / 2),
            reasons=(
                [] if native_count >= 2
                else [f"native_exchange_coverage_{native_count}_of_2"]
            ),
        )
        if qualities["liquidity_wall"].decision_usable:
            for zone in list(getattr(op, "wall_zones", []) or []):
                attack = float(getattr(zone, "active_attack_score", 0) or 0)
                removal = float(getattr(zone, "wall_removal_risk", 0) or 0)
                intensity = max(attack, removal)
                max_wall_intensity = max(max_wall_intensity, intensity)
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
                    role=(
                        "scoring" if qualities["native_liquidity"].decision_usable
                        else "informational"
                    ),
                ))

        # 5) 价格反馈只消费真实闭合 5m K 线；15m 绝不再冒充 5m。
        price_change, price_event_time = self._closed_candle_return(
            getattr(state, "candles_5m", None), 300, now,
        )
        qualities["market_response"] = self._quality(
            "market_response", price_event_time,
            self.config.source_max_age_sec["market_response"], now,
            available=price_change is not None, continuous=True,
            valid=price_change is not None,
            reasons=[] if price_change is not None else ["closed_5m_candle_unavailable"],
        )
        if price_change is not None and abs(price_change) >= thresholds["price_move_5m_feedback_pct"]:
            evidence.append(self._evidence(
                coin=coin, pillar="market_response", causal_root="price_response",
                name="closed_5m_price_feedback", direction="up" if price_change > 0 else "down",
                strength=abs(price_change) / thresholds["price_move_5m_feedback_pct"],
                confidence=0.7, event_time=price_event_time, now=now,
                source_id="binance_closed_5m_kline",
                values={"price_change_5m_pct": price_change, "closed": True},
                explanation="真实已收盘 5 分钟收益，仅作结果反馈，不单独触发预警。",
                role="informational",
            ))

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
        ibit_official = getattr(state, "ibit_official", None)
        cftc_cot = getattr(state, "cftc_bitcoin_cot", None)
        options = getattr(state, "option_info", None)
        stablecoin = getattr(state, "stablecoin_mcap", None)
        etf_observation = None
        if etf is not None:
            etf_payload = {
                "trading_day": str(getattr(etf, "trading_day", "") or ""),
                "net_3d": getattr(etf, "net_3d", None),
                "recent_days": [
                    row.model_dump() if hasattr(row, "model_dump") else row
                    for row in list(getattr(etf, "recent_days", []) or [])
                ],
            }
            etf_observation = self.store.record_context_observation(
                "coinglass_btc_etf", etf_payload["trading_day"] or "unknown",
                _seconds(getattr(etf, "published_at", 0)), etf_payload, now,
            )
        ibit_observation = None
        if ibit_official:
            ibit_payload = {
                key: value for key, value in dict(ibit_official).items()
                if key != "observed_at"
            }
            ibit_observation = self.store.record_context_observation(
                "ishares_ibit_official",
                str(ibit_official.get("as_of") or "unknown"),
                int(ibit_official.get("as_of_ts") or 0), ibit_payload, now,
            )
        cftc_observation = None
        if cftc_cot:
            cftc_payload = {
                key: value for key, value in dict(cftc_cot).items()
                if key != "observed_at"
            }
            cftc_observation = self.store.record_context_observation(
                "cftc_cme_cot", str(cftc_cot.get("report_date") or "unknown"),
                int(cftc_cot.get("report_as_of") or 0), cftc_payload, now,
            )
        options_meaningful = bool(
            options is not None and any((
                getattr(options, "iv_atm", None) is not None,
                getattr(options, "iv_skew", None) is not None,
                bool(getattr(options, "term_structure", None)),
                bool(getattr(options, "strike_clusters", None)),
            ))
        )
        onchain_events = self.onchain_store.recent_events(
            decision_time=now, since=now - 24 * 3600, limit=50,
        )
        trend_horizons: dict[str, dict[str, Any]] = {}
        for label, attr, seconds in (
            ("1m", "candles_1m", 60), ("5m", "candles_5m", 300),
            ("15m", "candles_15m", 900), ("1h", "candles_1h", 3600),
            ("4h", "candles_4h", 14_400), ("1d", "candles_daily", 86_400),
        ):
            change, as_of = self._closed_candle_return(getattr(state, attr, None), seconds, now)
            trend_horizons[label] = {
                "availability": "available" if change is not None else "unavailable",
                "change_pct": change, "as_of": as_of, "closed": change is not None,
                "direction": (
                    "up" if change is not None and change > 0
                    else "down" if change is not None and change < 0 else "unknown"
                ),
            }
        live_facts = {
            "spot_imbalance_5m": {
                "label": "现货主动成交失衡", "value": spot_imbalance,
                "direction": "up" if spot_imbalance > 0 else "down" if spot_imbalance < 0 else "unknown",
                "as_of": spot_as_of,
            },
            "futures_imbalance_5m": {
                "label": "合约主动成交失衡", "value": f_imbalance,
                "direction": "up" if f_imbalance > 0 else "down" if f_imbalance < 0 else "unknown",
                "as_of": futures_as_of,
            },
            "oi_change_1h_pct": {
                "label": "OI 一小时变化", "value": oi_change,
                "direction": "up" if (oi_change or 0) > 0 else "down" if (oi_change or 0) < 0 else "unknown",
                "as_of": oi_as_of,
            },
            "funding_rate": {
                "label": "资金费率", "value": funding_rate,
                "direction": "down" if (funding_rate or 0) > 0 else "up" if (funding_rate or 0) < 0 else "unknown",
                "as_of": funding_as_of,
            },
            "realized_liquidation_usd": {
                "label": "一小时已实现清算", "value": (
                    realized.executed_notional_usd if realized else None
                ), "direction": (
                    "down" if realized and realized.long_executed_notional_usd > realized.short_executed_notional_usd
                    else "up" if realized else "unknown"
                ), "as_of": liq_as_of,
            },
            "wall_attack_intensity": {
                "label": "盘口墙攻击/撤离", "value": max_wall_intensity,
                "direction": "unknown", "as_of": op_as_of,
            },
        }
        poll_failures = copy.deepcopy(getattr(state, "poll_failures", {}) or {})
        dependency_failures = copy.deepcopy(
            getattr(state, "dependency_failures", {}) or {}
        )
        strategic_ai_failure = str(dependency_failures.get("strategic_ai") or "")
        context = {
            "market_overview": {"trend_horizons": trend_horizons},
            "native_liquidity": {
                **native_liquidity,
                "availability": (
                    "available" if qualities["native_liquidity"].decision_usable
                    else "unavailable"
                ),
                "note": "Binance、OKX、Coinbase 原生现货深度至少两家同时新鲜才参与流动性确认。",
            },
            "live_facts": live_facts,
            "ordinary_anomalies": self._ordinary_anomalies(coin, live_facts, now),
            "leverage_refill_ratio": lrr,
            "etf": {
                "availability": "available" if (ibit_official or etf) else "unavailable",
                "source": "ishares_ibit_official" if ibit_official else (
                    getattr(etf, "source", "coinglass") if etf else "unavailable"
                ),
                "source_strength": "issuer_official" if ibit_official else (
                    "secondary_cross_check" if etf else "unavailable"
                ),
                "official_ibit": {
                    "as_of": ibit_official.get("as_of"),
                    "as_of_ts": ibit_official.get("as_of_ts"),
                    "known_at": int((ibit_observation or {}).get("first_observed_at") or 0),
                    "revision_version": int((ibit_observation or {}).get("version") or 0),
                    "revised": bool((ibit_observation or {}).get("revised")),
                    "shares_outstanding": ibit_official.get("shares_outstanding"),
                    "bitcoin_quantity": ibit_official.get("bitcoin_quantity"),
                    "bitcoin_market_value_usd": ibit_official.get("bitcoin_market_value_usd"),
                } if ibit_official else None,
                "trading_day": getattr(etf, "trading_day", ""),
                "published_at": _seconds(getattr(etf, "published_at", 0)),
                "known_at": int((etf_observation or {}).get("first_observed_at") or 0),
                "revision_version": int((etf_observation or {}).get("version") or 0),
                "revised": bool((etf_observation or {}).get("revised")),
                "net_3d": getattr(etf, "net_3d", None),
                "recent_days": [
                    row.model_dump() if hasattr(row, "model_dump") else row
                    for row in list(getattr(etf, "recent_days", []) or [])
                ],
                "note": (
                    "IBIT 发行商官方日终持仓为第一层，Coinglass 为交叉检查；"
                    "日度持仓/申赎结果不能描述为贝莱德刚刚在市场买卖 BTC。"
                ),
            },
            "options": {
                "availability": "available" if options_meaningful else "unavailable",
                "source": getattr(options, "source", "coinglass") if options else "unavailable",
                "known_at": _seconds(getattr(options, "known_at", 0)),
                "put_call_oi_ratio": getattr(options, "put_call_oi_ratio", None),
                "iv_atm": getattr(options, "iv_atm", None),
                "iv_skew": getattr(options, "iv_skew", None),
                "term_structure": getattr(options, "term_structure", []),
                "strike_clusters": getattr(options, "strike_clusters", []),
                "expiry_concentration": getattr(options, "expiry_concentration", None),
                "max_pain_weight": "low",
                "gex": getattr(options, "gex_status", "unavailable"),
                "reason": "" if options_meaningful else "缺少可靠 ATM IV、偏斜或期限结构",
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
            "institutional_futures": {
                "availability": "available" if cftc_cot else "unavailable",
                "source": "cftc_official_cme_futures_only",
                "decision_role": "informational",
                "report_date": cftc_cot.get("report_date") if cftc_cot else None,
                "report_as_of": cftc_cot.get("report_as_of") if cftc_cot else 0,
                "known_at": int((cftc_observation or {}).get("first_observed_at") or 0),
                "revision_version": int((cftc_observation or {}).get("version") or 0),
                "revised": bool((cftc_observation or {}).get("revised")),
                "open_interest_contracts": cftc_cot.get("open_interest_contracts") if cftc_cot else None,
                "noncommercial_net": cftc_cot.get("noncommercial_net") if cftc_cot else None,
                "noncommercial_net_change": cftc_cot.get("noncommercial_net_change") if cftc_cot else None,
                "note": (
                    "CFTC 周度持仓只作慢周期背景；known_at 是系统首次看到该报告的时间。"
                    if cftc_cot else "尚未取得本期 CFTC 官方 PIT 快照。"
                ),
            },
            "exchange_flows": {
                "availability": "unavailable", "source": "cryptoquant",
                "decision_role": "informational",
                "reason": "地址聚类可能回溯修订；未保存首次观测与修订版前不计分",
            },
            "institutional_entities": {
                "availability": "unavailable", "sources": ["nansen_bitcoin", "arkham"],
                "decision_role": "informational",
                "reason": "缺少独立实体标签交叉确认，不把钱包转账解释为机构买卖",
            },
            "dependency_degradation": {
                "market_data_poll_failures": poll_failures,
                "ai": "isolated_not_scoring",
                "ai_detail": {
                    "status": "degraded" if strategic_ai_failure else "available_or_idle",
                    "reason": strategic_ai_failure or None,
                    "decision_role": "isolated_not_scoring",
                },
                "note": "行情依赖失败独立展示；AI 永不进入联合风险确定性评分。",
            },
        }
        qualities["context"] = self._quality(
            "context", max(
                _seconds(getattr(etf, "ts", 0)),
                int((ibit_official or {}).get("observed_at") or 0),
                _seconds(getattr(options, "ts", 0)),
                _seconds(getattr(stablecoin, "ts", 0)),
            ), self.config.source_max_age_sec["context"], now,
            available=bool(ibit_official or etf or options or stablecoin), continuous=None,
            valid=True,
        )
        return evidence, qualities, realized, densities, context

    def _summarize_roots(
        self, evidence: list[EvidenceItem], qualities: dict[str, SourceQuality],
    ) -> tuple[dict[str, PillarSnapshot], dict[str, EvidenceItem]]:
        grouped: dict[str, list[EvidenceItem]] = {}
        for item in evidence:
            if item.role != "scoring" or item.direction not in {"up", "down"}:
                continue
            grouped.setdefault(item.causal_root, []).append(item)
        dominance = self.calibration.thresholds["root_direction_dominance_ratio"]
        roots: dict[str, EvidenceItem] = {}
        conflicted: set[str] = set()
        for root_name, items in sorted(grouped.items()):
            scores = {
                direction: sum(
                    item.confidence * max(item.raw_strength, 0.0)
                    for item in items if item.direction == direction
                )
                for direction in ("up", "down")
            }
            if scores["up"] > 0 and scores["down"] > 0:
                ratio = max(scores.values()) / max(min(scores.values()), 1e-12)
                if ratio < dominance:
                    conflicted.add(root_name)
                    continue
            winner = max(("up", "down"), key=lambda side: (scores[side], side))
            if scores[winner] <= 0:
                continue
            candidates = [item for item in items if item.direction == winner]
            representative = max(
                candidates,
                key=lambda item: (
                    item.confidence * item.raw_strength,
                    item.raw_strength,
                    item.evidence_id,
                ),
            )
            winning_raw = sum(item.raw_strength for item in candidates)
            roots[root_name] = representative.model_copy(update={
                "raw_strength": winning_raw,
                "strength": _clamp(winning_raw),
                "values": {
                    **representative.values,
                    "root_up_score": scores["up"],
                    "root_down_score": scores["down"],
                    "root_dominance_ratio": (
                        max(scores.values()) / max(min(scores.values()), 1e-12)
                        if min(scores.values()) > 0 else None
                    ),
                },
            })
        pillars: dict[str, PillarSnapshot] = {}
        for pillar in (
            "spot_demand", "leveraged_positioning", "liquidation_risk",
            "liquidity_structure", "market_response", "context",
        ):
            items = [item for item in evidence if item.pillar == pillar]
            root_names = sorted({item.causal_root for item in items})
            resolved_items = [
                item for root, item in roots.items()
                if item.pillar == pillar and root not in conflicted
            ]
            strongest = max(
                resolved_items,
                key=lambda item: (item.confidence * item.raw_strength, item.evidence_id),
                default=None,
            )
            pillar_conflicted = any(
                item.pillar == pillar and item.causal_root in conflicted for item in items
            )
            pillars[pillar] = PillarSnapshot(
                pillar=pillar,
                direction="mixed" if pillar_conflicted and not strongest else (
                    strongest.direction if strongest else "unknown"
                ),
                confidence=strongest.confidence if strongest else 0.0,
                causal_roots=root_names,
                evidence_ids=[item.evidence_id for item in items],
                decision_usable=any(
                    quality.decision_usable for source_id, quality in qualities.items()
                    if (
                        (pillar == "spot_demand" and "spot" in source_id)
                        or (pillar == "leveraged_positioning" and source_id in {"standardized_oi", "binance_futures_aggtrade", "funding"})
                        or (pillar == "liquidation_risk" and "liquidation" in source_id)
                        or (pillar == "liquidity_structure" and source_id in {"liquidity_wall", "native_liquidity"})
                        or (pillar == "market_response" and source_id == "market_response")
                        or (pillar == "context" and source_id == "context")
                    )
                ),
                note=(
                    "同一因果根多空优势不足，按 mixed 排除方向票。"
                    if pillar_conflicted else "同一 causal root 只计一次独立证据。"
                ),
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
            if not context.degraded_since:
                context.degraded_since = now
            return context, transitions, "data_degraded：最后确认状态已冻结，禁止升级、解决和邮件"
        context.degraded_since = 0
        context.last_confirmed_at = now

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
            elif desired in {"watch", "normal"} and now - context.stage_since >= int(
                self.calibration.thresholds["warning_to_watch_sec"]
            ):
                target = "watch"
        elif context.stage == "critical":
            if desired == "critical":
                context.last_critical_at = now
            elif now - context.stage_since >= int(
                self.calibration.thresholds["critical_to_warning_sec"]
            ):
                target = "warning"
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

    def evaluate_coin(
        self, coin: str, decision_time: Optional[int] = None, *,
        frame: Optional[DecisionFrame] = None,
    ) -> MarketIncidentSnapshot:
        coin = coin.upper()
        if coin not in self.config.coins:
            raise ValueError(f"market risk disabled for coin {coin}")
        now = int(decision_time or time.time())
        self._ensure_governance_epoch(now)
        existing = self.store.latest(coin)
        if existing is not None and existing.decision_time == now:
            return existing
        state = frame or self._capture_decision_frame(coin, now)
        evidence, qualities, realized, densities, context_payload = self._extract(coin, state, now)
        context_payload, context_pit = self._sanitize_context_pit(context_payload, now)
        pit_violations = [
            reason
            for source_id, quality in qualities.items()
            for reason in quality.reasons
            if reason.startswith("pit_")
        ] + [
            f"{item.source_id}:event_time_after_decision_{item.values['rejected_future_event_time']}"
            for item in evidence
            if "rejected_future_event_time" in item.values
        ] + context_pit
        for source_id, quality in qualities.items():
            for reason in quality.reasons:
                if reason.startswith("pit_"):
                    self.store.add_gap_marker({
                        "source_id": source_id, "coin": coin,
                        "observed_at": now, "reason": reason,
                    })
        for item in evidence:
            if "rejected_future_event_time" in item.values:
                self.store.add_gap_marker({
                    "source_id": item.source_id, "coin": coin,
                    "observed_at": now,
                    "reason": "event_time_after_decision",
                    "reported_event_time": item.values["rejected_future_event_time"],
                })
        for reason in context_pit:
            self.store.add_gap_marker({
                "source_id": "market_risk_context_pit_guard", "coin": coin,
                "observed_at": now, "reason": reason,
            })
        if pit_violations:
            self.store.close_governance_epoch(
                self._governance_scope, "market_risk_pit_violation", now,
                {"coin": coin, "violations": list(dict.fromkeys(pit_violations))},
            )
        pillars, roots = self._summarize_roots(evidence, qualities)
        desired, direction, aligned_roots, research = self._desired_stage(roots, pillars)

        spot_usable = qualities["binance_spot_aggtrade"].decision_usable
        live_liquidity_usable = bool(
            qualities["native_liquidity"].decision_usable
        )
        confirmation_usable = sum((
            qualities["standardized_oi"].decision_usable,
            qualities["realized_liquidations"].decision_usable
            or qualities["estimated_liquidation_density"].decision_usable,
            live_liquidity_usable,
        ))
        degraded = bool(not spot_usable or confirmation_usable < 2 or pit_violations)
        prior_machine = self._contexts[coin].model_copy(deep=True)
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
            live_direction=direction,
            incident_id=machine.incident_id, episode_id=machine.episode_id,
            stage_since=machine.stage_since,
            mode=self.mode,
            shadow_mode=self.mode == "shadow",
            stage_frozen=degraded,
            frozen_since=machine.degraded_since,
            last_confirmed_at=machine.last_confirmed_at,
            valid_for_calibration=not degraded and not pit_violations,
            pit_violations=pit_violations,
            research_signals=research,
            causal_roots=aligned_roots,
            live_causal_roots=aligned_roots,
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
                self.mode == "production_alerting"
                and self._email_channel_enabled()
                and self.calibration.admitted_for_production
                and machine.stage in {"warning", "critical"}
                and not degraded
            ),
        )
        from processors.market_risk_replay import validate_snapshot_pit
        submit_violations = validate_snapshot_pit(snapshot)
        if submit_violations:
            prior_machine.degraded_since = prior_machine.degraded_since or now
            self._contexts[coin] = prior_machine
            machine = prior_machine
            transitions = []
            snapshot = snapshot.model_copy(update={
                "stage": machine.stage,
                "direction": machine.direction,
                "incident_id": machine.incident_id,
                "episode_id": machine.episode_id,
                "stage_since": machine.stage_since,
                "quality_layer": "data_degraded",
                "stage_frozen": True,
                "frozen_since": machine.degraded_since,
                "valid_for_calibration": False,
                "pit_violations": list(dict.fromkeys([
                    *snapshot.pit_violations, *submit_violations,
                ])),
                "notification_eligible": False,
            })
            for violation in submit_violations:
                self.store.add_gap_marker({
                    "source_id": "market_risk_pit_guard", "coin": coin,
                    "observed_at": now, "reason": violation,
                })
            self.store.close_governance_epoch(
                self._governance_scope, "market_risk_submit_pit_violation", now,
                {"coin": coin, "violations": submit_violations},
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

    def _sanitize_context_pit(
        self, payload: dict[str, Any], decision_time: int,
    ) -> tuple[dict[str, Any], list[str]]:
        violations: list[str] = []

        def walk(value: Any, path: str) -> Any:
            if isinstance(value, dict):
                result = {key: walk(child, f"{path}.{key}") for key, child in value.items()}
                for field in ("known_at", "published_at"):
                    raw = result.get(field)
                    try:
                        timestamp = int(raw or 0)
                    except (TypeError, ValueError):
                        timestamp = 0
                    if timestamp > decision_time:
                        violations.append(f"{path}:{field}_after_decision")
                        result[f"reported_{field}"] = timestamp
                        result[field] = decision_time
                        result["availability"] = "unavailable"
                        result["reason"] = "未来可知时间已被 PIT 门禁拒绝"
                return result
            if isinstance(value, list):
                return [walk(child, f"{path}[{index}]") for index, child in enumerate(value)]
            return value

        return walk(payload, "context"), violations
