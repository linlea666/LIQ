"""信号总线 · L3 SignalBus

职责：
  - 所有 producer 的 CandidateSignal 统一入总线
  - 按 coin / source / ts 范围查询
  - 自动清理过期信号（expires_at 或 max_age）

复用决策：
  - 独立新写（简单内存队列）；不复用 engine.caches 的雪花结构
  - 后续 P2 可接入持久化（sqlite），当前阶段 in-memory 够用

落实日志锚点：
  - D.D02_DUAL_ENGINE：ingest 时累计 signals_in；每小时 prune_expired 上报

线程安全：
  - 读写加 RLock（单例跨线程共享）
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

from models.candidate_signal import CandidateSignal

logger = logging.getLogger(__name__)


class SignalBus:
    """内存型信号总线 · 按 coin 分桶"""

    def __init__(self, max_age_sec: int = 3600, max_per_coin: int = 500) -> None:
        self._max_age_sec = max(60, int(max_age_sec))
        # 容量下限 1（不是 10）：测试场景允许小容量，生产默认 500 不受影响
        self._max_per_coin = max(1, int(max_per_coin))
        self._buckets: dict[str, deque[CandidateSignal]] = {}
        self._seen_keys: dict[str, set[tuple]] = {}
        self._lock = threading.RLock()
        self._signals_in_total: int = 0
        self._dup_dropped_total: int = 0
        self._last_prune_ts: int = 0

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 写入
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def ingest(self, signal: CandidateSignal) -> bool:
        """追加一条候选信号。

        约束：
          - coin 从 signal.provenance.get('coin') 提取（上游必填）
          - 同 coin+source+source_id+ts 完全一致时跳过（幂等）
          - 超出 max_per_coin 时丢弃最旧
        返回：
          True  — 新增
          False — 被幂等 / 非法丢弃
        """
        coin = signal.provenance.get("coin") if signal.provenance else None
        if not coin:
            logger.debug("[SignalBus] ingest dropped (no coin): source=%s", signal.source)
            return False
        coin = str(coin).upper()

        key = (signal.source, signal.source_id, int(signal.ts))
        with self._lock:
            bucket = self._buckets.setdefault(coin, deque())
            seen = self._seen_keys.setdefault(coin, set())

            if key in seen:
                self._dup_dropped_total += 1
                return False
            seen.add(key)
            bucket.append(signal)
            self._signals_in_total += 1

            # 裁剪超长
            while len(bucket) > self._max_per_coin:
                old = bucket.popleft()
                old_key = (old.source, old.source_id, int(old.ts))
                seen.discard(old_key)
        return True

    def ingest_many(self, signals: list[CandidateSignal]) -> int:
        """批量写入，返回实际新增条数"""
        added = 0
        for s in signals:
            if self.ingest(s):
                added += 1
        return added

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 查询
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def query(
        self,
        coin: str,
        *,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,
        sources: Optional[list[str]] = None,
        actions: Optional[list[str]] = None,
        include_expired: bool = False,
    ) -> list[CandidateSignal]:
        """按条件查询；返回按 ts 降序的副本列表"""
        coin = coin.upper()
        now = int(time.time())
        results: list[CandidateSignal] = []
        with self._lock:
            bucket = self._buckets.get(coin)
            if not bucket:
                return results
            for sig in bucket:
                if min_ts is not None and sig.ts < min_ts:
                    continue
                if max_ts is not None and sig.ts > max_ts:
                    continue
                if not include_expired and sig.expires_at and sig.expires_at < now:
                    continue
                if sources:
                    if not any(
                        sig.source == s or sig.source.startswith(s) for s in sources
                    ):
                        continue
                if actions and sig.action not in actions:
                    continue
                results.append(sig.model_copy(deep=True))
        results.sort(key=lambda s: s.ts, reverse=True)
        return results

    def latest(self, coin: str, source: str) -> Optional[CandidateSignal]:
        """返回该 coin 下指定 source 的最新一条"""
        items = self.query(coin, sources=[source])
        return items[0] if items else None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 清理
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def prune_expired(self, now_ts: Optional[int] = None) -> int:
        """清理过期 + 超龄信号，返回清理数量"""
        now = int(now_ts if now_ts is not None else time.time())
        removed = 0
        with self._lock:
            for coin, bucket in list(self._buckets.items()):
                kept: deque[CandidateSignal] = deque()
                seen_new: set[tuple] = set()
                for sig in bucket:
                    age = now - int(sig.ts or 0)
                    if age > self._max_age_sec:
                        removed += 1
                        continue
                    if sig.expires_at and sig.expires_at < now:
                        removed += 1
                        continue
                    kept.append(sig)
                    seen_new.add((sig.source, sig.source_id, int(sig.ts)))
                self._buckets[coin] = kept
                self._seen_keys[coin] = seen_new
            self._last_prune_ts = now
        return removed

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 统计
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    def stats(self) -> dict:
        """返回 {coins_count, total_signals, by_source, by_action, dup_dropped_total}"""
        with self._lock:
            total = sum(len(b) for b in self._buckets.values())
            by_source: dict[str, int] = {}
            by_action: dict[str, int] = {}
            for bucket in self._buckets.values():
                for sig in bucket:
                    by_source[sig.source] = by_source.get(sig.source, 0) + 1
                    by_action[sig.action] = by_action.get(sig.action, 0) + 1
            return {
                "coins_count": len(self._buckets),
                "total_signals": total,
                "by_source": by_source,
                "by_action": by_action,
                "signals_in_total": self._signals_in_total,
                "dup_dropped_total": self._dup_dropped_total,
                "last_prune_ts": self._last_prune_ts,
            }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 全局单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_bus: Optional[SignalBus] = None
_bus_lock = threading.Lock()


def get_bus() -> SignalBus:
    """获取全局 SignalBus 单例"""
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = SignalBus()
    return _bus


def reset_bus_for_tests() -> None:
    """测试用：重置单例（不用于生产）"""
    global _bus
    with _bus_lock:
        _bus = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 适配器：把现有 KeyLevelSignal / RangeSignal / news events 转成 CandidateSignal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# KeyLevelSignal.action 到 CandidateSignal (action/direction) 的映射
_KL_ACTION_MAP: dict[str, tuple[str, str]] = {
    "snipe_long": ("long", "bullish"),
    "snipe_short": ("short", "bearish"),
    "flip_long": ("long", "bullish"),
    "flip_short": ("short", "bearish"),
    "scalp_long": ("long", "bullish"),
    "scalp_short": ("short", "bearish"),
    "wait_sweep": ("wait", "neutral"),
    "wait_approach": ("wait", "neutral"),
}


def adapt_key_level_signal(
    coin: str,
    kl_sig,  # type: KeyLevelSignal
    kl_level=None,  # type: KeyLevelV2 | None
    *,
    ts: Optional[int] = None,
) -> CandidateSignal:
    """KeyLevelSignal → CandidateSignal（复用策略：扩展/投影，不改原模型）

    source 取值：
      "tracker_v2.swept" / "tracker_v2.bounced" / "tracker_v2.flip_broken" / ...
    source_id 取值：
      "{coin}:{level_price}:{state}"
    provenance 必带 coin，其它字段视 kl_level 是否提供
    """
    now = int(ts if ts is not None else time.time())
    state = (getattr(kl_sig, "state", "") or "").strip() or "unknown"
    source = f"tracker_v2.{state}"
    source_id = f"{coin.upper()}:{kl_sig.level_price}:{state}"

    action, direction = _KL_ACTION_MAP.get(
        getattr(kl_sig, "action", "") or "", ("wait", "neutral")
    )

    # 分数：A/B/C → 70/55/40 兜底（Synthesizer 会再做 tier_bonus）
    raw_score = {"S": 85, "A": 72, "B": 55, "C": 40}.get(
        getattr(kl_sig, "confidence", "C"), 40
    )

    rr = getattr(kl_sig, "rr_ratio", None)
    warnings_list = list(getattr(kl_sig, "warnings", []) or [])

    provenance: dict = {
        "coin": coin.upper(),
        "level_price": kl_sig.level_price,
        "state": state,
        "side": getattr(kl_sig, "side", ""),
    }
    if kl_level is not None:
        provenance.update({
            "cascade_risk": float(getattr(kl_level, "cascade_risk", 0.0) or 0.0),
            "confluence_score": float(getattr(kl_level, "confluence_score", 0.0) or 0.0),
            "strength_tier": getattr(kl_level, "strength_tier", "C"),
            "final_score": float(getattr(kl_level, "final_score", 0.0) or 0.0),
            "pattern_detected": getattr(kl_level, "pattern_detected", ""),
            "pattern_strength": float(getattr(kl_level, "pattern_strength", 0.0) or 0.0),
            "timeframe": getattr(kl_level, "timeframe", ""),
        })

    # 过期时间：CandidateSignal 默认 60min 后过期（与 SignalBus 滚动窗对齐）
    expires_at = now + 3600

    return CandidateSignal(
        source=source,
        source_id=source_id,
        ts=now,
        action=action,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        anchor_price=float(kl_sig.level_price),
        entry_price=kl_sig.entry_price,
        stop_loss=kl_sig.stop_loss,
        tp1=kl_sig.tp1,
        tp2=kl_sig.tp2,
        rr_ratio=rr,
        confidence=getattr(kl_sig, "confidence", "C"),  # type: ignore[arg-type]
        score=float(raw_score),
        reason=getattr(kl_sig, "reason", "") or "",
        warnings=warnings_list,
        expires_at=expires_at,
        provenance=provenance,
    )


def adapt_range_signal(coin: str, range_data) -> Optional[CandidateSignal]:
    """RangeSignalData → CandidateSignal（最小版：P1 再细化）

    P0 阶段仅做占位：若 range_data 不为空则生成一个观望信号，真正的
    突破/回归信号打分留给 P1 专门处理。
    """
    if range_data is None:
        return None
    try:
        now = int(time.time())
        return CandidateSignal(
            source="range_signal.observe",
            source_id=f"{coin.upper()}:range",
            ts=now,
            action="wait",
            direction="neutral",
            anchor_price=float(getattr(range_data, "current_price", 0) or 0),
            confidence="C",
            score=35.0,
            reason="箱体信号观察中（P0 占位，P1 将升级）",
            expires_at=now + 3600,
            provenance={"coin": coin.upper(), "placeholder": True},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("adapt_range_signal failed: %s", e)
        return None


def adapt_news_event(
    event,  # event: MarketEventSignal
    *,
    target_coins: Optional[list[str]] = None,
) -> list[CandidateSignal]:
    """MarketEventSignal → CandidateSignal 列表（每个 coin 一条）

    保守策略（避免新闻被 L4 误当成独立交易信号）：
      - action 恒为 "wait"（新闻只作为 corroboration / safety hint，不独立触发交易）
      - direction 来自 AI 判定；impact_score=0 的 normal 信号丢弃
      - confidence 按 tier 映射：blackswan→A, major→B, 其余→C
      - score 来自 impact_score 与 confidence 的综合（0-100 归一）
      - source 前缀 "news_event." → 已被 synthesizer 识别为 corroboration 源
      - 目标币种：有 impact_on_assets 时按 asset 展开；无则默认 BTC

    返回：list[CandidateSignal]（可能 0..N 条；每 coin 一条）
    """
    try:
        if event is None:
            return []
        # event 应为 MarketEventSignal pydantic；duck-typing 取字段
        direction = str(getattr(event, "direction", "neutral") or "neutral")
        if direction not in {"bullish", "bearish", "neutral", "potential_reversal"}:
            direction = "neutral"
        tier = str(getattr(event, "tier", "normal") or "normal").lower()
        impact = int(getattr(event, "impact_score", 0) or 0)
        event_id = str(getattr(event, "event_id", "") or "")
        ts = int(getattr(event, "ts", 0) or 0) or int(time.time())
        expires_at = int(getattr(event, "expires_at", 0) or 0) or (ts + 24 * 3600)
        flip_flop = bool(getattr(event, "flip_flop_warning", False))
        summary = str(getattr(event, "summary_cn", "") or getattr(event, "first_order_impact", "") or "")
        risk_type = str(getattr(event, "risk_type", "none") or "none")
        narrative_theme = str(getattr(event, "narrative_theme", "") or "")

        # 噪声 / neutral 且非黑天鹅 → 丢弃
        if tier == "minor" and abs(impact) < 2:
            return []
        if direction == "neutral" and tier != "blackswan":
            return []

        tier_to_conf: dict[str, str] = {"blackswan": "A", "major": "B", "normal": "C", "minor": "C"}
        confidence = tier_to_conf.get(tier, "C")

        # score 归一（0-100）
        # impact 1→20, 3→60, 5→100；黑天鹅基底抬高
        base = min(100.0, max(0.0, abs(impact) * 20.0))
        if tier == "blackswan":
            base = max(base, 70.0)
        # flip-flop 打 0.7 折扣（除非黑天鹅）
        if flip_flop and tier != "blackswan":
            base = round(base * 0.7, 1)
        # 置信度系数
        base = base * float(getattr(event, "confidence", 0.5) or 0.5)

        # 目标币种
        coins = [c.upper() for c in (target_coins or [])]
        if not coins:
            impact_on = getattr(event, "impact_on_assets", []) or []
            hit_btc = any(str(getattr(a, "asset", "")).upper() in {"BTC", "ETH"} for a in impact_on)
            coins = ["BTC"] if hit_btc or tier == "blackswan" else ["BTC"]
            # 只影响 ETH 的情况（未来扩展）
            eth_only = impact_on and all(str(getattr(a, "asset", "")).upper() == "ETH" for a in impact_on)
            if eth_only:
                coins = ["ETH"]

        warnings_list: list[str] = []
        if flip_flop:
            warnings_list.append("同主题 24h 内反复拉扯 · 信号权重已降档")
        if tier == "blackswan":
            warnings_list.append("黑天鹅事件 · 建议立即降仓")

        reason = f"新闻[{tier}]{risk_type}: {summary[:40]}" if summary else f"新闻[{tier}]"

        out: list[CandidateSignal] = []
        for coin in coins:
            provenance = {
                "coin": coin,
                "news_event": True,
                "event_id": event_id,
                "tier": tier,
                "risk_type": risk_type,
                "narrative_theme": narrative_theme,
                "impact_score": impact,
                "flip_flop": flip_flop,
            }
            out.append(CandidateSignal(
                source="news_event.ai",
                source_id=event_id,
                ts=ts,
                action="wait",
                direction=direction,  # type: ignore[arg-type]
                anchor_price=0.0,
                confidence=confidence,  # type: ignore[arg-type]
                score=round(base, 2),
                reason=reason[:120],
                warnings=warnings_list,
                expires_at=expires_at,
                provenance=provenance,
            ))
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("adapt_news_event failed: %s", e)
        return []


def adapt_geo_risk_event(
    event,  # event: GeoRiskEvent
    *,
    target_coins: Optional[list[str]] = None,
) -> list[CandidateSignal]:
    """GeoRiskEvent → CandidateSignal 列表（每 coin 一条）

    保守策略：
      - action 恒为 "wait"
      - direction 取 estimated_direction，若不明则 bearish（地缘升级默认偏空）
      - confidence 按 severity：escalation/blackswan→A, stable/小变动→B
      - 地缘降档事件（de-escalation）方向偏 bullish
    """
    try:
        if event is None:
            return []
        theme_id = str(getattr(event, "theme_id", "") or "")
        event_id = str(getattr(event, "event_id", "") or theme_id)
        ts = int(getattr(event, "ts", 0) or 0) or int(time.time())
        level_before = int(getattr(event, "level_before", 0) or 0)
        level_after = int(getattr(event, "level_after", 0) or 0)
        severity = str(getattr(event, "severity", "stable") or "stable").lower()
        direction = str(getattr(event, "estimated_direction", "neutral") or "neutral").lower()
        if direction not in {"bullish", "bearish", "neutral", "potential_reversal"}:
            direction = "neutral"
        is_blackswan = bool(getattr(event, "is_blackswan", False))
        flip_flop = bool(getattr(event, "flip_flop_warning", False))
        confidence_val = float(getattr(event, "confidence", 0.5) or 0.5)
        reaction = float(getattr(event, "estimated_crypto_reaction_pct", 0.0) or 0.0)
        summary = str(getattr(event, "summary_cn", "") or "")

        # 等级无变化 且 非黑天鹅 → 不产出
        if level_after == level_before and not is_blackswan:
            return []

        if direction == "neutral":
            # 升级 → 偏空；降级 → 偏多
            if level_after > level_before:
                direction = "bearish"
            elif level_after < level_before:
                direction = "bullish"

        tier_conf: str
        if is_blackswan or severity == "escalation" or level_after >= 4:
            tier_conf = "A"
        elif severity in {"rising", "de-escalation"} or abs(level_after - level_before) >= 1:
            tier_conf = "B"
        else:
            tier_conf = "C"

        # score：等级跨度 + 预估幅度
        base = min(100.0, abs(level_after - level_before) * 25.0 + abs(reaction) * 8.0)
        if is_blackswan:
            base = max(base, 85.0)
        if flip_flop:
            base = round(base * 0.7, 1)
        base = base * confidence_val

        coins = [c.upper() for c in (target_coins or [])] or ["BTC"]

        warnings_list: list[str] = []
        if flip_flop:
            warnings_list.append(f"{theme_id} 同主题 24h 内反复 · 权重降档")
        if is_blackswan:
            warnings_list.append("地缘黑天鹅 · 建议暂停开仓")
        if level_after >= 4:
            warnings_list.append(f"地缘风险等级 {level_after}/5 · SafetyGate 将自动触发")

        reason = (
            f"地缘[{theme_id}] {level_before}→{level_after}({severity}) "
            f"{summary[:30]}"
        ).strip()

        out: list[CandidateSignal] = []
        for coin in coins:
            provenance = {
                "coin": coin,
                "geo_risk": True,
                "event_id": event_id,
                "theme_id": theme_id,
                "level_before": level_before,
                "level_after": level_after,
                "severity": severity,
                "is_blackswan": is_blackswan,
                "flip_flop": flip_flop,
            }
            out.append(CandidateSignal(
                source="geo_risk.tracker",
                source_id=event_id,
                ts=ts,
                action="wait",
                direction=direction,  # type: ignore[arg-type]
                anchor_price=0.0,
                confidence=tier_conf,  # type: ignore[arg-type]
                score=round(base, 2),
                reason=reason[:120],
                warnings=warnings_list,
                expires_at=ts + 24 * 3600,
                provenance=provenance,
            ))
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("adapt_geo_risk_event failed: %s", e)
        return []
