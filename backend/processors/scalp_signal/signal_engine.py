"""短线信号引擎主任务 · 串联 RegimeGate / Strategy / Veto / Scorer / Conflict / Settlement

异步循环（每 tick_interval_sec 一次）：
  0) 记录 (now, ticker.last) 到 _price_buffer（P0-1 结算价中位数依赖）
  1) settle_due_signals（active 到期 + cancelled 到期 shadow settle）
  2) RegimeGate.evaluate(state) → allow?
     - regime ∈ {extreme, high_vol_chop} → 取消所有活跃信号（kind=regime_flip）
     - 不允许 + 普通 → 跳过本轮
  3) MTFBiasComputer.compute(state, horizon) → bias
  4) for strategy in registry.enabled_for(...):
     - candidate = strategy.detect(ctx)
     - veto.evaluate
     - if not passed: continue
     - score(candidate, ctx, ...calibration_lookup, hist_winrate_with_n) 
     - if confidence < threshold: continue
     - 收集 → CandidateBundle 列表
  5) conflict_resolver.resolve(bundles)
     - accepted → build_signal（含 features_snapshot / version / config_hash） + add_active + notify
     - rejected → 仅日志（不持久化）
  6) calibrator.recompute_stats / recompute_calibration（结算后触发）

P0 修复要点：
  - P0-1 _price_buffer + _price_lookup 实现 ±10s 中位数结算价
  - P0-2 calibration_lookup 注入 ConfidenceScorer，hit_probability 校准化
  - P0-3 historical_winrate 带 sample_size 注入，scorer 内做样本量惩罚
  - P0-4 cancelled 信号保留活跃池，shadow_settle 由 settle_due_signals 接管
  - P0-6 ConflictResolver 在 build 前消除反向/同向冲突
  - P0-7 _build_signal 写入 features_snapshot / strategy_version / scorer_version / config_hash

铁律：
  - 引擎仅消费 state（只读），永不写回 state
  - 任何异常逐策略隔离
  - 可观测性：
      * 每轮 tick 打 1 条 INFO `scalp tick | …`（含结算/池子/各策略阶段），便于 grep 巡检
      * veto/threshold 细节仍可用 DEBUG：`scalp veto blocked` / `scalp threshold not met`
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

from models.scalp_signal import (
    SETTLEMENT_WINDOW_SEC,
    ScalpConfig,
    ScalpSignal,
    StrategyName,
    calc_expiry_ts,
    make_signal_id,
)

from processors.scalp_signal.base_strategy import StrategyCandidate, StrategyContext
from processors.scalp_signal.calibrator import Calibrator
from processors.scalp_signal.confidence_scorer import ConfidenceScorer, ScoringResult
from processors.scalp_signal.conflict_resolver import (
    CandidateBundle,
    resolve as resolve_conflicts,
)
from processors.scalp_signal.mtf_bias import MTFBiasComputer
from processors.scalp_signal.regime_gate import RegimeGate
from processors.scalp_signal.settlement import (
    PriceLookupResult,
    SettlementBatch,
    cancel_active_by_predicate,
    median_price_in_window,
    settle_due_signals,
)
from processors.scalp_signal.strategy_registry import StrategyRegistry
from processors.scalp_signal.veto_gate import VetoGate

from storage.scalp_signal_store import ScalpSignalStore

logger = logging.getLogger(__name__)


# 引擎参数
DEFAULT_TICK_INTERVAL_SEC = 30.0
# P0-1：高频价格采样间隔（独立于主 tick）；过短增加开销，过长无法做中位数
PRICE_SAMPLING_INTERVAL_SEC = 5.0
# Price buffer 容量：覆盖最近 ~10 分钟（120 个 5s 样本足够 ±10s 中位数 + 容错）
PRICE_BUFFER_MAX_LEN = 240
# 版本常量（P0-7）：全局算法版本，发生不向后兼容修改时升级
STRATEGY_VERSION = "v1"
SCORER_VERSION = "v1"


# 通知回调签名（异步函数：signal → None）
NotifyCallback = Callable[[ScalpSignal], Awaitable[None]]
SettleCallback = Callable[[ScalpSignal], Awaitable[None]]


def _config_hash(cfg: ScalpConfig) -> str:
    """根据当前 ScalpConfig 算 12 位 hash（P0-7）

    覆盖：strategies / coin / horizon_min / notification（不含 enabled 总开关）
    """
    payload = {
        "coin": cfg.coin,
        "horizon_min": cfg.horizon_min,
        "strategies": {
            k.value: {
                "enabled": v.enabled,
                "confidence_threshold": v.confidence_threshold,
                "cooldown_min": v.cooldown_min,
            }
            for k, v in cfg.strategies.items()
        },
        "notification": {
            "browser_min_confidence": cfg.notification.browser_min_confidence,
            "email_min_confidence": cfg.notification.email_min_confidence,
        },
    }
    import json
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha1(raw).hexdigest()[:12]


def _build_features_snapshot(
    ctx: StrategyContext,
    candidate: StrategyCandidate,
    *,
    bias_summary: str,
) -> dict:
    """从 StrategyContext + state 抽取关键事实快照（P0-7）

    所有字段都是只读 state 派生，失败 → None / 0（不报错）
    设计目标：5 分钟后回看信号能复现"当时上下文"，无需上游 state
    """
    state = ctx.state
    snap: dict = {
        "bias_score": round(float(ctx.bias_score), 3),
        "bias_summary": bias_summary,
        "regime": ctx.regime,
        "range_position_pct": round(float(ctx.range_position_pct or 0.0), 2),
        "raw_strength": round(float(candidate.raw_strength), 3),
    }

    # range_signal（box_state / box_width / test_count）
    rs = getattr(state, "range_signal", None)
    if rs is not None:
        snap["box_state"] = getattr(rs, "box_state", "")
        snap["box_width_pct"] = round(float(getattr(rs, "box_width_pct", 0) or 0), 3)
        snap["box_quality"] = int(getattr(rs, "box_quality", 0) or 0)
        snap["range_test_count_max"] = int(max(
            getattr(rs, "range_upper_test_count", 0) or 0,
            getattr(rs, "range_lower_test_count", 0) or 0,
        ))

    # regime_snapshot（adx / volatility）
    regime_snap = getattr(state, "regime_snapshot", None)
    if regime_snap is not None:
        snap["adx"] = round(float(getattr(regime_snap, "adx", 0) or 0), 2)
        snap["atr_pct"] = round(float(getattr(regime_snap, "atr_pct", 0) or 0), 3)

    # 最近 KL（从 candidate.evidence 中已有，但显式存一份方便检索）
    kl_snap = getattr(state, "key_level_snapshot_v2", None)
    if kl_snap is not None:
        levels = getattr(kl_snap, "levels", None) or getattr(kl_snap, "key_levels", None) or []
        ref = candidate.reference_price
        nearest_dist = None
        nearest_score = None
        nearest_state = None
        if ref > 0:
            for lv in levels:
                lv_price = getattr(lv, "price", 0) or 0
                if lv_price <= 0:
                    continue
                d = abs(lv_price - ref) / ref * 100.0
                if nearest_dist is None or d < nearest_dist:
                    nearest_dist = d
                    nearest_score = float(getattr(lv, "final_score", 0) or 0)
                    nearest_state = getattr(lv, "state", "")
        if nearest_dist is not None:
            snap["nearest_kl_dist_pct"] = round(nearest_dist, 3)
            snap["nearest_kl_score"] = round(nearest_score or 0.0, 1)
            snap["nearest_kl_state"] = nearest_state

    # ticker
    ticker = getattr(state, "ticker", None)
    if ticker is not None:
        snap["ticker_last"] = float(getattr(ticker, "last", 0) or 0)
        snap["ticker_age_sec"] = max(0, int(ctx.now_ts - (getattr(ticker, "ts", 0) or 0)))

    # 候选自身的 evidence_score 总和（核证据强度）
    snap["evidence_count"] = len(candidate.evidence)
    return snap


class SignalEngine:
    """短线信号引擎主体

    构造时注入：
        store / registry / regime_gate / mtf_bias / scorer / veto_gate / calibrator
        config_getter: 每 tick 读最新配置
        state_getter: 每 tick 读最新 CoinState（接到 engine.py 的 _states）
        blackswan_getter: 返回 (active, age_sec)
        on_signal_created / on_signal_settled: WS / 邮件回调
    """

    def __init__(
        self,
        *,
        store: ScalpSignalStore,
        registry: StrategyRegistry,
        regime_gate: Optional[RegimeGate] = None,
        mtf_bias: Optional[MTFBiasComputer] = None,
        scorer: Optional[ConfidenceScorer] = None,
        veto_gate: Optional[VetoGate] = None,
        calibrator: Optional[Calibrator] = None,
        config_getter: Callable[[], ScalpConfig],
        state_getter: Callable[[str], Any],
        blackswan_getter: Optional[Callable[[], tuple[bool, Optional[int]]]] = None,
        on_signal_created: Optional[NotifyCallback] = None,
        on_signal_settled: Optional[SettleCallback] = None,
        tick_interval_sec: float = DEFAULT_TICK_INTERVAL_SEC,
    ) -> None:
        self._store = store
        self._registry = registry
        self._regime_gate = regime_gate or RegimeGate()
        self._mtf_bias = mtf_bias or MTFBiasComputer()
        self._scorer = scorer or ConfidenceScorer()
        self._veto_gate = veto_gate or VetoGate()
        self._calibrator = calibrator or Calibrator(store)
        self._config_getter = config_getter
        self._state_getter = state_getter
        self._blackswan_getter = blackswan_getter or (lambda: (False, None))
        self._on_created = on_signal_created
        self._on_settled = on_signal_settled
        self._tick_interval = tick_interval_sec
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._price_task: Optional[asyncio.Task] = None
        # P0-1 价格 buffer：(ts, last_price) 双端队列；高频采样独立任务填充
        self._price_buffer: deque[tuple[int, float]] = deque(maxlen=PRICE_BUFFER_MAX_LEN)

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        # P0-1：独立的高频价格采样任务（5s 一次），覆盖结算窗口
        self._price_task = asyncio.create_task(self._price_sampling_loop())
        logger.info(
            "scalp signal engine started | tick=%.1fs | price_sample=%.1fs",
            self._tick_interval, PRICE_SAMPLING_INTERVAL_SEC,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        for t in (self._task, self._price_task):
            if t is not None:
                try:
                    await asyncio.wait_for(t, timeout=5.0)
                except asyncio.TimeoutError:
                    t.cancel()
        self._task = None
        self._price_task = None
        logger.info("scalp signal engine stopped")

    async def _price_sampling_loop(self) -> None:
        """P0-1：独立的高频价格采样任务（不耦合主 tick）

        每 PRICE_SAMPLING_INTERVAL_SEC 取一次 ticker.last 入 buffer。
        失败 / 价格无效时只 debug 日志，不抛异常。
        """
        while not self._stop_event.is_set():
            try:
                cfg = self._config_getter()
                if cfg is not None and cfg.enabled:
                    state = self._state_getter(cfg.coin)
                    price = self._safe_attr(state, "ticker", "last", default=0.0) or 0.0
                    if price > 0:
                        self._price_buffer.append((int(time.time()), float(price)))
            except Exception as e:  # noqa: BLE001
                logger.debug("scalp price sampling failed: %s", e)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=PRICE_SAMPLING_INTERVAL_SEC,
                )
            except asyncio.TimeoutError:
                continue

    # ── 主循环 ────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick_once()
            except Exception as e:  # noqa: BLE001
                logger.error("scalp engine tick failed | err=%s", e, exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._tick_interval,
                )
            except asyncio.TimeoutError:
                continue

    async def tick_once(self, *, now_ts: Optional[int] = None) -> dict:
        """单次 tick · 返回结构化报告（settled / shadow_settled / cancelled / generated / rejected）"""
        now = now_ts if now_ts is not None else int(time.time())
        cfg = self._config_getter()
        result: dict[str, Any] = {
            "settled": [],
            "shadow_settled": [],
            "cancelled": [],
            "generated": [],
            "rejected": [],
            "skipped_reason": None,
            "regime": None,
        }

        if not cfg.enabled:
            result["skipped_reason"] = "config_disabled"
            logger.debug(
                "scalp tick skip | reason=config_disabled coin=%s (引擎总开关关闭)",
                cfg.coin,
            )
            return result

        coin = cfg.coin
        state = self._state_getter(coin)
        if state is None:
            result["skipped_reason"] = "no_state"
            logger.warning(
                "scalp tick skip | reason=no_state coin=%s (无 CoinState，主 engine 是否未就绪？)",
                coin,
            )
            return result

        # 0) 记录 price tick（P0-1 结算依据）
        cur_price = self._safe_attr(state, "ticker", "last", default=0.0) or 0.0
        if cur_price > 0:
            self._price_buffer.append((now, float(cur_price)))

        # 1) 结算到期信号（包含 active settle + cancelled shadow settle）
        batch: SettlementBatch = settle_due_signals(
            self._store,
            price_lookup=self._make_price_lookup(),
            now_ts=now,
            window_sec=SETTLEMENT_WINDOW_SEC,
        )
        if batch.settled or batch.shadow_settled:
            for r in batch.settled:
                result["settled"].append(r.signal_id)
                if self._on_settled is not None:
                    sig = self._find_in_history(r.signal_id)
                    if sig is not None:
                        await self._safe_callback(self._on_settled, sig)
            for r in batch.shadow_settled:
                result["shadow_settled"].append(r.signal_id)
                if self._on_settled is not None:
                    sig = self._find_in_history(r.signal_id)
                    if sig is not None:
                        await self._safe_callback(self._on_settled, sig)
            try:
                self._calibrator.recompute_stats(coin=coin, horizon_min=cfg.horizon_min, now_ts=now)
                self._calibrator.recompute_calibration(coin=coin, horizon_min=cfg.horizon_min, now_ts=now)
            except Exception as e:  # noqa: BLE001
                logger.warning("calibrator recompute failed: %s", e)

        # 2) 黑天鹅 → 取消所有活跃信号 + 跳过本轮
        bs_active, bs_age = self._blackswan_getter()
        if bs_active:
            cancelled = cancel_active_by_predicate(
                self._store,
                lambda _sig: f"blackswan active (age={bs_age}s)",
                invalidation_kind="blackswan",
                now_ts=now, price_at_cancel=cur_price or None,
            )
            result["cancelled"] = [s.signal_id for s in cancelled]
            result["skipped_reason"] = "blackswan_active"
            logger.info(
                "scalp tick skip | coin=%s reason=blackswan_active cancelled_active=%d "
                "settled=%d shadow=%d",
                coin, len(cancelled), len(result["settled"]), len(result["shadow_settled"]),
            )
            return result

        # 3) RegimeGate 检查
        gate = self._regime_gate.evaluate(state)
        result["regime"] = gate.regime
        if not gate.allow:
            if gate.regime in {"extreme", "high_vol_chop"}:
                cancelled = cancel_active_by_predicate(
                    self._store,
                    lambda _sig: f"regime changed to {gate.regime}",
                    invalidation_kind="regime_flip",
                    now_ts=now, price_at_cancel=cur_price or None,
                )
                result["cancelled"] = [s.signal_id for s in cancelled]
            result["skipped_reason"] = gate.skip_reason
            logger.info(
                "scalp tick skip | coin=%s reason=regime_gate:%s regime=%s "
                "cancelled_active=%d settled=%d shadow=%d pending_settle=%d",
                coin, gate.skip_reason, gate.regime, len(result["cancelled"]),
                len(result["settled"]), len(result["shadow_settled"]),
                len(batch.pending),
            )
            return result

        # 4) MTF Bias
        bias = self._mtf_bias.compute(state, cfg.horizon_min)

        # 5) 策略评估 → 收集候选
        ctx = StrategyContext(
            state=state,
            btc_state=self._state_getter("BTC") if coin != "BTC" else None,
            coin=coin,
            horizon_min=cfg.horizon_min,
            regime=gate.regime,
            range_position_pct=gate.range_position_pct,
            bias_score=bias.bias_score,
            bias_components=bias.components,
            now_ts=now,
        )

        bundles: list[tuple[CandidateBundle, StrategyCandidate, ScoringResult, list, str]] = []
        stage_parts: list[str] = []
        # bundle, candidate, scoring, veto.reasons_passed, bias_summary
        for strategy in self._registry.enabled_for(cfg, gate.regime, cfg.horizon_min):
            try:
                bundle_pack, stage = self._evaluate_one(
                    strategy=strategy, ctx=ctx, cfg=cfg,
                    blackswan_active=bs_active,
                    blackswan_age_sec=bs_age,
                    now=now,
                )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "scalp strategy %s evaluate failed | err=%s",
                    strategy.name.value, e, exc_info=True,
                )
                stage_parts.append(f"{strategy.name.value}:exception")
                continue
            stage_parts.append(f"{strategy.name.value}:{stage}")
            if bundle_pack is None:
                continue
            bundles.append(bundle_pack + (bias.summary_cn,))

        # 6) 冲突解决
        if not bundles:
            logger.info(
                "scalp tick | coin=%s horizon=%dm regime=%s gate=allow bias=%+.3f | "
                "settle=%d shadow=%d pending_settle=%d price_buf=%d active_pool=%d | "
                "stages [%s] candidates=0 generated=0",
                coin,
                cfg.horizon_min,
                gate.regime,
                bias.bias_score,
                len(batch.settled),
                len(batch.shadow_settled),
                len(batch.pending),
                len(self._price_buffer),
                len(self._store.get_active()),
                "; ".join(stage_parts) if stage_parts else "(no enabled strategies)",
            )
            return result
        cb_list = [b[0] for b in bundles]
        report = resolve_conflicts(cb_list)
        for note in report.notes:
            logger.info("scalp conflict_resolver | %s", note)

        accepted_ids = {id(rc.bundle) for rc in report.accepted}
        for rc in report.rejected:
            logger.debug(
                "scalp candidate rejected by conflict_resolver | strategy=%s direction=%s "
                "confidence=%d reason=%s",
                rc.bundle.strategy_name, rc.bundle.direction,
                rc.bundle.confidence, rc.reject_reason,
            )
            result["rejected"].append({
                "strategy": rc.bundle.strategy_name,
                "direction": rc.bundle.direction,
                "confidence": rc.bundle.confidence,
                "reason": rc.reject_reason,
            })

        # 7) 接受的候选 → 装配 + 持久化 + 通知
        cfg_hash = _config_hash(cfg)
        for bundle, candidate, scoring, veto_reasons, bias_summary in bundles:
            if id(bundle) not in accepted_ids:
                continue
            signal = self._build_signal(
                strategy_name=StrategyName(bundle.strategy_name),
                candidate=candidate, ctx=ctx, scoring=scoring,
                veto_reasons=veto_reasons,
                bias_summary=bias_summary, now=now, config_hash=cfg_hash,
            )
            try:
                self._store.add_active(signal)
            except ValueError:
                logger.warning("scalp signal_id collision: %s", signal.signal_id)
                continue
            logger.info(
                "scalp signal created | id=%s strategy=%s direction=%s confidence=%d "
                "hit_p=%s ref=%.4f horizon=%d regime=%s cfg_hash=%s",
                signal.signal_id, signal.strategy.value, signal.direction,
                signal.confidence,
                f"{signal.hit_probability:.3f}" if signal.hit_probability is not None else "n/a",
                signal.reference_price, signal.horizon_min, signal.regime,
                cfg_hash,
            )
            result["generated"].append(signal.signal_id)
            if self._on_created is not None:
                await self._safe_callback(self._on_created, signal)

        logger.info(
            "scalp tick | coin=%s horizon=%dm regime=%s gate=allow bias=%+.3f | "
            "settle=%d shadow=%d pending_settle=%d price_buf=%d active_pool=%d | "
            "stages [%s] candidates=%d conflict_rejected=%d generated=%d ids=%s",
            coin,
            cfg.horizon_min,
            gate.regime,
            bias.bias_score,
            len(batch.settled),
            len(batch.shadow_settled),
            len(batch.pending),
            len(self._price_buffer),
            len(self._store.get_active()),
            "; ".join(stage_parts) if stage_parts else "-",
            len(bundles),
            len(report.rejected),
            len(result["generated"]),
            result["generated"] or "-",
        )
        return result

    # ── 单策略评估（detect + veto + score → CandidateBundle | None）───

    def _evaluate_one(
        self,
        *,
        strategy,
        ctx: StrategyContext,
        cfg: ScalpConfig,
        blackswan_active: bool,
        blackswan_age_sec: Optional[int],
        now: int,
    ) -> tuple[
        Optional[tuple[CandidateBundle, StrategyCandidate, ScoringResult, list]],
        str,
    ]:
        """返回 (bundle 或 None, 管线阶段缩写)

        stage 取值（用于 INFO 摘要）：
          no_detect | off | veto|<reason> | threshold|<conf>lt<th> | pass
        """
        candidate = strategy.detect(ctx)
        if candidate is None:
            return None, "no_detect"

        sc = cfg.strategies.get(strategy.name)
        if sc is None or not sc.enabled:
            return None, "off"
        last_ts = self._store.get_last_signal_ts(
            strategy.name, candidate.direction, coin=ctx.coin,
        )
        cooldown_sec = sc.cooldown_min * 60

        veto = self._veto_gate.evaluate(
            candidate, ctx,
            last_signal_ts=last_ts, cooldown_sec=cooldown_sec,
            blackswan_active=blackswan_active, blackswan_age_sec=blackswan_age_sec,
            now_ts=now,
        )
        if not veto.passed:
            logger.debug(
                "scalp veto blocked | strategy=%s direction=%s reason=%s detail=%s",
                strategy.name.value, candidate.direction,
                veto.reason_blocked, veto.block_detail,
            )
            rb = veto.reason_blocked or "unknown"
            return None, f"veto|{rb}"

        # P0-3：带 sample_size 注入；P0-2：calibration_lookup 注入
        hist_wr, hist_n = self._calibrator.historical_winrate_with_sample_size(strategy.name)
        scoring = self._scorer.score(
            candidate, ctx,
            strategy_name=strategy.name,
            historical_winrate=hist_wr,
            historical_winrate_sample_size=hist_n,
            calibration_lookup=self._calibrator.lookup_calibrated_probability,
            now_ts=now,
        )

        if scoring.confidence < sc.confidence_threshold:
            logger.debug(
                "scalp threshold not met | strategy=%s confidence=%d < %d",
                strategy.name.value, scoring.confidence, sc.confidence_threshold,
            )
            return None, (
                f"threshold|{scoring.confidence}lt{sc.confidence_threshold}"
            )

        bundle = CandidateBundle(
            strategy_name=strategy.name.value,
            direction=candidate.direction,
            confidence=scoring.confidence,
            candidate=candidate,
            scoring=scoring,
        )
        return (bundle, candidate, scoring, veto.reasons_passed), "pass"

    # ── 装配 ──────────────────────────────────────────────────

    def _build_signal(
        self,
        *,
        strategy_name: StrategyName,
        candidate: StrategyCandidate,
        ctx: StrategyContext,
        scoring: ScoringResult,
        veto_reasons: list,
        bias_summary: str,
        now: int,
        config_hash: str,
    ) -> ScalpSignal:
        """装配最终 ScalpSignal · 含 P0-7 版本化 + features_snapshot"""
        sid = make_signal_id(ctx.coin, strategy_name, now, candidate.direction)
        all_evidence = list(candidate.evidence) + list(scoring.extra_evidence)
        features = _build_features_snapshot(ctx, candidate, bias_summary=bias_summary)
        return ScalpSignal(
            signal_id=sid,
            coin=ctx.coin,
            horizon_min=ctx.horizon_min,
            direction=candidate.direction,
            strategy=strategy_name,
            reference_price=candidate.reference_price,
            created_at=now,
            expiry_ts=calc_expiry_ts(now, ctx.horizon_min),
            entry_window_sec=60,
            confidence=scoring.confidence,
            hit_probability=scoring.hit_probability,
            hit_probability_source=scoring.hit_probability_source,
            calibration_sample_size=scoring.calibration_sample_size,
            regime=ctx.regime,
            bias_score=ctx.bias_score,
            factor_breakdown=scoring.factor_breakdown,
            evidence=all_evidence,
            veto_check_passed=list(veto_reasons),
            strategy_version=STRATEGY_VERSION,
            scorer_version=SCORER_VERSION,
            config_hash=config_hash,
            features_snapshot=features,
            state="active",
            test_mode=True,
        )

    # ── Price lookup（P0-1 结算价中位数）──────────────────────

    def _make_price_lookup(self):
        """生成 PriceLookupFn 闭包（绑定当前 buffer 快照）"""
        # 复制 buffer 防 deque 在迭代过程中变更
        samples = list(self._price_buffer)

        def _lookup(target_ts: int, window_sec: int) -> PriceLookupResult:
            return median_price_in_window(
                samples, target_ts=target_ts, window_sec=window_sec,
            )
        return _lookup

    # ── Helpers ───────────────────────────────────────────────

    def _find_in_history(self, signal_id: str) -> Optional[ScalpSignal]:
        for s in self._store.iter_history(limit=200):
            if s.signal_id == signal_id:
                return s
        return None

    @staticmethod
    def _safe_attr(obj, *path: str, default=None):
        cur = obj
        for seg in path:
            if cur is None:
                return default
            cur = getattr(cur, seg, None)
        return cur if cur is not None else default

    @staticmethod
    async def _safe_callback(cb: Callable, *args) -> None:
        try:
            await cb(*args)
        except Exception as e:  # noqa: BLE001
            logger.error("scalp callback failed: %s", e, exc_info=True)
