"""优先级请求调度器。

为什么必须有它：
币安接口的真实限流阈值未公开，而"一个 3000 币的池子 × 每分钟一次"
= 3000 rpm，一定会被封。所以请求预算是这个系统最稀缺的资源，
必须显式分配，而不是让各个采集器各自 sleep 后随意发请求。

三重约束同时生效：
  1. 全局桶——所有请求共享的总 rpm 上限（可被 429 自适应下调）。
  2. 分层桶——每层独立上限，防止低价值层（如 reject 抽样）
     把预算吃光后让 S2 币拿不到配额。
  3. 优先级让行——有高优先级请求在排队时，低优先级请求主动让行，
     否则"每层都有独立配额"仍可能出现低优先级先抢到全局令牌的情况。

关键设计取舍：discovery 层永不降频。宁可牺牲存量币的刷新频率，
也不能停止发现新币——错过的新币是永久损失，而存量币晚 2 分钟刷新可以补。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from .obs.events import EventType, bus
from .obs.metrics import metrics
from .settings import Settings

logger = logging.getLogger("radar.scheduler")

# 层优先级（数字越小越优先）。discovery 最高，reject 抽样最低。
TIER_PRIORITY: dict[str, int] = {
    "discovery": 0,
    "burst": 1,
    "s2": 2,
    "s1": 3,
    "audit": 3,
    "s0": 4,
    "social": 5,
    "watching": 6,
    "reject": 7,
}

# 永不因 429 自适应而降低配额的层
PROTECTED_TIERS = frozenset({"discovery"})


class TokenBucket:
    """令牌桶。容量默认为 1 分钟的额度，允许小幅突发但不累积无限额度。"""

    def __init__(self, rate_per_min: float, *, capacity: float | None = None) -> None:
        self._rate_per_min = max(0.01, rate_per_min)
        self._capacity = capacity if capacity is not None else max(1.0, rate_per_min)
        self._tokens = self._capacity
        self._last = time.monotonic()

    @property
    def rate_per_min(self) -> float:
        return self._rate_per_min

    def set_rate(self, rate_per_min: float) -> None:
        self._refill()
        self._rate_per_min = max(0.01, rate_per_min)
        self._capacity = max(1.0, self._rate_per_min)
        self._tokens = min(self._tokens, self._capacity)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed <= 0:
            return
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_min / 60.0)

    def available(self) -> float:
        self._refill()
        return self._tokens

    def take(self, amount: float = 1.0) -> None:
        """取走令牌。调用前必须已通过 available() 确认充足。"""
        self._tokens -= amount

    def seconds_until_available(self, amount: float = 1.0) -> float:
        self._refill()
        deficit = amount - self._tokens
        if deficit <= 0:
            return 0.0
        return deficit * 60.0 / self._rate_per_min


@dataclass
class TierRuntime:
    name: str
    priority: int
    bucket: TokenBucket
    interval_sec: int
    configured_rpm: float
    granted: int = 0
    deferred: int = 0


@dataclass
class BurstEntry:
    """警报后进入高频采样窗口的代币。"""

    key: tuple[str, str]
    until_ms: int
    reason: str


class RequestScheduler:
    """请求配额调度器 + 警报爆发窗口管理。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        cfg = settings.scheduler
        self._global_rpm_hard = float(cfg.get("global_rpm", 90))
        self._target_rpm = float(cfg.get("target_rpm", 72))
        self._jitter_ratio = float(cfg.get("jitter_ratio", 0.15))

        adaptive = cfg.get("adaptive", {}) or {}
        self._adaptive_window = float(adaptive.get("window_sec", 300))
        self._rate_limit_threshold = float(adaptive.get("rate_limit_threshold", 0.05))
        self._downscale_ratio = float(adaptive.get("downscale_ratio", 0.8))
        self._min_rpm = float(adaptive.get("min_rpm", 30))
        self._recover_after = float(adaptive.get("recover_after_sec", 600))

        self._scale = 1.0
        self._last_rate_limit_ms = 0
        self._last_scale_change_ms = 0

        self._global = TokenBucket(min(self._target_rpm, self._global_rpm_hard))
        self._tiers: dict[str, TierRuntime] = {}
        for name, tier_cfg in settings.tiers.items():
            self._tiers[name] = TierRuntime(
                name=name,
                priority=TIER_PRIORITY.get(name, 9),
                bucket=TokenBucket(tier_cfg.max_rpm),
                interval_sec=tier_cfg.interval_sec,
                configured_rpm=tier_cfg.max_rpm,
            )

        # 各优先级当前排队等待的请求数，用于让行判断
        self._waiting: dict[int, int] = {}
        self._burst: dict[tuple[str, str], BurstEntry] = {}
        self._burst_window_sec = int(cfg.get("burst_window_sec", 480))
        self._saturation_since_ms = 0

    # ── 配额获取 ────────────────────────────────────────────────────────
    async def acquire(self, tier_name: str, *, timeout_sec: float = 120.0) -> bool:
        """获取一次请求配额。

        返回 False 表示等待超时——调用方应放弃本次请求而不是无限期堵塞，
        否则整个采集循环会被一个拿不到配额的低优先级任务卡死。
        """
        tier = self._tiers.get(tier_name)
        if tier is None:
            raise KeyError(f"未定义的调度层: {tier_name}")

        priority = tier.priority
        self._waiting[priority] = self._waiting.get(priority, 0) + 1
        deadline = time.monotonic() + timeout_sec
        deferred_logged = False
        try:
            while True:
                if self._has_higher_priority_waiter(priority):
                    # 主动让行：高优先级请求在排队时不去争抢全局令牌
                    if not deferred_logged:
                        tier.deferred += 1
                        deferred_logged = True
                    await asyncio.sleep(0.05)
                    if time.monotonic() >= deadline:
                        return False
                    continue

                tier_wait = tier.bucket.seconds_until_available()
                global_wait = self._global.seconds_until_available()
                if tier_wait <= 0 and global_wait <= 0:
                    # 单线程事件循环内检查与扣减之间没有 await，因此不存在竞态
                    tier.bucket.take()
                    self._global.take()
                    tier.granted += 1
                    return True

                if not deferred_logged and max(tier_wait, global_wait) > 1.0:
                    tier.deferred += 1
                    deferred_logged = True
                    self._note_saturation(tier_name, tier_wait, global_wait)

                sleep_for = min(max(0.05, max(tier_wait, global_wait)), 5.0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(sleep_for, remaining))
        finally:
            self._waiting[priority] = max(0, self._waiting.get(priority, 1) - 1)

    def _has_higher_priority_waiter(self, priority: int) -> bool:
        for other, count in self._waiting.items():
            if other < priority and count > 0:
                return True
        return False

    def _note_saturation(self, tier_name: str, tier_wait: float, global_wait: float) -> None:
        now_ms = int(time.time() * 1000)
        # 预算饱和事件做时间聚合，避免持续饱和时刷屏
        if now_ms - self._saturation_since_ms < 60_000:
            return
        self._saturation_since_ms = now_ms
        bus.emit(
            EventType.BUDGET_SATURATED,
            module="scheduler",
            summary=f"{tier_name} 等待配额 {max(tier_wait, global_wait):.1f}s",
            payload={
                "tier": tier_name,
                "tier_wait_sec": round(tier_wait, 2),
                "global_wait_sec": round(global_wait, 2),
                "scale": round(self._scale, 3),
                "effective_rpm": round(self.effective_rpm, 1),
                "actual_rpm": metrics.actual_rpm(),
            },
        )

    # ── 429 自适应 ──────────────────────────────────────────────────────
    def record_rate_limit(self) -> None:
        self._last_rate_limit_ms = int(time.time() * 1000)

    def evaluate_adaptive(self) -> None:
        """根据滚动窗口内的 429 占比调整全局配额。由后台任务定期调用。"""
        ratio = metrics.rate_limit_ratio(self._adaptive_window)
        now_ms = int(time.time() * 1000)
        metrics.gauge("rate_limit_ratio", ratio)

        if ratio > self._rate_limit_threshold:
            new_scale = max(self._min_rpm / self._target_rpm, self._scale * self._downscale_ratio)
            if new_scale < self._scale - 1e-6:
                old_rpm = self.effective_rpm
                self._scale = new_scale
                self._last_scale_change_ms = now_ms
                self._apply_scale()
                bus.emit(
                    EventType.TIER_DEGRADED,
                    module="scheduler",
                    summary=f"429 占比 {ratio:.1%}，全局配额 {old_rpm:.0f} → {self.effective_rpm:.0f} rpm",
                    payload={
                        "rate_limit_ratio": round(ratio, 4),
                        "scale": round(self._scale, 3),
                        "effective_rpm": round(self.effective_rpm, 1),
                    },
                )
            return

        # 无 429 且已降速：达到恢复时长后逐步回升
        if self._scale < 1.0:
            quiet_ms = now_ms - max(self._last_rate_limit_ms, self._last_scale_change_ms)
            if quiet_ms >= self._recover_after * 1000:
                old_rpm = self.effective_rpm
                self._scale = min(1.0, self._scale / self._downscale_ratio)
                self._last_scale_change_ms = now_ms
                self._apply_scale()
                logger.info(
                    "限流恢复，全局配额 %.0f → %.0f rpm（scale=%.2f）",
                    old_rpm, self.effective_rpm, self._scale,
                )

    def _apply_scale(self) -> None:
        self._global.set_rate(min(self.effective_rpm, self._global_rpm_hard))
        for tier in self._tiers.values():
            if tier.name in PROTECTED_TIERS:
                tier.bucket.set_rate(tier.configured_rpm)
            else:
                tier.bucket.set_rate(max(0.5, tier.configured_rpm * self._scale))

    @property
    def effective_rpm(self) -> float:
        return self._target_rpm * self._scale

    @property
    def scale(self) -> float:
        return self._scale

    # ── 轮询间隔 ────────────────────────────────────────────────────────
    def interval_with_jitter(self, tier_name: str) -> float:
        """带抖动的轮询间隔。

        抖动是必需的：若所有代币的轮询间隔完全一致，重启后会形成
        整齐的请求尖峰（同一秒钟几百个请求），既容易触发限流，
        也让 rpm 曲线在峰谷之间剧烈波动。
        """
        tier = self._tiers.get(tier_name)
        base = float(tier.interval_sec if tier else 300)
        if base <= 0:
            return 0.0
        jitter = base * self._jitter_ratio
        return max(1.0, base + random.uniform(-jitter, jitter))

    # ── 警报爆发窗口 ────────────────────────────────────────────────────
    def open_burst(self, key: tuple[str, str], reason: str,
                   *, window_sec: int | None = None) -> None:
        """警报触发后开启高频采样窗口。

        目的是把"报警后前几分钟"的价格轨迹采密——延迟入场收益、
        纸面成交价、Sustained ATH 全都依赖这段高分辨率数据，
        事后无法补采。
        """
        duration = window_sec or self._burst_window_sec
        until = int(time.time() * 1000) + duration * 1000
        existing = self._burst.get(key)
        if existing is not None and existing.until_ms >= until:
            return
        self._burst[key] = BurstEntry(key=key, until_ms=until, reason=reason)
        bus.emit(
            EventType.BURST_WINDOW_OPENED,
            module="scheduler",
            chain_id=key[0],
            contract_address=key[1],
            summary=f"{reason} 触发 {duration}s 高频采样窗口",
            payload={"reason": reason, "window_sec": duration},
        )

    def in_burst(self, key: tuple[str, str]) -> bool:
        entry = self._burst.get(key)
        if entry is None:
            return False
        if entry.until_ms <= int(time.time() * 1000):
            self._burst.pop(key, None)
            bus.emit(
                EventType.BURST_WINDOW_CLOSED,
                module="scheduler",
                chain_id=key[0],
                contract_address=key[1],
                summary="高频采样窗口结束",
                payload={"reason": entry.reason},
            )
            return False
        return True

    def burst_keys(self) -> list[tuple[str, str]]:
        return [k for k in list(self._burst) if self.in_burst(k)]

    def prune_burst(self) -> None:
        for key in list(self._burst):
            self.in_burst(key)

    # ── 可观测 ──────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "global": {
                "hard_rpm": self._global_rpm_hard,
                "target_rpm": self._target_rpm,
                "effective_rpm": round(self.effective_rpm, 1),
                "scale": round(self._scale, 3),
                "available_tokens": round(self._global.available(), 2),
                "actual_rpm": metrics.actual_rpm(),
                "rate_limit_ratio_5m": round(metrics.rate_limit_ratio(self._adaptive_window), 4),
            },
            "tiers": [
                {
                    "name": t.name,
                    "priority": t.priority,
                    "configured_rpm": t.configured_rpm,
                    "current_rpm": round(t.bucket.rate_per_min, 2),
                    "available_tokens": round(t.bucket.available(), 2),
                    "interval_sec": t.interval_sec,
                    "granted": t.granted,
                    "deferred": t.deferred,
                    "protected": t.name in PROTECTED_TIERS,
                }
                for t in sorted(self._tiers.values(), key=lambda x: x.priority)
            ],
            "burst_tokens": len(self.burst_keys()),
        }
