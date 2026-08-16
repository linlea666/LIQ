"""特征引擎。

三条必须遵守的纪律，每一条都对应一类真实会毁掉整个策略的错误：

1. **分母下限**。用 2 万市值的币算"净流入/市值"，
   随便几百美元就能得出 3% 的惊人比率。所有比率类特征
   的分母都强制下限，否则评分会被最小最垃圾的币霸榜。

2. **小基数不放大**。holders 从 1 涨到 3 是 +200%，
   从 1000 涨到 1500 是 +50%，但后者才是真正的信号。
   因此变化率同时输出相对值、绝对值与对数值，
   且相对值在基数低于阈值时按下限基数计算。

3. **As-of 严格性**。所有历史对比只使用"当时已经知道"的数据点，
   绝不允许用后来的观测回填过去的特征——否则回测结果全是幻觉。

另外：所有比率都做 winsorize（截断极端值）。Meme 币的极端值不是噪声，
但如果不截断，单个 +50000% 的异常样本会让整个归一化尺度失去意义。
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .models import FeatureSet, TokenView

# 特征版本：任何计算口径变化都必须递增，否则新旧特征混在一起无法比较
FEATURE_VERSION = "f1.0.0"


def _safe_div(numerator: float | None, denominator: float | None,
              *, min_denominator: float) -> float | None:
    if numerator is None or denominator is None:
        return None
    effective = max(denominator, min_denominator)
    if effective <= 0:
        return None
    return numerator / effective


def _winsorize(value: float | None, cap: float) -> float | None:
    if value is None:
        return None
    return max(-cap, min(cap, value))


def _growth_triplet(
    current: float | None,
    previous: float | None,
    *,
    min_base: float,
    cap: float,
) -> tuple[float | None, float | None, float | None]:
    """返回 (相对变化, 绝对变化, 对数变化)。

    相对变化使用 max(previous, min_base) 作为分母：
    这正是"小基数不放大"的落点——1 → 3 不会被算成 +200%。
    """
    if current is None or previous is None:
        return None, None, None
    absolute = current - previous
    base = max(previous, min_base)
    relative = absolute / base if base > 0 else None
    if current > 0 and previous > 0:
        logarithmic = math.log(current / previous)
    else:
        logarithmic = None
    return _winsorize(relative, cap), absolute, _winsorize(logarithmic, math.log(cap + 1))


class FeatureEngine:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self._min_mc = float(config.get("min_market_cap_denominator", 30000.0))
        self._min_liq = float(config.get("min_liquidity_denominator", 3000.0))
        self._min_holders = float(config.get("min_holder_denominator", 30.0))
        self._cap = float(config.get("winsorize_growth_cap", 5.0))
        self._windows: tuple[int, ...] = tuple(
            int(w) for w in (config.get("lookback_windows_sec") or [300, 900, 3600])
        )

    def compute(self, view: TokenView, now_ms: int) -> FeatureSet:
        fs = FeatureSet()
        market_cap = view.getf("market_cap")
        liquidity = view.getf("liquidity")
        holders = view.getf("holders")
        age_sec = view.age_sec(now_ms)

        fs.set("token_age_sec", float(age_sec) if age_sec is not None else None)
        fs.set("token_age_hours", age_sec / 3600.0 if age_sec is not None else None)

        self._structural_ratios(view, fs, market_cap, liquidity, holders)
        self._flow_ratios(view, fs, market_cap, liquidity)
        self._pressure(view, fs)
        self._velocity(view, fs, now_ms)
        self._distribution_health(view, fs)
        self._social(view, fs, market_cap)
        self._age_normalized(view, fs, age_sec, holders)
        return fs

    # ── 结构性比率 ──────────────────────────────────────────────────────
    def _structural_ratios(self, view: TokenView, fs: FeatureSet,
                           market_cap: float | None, liquidity: float | None,
                           holders: float | None) -> None:
        fs.set("liquidity_mc_ratio", _safe_div(liquidity, market_cap, min_denominator=self._min_mc))
        fs.set("mc_per_holder", _safe_div(market_cap, holders, min_denominator=self._min_holders))
        fs.set(
            "liquidity_per_holder",
            _safe_div(liquidity, holders, min_denominator=self._min_holders),
        )
        kyc = view.getf("kyc_holders")
        fs.set("kyc_holder_ratio", _safe_div(kyc, holders, min_denominator=self._min_holders))

        # 未锁仓比例：仅在两个供应量都已知时才有意义
        circulating = view.getf("circulating_supply")
        total = view.getf("total_supply")
        if circulating and total and total > 0:
            fs.set("circulating_ratio", min(1.0, circulating / total))
        else:
            fs.set("circulating_ratio", None)

        fs.set("bonding_progress", view.getf("bonding_progress"))

    # ── 资金流比率 ──────────────────────────────────────────────────────
    def _flow_ratios(self, view: TokenView, fs: FeatureSet,
                     market_cap: float | None, liquidity: float | None) -> None:
        net_inflow = view.getf("net_inflow")
        fs.set(
            "net_inflow_mc_ratio",
            _winsorize(
                _safe_div(net_inflow, market_cap, min_denominator=self._min_mc), self._cap
            ),
        )
        fs.set(
            "net_inflow_liq_ratio",
            _winsorize(
                _safe_div(net_inflow, liquidity, min_denominator=self._min_liq), self._cap
            ),
        )

        # 成交额/市值：换手强度。优先用 1h 口径，缺失时回退无窗聚合量
        volume = view.getf("volume_1h")
        volume_source = "1h"
        if volume is None:
            volume = view.getf("volume_agg")
            volume_source = "agg"
        fs.set(
            "volume_mc_ratio",
            _winsorize(_safe_div(volume, market_cap, min_denominator=self._min_mc), self._cap * 4),
        )
        fs.set(
            "volume_liq_ratio",
            _winsorize(_safe_div(volume, liquidity, min_denominator=self._min_liq), self._cap * 4),
        )
        fs.notes["volume_source"] = volume_source

    # ── 买卖压力 ────────────────────────────────────────────────────────
    def _pressure(self, view: TokenView, fs: FeatureSet) -> None:
        buy = view.getf("volume_1h_buy")
        sell = view.getf("volume_1h_sell")
        if buy is not None and sell is not None and (buy + sell) > 0:
            fs.set("buy_sell_imbalance", (buy - sell) / (buy + sell))
        else:
            fs.set("buy_sell_imbalance", None)

        count_buy = view.getf("count_1h_buy") or view.getf("count_agg_buy")
        count_sell = view.getf("count_1h_sell") or view.getf("count_agg_sell")
        if count_buy is not None and count_sell is not None and (count_buy + count_sell) > 0:
            fs.set("trade_count_imbalance", (count_buy - count_sell) / (count_buy + count_sell))
        else:
            fs.set("trade_count_imbalance", None)

        # 每笔成交均额：过小说明是刷量机器人，过大说明是少数大户在推
        volume = view.getf("volume_1h") or view.getf("volume_agg")
        count = view.getf("count_1h") or view.getf("count_agg")
        if volume is not None and count and count > 0:
            fs.set("avg_trade_size_usd", volume / count)
        else:
            fs.set("avg_trade_size_usd", None)

        # 独立地址/成交笔数：接近 1 说明参与者分散，远小于 1 说明少数地址反复交易
        traders = view.getf("unique_trader_1h")
        if traders is not None and count and count > 0:
            fs.set("trader_per_trade", min(1.0, traders / count))
        else:
            fs.set("trader_per_trade", None)

    # ── 速度与加速度 ────────────────────────────────────────────────────
    def _velocity(self, view: TokenView, fs: FeatureSet, now_ms: int) -> None:
        """多窗口速度 + 加速度。

        加速度（速度的变化）比速度本身更有预警价值：
        持有人以恒定速度增长往往是机器人，
        而增速本身在加快才是真实的注意力涌入。
        """
        for window_sec in self._windows:
            label = _window_label(window_sec)
            past = view.history_at_or_before(now_ms - window_sec * 1000)
            if past is None:
                for suffix in ("holder_growth", "mc_growth", "price_growth",
                               "liq_growth", "holder_per_min"):
                    fs.set(f"{suffix}_{label}", None)
                continue

            elapsed_min = max(1e-6, (now_ms - past.ts) / 60000.0)

            rel, absolute, _ = _growth_triplet(
                view.getf("holders"), _as_float(past.holders),
                min_base=self._min_holders, cap=self._cap,
            )
            fs.set(f"holder_growth_{label}", rel)
            fs.set(f"holder_delta_{label}", absolute)
            fs.set(
                f"holder_per_min_{label}",
                absolute / elapsed_min if absolute is not None else None,
            )

            rel_mc, _, _ = _growth_triplet(
                view.getf("market_cap"), _as_float(past.market_cap),
                min_base=self._min_mc, cap=self._cap,
            )
            fs.set(f"mc_growth_{label}", rel_mc)

            rel_price, _, log_price = _growth_triplet(
                view.getf("price"), _as_float(past.price),
                min_base=1e-18, cap=self._cap,
            )
            fs.set(f"price_growth_{label}", rel_price)
            fs.set(f"price_log_growth_{label}", log_price)

            rel_liq, _, _ = _growth_triplet(
                view.getf("liquidity"), _as_float(past.liquidity),
                min_base=self._min_liq, cap=self._cap,
            )
            fs.set(f"liq_growth_{label}", rel_liq)

            # Top10 下降是好事，故单独记录变化量（负值 = 筹码在分散）
            top10_now = view.getf("top10_percent")
            top10_past = _as_float(past.top10_percent)
            fs.set(
                f"top10_delta_{label}",
                top10_now - top10_past if (top10_now is not None and top10_past is not None) else None,
            )

            sm_now = view.getf("smart_money_count")
            sm_past = _as_float(past.smart_money_count)
            fs.set(
                f"smart_money_delta_{label}",
                sm_now - sm_past if (sm_now is not None and sm_past is not None) else None,
            )

        fs.set("holder_acceleration", self._acceleration(fs, "holder_per_min"))

    def _acceleration(self, fs: FeatureSet, prefix: str) -> float | None:
        """短窗速度 vs 长窗速度：>0 表示在加速。"""
        if len(self._windows) < 2:
            return None
        short = fs.get(f"{prefix}_{_window_label(self._windows[0])}")
        long = fs.get(f"{prefix}_{_window_label(self._windows[-1])}")
        if short is None or long is None:
            return None
        return _winsorize(short - long, 1e6)

    # ── 筹码健康度 ──────────────────────────────────────────────────────
    def _distribution_health(self, view: TokenView, fs: FeatureSet) -> None:
        for name in ("top10_percent", "dev_percent", "sniper_percent",
                     "insider_percent", "bundler_percent", "new_wallet_percent",
                     "smart_money_percent", "kol_percent", "pro_percent",
                     "dev_sell_percent"):
            fs.set(name, view.getf(name))

        parts = [
            view.getf("dev_percent"), view.getf("sniper_percent"),
            view.getf("insider_percent"), view.getf("bundler_percent"),
        ]
        known = [p for p in parts if p is not None]
        fs.set("combined_concentration", sum(known) if known else None)
        # 已知项占四项的比例：用于区分"确实很低"与"根本没数据"
        fs.set("concentration_coverage", len(known) / 4.0)

        fs.set("smart_money_count", view.getf("smart_money_count"))
        fs.set("smart_money_traders", view.getf("smart_money_traders"))
        exit_rate = view.getf("exit_rate")
        fs.set("exit_rate", exit_rate)
        # 聪明钱已离场比例越高越危险，转成"留存率"便于正向加权
        fs.set("smart_money_retention", None if exit_rate is None else (100.0 - exit_rate) / 100.0)

    # ── 社交 ────────────────────────────────────────────────────────────
    def _social(self, view: TokenView, fs: FeatureSet, market_cap: float | None) -> None:
        hype = view.getf("social_hype")
        fs.set("social_hype", hype)
        fs.set("search_count_24h", view.getf("search_count_24h"))
        fs.set("twitter_followers", view.getf("twitter_followers"))
        # 热度/市值：找"讨论度已经很高但市值还没起来"的币
        fs.set(
            "hype_mc_ratio",
            _winsorize(_safe_div(hype, market_cap, min_denominator=self._min_mc), self._cap * 10),
        )
        sentiment = view.get("sentiment")
        fs.set(
            "sentiment_score",
            {"Positive": 1.0, "Neutral": 0.0, "Negative": -1.0}.get(sentiment)
            if isinstance(sentiment, str) else None,
        )

    # ── 年龄归一化 ──────────────────────────────────────────────────────
    def _age_normalized(self, view: TokenView, fs: FeatureSet,
                        age_sec: int | None, holders: float | None) -> None:
        """按年龄归一化的增长速度。

        "上线 8 分钟就有 400 个持有人"和"上线 3 天有 400 个持有人"
        是完全不同的两件事，但绝对值一模一样。
        """
        if age_sec is None or age_sec <= 0:
            fs.set("holders_per_hour_lifetime", None)
            fs.set("mc_per_hour_lifetime", None)
            return
        hours = max(age_sec / 3600.0, 1.0 / 60.0)
        fs.set("holders_per_hour_lifetime", holders / hours if holders is not None else None)
        market_cap = view.getf("market_cap")
        fs.set("mc_per_hour_lifetime", market_cap / hours if market_cap is not None else None)


def _window_label(window_sec: int) -> str:
    if window_sec % 3600 == 0:
        return f"{window_sec // 3600}h"
    if window_sec % 60 == 0:
        return f"{window_sec // 60}m"
    return f"{window_sec}s"


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
