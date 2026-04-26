"""挂单压力监测器 · 独立 snipe 信号生成器

输入：``OrderbookPressureSnapshot``（来自 ``processors/orderbook_pressure.py``）
输出：``list[OrderbookPressureSignal]``（已通过 30min 同价去重）

触发规则（保守，宁缺毋滥）：
  short 触发 = wall.label == "real_R" + 价格距离 ≤ 0.6×ATR + wall.confidence ≥ 60
  long  触发 = wall.label == "real_S" + 价格距离 ≤ 0.6×ATR + wall.confidence ≥ 60

入场/止损/止盈（与项目现有 snipe 风格一致）：
  - entry = wall.price_mid（极限挂单，靠近触发反应价位即入场）
  - stop  = wall 反向 0.5×ATR
  - tp    = 1.5×ATR 或下一个反向 wall.price_mid（若有更近的）

去重：以 ``"{coin}:{side}:{quantized_price}"`` 为 key，30 min 内同价位仅触发 1 次，
价位量化粒度 = max(0.25×ATR, 0.05% × last)。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from models.orderbook_pressure import (
    OrderbookPressureSignal,
    OrderbookPressureSnapshot,
    PressureWall,
    WallLabel,
)

logger = logging.getLogger(__name__)


DEFAULTS = {
    "trigger_distance_atr_mult": 0.6,    # 价格 ≤ 0.6×ATR 才触发
    "min_confidence": 60,                # wall.confidence 门槛
    "stop_atr_mult": 0.5,                # 反向 0.5×ATR 止损
    "tp_atr_mult": 1.5,                  # 1.5×ATR 默认 tp
    "signal_dedup_window_sec": 1800,     # 30 min 同价去重（与 yaml key 对齐）
    "dedup_quant_atr_mult": 0.25,        # 价位量化 0.25×ATR
    "dedup_min_quant_pct": 0.0005,       # 价位量化最低 0.05% × last
}


# ── 模块级 dedup（per-process；进程重启重置）──
_DEDUP_MAP: dict[str, float] = {}


def _quantize_price(price: float, last_price: float, atr: Optional[float], cfg: dict) -> int:
    """按 max(0.25×ATR, 0.05%×last) 把价格量化到整数 bucket。"""
    quant = 0.0
    if atr and atr > 0:
        quant = atr * float(cfg.get("dedup_quant_atr_mult", 0.25))
    min_q = last_price * float(cfg.get("dedup_min_quant_pct", 0.0005))
    quant = max(quant, min_q, 1e-8)
    return int(price / quant)


def _dedup_key(coin: str, side: str, price_bucket: int) -> str:
    return f"{coin}:{side}:{price_bucket}"


def _cleanup_dedup(now_sec: float, window_sec: int) -> None:
    """清理过期的 dedup key（防止内存无限膨胀）。"""
    if not _DEDUP_MAP:
        return
    cutoff = now_sec - window_sec * 2  # 保留两窗口的安全余量
    expired = [k for k, ts in _DEDUP_MAP.items() if ts < cutoff]
    for k in expired:
        del _DEDUP_MAP[k]


# ── 单 wall → signal 转换 ─────────────────────────────────────────────────
def _wall_to_signal(
    coin: str,
    wall: PressureWall,
    snap: OrderbookPressureSnapshot,
    atr: Optional[float],
    cfg: dict,
    now_sec: int,
    next_opposite_wall: Optional[PressureWall],
) -> Optional[OrderbookPressureSignal]:
    """把单个 wall 转换为 snipe 信号；不满足触发条件返回 None。"""
    label: WallLabel = wall.label
    if label not in ("real_R", "real_S"):
        return None
    if (wall.confidence or 0) < int(cfg.get("min_confidence", 60)):
        return None

    last_price = snap.last_price
    if last_price <= 0:
        return None

    # 距离判定（要求价格 "已接近" 该 wall）
    abs_distance = abs(wall.price_mid - last_price)
    if atr and atr > 0:
        max_dist = atr * float(cfg.get("trigger_distance_atr_mult", 0.6))
    else:
        # 无 ATR 时退化为 0.3% × last（更严格，防止误触）
        max_dist = last_price * 0.003
    if abs_distance > max_dist:
        return None

    # 入场 / 止损 / 止盈
    entry = wall.price_mid
    stop_mult = float(cfg.get("stop_atr_mult", 0.5))
    tp_mult = float(cfg.get("tp_atr_mult", 1.5))
    stop_offset = (atr * stop_mult) if (atr and atr > 0) else (last_price * 0.005)
    tp_offset = (atr * tp_mult) if (atr and atr > 0) else (last_price * 0.015)

    if label == "real_R":
        side = "short"
        stop_loss = entry + stop_offset
        tp_default = entry - tp_offset
        # 若下方有反向 real_S wall 更近，用它作为更保守的 tp
        if next_opposite_wall and next_opposite_wall.price_mid < entry:
            tp = max(tp_default, next_opposite_wall.price_mid)
        else:
            tp = tp_default
    else:  # real_S
        side = "long"
        stop_loss = entry - stop_offset
        tp_default = entry + tp_offset
        if next_opposite_wall and next_opposite_wall.price_mid > entry:
            tp = min(tp_default, next_opposite_wall.price_mid)
        else:
            tp = tp_default

    # 触发置信度：基础 wall.confidence + L4 共振 +5
    confidence = int(wall.confidence or 0)
    if wall.confluence_with_absorption:
        confidence = min(100, confidence + 5)

    factors: list[str] = []
    factors.append(f"wall_label={label}")
    factors.append(f"confidence={wall.confidence}")
    factors.append(f"change_kind={wall.change_kind}")
    if wall.has_active_whale:
        factors.append("has_active_whale")
    if wall.confluence_with_absorption:
        factors.append("absorption_confluence")
    if wall.cvd_state:
        factors.append(f"cvd={wall.cvd_state}")

    reason_parts = [
        f"{'上方真阻力' if label == 'real_R' else '下方真支撑'}@{entry:.4f}",
        f"距离 {abs_distance / max(last_price, 1e-9) * 100:+.2f}%",
        wall.reason or "",
    ]
    reason = " | ".join(p for p in reason_parts if p)

    pq = _quantize_price(entry, last_price, atr, cfg)
    dedup_key = _dedup_key(coin, side, pq)

    return OrderbookPressureSignal(
        coin=coin,
        ts_sec=now_sec,
        side=side,
        wall_label=label,
        wall_price=entry,
        distance_pct=(entry - last_price) / max(last_price, 1e-9) * 100.0,
        last_price=last_price,
        confidence=confidence,
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit=tp,
        reason=reason,
        factors=factors,
        dedup_key=dedup_key,
    )


# ── 顶层入口 ────────────────────────────────────────────────────────────
def generate_pressure_signals(
    snapshot: Optional[OrderbookPressureSnapshot],
    cfg_overrides: Optional[dict] = None,
    now_sec: Optional[int] = None,
) -> list[OrderbookPressureSignal]:
    """从 snapshot 中识别 real_R/real_S 触发，返回去重后新发的信号列表。

    注意：本函数有副作用 —— 写入模块级 ``_DEDUP_MAP``。每条返回的信号
    都已通过 30min 同价去重，调用方可直接 push / 持久化。
    """
    if snapshot is None or not snapshot.walls:
        return []

    cfg = dict(DEFAULTS)
    if cfg_overrides:
        cfg.update({k: v for k, v in cfg_overrides.items() if v is not None})

    now = int(now_sec if now_sec is not None else time.time())
    # key 兼容：yaml 用 signal_dedup_window_sec；旧调用可能用 dedup_window_sec
    window = int(
        cfg.get("signal_dedup_window_sec",
                cfg.get("dedup_window_sec", 1800))
    )
    _cleanup_dedup(now, window)

    coin = snapshot.coin
    atr = snapshot.atr

    # 预先按 side 分组并按距离排序，便于挑"下一个反向 wall"
    bid_walls = sorted(
        [w for w in snapshot.walls if w.side == "bid"],
        key=lambda w: snapshot.last_price - w.price_mid,  # 由近到远
    )
    ask_walls = sorted(
        [w for w in snapshot.walls if w.side == "ask"],
        key=lambda w: w.price_mid - snapshot.last_price,
    )

    new_signals: list[OrderbookPressureSignal] = []

    def _try_emit(wall: PressureWall, opposite_walls: list[PressureWall]) -> None:
        # 反向最近的 real wall 作为 tp 候选（任何 real_R/real_S 即可，untested 不算）
        next_opp = None
        for ow in opposite_walls:
            if ow.label in ("real_R", "real_S"):
                next_opp = ow
                break
        sig = _wall_to_signal(coin, wall, snapshot, atr, cfg, now, next_opp)
        if sig is None:
            return
        last_ts = _DEDUP_MAP.get(sig.dedup_key)
        if last_ts is not None and (now - last_ts) < window:
            return  # 30min 内同价已发过
        _DEDUP_MAP[sig.dedup_key] = float(now)
        new_signals.append(sig)
        logger.info(
            "[OP-Signal] %s side=%s label=%s entry=%.4f conf=%d reason=%s",
            coin, sig.side, sig.wall_label, sig.entry_price, sig.confidence, sig.reason,
        )

    for w in ask_walls:                 # 上方阻力 → 触发 short
        _try_emit(w, bid_walls)
    for w in bid_walls:                 # 下方支撑 → 触发 long
        _try_emit(w, ask_walls)

    return new_signals


def reset_dedup_for_test() -> None:
    """单测用：清空 dedup map。"""
    _DEDUP_MAP.clear()
