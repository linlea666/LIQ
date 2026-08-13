"""Bottom Model 指标注册表：数据源 → 日级/周级序列的声明式映射。

设计纪律：
- 只消费**原始序列**（价格/OI/清算/链上比率等），绝不引用趋势、MAA、
  现货抄底等模块的加工评分，避免循环污染。
- 一次外呼可产出多个指标（multi-output parse），节省 Coinglass 配额。
- 每个 FetchSpec 独立 fail-open：解析失败只损失自身指标。

历史窗口备忘（2026-08 实测）：
- Coinglass 链上指标（SOPR/NUPL/200W/STH-RP 等）：2009~2010 起全历史
- Coinglass 聚合 OI / CME OI：2021-02 起（limit=2000 上限）
- Coinglass 聚合清算 / OI 加权资金费：2023-11 起（窗口最短，百分位需标注）
- Coinglass Coinbase 溢价 / 聚合现货 CVD：2017-08 起（覆盖 2018 与 2022 大底）
- BGeometrics：近 4 年（免费档）
- Yahoo BTC=F 周线：2017-12（CME 上市）起
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

Rows = list[tuple[str, float]]

# 指标角色是模型契约的一部分，不允许再靠全文搜索猜测“采集了是否生效”。
# model_input: 进入生产规则或辅助状态；display_only: 仅上下文展示；
# audit_only: 仅离线审计；unused: 已进入弃用流程且不参与任何决策。
METRIC_CONTRACTS: dict[str, dict[str, Any]] = {
    "btc_close_1d": {"role": "model_input", "unit": "usd"},
    "btc_vol_1d": {"role": "unused", "unit": "usd", "deprecated": True},
    "btc_close_1w": {"role": "model_input", "unit": "usd"},
    "btc_high_1w": {"role": "model_input", "unit": "usd"},
    "btc_low_1w": {"role": "model_input", "unit": "usd"},
    "btc_price_onchain": {"role": "model_input", "unit": "usd"},
    "ma_200w": {"role": "model_input", "unit": "usd"},
    "sth_realized_price": {"role": "model_input", "unit": "usd"},
    "lth_realized_price": {"role": "display_only", "unit": "usd"},
    "nupl": {"role": "model_input", "unit": "ratio"},
    "sth_sopr": {"role": "model_input", "unit": "ratio"},
    "lth_sopr": {"role": "model_input", "unit": "ratio"},
    "sth_supply": {"role": "model_input", "unit": "btc"},
    "reserve_risk": {"role": "model_input", "unit": "ratio"},
    "puell_multiple": {"role": "model_input", "unit": "ratio"},
    "oi_agg_usd": {"role": "model_input", "unit": "usd"},
    "cme_oi_usd": {"role": "model_input", "unit": "usd"},
    "liq_long_usd": {"role": "model_input", "unit": "usd"},
    "liq_short_usd": {"role": "model_input", "unit": "usd"},
    "funding_oiw": {"role": "model_input", "unit": "percent_per_8h"},
    "etf_flow_usd": {"role": "model_input", "unit": "usd"},
    "coinbase_premium_rate": {"role": "model_input", "unit": "percent"},
    "spot_net_taker_usd": {"role": "model_input", "unit": "usd"},
    "stablecoin_total_mcap": {"role": "model_input", "unit": "usd"},
    "fear_greed": {"role": "model_input", "unit": "index"},
    "exchange_balance_btc": {"role": "model_input", "unit": "btc"},
    "global_m2_yoy": {"role": "model_input", "unit": "percent"},
    "mvrv_zscore": {"role": "model_input", "unit": "zscore"},
    "sth_mvrv": {"role": "model_input", "unit": "ratio"},
    "sopr": {"role": "model_input", "unit": "ratio"},
    "realized_loss": {"role": "model_input", "unit": "usd"},
    "realized_profit": {"role": "model_input", "unit": "usd"},
    "lth_realized_loss": {"role": "model_input", "unit": "usd"},
    "cme_close_1w": {"role": "display_only", "unit": "usd"},
    "cme_vol_1w": {"role": "model_input", "unit": "contracts"},
}


def metric_contract(metric: str) -> dict[str, Any]:
    """返回稳定的指标角色/单位契约；未知指标默认 fail-closed。"""
    return dict(METRIC_CONTRACTS.get(metric, {
        "role": "unused", "unit": "unknown", "deprecated": True,
    }))

# BTC-only 模块的固定取数参数（与 config.yaml coins.BTC 一致）
_BTC_EXCHANGE = "Binance"
_BTC_PAIR = "BTCUSDT"

# Coinglass 泛型行的元数据字段（auto 模式取值时排除）
_META_VALUE_KEYS = frozenset({"timestamp", "time", "price", "create_time"})


def _day_from_ms(ts_ms: Any) -> Optional[str]:
    try:
        ts = int(ts_ms)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 1e12:
        ts //= 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _to_float(raw: Any) -> Optional[float]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _row_value(row: dict, value_key: str) -> Optional[float]:
    """按指定字段取值；``auto`` = 第一个非元数据数值字段。"""
    if value_key != "auto":
        return _to_float(row.get(value_key))
    for key, raw in row.items():
        if key in _META_VALUE_KEYS:
            continue
        value = _to_float(raw)
        if value is not None:
            return value
    return None


def parse_ts_rows(raw: Any, outputs: dict[str, str]) -> dict[str, Rows]:
    """Coinglass 泛型解析：[{timestamp|time, ...}] → {metric: [(day, value)]}。

    outputs: {metric_name: value_key}，value_key 可为 "auto"。
    同一天多行（不应出现）取最后一行；非法行静默跳过。
    """
    result: dict[str, dict[str, float]] = {name: {} for name in outputs}
    if not isinstance(raw, list):
        return {name: [] for name in outputs}
    for row in raw:
        if not isinstance(row, dict):
            continue
        day = _day_from_ms(row.get("timestamp", row.get("time")))
        if day is None:
            continue
        for name, value_key in outputs.items():
            value = _row_value(row, value_key)
            if value is not None:
                result[name][day] = value
    return {name: sorted(days.items()) for name, days in result.items()}


# 读取期坏点过滤：指标 → 相邻两点绝对跳变上限。
# 只为存在**已观测到的**上游残缺数据的指标设置，阈值取历史真实最大跳变的 2 倍
# 以上，确保只可能命中物理上不可能的值。
#
# global_m2_yoy：2026-07-06 上游给出 global_m2_supply = 100.1 万亿（前一周
# 123.3 万亿），一周蒸发 19%，导致同比读数 -12.54%（全历史最低）。685 次周环比
# 中最大真实跳变仅 2.47 个百分点，该点跳变 19.86 个百分点。这类残缺点会把
# 宏观因子推向"极度紧缩=底部有利"的错误方向，必须在进入评分前剔除。
_MAX_ABS_STEP: dict[str, float] = {"global_m2_yoy": 5.0}


def sanitize_series(metric: str, rows: Rows) -> Rows:
    """剔除上游残缺造成的孤立坏点（连续 3 点以上视为真实水平位移，予以保留）。"""
    max_step = _MAX_ABS_STEP.get(metric)
    if max_step is None or len(rows) < 3:
        return rows
    kept: Rows = [rows[0]]
    pending: Rows = []
    for day, value in rows[1:]:
        if abs(value - kept[-1][1]) > max_step:
            pending.append((day, value))
            if len(pending) >= 3:      # 连续偏离 = 上游口径真的变了，接受
                kept.extend(pending)
                pending = []
            continue
        pending = []                   # 回到正常区间 → 确认前面是孤立坏点
        kept.append((day, value))
    if pending:
        logger.warning(
            "bottom_model.sanitize metric=%s dropped=%s (跳变超过 %.1f，判定为上游残缺)",
            metric, [day for day, _ in pending], max_step,
        )
    return kept


def parse_stablecoin_mcap(raw: Any) -> dict[str, Rows]:
    """稳定币市值 dict 格式：{data_list:[{USDT:..},..], time_list:[..]}。

    延续 polls/macro.py 的口径：总市值 = 行内数值字段求和，
    < 1e9 视为上游异常数据，跳过（实测早期存在跳变脏数据）。
    """
    if not isinstance(raw, dict):
        return {"stablecoin_total_mcap": []}
    data_list = raw.get("data_list") or []
    time_list = raw.get("time_list") or []
    n = min(len(data_list), len(time_list))
    days: dict[str, float] = {}
    for i in range(n):
        item = data_list[i]
        if not isinstance(item, dict):
            continue
        day = _day_from_ms(time_list[i])
        if day is None:
            continue
        # 上游即完整 USD（2026-08 实测总值 ≈ 2.6e11）；<$1B 视为早期脏数据跳过
        total = float(sum(v for v in item.values() if isinstance(v, (int, float))))
        if total < 1e9:
            continue
        days[day] = total
    return {"stablecoin_total_mcap": sorted(days.items())}


def parse_fear_greed(raw: Any) -> dict[str, Rows]:
    """恐惧贪婪指数：兼容 dict{data_list,time_list} 与 list[{time,value}] 两种形态。"""
    days: dict[str, float] = {}
    if isinstance(raw, dict):
        data_list = raw.get("data_list") or []
        time_list = raw.get("time_list") or []
        for i in range(min(len(data_list), len(time_list))):
            day = _day_from_ms(time_list[i])
            value = _to_float(data_list[i])
            if day is not None and value is not None:
                days[day] = value
    elif isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            day = _day_from_ms(row.get("timestamp", row.get("time")))
            value = _to_float(row.get("value", row.get("values")))
            if day is not None and value is not None:
                days[day] = value
    return {"fear_greed": sorted(days.items())}


def parse_exchange_balance(raw: Any) -> dict[str, Rows]:
    """交易所余额 dict 格式：{time_list, price_list, data_map:{exchange:[...]}}。

    汇总为全交易所 BTC 总余额；某交易所当日为 None 则跳过该所（口径会随
    交易所覆盖变化漂移，因子层只用 30d 变化率，不用绝对值）。
    """
    if not isinstance(raw, dict):
        return {"exchange_balance_btc": []}
    time_list = raw.get("time_list") or []
    data_map = raw.get("data_map") or {}
    if not isinstance(data_map, dict):
        return {"exchange_balance_btc": []}
    days: dict[str, float] = {}
    for i, ts in enumerate(time_list):
        day = _day_from_ms(ts)
        if day is None:
            continue
        total = 0.0
        seen = False
        for series in data_map.values():
            if not isinstance(series, list) or i >= len(series):
                continue
            value = _to_float(series[i])
            if value is not None:
                total += value
                seen = True
        if seen and total > 0:
            days[day] = total
    return {"exchange_balance_btc": sorted(days.items())}


def parse_spot_cvd(raw: Any) -> dict[str, Rows]:
    """聚合现货 CVD → 日净 taker 买入额（USD）。

    两点口径处理：
    - 上游从 2014-04 起返回行，但 2017-08-17 之前买卖量恒为 0（无数据的占位行）。
      必须剔除，否则 0 会被当成"买卖平衡"的真实观测，把分位分布整体拉向中性。
    - 只取每日净额（buy − sell），不用 cum_vol_delta：累积序列单调漂移，
      其分位数只反映"距起点多久"而非需求强弱。
    """
    if not isinstance(raw, list):
        return {"spot_net_taker_usd": []}
    days: dict[str, float] = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        day = _day_from_ms(row.get("time", row.get("timestamp")))
        buy = _to_float(row.get("agg_taker_buy_vol"))
        sell = _to_float(row.get("agg_taker_sell_vol"))
        if day is None or buy is None or sell is None:
            continue
        if buy <= 0 and sell <= 0:
            continue
        days[day] = buy - sell
    return {"spot_net_taker_usd": sorted(days.items())}


def parse_yahoo_weekly(rows: Any) -> dict[str, Rows]:
    """YahooCMESource.fetch_weekly_history 输出 → 周级序列（day = 周一日期）。"""
    close: Rows = []
    volume: Rows = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = _day_from_ms(row.get("week_start_ts"))
            if day is None:
                continue
            close_v = _to_float(row.get("close"))
            vol_v = _to_float(row.get("volume"))
            if close_v is not None:
                close.append((day, close_v))
            if vol_v is not None:
                volume.append((day, vol_v))
    return {"cme_close_1w": close, "cme_vol_1w": volume}


@dataclass(frozen=True)
class FetchSpec:
    """一次外呼的声明：key 为采集账本主键，metrics 为产出的指标名集合。"""

    key: str
    source: str                # coinglass | bgeometrics | yahoo_cme
    cadence: str               # daily | weekly
    metrics: tuple[str, ...]
    fetch: Callable[[Any], Awaitable[Any]]      # (source_obj) -> raw
    parse: Callable[[Any], dict[str, Rows]]     # raw -> {metric: rows}
    note: str = ""
    # 新鲜度容忍覆盖（天）：None = 按 cadence 默认；用于上游固有滞后的
    # 指标（如 global_m2_yoy 滞后约 1 个月），避免 data_quality 恒报 stale
    staleness_days: Optional[int] = None
    # 多输出接口只有 required_metrics 全部通过完整性与时效检查，才允许整组落库。
    # None 表示 metrics 全部必需；显式空元组表示无必需输出（当前没有这种 spec）。
    required_metrics: Optional[tuple[str, ...]] = None
    # 上游未提供历史发布时间/vintage 时，只能使用确定性的近似发布滞后；
    # 对应观测会标为 PIT_APPROX，绝不冒充严格 point-in-time 数据。
    publication_lag_sec: int = 0

    def required(self) -> tuple[str, ...]:
        return self.metrics if self.required_metrics is None else self.required_metrics


def _cg(method: str, /, **kwargs) -> Callable[[Any], Awaitable[Any]]:
    async def _fetch(cg: Any) -> Any:
        return await getattr(cg, method)(**kwargs)
    return _fetch


def _bg(endpoint: str) -> Callable[[Any], Awaitable[Any]]:
    async def _fetch(bg: Any) -> Any:
        rows = await bg.fetch_metric_series(endpoint)
        return rows
    return _fetch


def _bg_parse(metric: str) -> Callable[[Any], dict[str, Rows]]:
    def _parse(raw: Any) -> dict[str, Rows]:
        return {metric: list(raw) if isinstance(raw, list) else []}
    return _parse


async def _yahoo_fetch(source: Any) -> Any:
    return await source.fetch_weekly_history()


def build_registry() -> list[FetchSpec]:
    """全量指标注册表。顺序即采集顺序（Coinglass 靠前便于统一 spacing）。"""
    specs: list[FetchSpec] = [
        # ── Coinglass · 价格与结构 ──
        FetchSpec(
            key="btc_price_1d", source="coinglass", cadence="daily",
            metrics=("btc_close_1d", "btc_vol_1d"),
            required_metrics=("btc_close_1d",),
            fetch=_cg("fetch_price_history", exchange=_BTC_EXCHANGE,
                      symbol=_BTC_PAIR, interval="1d", limit=2000),
            parse=lambda raw: parse_ts_rows(
                raw, {"btc_close_1d": "close", "btc_vol_1d": "volume_usd"},
            ),
            note="Binance 合约日线收盘价+成交额（~5.5 年）",
        ),
        FetchSpec(
            key="btc_price_1w", source="coinglass", cadence="weekly",
            metrics=("btc_close_1w", "btc_high_1w", "btc_low_1w"),
            fetch=_cg("fetch_price_history", exchange=_BTC_EXCHANGE,
                      symbol=_BTC_PAIR, interval="1w", limit=500),
            parse=lambda raw: parse_ts_rows(raw, {
                "btc_close_1w": "close", "btc_high_1w": "high", "btc_low_1w": "low",
            }),
            note="周线结构（LL/HL 检测）用",
        ),
        FetchSpec(
            key="ma_200w", source="coinglass", cadence="daily",
            metrics=("ma_200w", "btc_price_onchain"),
            fetch=_cg("fetch_200w_ma_heatmap"),
            parse=lambda raw: parse_ts_rows(raw, {
                "ma_200w": "moving_average_1440", "btc_price_onchain": "price",
            }),
            note="200 周均线 + 2010 起全历史日价（全历史回撤百分位用）",
        ),
        # ── Coinglass · 链上估值/投降 ──
        FetchSpec(
            key="sth_realized_price", source="coinglass", cadence="daily",
            metrics=("sth_realized_price",),
            fetch=_cg("fetch_sth_realized_price"),
            parse=lambda raw: parse_ts_rows(
                raw, {"sth_realized_price": "sth_realized_price"},
            ),
        ),
        FetchSpec(
            key="lth_realized_price", source="coinglass", cadence="daily",
            metrics=("lth_realized_price",),
            fetch=_cg("fetch_lth_realized_price"),
            parse=lambda raw: parse_ts_rows(
                raw, {"lth_realized_price": "lth_realized_price"},
            ),
        ),
        FetchSpec(
            key="nupl", source="coinglass", cadence="daily",
            metrics=("nupl",),
            fetch=_cg("fetch_nupl"),
            parse=lambda raw: parse_ts_rows(raw, {"nupl": "net_unpnl"}),
        ),
        FetchSpec(
            key="sth_sopr", source="coinglass", cadence="daily",
            metrics=("sth_sopr",),
            fetch=_cg("fetch_sth_sopr"),
            parse=lambda raw: parse_ts_rows(raw, {"sth_sopr": "sth_sopr"}),
        ),
        FetchSpec(
            key="lth_sopr", source="coinglass", cadence="daily",
            metrics=("lth_sopr",),
            fetch=_cg("fetch_lth_sopr"),
            parse=lambda raw: parse_ts_rows(raw, {"lth_sopr": "lth_sopr"}),
        ),
        FetchSpec(
            key="sth_supply", source="coinglass", cadence="daily",
            metrics=("sth_supply",),
            fetch=_cg("fetch_sth_supply"),
            parse=lambda raw: parse_ts_rows(raw, {"sth_supply": "auto"}),
        ),
        # lth_supply 已停采：与 sth_supply 信息镜像冗余（总供应恒增背景下
        # 两者近似互补），投降因子的筹码转移视角由 sth_supply_drop 覆盖
        FetchSpec(
            key="reserve_risk", source="coinglass", cadence="daily",
            metrics=("reserve_risk",),
            fetch=_cg("fetch_reserve_risk"),
            parse=lambda raw: parse_ts_rows(raw, {"reserve_risk": "auto"}),
        ),
        FetchSpec(
            key="puell_multiple", source="coinglass", cadence="daily",
            metrics=("puell_multiple",),
            fetch=_cg("fetch_puell_multiple"),
            parse=lambda raw: parse_ts_rows(raw, {"puell_multiple": "auto"}),
        ),
        # ── Coinglass · 杠杆 ──
        FetchSpec(
            key="oi_agg_usd", source="coinglass", cadence="daily",
            metrics=("oi_agg_usd",),
            fetch=_cg("fetch_oi_aggregated_history", symbol="BTC",
                      interval="1d", limit=2000),
            parse=lambda raw: parse_ts_rows(raw, {"oi_agg_usd": "close"}),
            note="聚合 OI（2021-02 起）",
        ),
        FetchSpec(
            key="cme_oi_usd", source="coinglass", cadence="daily",
            metrics=("cme_oi_usd",),
            fetch=_cg("fetch_oi_history", exchange="CME", symbol="BTCUSD",
                      interval="1d", limit=2000),
            parse=lambda raw: parse_ts_rows(raw, {"cme_oi_usd": "close"}),
            note="CME 机构持仓（2021-02 起）",
        ),
        FetchSpec(
            key="liq_agg", source="coinglass", cadence="daily",
            metrics=("liq_long_usd", "liq_short_usd"),
            fetch=_cg("fetch_liquidation_aggregated_history", symbol="BTC",
                      interval="1d", limit=1000),
            parse=lambda raw: parse_ts_rows(raw, {
                "liq_long_usd": "aggregated_long_liquidation_usd",
                "liq_short_usd": "aggregated_short_liquidation_usd",
            }),
            note="聚合清算（仅 2023-11 起，百分位窗口需标注）",
        ),
        FetchSpec(
            key="funding_oiw", source="coinglass", cadence="daily",
            metrics=("funding_oiw",),
            fetch=_cg("fetch_fr_oi_weight_history", symbol="BTC",
                      interval="1d", limit=1000),
            parse=lambda raw: parse_ts_rows(raw, {"funding_oiw": "close"}),
            note="OI 加权资金费（仅 2023-11 起）",
        ),
        # ── Coinglass · 需求/宏观 ──
        FetchSpec(
            key="etf_flow_usd", source="coinglass", cadence="daily",
            metrics=("etf_flow_usd",),
            fetch=_cg("fetch_btc_etf_flow_history"),
            parse=lambda raw: parse_ts_rows(raw, {"etf_flow_usd": "flow_usd"}),
            note="ETF 日净流（2024-01 起）",
        ),
        FetchSpec(
            key="coinbase_premium", source="coinglass", cadence="daily",
            metrics=("coinbase_premium_rate",),
            fetch=_cg("fetch_coinbase_premium", symbol="BTC", interval="1d",
                      limit=4500),
            parse=lambda raw: parse_ts_rows(
                raw, {"coinbase_premium_rate": "premium_rate"},
            ),
            note="Coinbase 现货溢价率 %（2017-08 起）；美国现货买盘强度代理",
        ),
        FetchSpec(
            key="spot_cvd", source="coinglass", cadence="daily",
            metrics=("spot_net_taker_usd",),
            fetch=_cg("fetch_spot_aggregated_cvd", symbol="BTC", interval="1d",
                      limit=4500),
            parse=parse_spot_cvd,
            note="聚合现货日净 taker 买入 USD（2017-08 起有效，早期零行已剔除）",
        ),
        FetchSpec(
            key="stablecoin_mcap", source="coinglass", cadence="daily",
            metrics=("stablecoin_total_mcap",),
            fetch=_cg("fetch_stablecoin_mcap", limit=2000),
            parse=parse_stablecoin_mcap,
        ),
        FetchSpec(
            key="fear_greed", source="coinglass", cadence="daily",
            metrics=("fear_greed",),
            fetch=_cg("fetch_fear_greed"),
            parse=parse_fear_greed,
        ),
        FetchSpec(
            key="exchange_balance", source="coinglass", cadence="daily",
            metrics=("exchange_balance_btc",),
            fetch=_cg("fetch_exchange_balance_chart", symbol="BTC"),
            parse=parse_exchange_balance,
            note="全交易所 BTC 余额合计（2024-08 起 ~2 年）",
        ),
        FetchSpec(
            key="global_m2_yoy", source="coinglass", cadence="weekly",
            metrics=("global_m2_yoy",),
            fetch=_cg("fetch_btc_vs_global_m2"),
            parse=lambda raw: parse_ts_rows(
                raw, {"global_m2_yoy": "global_m2_yoy_growth"},
            ),
            note="全球 M2 同比增速（2013 起周级，上游滞后约 1 个月）",
            staleness_days=45,
        ),
        # ── BGeometrics · Coinglass 缺失的估值/投降指标 ──
        FetchSpec(
            key="mvrv_zscore", source="bgeometrics", cadence="daily",
            metrics=("mvrv_zscore",),
            fetch=_bg("mvrv-zscore"), parse=_bg_parse("mvrv_zscore"),
        ),
        FetchSpec(
            key="sth_mvrv", source="bgeometrics", cadence="daily",
            metrics=("sth_mvrv",),
            fetch=_bg("sth-mvrv"), parse=_bg_parse("sth_mvrv"),
        ),
        FetchSpec(
            key="sopr", source="bgeometrics", cadence="daily",
            metrics=("sopr",),
            fetch=_bg("sopr"), parse=_bg_parse("sopr"),
        ),
        FetchSpec(
            key="realized_loss", source="bgeometrics", cadence="daily",
            metrics=("realized_loss",),
            fetch=_bg("realized-loss"), parse=_bg_parse("realized_loss"),
            note="日亏损兑现（负数，USD）",
        ),
        FetchSpec(
            key="realized_profit", source="bgeometrics", cadence="daily",
            metrics=("realized_profit",),
            fetch=_bg("realized-profit"), parse=_bg_parse("realized_profit"),
        ),
        FetchSpec(
            key="lth_realized_loss", source="bgeometrics", cadence="daily",
            metrics=("lth_realized_loss",),
            fetch=_bg("realized_loss_lth"), parse=_bg_parse("lth_realized_loss"),
            note="LTH 投降强度（端点命名带下划线，与其余端点不同）",
        ),
        # realized_price_bg 已停采：与 Coinglass 的 STH/LTH Realized Price
        # 信息冗余，且释放 BGeometrics 15 次/天配额（留给失败自愈重试）
        # ── Yahoo · CME 恐慌周量 ──
        FetchSpec(
            key="cme_weekly", source="yahoo_cme", cadence="weekly",
            metrics=("cme_close_1w", "cme_vol_1w"),
            required_metrics=("cme_vol_1w",),
            fetch=_yahoo_fetch, parse=parse_yahoo_weekly,
            note="BTC=F 前月合约周线（2017-12 起）；恐慌周量百分位用",
        ),
    ]
    return specs
