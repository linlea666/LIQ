"""Outcome 追踪。

这是整个系统里唯一能回答"我们的判断到底对不对"的部分。
没有它，雷达就只是一个会发邮件的排行榜——三个月后依然不知道
S1 阈值该往上还是往下调。

四个决定成败的设计约束：

1. **增量维护，不事后重算**。快照在 48 小时后会被降采样抽稀，
   一个 3 分钟内拉升 5 倍又砸回的币，在抽稀后的数据里完全看不出来。
   因此峰谷必须在观测发生的当下就记录下来，事后重算永远补不回。

2. **右删失必须显式标注**。一条 2 小时前的警报在 720 小时窗口上
   当然还没有结果，但如果把它当成"收益 0%"计入统计，
   KPI 会被大量未成熟样本系统性地拖向零。每个窗口都带 matured 标记，
   统计时只用成熟样本。

3. **三种 ATH 口径不能混为一谈**。屏幕上的最高价、能真正卖出去的价格、
   以及扣掉滑点后实际拿到的钱，在 Meme 币上可以差好几倍。
   只报第一个等于系统性地高估自己。

4. **报警价不是可成交价**。从报警到人看到邮件再到下单，最快也要十几秒。
   延迟入场收益记录 15/30/60/120 秒后的价格，
   这是"纸面收益"与"可能收益"之间最大的一道折扣。
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .domain.models import INTERVAL_LOOKBACK_MS, TokenView
from .obs.events import EventType, bus
from .obs.logging_setup import now_ms
from .storage import repo
from .storage.db import Database, json_dump

logger = logging.getLogger("radar.tracker")

# 达到该倍数即记录首次到达时间
_MULTIPLE_TARGETS = ((2.0, "time_to_2x_sec"), (5.0, "time_to_5x_sec"),
                     (10.0, "time_to_10x_sec"))


@dataclass(slots=True)
class HorizonWindow:
    """单个时间窗口内的极值累积器。

    窗口到期后冻结：此后再高的价格也不属于这个窗口。
    这样"24 小时内最大浮盈"就真的只统计前 24 小时，
    而不会被第 5 天的行情污染。
    """

    label: str
    horizon_ms: int
    max_price: float | None = None
    min_price: float | None = None
    matured: bool = False

    def update(self, price: float, elapsed_ms: int) -> None:
        if self.matured:
            return
        if elapsed_ms > self.horizon_ms:
            self.matured = True
            return
        self.max_price = price if self.max_price is None else max(self.max_price, price)
        self.min_price = price if self.min_price is None else min(self.min_price, price)

    def as_dict(self, signal_price: float | None) -> dict[str, Any]:
        mfe = mae = None
        if signal_price and signal_price > 0:
            if self.max_price is not None:
                mfe = (self.max_price / signal_price - 1.0) * 100.0
            if self.min_price is not None:
                mae = (self.min_price / signal_price - 1.0) * 100.0
        return {
            "mfe_pct": None if mfe is None else round(mfe, 2),
            "mae_pct": None if mae is None else round(mae, 2),
            "matured": self.matured,
        }


@dataclass(slots=True)
class PaperPosition:
    """一档纸面仓位。

    存在的理由：只知道"报警后涨了 8 倍"是不够的——
    在一个流动性 2 万美元的池子里买 1000 美元，滑点会吃掉相当一部分。
    三档仓位并列可以直观看出策略在多大资金量下才开始失效。
    """

    size_usd: float
    entry_price: float
    entry_price_source: str
    est_slippage_pct: float
    effective_entry_price: float
    peak_value_usd: float
    current_value_usd: float

    def update(self, price: float) -> None:
        if self.effective_entry_price <= 0:
            return
        # 卖出同样要付滑点，因此用同一比例折算退出价值
        gross = self.size_usd * (price / self.effective_entry_price)
        value = gross * (1.0 - self.est_slippage_pct / 100.0)
        self.current_value_usd = value
        self.peak_value_usd = max(self.peak_value_usd, value)


@dataclass
class TrackedOutcome:
    """单条警报的追踪状态。"""

    alert_id: int
    token_id: int
    token_key: tuple[str, str]
    alert_kind: str
    signal_at: int
    signal_price: float | None
    signal_market_cap: float | None
    signal_liquidity: float | None
    trending_seen_at: int = 0

    raw_ath_price: float | None = None
    raw_ath_mc: float | None = None
    raw_ath_at: int = 0
    sustained_ath_price: float | None = None
    sustained_ath_mc: float | None = None
    sustained_ath_at: int = 0
    min_price: float | None = None
    min_price_at: int = 0

    entries: dict[str, float] = field(default_factory=dict)
    horizons: list[HorizonWindow] = field(default_factory=list)
    time_to: dict[str, int] = field(default_factory=dict)
    positions: list[PaperPosition] = field(default_factory=list)

    # 滑动窗口（时间, 价格, 可成交量代理），用于可持续 ATH 判定
    hold_window: list[tuple[int, float, float]] = field(default_factory=list)
    last_price: float | None = None
    last_updated: int = 0
    is_final: bool = False

    @property
    def max_horizon_ms(self) -> int:
        return max((h.horizon_ms for h in self.horizons), default=0)

    def peak_multiple(self) -> float | None:
        if not self.signal_price or self.raw_ath_price is None:
            return None
        return self.raw_ath_price / self.signal_price

    def current_multiple(self) -> float | None:
        if not self.signal_price or self.last_price is None:
            return None
        return self.last_price / self.signal_price


class OutcomeTracker:
    """追踪所有未定案警报的后续表现。"""

    def __init__(self, *, db: Database, config: Mapping[str, Any],
                 fingerprint: Mapping[str, str]) -> None:
        self._db = db
        self._fingerprint = dict(fingerprint)

        cfg = config.get("tracker", {}) or {}
        self._milestones: list[float] = sorted(
            float(x) for x in cfg.get("milestones_usd", [])
        )
        self._milestone_hysteresis = float(cfg.get("milestone_hysteresis_pct", 3.0)) / 100.0
        self._horizons_hours: list[float] = [
            float(h) for h in cfg.get("outcome_horizons_hours", [1, 4, 24])
        ]
        self._entry_delays: list[int] = [
            int(s) for s in cfg.get("entry_delay_sec", [15, 30, 60, 120])
        ]
        self._sustained_hold_ms = int(float(cfg.get("sustained_min_hold_sec", 300)) * 1000)
        self._sustained_min_volume = float(cfg.get("sustained_min_volume_usd", 2000.0))
        self._position_sizes: list[float] = [
            float(x) for x in cfg.get("paper_position_sizes", [100, 500, 1000])
        ]
        self._slippage_factor = float(cfg.get("liquidity_slippage_factor", 2.0))

        # alert_id → 追踪状态
        self._tracked: dict[int, TrackedOutcome] = {}
        # 代币 → 该币下所有未定案的 alert_id，避免每次观测遍历全表
        self._by_token: dict[tuple[str, str], set[int]] = {}
        # 代币 → 已记录的里程碑（用于首次上穿判定）
        # 已记录的档位（含从数据库恢复的），用于防重复落库
        self._milestone_hit: dict[tuple[str, str], set[float]] = {}
        # 本次进程内已建立基线的币。刻意不持久化：它的语义是
        # "我们这一轮运行从哪个市值开始看这个币的"
        self._milestone_baselined: set[tuple[str, str]] = set()

        self._tz = timezone(timedelta(
            hours=int((config.get("service", {}) or {}).get("tz_offset_hours", 8))
        ))
        # milestones_seeded 单独计数并暴露在 snapshot 里：
        # 否则里程碑表长期为空时，没人分得清是"抑制在正常工作"还是"逻辑坏了"
        self.stats = {"tracked": 0, "finalized": 0, "milestones": 0,
                      "milestones_seeded": 0}

    # ═════════════════════════════════════════════════════════════════════
    # 注册与更新
    # ═════════════════════════════════════════════════════════════════════

    def track(self, *, alert_id: int, alert_kind: str, view: TokenView,
              at_ms: int) -> TrackedOutcome | None:
        """为一条新警报开启追踪。"""
        if alert_id in self._tracked or view.token_id is None:
            return None

        price = view.getf("price")
        liquidity = view.getf("liquidity")
        tracked = TrackedOutcome(
            alert_id=alert_id,
            token_id=view.token_id,
            token_key=view.key,
            alert_kind=alert_kind,
            signal_at=at_ms,
            signal_price=price,
            signal_market_cap=view.getf("market_cap"),
            signal_liquidity=liquidity,
            trending_seen_at=view.trending_seen_at,
            horizons=[
                HorizonWindow(label=_horizon_label(h), horizon_ms=int(h * 3_600_000))
                for h in self._horizons_hours
            ],
        )
        if price and price > 0:
            tracked.positions = [
                self._open_position(size, price, liquidity)
                for size in self._position_sizes
            ]

        self._tracked[alert_id] = tracked
        self._by_token.setdefault(view.key, set()).add(alert_id)
        self.stats["tracked"] += 1
        self._persist(tracked)
        return tracked

    def _open_position(self, size_usd: float, price: float,
                       liquidity: float | None) -> PaperPosition:
        """按仓位占流动性的比例估算滑点。

        这是**粗略上界，不是真实报价**：真实滑点取决于池子曲线、
        路由拆单和抢跑。刻意做保守估计——高估滑点会让策略看起来更差，
        而低估会让我们据以放大仓位，那个方向的错误代价高得多。
        """
        if liquidity and liquidity > 0:
            slippage = min(50.0, self._slippage_factor * (size_usd / liquidity) * 100.0)
        else:
            # 流动性未知时按最大惩罚处理：未知不等于没有滑点
            slippage = 50.0
        effective = price * (1.0 + slippage / 100.0)
        return PaperPosition(
            size_usd=size_usd,
            entry_price=price,
            entry_price_source="signal",
            est_slippage_pct=round(slippage, 3),
            effective_entry_price=effective,
            peak_value_usd=size_usd,
            current_value_usd=size_usd,
        )

    def on_observation(self, view: TokenView, at_ms: int) -> None:
        """每次评估后调用，更新该币下所有未定案的追踪记录。"""
        self._check_milestones(view, at_ms)

        alert_ids = self._by_token.get(view.key)
        if not alert_ids:
            return

        price = view.getf("price")
        market_cap = view.getf("market_cap")
        # 区间极值来自榜单接口的一分钟价格序列，用来填补轮询间隙。
        # 没有它，一个 3 分钟内暴涨又暴跌的币在 30 秒采样下会被完全漏掉
        interval_high = view.getf("interval_high")
        interval_low = view.getf("interval_low")
        interval_seen_at = view.interval_seen_at
        volume_proxy = view.getf("volume_5m") or 0.0

        for alert_id in list(alert_ids):
            tracked = self._tracked.get(alert_id)
            if tracked is None:
                alert_ids.discard(alert_id)
                continue
            self._update_one(tracked, view, at_ms, price, market_cap,
                             interval_high, interval_low, interval_seen_at,
                             volume_proxy)

    def _update_one(self, tracked: TrackedOutcome, view: TokenView, at_ms: int,
                    price: float | None, market_cap: float | None,
                    interval_high: float | None, interval_low: float | None,
                    interval_seen_at: int, volume_proxy: float) -> None:
        if at_ms < tracked.signal_at:
            return
        elapsed_ms = at_ms - tracked.signal_at

        if price is not None and price > 0:
            tracked.last_price = price
            self._record_entry_prices(tracked, price, elapsed_ms)
            self._update_extremes(tracked, price, market_cap, at_ms)
            self._update_sustained(tracked, price, market_cap, at_ms, volume_proxy)
            self._record_time_to_multiples(tracked, price, elapsed_ms)
            for position in tracked.positions:
                position.update(price)

        # 区间极值只用于极值统计，不参与可持续 ATH 与纸面成交：
        # 我们只知道这一分钟内到过那个价，不知道它停留了多久。
        # 且必须保证极值窗口**完全落在信号之后**才可采信：
        #   - 合并视图会永久携带最后一次非空极值，崩盘后仍是旧高点；
        #   - 极值本身回看 INTERVAL_LOOKBACK_MS，观测刚好在信号后到达时
        #     窗口仍可能盖住信号之前的拉盘顶。
        # 两者任何一个漏掉，都会把"警报前的顶"算成"警报后的收益"，
        # 伪造出数百倍的假 MOON——这正是 V1 实盘发生过的事故。
        interval_usable = (
            interval_seen_at > 0
            and interval_seen_at - INTERVAL_LOOKBACK_MS >= tracked.signal_at
        )
        if not interval_usable:
            interval_high = interval_low = None

        if interval_high is not None and interval_high > 0:
            self._update_extremes(tracked, interval_high, None, at_ms, price_only=True)
        if interval_low is not None and interval_low > 0:
            self._update_extremes(tracked, interval_low, None, at_ms, price_only=True)

        for window in tracked.horizons:
            if price is not None and price > 0:
                window.update(price, elapsed_ms)
            if interval_high is not None and interval_high > 0:
                window.update(interval_high, elapsed_ms)
            if interval_low is not None and interval_low > 0:
                window.update(interval_low, elapsed_ms)

        tracked.last_updated = at_ms
        if elapsed_ms > tracked.max_horizon_ms:
            self._finalize(tracked, view)
        else:
            self._persist(tracked)

    def _record_entry_prices(self, tracked: TrackedOutcome, price: float,
                             elapsed_ms: int) -> None:
        """记录延迟入场价。

        采样是离散的，不可能正好落在第 15 秒，因此取第一个不早于该延迟点的观测。
        但必须设容差：把第 65 秒采到的价格记成"30 秒入场价"，
        恰好毁掉这个指标存在的唯一目的——量化那几十秒的折扣。
        采样间隔过大时宁可留空（诚实的"不知道"），也不要填一个错的数。
        警报后该币会进入 25 秒高频窗口，正常情况下容差绰绰有余。
        """
        for delay in self._entry_delays:
            key = f"entry_{delay}s"
            if key in tracked.entries:
                continue
            delay_ms = delay * 1000
            if elapsed_ms < delay_ms:
                continue
            if elapsed_ms - delay_ms <= max(30_000, delay_ms):
                tracked.entries[key] = price

    def _update_extremes(self, tracked: TrackedOutcome, price: float,
                         market_cap: float | None, at_ms: int,
                         *, price_only: bool = False) -> None:
        if tracked.raw_ath_price is None or price > tracked.raw_ath_price:
            tracked.raw_ath_price = price
            tracked.raw_ath_at = at_ms
            if not price_only:
                tracked.raw_ath_mc = market_cap
        if tracked.min_price is None or price < tracked.min_price:
            tracked.min_price = price
            tracked.min_price_at = at_ms

    def _update_sustained(self, tracked: TrackedOutcome, price: float,
                          market_cap: float | None, at_ms: int,
                          volume_proxy: float) -> None:
        """可持续 ATH：价格在窗口内**持续**站住的最高水平。

        定义为「回看 min_hold_sec，这段时间里的最低价」的历史最大值。
        这个定义自动排除掉插针：价格插到 10 倍再瞬间砸回，
        回看窗口里的最低价仍然很低，因此不会被计入。

        成交量条件用 volume_5m 做代理。这是个近似——真正需要的是
        「这段时间的成交额」，而接口只提供固定窗口的成交量。
        近似的方向是保守的：成交清淡的插针不会被误判为可持续。
        """
        window = tracked.hold_window
        window.append((at_ms, price, volume_proxy))
        cutoff = at_ms - self._sustained_hold_ms
        while window and window[0][0] < cutoff:
            window.pop(0)

        if len(window) < 2:
            return
        # 窗口必须真正覆盖 min_hold_sec，否则刚开始追踪时
        # 只有两个相邻采样点也会被当作"持续了 5 分钟"
        if at_ms - window[0][0] < self._sustained_hold_ms:
            return
        if max(v for _, _, v in window) < self._sustained_min_volume:
            return

        floor_price = min(p for _, p, _ in window)
        if tracked.sustained_ath_price is None or floor_price > tracked.sustained_ath_price:
            tracked.sustained_ath_price = floor_price
            tracked.sustained_ath_at = at_ms
            if market_cap and price > 0:
                tracked.sustained_ath_mc = market_cap * (floor_price / price)

    def _record_time_to_multiples(self, tracked: TrackedOutcome, price: float,
                                  elapsed_ms: int) -> None:
        if not tracked.signal_price or tracked.signal_price <= 0:
            return
        multiple = price / tracked.signal_price
        for target, column in _MULTIPLE_TARGETS:
            if column not in tracked.time_to and multiple >= target:
                tracked.time_to[column] = int(elapsed_ms / 1000)

    # ═════════════════════════════════════════════════════════════════════
    # 里程碑
    # ═════════════════════════════════════════════════════════════════════

    def _check_milestones(self, view: TokenView, at_ms: int) -> None:
        """市值里程碑：只记**我们亲眼看到的**首次上穿。

        滞回是必需的：市值在 100 万上下抖动的币，若不加滞回
        会在几分钟内产生几十条里程碑记录，把这张表变成噪音。

        更重要的是首次观测时要建立基线。一个我们发现时市值已经 100 万的币，
        它跨越 3 万、5 万、10 万、30 万的那些时刻发生在我们看到它之前——
        把它们记成"刚刚上穿"不只是噪音，而是直接伪造了这张表的核心用途：
        「多久涨到 100 万」会变成「我们发现它时它已经多大了」，
        两个完全不同的问题共用同一个字段，而且没有任何办法事后区分。
        """
        if view.token_id is None or not self._milestones:
            return
        market_cap = view.getf("market_cap")
        if market_cap is None or market_cap <= 0:
            return

        hit = self._milestone_hit.setdefault(view.key, set())

        if view.key not in self._milestone_baselined:
            # 本次进程第一次看到这个币：把它当前已越过的档位并入已记录集合，
            # 不落库也不发事件。之后再跨越新档位才是我们真正见证的。
            #
            # 基线按"本次进程"而不是"首次入库"判定，是为了同时覆盖停机期：
            # 服务停了两小时，期间某个币从 4 万涨到 500 万，重启后我们
            # 同样没有见证那些跨越，不该把它们记成刚刚发生
            self._milestone_baselined.add(view.key)
            baseline = {
                m for m in self._milestones
                if market_cap >= m * (1.0 + self._milestone_hysteresis)
            } - hit
            hit |= baseline
            self.stats["milestones_seeded"] += len(baseline)
            return

        # 只检查已被越过的那些档位，避免每次观测遍历全部里程碑
        index = bisect.bisect_right(self._milestones, market_cap)
        for milestone in self._milestones[:index]:
            if milestone in hit:
                continue
            if market_cap < milestone * (1.0 + self._milestone_hysteresis):
                continue
            hit.add(milestone)
            self.stats["milestones"] += 1
            repo.insert_milestone(
                self._db,
                view=view,
                milestone_usd=milestone,
                direction="up",
                sequence=1,
                is_first_upcross=True,
                occurred_at=at_ms,
                data_quality=view.last_scores.get("data_quality"),
                mc_source=str(view.field_source.get("market_cap", "")),
                snapshot_id=view.last_snapshot_id,
            )
            bus.emit_token(
                EventType.MC_MILESTONE,
                token=view,
                module="tracker",
                summary=f"市值首次上穿 {_money(milestone)}",
                payload={"milestone_usd": milestone, "market_cap": market_cap},
            )

    def seed_milestones(self, key: tuple[str, str], milestones: Sequence[float]) -> None:
        """重启后回填已记录的里程碑，防止重复上报。"""
        self._milestone_hit.setdefault(key, set()).update(float(m) for m in milestones)

    # ═════════════════════════════════════════════════════════════════════
    # 落库与定案
    # ═════════════════════════════════════════════════════════════════════

    def _persist(self, tracked: TrackedOutcome) -> None:
        repo.upsert_outcome(
            self._db,
            alert_id=tracked.alert_id,
            token_id=tracked.token_id,
            signal_at=tracked.signal_at,
            values=self._outcome_values(tracked),
        )
        for position in tracked.positions:
            repo.upsert_paper_position(
                self._db,
                alert_id=tracked.alert_id,
                token_id=tracked.token_id,
                size_usd=position.size_usd,
                opened_at=tracked.signal_at,
                values={
                    "entry_price": position.entry_price,
                    "entry_price_source": position.entry_price_source,
                    "est_slippage_pct": position.est_slippage_pct,
                    "effective_entry_price": position.effective_entry_price,
                    "peak_value_usd": position.peak_value_usd,
                    "current_value_usd": position.current_value_usd,
                    "closed_at": tracked.last_updated if tracked.is_final else None,
                    "exit_price": tracked.last_price if tracked.is_final else None,
                    "realized_multiple": (
                        position.current_value_usd / position.size_usd
                        if position.size_usd else None
                    ),
                    "status": "closed" if tracked.is_final else "open",
                    "last_updated": tracked.last_updated,
                },
            )

    def _outcome_values(self, tracked: TrackedOutcome) -> dict[str, Any]:
        signal_price = tracked.signal_price
        peak = tracked.peak_multiple()
        current = tracked.current_multiple()

        mfe = mae = None
        if signal_price and signal_price > 0:
            if tracked.raw_ath_price is not None:
                mfe = (tracked.raw_ath_price / signal_price - 1.0) * 100.0
            if tracked.min_price is not None:
                mae = (tracked.min_price / signal_price - 1.0) * 100.0

        lead_time = None
        if tracked.trending_seen_at and tracked.trending_seen_at > tracked.signal_at:
            # 正数表示我们先于热门榜发现——这正是这套系统存在的理由
            lead_time = int((tracked.trending_seen_at - tracked.signal_at) / 1000)

        values: dict[str, Any] = {
            "signal_price": signal_price,
            "signal_market_cap": tracked.signal_market_cap,
            "signal_liquidity": tracked.signal_liquidity,
            "raw_ath_price": tracked.raw_ath_price,
            "raw_ath_mc": tracked.raw_ath_mc,
            "raw_ath_at": tracked.raw_ath_at or None,
            "sustained_ath_price": tracked.sustained_ath_price,
            "sustained_ath_mc": tracked.sustained_ath_mc,
            "sustained_ath_at": tracked.sustained_ath_at or None,
            "liq_adjusted_multiple": self._liq_adjusted_multiple(tracked),
            "min_price": tracked.min_price,
            "min_price_at": tracked.min_price_at or None,
            "horizons_json": json_dump({
                w.label: w.as_dict(signal_price) for w in tracked.horizons
            }),
            "peak_multiple": None if peak is None else round(peak, 4),
            "current_multiple": None if current is None else round(current, 4),
            "mfe_pct": None if mfe is None else round(mfe, 2),
            "mae_pct": None if mae is None else round(mae, 2),
            "outcome_label": _label_for(peak, mae),
            "trending_seen_at": tracked.trending_seen_at or None,
            "lead_time_sec": lead_time,
            "last_updated": tracked.last_updated or tracked.signal_at,
            "is_final": 1 if tracked.is_final else 0,
        }
        values.update(tracked.entries)
        values.update(tracked.time_to)
        return values

    def _liq_adjusted_multiple(self, tracked: TrackedOutcome) -> float | None:
        """扣掉滑点后最大的一档仓位实际能拿到的倍数。

        用最大仓位而不是最小仓位：这个数字是用来判断"策略能承载多少钱"的，
        乐观的那一档没有决策价值。
        """
        if not tracked.positions:
            return None
        largest = max(tracked.positions, key=lambda p: p.size_usd)
        if largest.size_usd <= 0:
            return None
        return round(largest.peak_value_usd / largest.size_usd, 4)

    def _finalize(self, tracked: TrackedOutcome, view: TokenView | None) -> None:
        if tracked.is_final:
            return
        tracked.is_final = True
        for window in tracked.horizons:
            window.matured = True
        self._persist(tracked)

        self._tracked.pop(tracked.alert_id, None)
        alerts = self._by_token.get(tracked.token_key)
        if alerts is not None:
            alerts.discard(tracked.alert_id)
            if not alerts:
                self._by_token.pop(tracked.token_key, None)
        self.stats["finalized"] += 1

        peak = tracked.peak_multiple()
        if view is not None:
            bus.emit_token(
                EventType.OUTCOME_FINALIZED,
                token=view,
                module="tracker",
                alert_id=tracked.alert_id,
                summary=(
                    f"{tracked.alert_kind} 追踪定案"
                    + (f"，峰值 {peak:.2f}x" if peak else "")
                ),
                payload={"peak_multiple": peak,
                         "outcome_label": _label_for(peak, None)},
            )

    def sweep(self, at_ms: int | None = None) -> int:
        """定期清理已超过最长窗口但不再有新观测的追踪记录。

        必需：一枚币变成 DEAD 之后就不会再有观测进来，
        `_update_one` 永远不会被调用，这条记录会永久占着内存，
        而且 outcomes 表里的 is_final 永远是 0，KPI 统计会一直把它当作未成熟。
        """
        now = at_ms or now_ms()
        expired = [
            t for t in self._tracked.values()
            if now - t.signal_at > t.max_horizon_ms
        ]
        for tracked in expired:
            self._finalize(tracked, None)
        return len(expired)

    # ═════════════════════════════════════════════════════════════════════
    # 重启恢复
    # ═════════════════════════════════════════════════════════════════════

    async def restore(self) -> int:
        """从数据库恢复未定案的追踪记录。

        不恢复的后果不是"少一点数据"，而是这些警报的 Outcome
        永远停在重启那一刻的数值，且 is_final 永远为 0——
        它们会一直污染 KPI 统计，而且没有任何报错。
        """
        rows = await self._db.fetch_all(
            "SELECT o.*, a.alert_kind, t.chain_id, t.contract_address "
            "FROM outcomes o "
            "JOIN alerts a ON a.alert_id = o.alert_id "
            "JOIN token_master t ON t.token_id = o.token_id "
            "WHERE o.is_final = 0 ORDER BY o.signal_at DESC LIMIT 5000"
        )
        restored = 0
        now = now_ms()
        for row in rows:
            tracked = self._tracked_from_row(row)
            if tracked is None:
                continue
            if now - tracked.signal_at > tracked.max_horizon_ms:
                # 停机期间已经跨过最长窗口，直接定案而不是留在内存里
                self._finalize(tracked, None)
                continue
            self._tracked[tracked.alert_id] = tracked
            self._by_token.setdefault(tracked.token_key, set()).add(tracked.alert_id)
            restored += 1

        milestones = await self._db.fetch_all(
            "SELECT t.chain_id, t.contract_address, m.milestone_usd "
            "FROM milestones m JOIN token_master t ON t.token_id = m.token_id "
            "WHERE m.direction='up'"
        )
        for row in milestones:
            self.seed_milestones(
                (str(row["chain_id"]), str(row["contract_address"])),
                [float(row["milestone_usd"])],
            )

        self.stats["tracked"] += restored
        logger.info("恢复 %d 条未定案追踪记录", restored)
        return restored

    def _tracked_from_row(self, row: Mapping[str, Any]) -> TrackedOutcome | None:
        try:
            alert_id = int(row["alert_id"])
        except (TypeError, ValueError, KeyError):
            return None

        tracked = TrackedOutcome(
            alert_id=alert_id,
            token_id=int(row["token_id"]),
            token_key=(str(row["chain_id"]), str(row["contract_address"])),
            alert_kind=str(row["alert_kind"]),
            signal_at=int(row["signal_at"]),
            signal_price=_f(row["signal_price"]),
            signal_market_cap=_f(row["signal_market_cap"]),
            signal_liquidity=_f(row["signal_liquidity"]),
            trending_seen_at=int(row["trending_seen_at"] or 0),
            horizons=[
                HorizonWindow(label=_horizon_label(h), horizon_ms=int(h * 3_600_000))
                for h in self._horizons_hours
            ],
        )
        # 极值必须一并恢复，否则重启会把历史最高价抹掉，
        # 一枚在重启前涨了 8 倍随后回落的币会被记成从未涨过
        tracked.raw_ath_price = _f(row["raw_ath_price"])
        tracked.raw_ath_mc = _f(row["raw_ath_mc"])
        tracked.raw_ath_at = int(row["raw_ath_at"] or 0)
        tracked.sustained_ath_price = _f(row["sustained_ath_price"])
        tracked.sustained_ath_mc = _f(row["sustained_ath_mc"])
        tracked.sustained_ath_at = int(row["sustained_ath_at"] or 0)
        tracked.min_price = _f(row["min_price"])
        tracked.min_price_at = int(row["min_price_at"] or 0)
        tracked.last_updated = int(row["last_updated"] or row["signal_at"])

        for delay in self._entry_delays:
            key = f"entry_{delay}s"
            value = _f(row[key]) if key in row.keys() else None
            if value is not None:
                tracked.entries[key] = value
        for _, column in _MULTIPLE_TARGETS:
            value = row[column] if column in row.keys() else None
            if value is not None:
                tracked.time_to[column] = int(value)

        if tracked.signal_price and tracked.signal_price > 0:
            tracked.positions = [
                self._open_position(size, tracked.signal_price,
                                    tracked.signal_liquidity)
                for size in self._position_sizes
            ]
        return tracked

    # ── 诊断 ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "active": len(self._tracked),
            "tokens": len(self._by_token),
            "tracked_total": self.stats["tracked"],
            "finalized": self.stats["finalized"],
            "milestones": self.stats["milestones"],
            # 这两个数一起看才有意义：seeded 远大于 milestones 是正常的
            # （多数币被发现时已经越过低档位），但 milestones 长期为 0
            # 而 seeded 在涨，说明我们只在接手存量、没抓到任何真实成长
            "milestones_seeded": self.stats["milestones_seeded"],
        }


# ═════════════════════════════════════════════════════════════════════════
# 每日 KPI
# ═════════════════════════════════════════════════════════════════════════

class KpiReporter:
    """按成熟队列汇总每日 KPI。

    统计口径的关键在于分母：只统计"信号发出时间 + 窗口长度 <= 现在"的警报。
    把昨天刚发的警报计入 7 天窗口的统计，等于用一堆还没到期的样本
    把成功率系统性地拖向零，然后据此把阈值越调越严。
    """

    def __init__(self, *, db: Database, config: Mapping[str, Any],
                 fingerprint: Mapping[str, str]) -> None:
        self._db = db
        self._fingerprint = dict(fingerprint)
        cfg = config.get("tracker", {}) or {}
        self._horizons_hours = [float(h) for h in cfg.get("outcome_horizons_hours",
                                                          [1, 4, 24])]
        self._tz = timezone(timedelta(
            hours=int((config.get("service", {}) or {}).get("tz_offset_hours", 8))
        ))

    async def build(self, at_ms: int | None = None) -> list[dict[str, Any]]:
        now = at_ms or now_ms()
        stat_date = datetime.fromtimestamp(now / 1000, self._tz).strftime("%Y-%m-%d")
        day_start = int(
            datetime.fromtimestamp(now / 1000, self._tz)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp() * 1000
        )

        rows = await self._db.fetch_all(
            "SELECT a.alert_kind, a.strategy_version, o.signal_at, o.peak_multiple, "
            "o.mae_pct, o.horizons_json, o.liq_adjusted_multiple "
            "FROM outcomes o JOIN alerts a ON a.alert_id = o.alert_id "
            "WHERE a.is_near_miss = 0 AND o.signal_at >= ? AND o.signal_at < ?",
            (day_start - 30 * 86_400_000, now),
        )

        results: list[dict[str, Any]] = []
        for horizon_hours in self._horizons_hours:
            label = _horizon_label(horizon_hours)
            horizon_ms = int(horizon_hours * 3_600_000)
            buckets: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
            for row in rows:
                # 右删失过滤：未到期的样本一律不进分母
                if now - int(row["signal_at"]) < horizon_ms:
                    continue
                key = (str(row["alert_kind"]),
                       str(row["strategy_version"] or "unknown"))
                buckets.setdefault(key, []).append(row)

            for (kind, version), items in buckets.items():
                payload = _kpi_payload(items, label)
                repo.upsert_kpi_daily(
                    self._db,
                    stat_date=stat_date,
                    strategy_version=version,
                    alert_kind=kind,
                    horizon=label,
                    matured_count=len(items),
                    payload=payload,
                    created_at=now,
                )
                results.append({"alert_kind": kind, "strategy_version": version,
                                "horizon": label, "matured_count": len(items),
                                **payload})
        return results

    async def summarize(self, *, window_days: int = 7,
                        at_ms: int | None = None) -> dict[str, Any]:
        """近 N 天推送质量汇总（纯查询，不写库）。

        与 build() 的区别是口径：build 是"截至今天的 30 天滚动"按日落库，
        这里回答的是"最近一周发出的推送表现如何"——周报邮件和前端
        质量看板都用这个口径。聚合逻辑复用 _kpi_payload，
        保证与 kpi_daily 的指标定义完全一致。
        """
        now = at_ms or now_ms()
        since = now - window_days * 86_400_000
        rows = await self._db.fetch_all(
            "SELECT a.alert_kind, a.strategy_version, o.signal_at, o.peak_multiple, "
            "o.mae_pct, o.horizons_json, o.liq_adjusted_multiple, o.outcome_label "
            "FROM outcomes o JOIN alerts a ON a.alert_id = o.alert_id "
            "WHERE a.is_near_miss = 0 AND o.signal_at >= ? AND o.signal_at < ?",
            (since, now),
        )

        groups: list[dict[str, Any]] = []
        for horizon_hours in self._horizons_hours:
            label = _horizon_label(horizon_hours)
            horizon_ms = int(horizon_hours * 3_600_000)
            buckets: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
            for row in rows:
                if now - int(row["signal_at"]) < horizon_ms:
                    continue
                key = (str(row["alert_kind"]),
                       str(row["strategy_version"] or "unknown"))
                buckets.setdefault(key, []).append(row)
            for (kind, version), items in sorted(buckets.items()):
                labels: dict[str, int] = {}
                for item in items:
                    if item["outcome_label"]:
                        name = str(item["outcome_label"])
                        labels[name] = labels.get(name, 0) + 1
                groups.append({
                    "alert_kind": kind, "strategy_version": version,
                    "horizon": label, "matured_count": len(items),
                    "labels": labels, **_kpi_payload(items, label),
                })

        return {
            "window_days": window_days,
            "since": since,
            "until": now,
            # 已发出总数含未成熟样本：看板必须能区分"没发"和"还没到期"
            "total_alerts": len(rows),
            "groups": groups,
        }


def _kpi_payload(items: Sequence[Mapping[str, Any]], label: str) -> dict[str, Any]:
    peaks = [float(r["peak_multiple"]) for r in items
             if r["peak_multiple"] is not None]
    liq_peaks = [float(r["liq_adjusted_multiple"]) for r in items
                 if r["liq_adjusted_multiple"] is not None]
    maes = [float(r["mae_pct"]) for r in items if r["mae_pct"] is not None]

    return {
        "hit_2x_ratio": _ratio(peaks, 2.0),
        "hit_5x_ratio": _ratio(peaks, 5.0),
        "hit_10x_ratio": _ratio(peaks, 10.0),
        # 中位数而非均值：一个 300 倍的样本会让均值完全失去意义
        "median_peak_multiple": round(statistics.median(peaks), 3) if peaks else None,
        "median_liq_adjusted": (
            round(statistics.median(liq_peaks), 3) if liq_peaks else None
        ),
        "median_mae_pct": round(statistics.median(maes), 2) if maes else None,
        "rug_ratio": _ratio([-m for m in maes], 80.0),
        "horizon": label,
    }


def _ratio(values: Sequence[float], threshold: float) -> float | None:
    if not values:
        return None
    return round(sum(1 for v in values if v >= threshold) / len(values), 4)


# ═════════════════════════════════════════════════════════════════════════
# 后台维护任务
# ═════════════════════════════════════════════════════════════════════════

class TrackerService:
    """把追踪器的周期性维护包装成一个后台任务。"""

    def __init__(self, *, tracker: OutcomeTracker, kpi: KpiReporter,
                 config: Mapping[str, Any]) -> None:
        self._tracker = tracker
        self._kpi = kpi
        self._tz = timezone(timedelta(
            hours=int((config.get("service", {}) or {}).get("tz_offset_hours", 8))
        ))
        self._last_kpi_hour = ""
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="tracker_maintenance")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                self._tracker.sweep()
                await self._maybe_build_kpi()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("追踪器维护任务异常")
            for _ in range(60):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def _maybe_build_kpi(self) -> None:
        """每小时增量重算当日 KPI。

        旧实现每天只在 9 点后跑一次，之后成熟的警报永远进不了当日
        KPI（实盘一整天 11 条警报成熟后 kpi_daily 仍是 0 行）。
        kpi_daily 的 UNIQUE(stat_date, strategy_version, alert_kind, horizon)
        + upsert 天然幂等，每小时重算只会覆盖当日行，不会产生重复。
        """
        local = datetime.fromtimestamp(now_ms() / 1000, self._tz)
        hour_key = local.strftime("%Y-%m-%d %H")
        if self._last_kpi_hour == hour_key:
            return
        self._last_kpi_hour = hour_key
        results = await self._kpi.build()
        bus.emit(
            EventType.KPI_GENERATED,
            module="tracker",
            summary=f"重算 {local.strftime('%Y-%m-%d')} 的 KPI，共 {len(results)} 组",
            payload={"groups": len(results), "hour": local.hour},
        )


# ═════════════════════════════════════════════════════════════════════════
# 工具
# ═════════════════════════════════════════════════════════════════════════

def _horizon_label(hours: float) -> str:
    if hours >= 24 and hours % 24 == 0:
        return f"{int(hours / 24)}d"
    return f"{int(hours)}h" if hours == int(hours) else f"{hours}h"


def _label_for(peak: float | None, mae_pct: float | None) -> str | None:
    """给 Outcome 一个粗粒度标签，便于快速筛选。

    刻意粗糙：精细的分类应该在研究阶段用原始数据做，
    这里只是为了让"最近有哪些 10 倍币"这类查询不用全表扫描。
    """
    if peak is None:
        return None
    if mae_pct is not None and mae_pct <= -80.0:
        return "RUG"
    if peak >= 10.0:
        return "MOON"
    if peak >= 5.0:
        return "STRONG"
    if peak >= 2.0:
        return "WIN"
    if peak >= 1.2:
        return "SMALL_WIN"
    return "FLAT"


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"
