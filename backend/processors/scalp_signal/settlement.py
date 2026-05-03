"""信号结算 · 到期自动判赢/输/平 + 状态机迁移 + 结算价精度 + Shadow settlement

设计原则：
  - 无副作用的纯结算函数 + orchestrators
  - **P0-1 结算价取 expiry_ts ±10s 内 last 的中位数**（防插针，防单点价异常）
  - **P0-4 cancelled 信号到期仍 shadow settle**（双口径统计 → 衡量取消触发器有效性）
  - 状态机迁移：
      active           → expired_won/lost/push（正常路径）
      active           → cancelled（regime 反转 / 黑天鹅 / 数据 stale / 用户手动）
      cancelled (等待) → 到期后做 shadow_settle，写入 shadow_outcome → archive
  - 任何异常逐条隔离

铁律：
  - 仅消费 store + price_lookup callable，不读 state
  - 价格缺失（buffer 未启动 / 中位数样本不足）→ 不强行结算，下一 tick 重试
  - cancel_one 不再 archive；改为 in-place 标记 + 保留活跃池

为什么不复用其他模块的"settlement"？
  - 项目中无现成的"二元方向预测结算"逻辑
  - OpportunityState 是基于 RR 的（开仓 → 止盈/止损/超时）
  - 故 dev-constraints #3 选择"独立新写"
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from models.scalp_signal import (
    SETTLEMENT_WINDOW_MIN_SAMPLES,
    SETTLEMENT_WINDOW_SEC,
    SHADOW_SETTLE_GRACE_SEC,
    InvalidationKind,
    ScalpSignal,
    SettlementQuality,
    SignalOutcome,
    StateTransition,
    calc_outcome,
)

from storage.scalp_signal_store import ScalpSignalStore

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 价格查询接口（供 SignalEngine 注入）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PriceLookupResult:
    """中位价查询结果（P0-1）"""
    price: Optional[float]                     # 中位数；缺失返 None
    sample_size: int = 0                       # 用于 settlement_quality 判定
    quality: SettlementQuality = "no_data"


# 类型签名：(target_ts, window_sec) → PriceLookupResult
PriceLookupFn = Callable[[int, int], PriceLookupResult]


def make_fallback_lookup(current_price: Optional[float]) -> PriceLookupFn:
    """退化版 price_lookup（仅用最新价，单元测试 / 冷启动）"""

    def _lookup(_target: int, _window: int) -> PriceLookupResult:
        if current_price is None or current_price <= 0:
            return PriceLookupResult(price=None, sample_size=0, quality="no_data")
        return PriceLookupResult(
            price=float(current_price), sample_size=1, quality="fallback",
        )

    return _lookup


# ─────────────────────────────────────────────────────────────────────────────
# 单条结算（无副作用，可复用 active / shadow 两条路径）
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SettlementOutcomeRecord:
    """单次结算结果（用于上报通知 / 日志）"""
    signal_id: str
    strategy: str
    direction: str
    reference_price: float
    settlement_price: float
    outcome: str                              # "won" / "lost" / "push"
    confidence: int
    horizon_min: int
    settlement_quality: SettlementQuality = "ok"
    settlement_window_samples: int = 0
    is_shadow: bool = False                   # P0-4：True 表示 shadow_outcome（cancelled 信号）


def settle_one(
    signal: ScalpSignal,
    *,
    settlement_price: float,
    now_ts: int,
    note: str = "",
    quality: SettlementQuality = "ok",
    window_samples: int = 0,
) -> SettlementOutcomeRecord:
    """对单条 active 信号执行结算（写入 outcome / state / settled_at + 结算精度元信息）

    Raises:
        ValueError: 信号已结算或 reference_price 无效
    """
    if signal.state != "active":
        raise ValueError(f"signal {signal.signal_id} not active: {signal.state}")
    if signal.reference_price <= 0:
        raise ValueError(f"signal {signal.signal_id} invalid reference_price")

    outcome = calc_outcome(
        direction=signal.direction,
        reference_price=signal.reference_price,
        settlement_price=settlement_price,
    )

    new_state = (
        "expired_won" if outcome == "won"
        else "expired_lost" if outcome == "lost"
        else "expired_push"
    )

    signal.state_history.append(StateTransition(
        ts=now_ts,
        from_state="active",
        to_state=new_state,  # type: ignore[arg-type]
        reason=note or f"settled at expiry, outcome={outcome}",
        price_at_ts=settlement_price,
    ))
    signal.state = new_state  # type: ignore[assignment]
    signal.outcome = outcome  # type: ignore[assignment]
    signal.settlement_price = settlement_price
    signal.settled_at = now_ts
    signal.settlement_note = note
    signal.settlement_quality = quality
    signal.settlement_window_samples = int(max(0, window_samples))

    return SettlementOutcomeRecord(
        signal_id=signal.signal_id,
        strategy=signal.strategy.value,
        direction=signal.direction,
        reference_price=signal.reference_price,
        settlement_price=settlement_price,
        outcome=outcome,
        confidence=signal.confidence,
        horizon_min=signal.horizon_min,
        settlement_quality=quality,
        settlement_window_samples=int(max(0, window_samples)),
        is_shadow=False,
    )


def shadow_settle_one(
    signal: ScalpSignal,
    *,
    settlement_price: float,
    now_ts: int,
    quality: SettlementQuality = "ok",
    window_samples: int = 0,
) -> SettlementOutcomeRecord:
    """对单条 cancelled 信号执行 shadow 结算（P0-4）

    不改变 signal.state（保持 "cancelled"），但写入 shadow_* 字段供统计使用
    返回 SettlementOutcomeRecord（is_shadow=True）

    Raises:
        ValueError: 状态非 cancelled / 已 shadow 结算 / reference_price 无效
    """
    if signal.state != "cancelled":
        raise ValueError(f"signal {signal.signal_id} not cancelled: {signal.state}")
    if signal.shadow_outcome is not None:
        raise ValueError(f"signal {signal.signal_id} already shadow-settled")
    if signal.reference_price <= 0:
        raise ValueError(f"signal {signal.signal_id} invalid reference_price")

    outcome = calc_outcome(
        direction=signal.direction,
        reference_price=signal.reference_price,
        settlement_price=settlement_price,
    )

    signal.shadow_settlement_price = settlement_price
    signal.shadow_outcome = outcome
    signal.shadow_settled_at = now_ts
    # 复用 settlement_quality 字段记录 shadow 结算精度
    signal.settlement_quality = quality
    signal.settlement_window_samples = int(max(0, window_samples))
    signal.state_history.append(StateTransition(
        ts=now_ts,
        from_state="cancelled",
        to_state="cancelled",
        reason=f"shadow settled, outcome={outcome} (cancellation_kind={signal.invalidation_kind})",
        price_at_ts=settlement_price,
    ))

    return SettlementOutcomeRecord(
        signal_id=signal.signal_id,
        strategy=signal.strategy.value,
        direction=signal.direction,
        reference_price=signal.reference_price,
        settlement_price=settlement_price,
        outcome=outcome,
        confidence=signal.confidence,
        horizon_min=signal.horizon_min,
        settlement_quality=quality,
        settlement_window_samples=int(max(0, window_samples)),
        is_shadow=True,
    )


def cancel_one(
    signal: ScalpSignal,
    *,
    reason: str,
    now_ts: int,
    invalidation_kind: InvalidationKind,
    price_at_cancel: Optional[float] = None,
) -> None:
    """主动取消信号（regime 反转 / 黑天鹅 / 用户手动 / 数据 stale / 冲突）

    P0-4：in-place 修改 state="cancelled"，**保留在活跃池**等到 expiry+GRACE 做 shadow 结算
    """
    if signal.state != "active":
        return

    signal.state_history.append(StateTransition(
        ts=now_ts,
        from_state="active",
        to_state="cancelled",
        reason=reason,
        price_at_ts=price_at_cancel,
    ))
    signal.state = "cancelled"
    signal.invalidation_kind = invalidation_kind
    signal.settlement_note = reason
    # 注意：cancelled 信号 settled_at 仍未设置 / state 已记
    # settled_at 在 shadow_settle 时才写


# ─────────────────────────────────────────────────────────────────────────────
# 批量结算 orchestrators
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SettlementBatch:
    """单次 tick 的结算批结果（settle + shadow_settle 合并报告）"""
    settled: list[SettlementOutcomeRecord] = field(default_factory=list)
    shadow_settled: list[SettlementOutcomeRecord] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)        # signal_id 列表（价格未取到）


def settle_due_signals(
    store: ScalpSignalStore,
    *,
    price_lookup: PriceLookupFn,
    now_ts: Optional[int] = None,
    window_sec: int = SETTLEMENT_WINDOW_SEC,
) -> SettlementBatch:
    """扫活跃池：
      - active 且 expiry_ts ≤ now → 用 ±window_sec 中位数结算 → archive
      - cancelled 且 expiry_ts + GRACE ≤ now → shadow_settle → archive
      - active 但 expiry 才过几秒、价格样本不足 → 留待下一 tick

    Args:
        store: ScalpSignalStore
        price_lookup: 注入的价格查询函数（target_ts, window_sec → PriceLookupResult）
        now_ts: 评估时间，None 时取 time.time()
        window_sec: 结算窗口半宽（默认 ±10s）

    Returns:
        SettlementBatch
    """
    now = now_ts if now_ts is not None else int(time.time())
    batch = SettlementBatch()
    active = store.get_active()

    for sig in active:
        if sig.state == "active":
            if sig.expiry_ts > now:
                continue
            _try_active_settle(
                sig, store=store, price_lookup=price_lookup, now=now,
                window_sec=window_sec, batch=batch,
            )
        elif sig.state == "cancelled":
            # P0-4 shadow settlement：等到 expiry_ts + GRACE 才做
            if sig.expiry_ts + SHADOW_SETTLE_GRACE_SEC > now:
                continue
            if sig.shadow_outcome is not None:
                # 已 shadow 结算但仍在活跃池（容错：archive 失败重试）
                _safe_archive(sig, store=store)
                continue
            _try_shadow_settle(
                sig, store=store, price_lookup=price_lookup, now=now,
                window_sec=window_sec, batch=batch,
            )
        # 其他 state（已 expired_*）不应出现在活跃池，安全忽略

    return batch


def _try_active_settle(
    sig: ScalpSignal,
    *,
    store: ScalpSignalStore,
    price_lookup: PriceLookupFn,
    now: int,
    window_sec: int,
    batch: SettlementBatch,
) -> None:
    target_ts = int(sig.expiry_ts)
    pl = price_lookup(target_ts, window_sec)
    if pl.price is None or pl.price <= 0:
        # 等下一 tick；超过 GRACE 仍无价 → fallback 用现价（避免无限堆积）
        if now - sig.expiry_ts <= SHADOW_SETTLE_GRACE_SEC:
            batch.pending.append(sig.signal_id)
            return
        logger.warning(
            "scalp settlement price unavailable beyond grace | id=%s expiry_age=%ds",
            sig.signal_id, now - sig.expiry_ts,
        )
        batch.pending.append(sig.signal_id)
        return

    try:
        rec = settle_one(
            sig, settlement_price=float(pl.price), now_ts=now,
            note=f"auto settlement at expiry (quality={pl.quality}, n={pl.sample_size})",
            quality=pl.quality, window_samples=pl.sample_size,
        )
        store.update_active(sig)
        store.archive_signal(sig)
        store.remove_active(sig.signal_id)
        batch.settled.append(rec)
        logger.info(
            "scalp signal settled | id=%s outcome=%s ref=%.4f settle=%.4f q=%s n=%d",
            sig.signal_id, rec.outcome, sig.reference_price, pl.price,
            pl.quality, pl.sample_size,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("scalp settle_one failed | id=%s err=%s", sig.signal_id, e, exc_info=True)


def _try_shadow_settle(
    sig: ScalpSignal,
    *,
    store: ScalpSignalStore,
    price_lookup: PriceLookupFn,
    now: int,
    window_sec: int,
    batch: SettlementBatch,
) -> None:
    target_ts = int(sig.expiry_ts)
    pl = price_lookup(target_ts, window_sec)
    if pl.price is None or pl.price <= 0:
        if now - sig.expiry_ts <= SHADOW_SETTLE_GRACE_SEC * 3:
            batch.pending.append(sig.signal_id)
            return
        # 长时间无价 → 直接 archive，不写 shadow_outcome（保留为 cancelled）
        logger.warning(
            "scalp shadow settle abandoned (no price) | id=%s", sig.signal_id,
        )
        _safe_archive(sig, store=store)
        return

    try:
        rec = shadow_settle_one(
            sig, settlement_price=float(pl.price), now_ts=now,
            quality=pl.quality, window_samples=pl.sample_size,
        )
        store.update_active(sig)
        store.archive_signal(sig)
        store.remove_active(sig.signal_id)
        batch.shadow_settled.append(rec)
        logger.info(
            "scalp shadow settled | id=%s shadow_outcome=%s kind=%s ref=%.4f settle=%.4f n=%d",
            sig.signal_id, rec.outcome, sig.invalidation_kind,
            sig.reference_price, pl.price, pl.sample_size,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("scalp shadow_settle failed | id=%s err=%s", sig.signal_id, e, exc_info=True)


def _safe_archive(sig: ScalpSignal, *, store: ScalpSignalStore) -> None:
    try:
        store.update_active(sig)
        store.archive_signal(sig)
        store.remove_active(sig.signal_id)
    except Exception as e:  # noqa: BLE001
        logger.error("scalp safe_archive failed | id=%s err=%s", sig.signal_id, e)


def cancel_active_by_predicate(
    store: ScalpSignalStore,
    predicate,
    *,
    invalidation_kind: InvalidationKind,
    now_ts: Optional[int] = None,
    price_at_cancel: Optional[float] = None,
) -> list[ScalpSignal]:
    """批量取消活跃信号（如 regime 反转、黑天鹅来袭、冲突解决）

    P0-4：取消后 in-place 标记 state="cancelled"，**保留**在活跃池等 shadow settle

    Args:
        predicate: 函数 sig → 取消原因（None / "" 表示不取消）
        invalidation_kind: 取消原因分类（用于 calibration_shadow 双口径统计）

    Returns:
        被取消的信号列表
    """
    now = now_ts if now_ts is not None else int(time.time())
    cancelled: list[ScalpSignal] = []
    for sig in store.get_active():
        if sig.state != "active":
            continue
        try:
            reason = predicate(sig)
        except Exception as e:  # noqa: BLE001
            logger.warning("cancel predicate error | id=%s err=%s", sig.signal_id, e)
            continue
        if not reason:
            continue
        try:
            cancel_one(
                sig, reason=reason, now_ts=now,
                invalidation_kind=invalidation_kind,
                price_at_cancel=price_at_cancel,
            )
            store.update_active(sig)  # 留在活跃池
            cancelled.append(sig)
        except Exception as e:  # noqa: BLE001
            logger.error("scalp cancel failed | id=%s err=%s", sig.signal_id, e)
    return cancelled


# ─────────────────────────────────────────────────────────────────────────────
# 中位价计算（供 SignalEngine 内的 buffer 使用）
# ─────────────────────────────────────────────────────────────────────────────

def median_price_in_window(
    samples: list[tuple[int, float]],
    *,
    target_ts: int,
    window_sec: int = SETTLEMENT_WINDOW_SEC,
    min_samples: int = SETTLEMENT_WINDOW_MIN_SAMPLES,
) -> PriceLookupResult:
    """对 (ts, price) 样本列表筛选 |ts - target| ≤ window，返回中位数 + quality

    P0-1 核心算法：
      - 样本数 ≥ min_samples → quality="ok"，price=median
      - 样本数 == 1 → quality="low_samples"，price=唯一值
      - 样本数 == 0 → 退化到 "fallback"（用 target 之后第一个样本）；都无则 "no_data"
    """
    in_window = [
        (ts, p) for ts, p in samples
        if p > 0 and abs(ts - target_ts) <= window_sec
    ]
    n = len(in_window)
    if n >= min_samples:
        prices = sorted(p for _, p in in_window)
        return PriceLookupResult(
            price=statistics.median(prices),
            sample_size=n,
            quality="ok",
        )
    if n == 1:
        return PriceLookupResult(
            price=float(in_window[0][1]),
            sample_size=1,
            quality="low_samples",
        )
    # n == 0 → fallback：找 target_ts 之后第一个样本（防"刚启动 buffer 还没装满"）
    after = sorted([(ts, p) for ts, p in samples if p > 0 and ts >= target_ts], key=lambda x: x[0])
    if after:
        return PriceLookupResult(
            price=float(after[0][1]),
            sample_size=0,
            quality="fallback",
        )
    return PriceLookupResult(price=None, sample_size=0, quality="no_data")
