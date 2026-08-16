"""容错解析：币安响应 → TokenObservation。

为什么必须逐端点建字段映射表：
同一个业务概念在不同端点用了完全不同的字段名，例如"狙击者持仓占比"
  trending  → sniperHoldersPercent
  meme_rush → holdersSniperPercent
  detail    → sniperHoldingPercent
"Top10 占比"更是三种写法（holdersTop10Percent / top10HoldersPercentage）。
如果不集中管理，日后一定会出现某个链某个端点悄悄解析成 None
而评分照常输出的情况——这正是最危险的失效模式。

解析纪律：
  - 任何字段解析失败 → None（UNKNOWN），绝不填 0，绝不抛异常中断整批。
  - 记录未知字段（schema drift），接口改版时能主动发现而不是被动踩坑。
  - 单位语义可疑的字段直接不解析（如 inflow 的 priceChangeRate），
    宁可少一个特征，也不能让口径不明的数污染筹码/动量判断。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from ..domain.models import INTERVAL_LOOKBACK_MS, TokenObservation
from ..domain.tags import BOOL_TAG_FIELDS, RAW_TAG_MAP
from .coerce import (
    first_not_none,
    to_bool,
    to_int,
    to_non_negative_float,
    to_non_negative_int,
    to_percent,
    to_positive_float,
    to_ratio,
    to_signed_percent,
    to_text,
    to_timestamp_ms,
)
from .endpoints import STAGE_NAMES

logger = logging.getLogger("radar.parsers")

# 与解析规则绑定的版本号：字段映射有任何改动都必须递增，
# 否则历史快照无法追溯到"当时用哪套规则解析的"。
PARSER_VERSION = "p1.1.0"  # chart1h 极值按 INTERVAL_LOOKBACK_MS 裁剪

SUCCESS_CODE = "000000"


class SchemaDrift(Exception):
    """响应结构与预期不符（不是字段缺失，而是整体形状变了）。"""


def check_envelope(payload: Any) -> list[Any] | dict[str, Any]:
    """校验响应外层信封并返回 data。

    code != 000000 时抛出，由客户端统一转成 API_REQUEST_FAILED，
    避免每个采集器各写一遍判断。
    """
    if not isinstance(payload, dict):
        raise SchemaDrift(f"响应不是 JSON 对象: {type(payload).__name__}")
    code = payload.get("code")
    if code is not None and str(code) != SUCCESS_CODE:
        message = to_text(payload.get("message")) or to_text(payload.get("messageDetail")) or ""
        raise SchemaDrift(f"业务码 {code}: {message}")
    if payload.get("success") is False:
        raise SchemaDrift("success=false")
    data = payload.get("data")
    if data is None:
        raise SchemaDrift("data 为空")
    return data


# data 中列表所在的键，按端点区分（顺序即优先级）
_ROW_KEYS: dict[str, tuple[str, ...]] = {
    "trending": ("tokens",),
    "meme_rank": ("tokens",),
    "meme_rush": (),          # data 本身就是列表
    "inflow": (),
    "signal": (),
    "social": ("leaderBoardList",),
}


def extract_rows(endpoint_name: str, data: Any) -> list[dict[str, Any]]:
    """从 data 中取出行列表。形状不符时抛 SchemaDrift。"""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in _ROW_KEYS.get(endpoint_name, ()):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        # 兜底：扫描所有值找第一个字典列表，接口小改名时仍能工作
        for key, value in data.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                logger.warning("端点 %s 的列表键疑似变更为 %s", endpoint_name, key)
                return [r for r in value if isinstance(r, dict)]
        return []
    raise SchemaDrift(f"{endpoint_name}: data 形状异常 {type(data).__name__}")


# ─────────────────────────────────────────────────────────────────────────
# 标签
# ─────────────────────────────────────────────────────────────────────────

def _collect_tags(row: dict[str, Any]) -> tuple[str, ...]:
    """收集标签：既解析 tokenTag/tagInfoList 结构，也解析布尔型 tagXxx 字段。"""
    tags: set[str] = set()

    for container_key in ("tokenTag", "tagInfoList"):
        container = row.get(container_key)
        if not isinstance(container, dict):
            continue
        for category, entries in container.items():
            category_text = to_text(category)
            if category_text:
                tags.add(f"CAT:{category_text}")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = to_text(entry.get("tagName"))
                if not name:
                    continue
                mapped = RAW_TAG_MAP.get(name.lower())
                tags.add(mapped or f"TAG:{name}")

    # meme_rush 的显式风险标签：非空即命中
    for field, tag in BOOL_TAG_FIELDS:
        value = row.get(field)
        if value not in (None, "", 0, False, "0"):
            tags.add(tag)

    return tuple(sorted(tags))


# ─────────────────────────────────────────────────────────────────────────
# 免费风险数据：trending 的 auditInfo / inflow 的 tokenRiskLevel
# ─────────────────────────────────────────────────────────────────────────

def _parse_inline_audit(row: dict[str, Any]) -> tuple[int | None, tuple[str, ...]]:
    """从列表行里提取内联风险信息，省下专用审计接口的配额。

    注意 riskLevel 语义：0/1=低 2/3=中 4/5=高，-1 表示"没有结果"。
    -1 必须映射成 None（UNKNOWN），绝不能当作低风险。
    """
    level: int | None = None
    codes: tuple[str, ...] = ()

    audit = row.get("auditInfo")
    if isinstance(audit, dict):
        level = to_int(audit.get("riskLevel"))
        raw_codes = audit.get("riskCodes")
        if isinstance(raw_codes, list):
            codes = tuple(str(c) for c in raw_codes if c)
    if level is None:
        level = to_int(row.get("tokenRiskLevel"))
        raw_codes = row.get("tokenRiskCodes")
        if isinstance(raw_codes, list) and raw_codes:
            codes = tuple(str(c) for c in raw_codes if c)

    if level is not None and level < 0:
        level = None
    return level, codes


def parse_chart_extremes(
    raw: Any, *, observed_at: int | None = None,
) -> tuple[float | None, float | None, float | None]:
    """解析 trending 的 chart1h（JSON 字符串，含 60 个一分钟点）。

    返回 (区间最高价, 区间最低价, 区间成交额)。

    这是低成本填补轮询间隙的关键：轮询间隔 30~150 秒时，
    如果只记录轮询瞬间的价格，一个 3 分钟内拉升 5 倍又砸回的币
    会被完全漏掉，导致 Outcome 严重低估。
    原始序列提取极值后立即丢弃，不入库（否则体积不可控）。

    序列覆盖过去 60 分钟，但本次轮询真正的"间隙"只有最近一两分钟——
    传入 observed_at 时只保留 INTERVAL_LOOKBACK_MS 内的点（键为毫秒时间戳）。
    不裁剪的后果已被实盘证实：警报之前的拉盘顶会被灌进警报之后的 MFE，
    伪造出数百倍的假收益。时间戳无法解析的点直接丢弃（诚实的少算，
    优先于把来历不明的价格算进极值）。
    """
    if not raw:
        return None, None, None
    obj: Any = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError):
            return None, None, None
    if not isinstance(obj, dict):
        return None, None, None

    cutoff = None if observed_at is None else observed_at - INTERVAL_LOOKBACK_MS

    def recent_values(series: Any) -> list[Any]:
        if not isinstance(series, dict) or not series:
            return []
        if cutoff is None:
            return list(series.values())
        kept = []
        for key, value in series.items():
            ts = to_int(key)
            if ts is None or ts < cutoff:
                continue
            kept.append(value)
        return kept

    high = low = None
    values = [v for v in (to_positive_float(p) for p in recent_values(obj.get("p")))
              if v is not None]
    if values:
        high, low = max(values), min(values)

    volume = None
    vol_values = [v for v in (to_non_negative_float(x) for x in recent_values(obj.get("v")))
                  if v is not None]
    if vol_values:
        volume = sum(vol_values)

    return high, low, volume


# ─────────────────────────────────────────────────────────────────────────
# 各端点解析器
# ─────────────────────────────────────────────────────────────────────────

def _contract_of(row: dict[str, Any]) -> str | None:
    return to_text(
        first_not_none(row.get("contractAddress"), row.get("ca"), row.get("tokenAddress")),
        max_len=120,
    )


def parse_trending_row(chain_id: str, row: dict[str, Any], observed_at: int) -> TokenObservation | None:
    contract = _contract_of(row)
    if not contract:
        return None

    meta = row.get("metaInfo") if isinstance(row.get("metaInfo"), dict) else {}
    high, low, chart_volume = parse_chart_extremes(
        row.get("chart1h"), observed_at=observed_at,
    )
    audit_level, audit_codes = _parse_inline_audit(row)

    return TokenObservation(
        chain_id=chain_id,
        contract_address=contract,
        endpoint="trending",
        observed_at=observed_at,
        parser_version=PARSER_VERSION,
        symbol=to_text(first_not_none(row.get("symbol"), meta.get("originSymbol"))),
        name=to_text(first_not_none(meta.get("name"), meta.get("originName")), max_len=120),
        decimals=to_int(row.get("decimals")),
        launch_time_ms=to_timestamp_ms(row.get("launchTime")),

        price=to_positive_float(row.get("price")),
        market_cap=to_positive_float(row.get("marketCap")),
        liquidity=to_non_negative_float(row.get("liquidity")),
        volume_5m=to_non_negative_float(row.get("volume5m")),
        volume_1h=to_non_negative_float(row.get("volume1h")),
        volume_4h=to_non_negative_float(row.get("volume4h")),
        volume_24h=to_non_negative_float(row.get("volume24h")),
        volume_1h_buy=to_non_negative_float(row.get("volume1hBuy")),
        volume_1h_sell=to_non_negative_float(row.get("volume1hSell")),
        count_5m=to_non_negative_int(row.get("count5m")),
        count_1h=to_non_negative_int(row.get("count1h")),
        count_1h_buy=to_non_negative_int(row.get("count1hBuy")),
        count_1h_sell=to_non_negative_int(row.get("count1hSell")),
        unique_trader_5m=to_non_negative_int(row.get("uniqueTrader5m")),
        unique_trader_1h=to_non_negative_int(row.get("uniqueTrader1h")),
        unique_trader_24h=to_non_negative_int(row.get("uniqueTrader24h")),
        pct_change_5m=to_signed_percent(row.get("percentChange5m")),
        pct_change_1h=to_signed_percent(row.get("percentChange1h")),
        pct_change_4h=to_signed_percent(row.get("percentChange4h")),
        pct_change_24h=to_signed_percent(row.get("percentChange24h")),
        interval_high=high,
        interval_low=low,
        interval_volume=chart_volume,

        holders=to_non_negative_int(row.get("holders")),
        kyc_holders=to_non_negative_int(row.get("kycHolders")),
        top10_percent=to_percent(row.get("holdersTop10Percent")),
        dev_percent=to_percent(row.get("devHoldingPercent")),
        sniper_percent=to_percent(row.get("sniperHoldersPercent")),
        insider_percent=to_percent(row.get("insiderHoldingPercent")),
        bundler_percent=to_percent(row.get("bundlesHoldingPercent")),
        new_wallet_percent=to_percent(row.get("newAddressHoldersPercent")),
        smart_money_percent=to_percent(row.get("smartMoneyHoldingPercent")),
        kol_percent=to_percent(row.get("kolHoldingPercent")),
        pro_percent=to_percent(row.get("proHoldersPercent")),

        search_count_24h=to_non_negative_int(row.get("searchCount24h")),
        audit_risk_level=audit_level,
        audit_risk_codes=audit_codes,
        tags=_collect_tags(row),
        seen_on_trending=True,
    )


def parse_meme_rush_row(chain_id: str, row: dict[str, Any], observed_at: int,
                        stage: int) -> TokenObservation | None:
    """meme_rush 是筹码维度最全的端点，被动更新的主力来源。

    注意 volume/count 没有时间窗标注——对刚创建几分钟的币它等于"自上线累计"，
    因此绝不能映射成 volume_1h（那会让 3 天前迁移的币口径完全错乱）。
    统一存入 volume_agg / count_agg，由特征引擎结合代币年龄使用。
    """
    contract = _contract_of(row)
    if not contract:
        return None

    twitter = row.get("twitterInfo") if isinstance(row.get("twitterInfo"), dict) else {}
    migrate_status = to_int(row.get("migrateStatus"))
    stage_name = STAGE_NAMES.get(stage)
    if migrate_status == 1:
        stage_name = "migrated"

    return TokenObservation(
        chain_id=chain_id,
        contract_address=contract,
        endpoint="meme_rush",
        observed_at=observed_at,
        parser_version=PARSER_VERSION,
        symbol=to_text(row.get("symbol")),
        name=to_text(row.get("name"), max_len=120),
        decimals=to_int(row.get("decimals")),
        launch_time_ms=to_timestamp_ms(row.get("createTime")),
        creator_address=to_text(row.get("devAddress"), max_len=120),
        launch_platform=to_text(row.get("protocol"), max_len=40),
        stage=stage_name,
        protocol=to_text(row.get("protocol"), max_len=40),
        pool_address=to_text(row.get("pairAnchorAddress"), max_len=120),

        price=to_positive_float(row.get("price")),
        market_cap=to_positive_float(row.get("marketCap")),
        liquidity=to_non_negative_float(row.get("liquidity")),
        volume_agg=to_non_negative_float(row.get("volume")),
        count_agg=to_non_negative_int(row.get("count")),
        count_agg_buy=to_non_negative_int(row.get("countBuy")),
        count_agg_sell=to_non_negative_int(row.get("countSell")),
        pct_change_agg=to_signed_percent(row.get("priceChange")),

        holders=to_non_negative_int(row.get("holders")),
        top10_percent=to_percent(row.get("holdersTop10Percent")),
        dev_percent=to_percent(row.get("holdersDevPercent")),
        sniper_percent=to_percent(row.get("holdersSniperPercent")),
        insider_percent=to_percent(row.get("holdersInsiderPercent")),
        bundler_percent=to_percent(row.get("bundlerHoldingPercent")),
        new_wallet_percent=to_percent(row.get("newWalletHoldingPercent")),
        kol_percent=to_percent(row.get("kolHoldingPercent")),
        pro_percent=to_percent(row.get("proHoldingPercent")),

        buy_tax_pct=to_percent(row.get("taxRateBuy")),
        sell_tax_pct=to_percent(row.get("taxRateSell")),

        bonding_progress=to_percent(row.get("progress")),
        migrate_status=migrate_status,
        migrate_time_ms=to_timestamp_ms(row.get("migrateTime")),
        sniper_count=to_non_negative_int(row.get("sniperCount")),
        dev_sell_percent=to_percent(row.get("devSellPercent")),
        twitter_followers=to_non_negative_int(twitter.get("followersCnt")),
        tags=_collect_tags(row),
    )


def parse_meme_rank_row(chain_id: str, row: dict[str, Any],
                        observed_at: int) -> TokenObservation | None:
    """meme_rank 仅 BSC 支持，提供币安自有的综合分与 7 日维度。"""
    contract = _contract_of(row)
    if not contract:
        return None

    return TokenObservation(
        chain_id=chain_id,
        contract_address=contract,
        endpoint="meme_rank",
        observed_at=observed_at,
        parser_version=PARSER_VERSION,
        symbol=to_text(row.get("symbol")),
        launch_time_ms=to_timestamp_ms(row.get("createTime")),
        launch_platform=to_text(row.get("protocol"), max_len=40),
        migrate_time_ms=to_timestamp_ms(row.get("migrateTime")),

        price=to_positive_float(row.get("price")),
        market_cap=to_positive_float(row.get("marketCap")),
        liquidity=to_non_negative_float(row.get("liquidity")),
        volume_agg=to_non_negative_float(row.get("volume")),
        count_agg=to_non_negative_int(row.get("count")),
        pct_change_agg=to_signed_percent(row.get("percentChange")),
        unique_trader_24h=to_non_negative_int(row.get("uniqueTrader24h")),

        holders=to_non_negative_int(row.get("holders")),
        kyc_holders=to_non_negative_int(row.get("kycHolders")),
        top10_percent=to_percent(row.get("holdersTop10Percent")),

        binance_score=to_non_negative_float(row.get("score")),
        tags=_collect_tags(row),
    )


def parse_inflow_row(chain_id: str, row: dict[str, Any], observed_at: int,
                     *, period: str = "1h") -> TokenObservation | None:
    """聪明钱净流入榜。

    period 决定 volume/count 的时间窗；只有 period=1h 时才映射到 1h 列，
    否则只保留 inflow 本身，避免时间窗错配污染成交特征。

    刻意不解析 priceChangeRate：其单位（比率还是百分比）无法从响应确认，
    而涨跌幅在 trending / meme_rush 都有明确口径的版本可用。
    """
    contract = _contract_of(row)
    if not contract:
        return None

    audit_level, audit_codes = _parse_inline_audit(row)
    is_1h = period == "1h"

    return TokenObservation(
        chain_id=chain_id,
        contract_address=contract,
        endpoint="inflow",
        observed_at=observed_at,
        parser_version=PARSER_VERSION,
        symbol=to_text(row.get("tokenName")),
        decimals=to_int(row.get("tokenDecimals")),
        launch_time_ms=to_timestamp_ms(row.get("launchTime")),
        launch_platform=to_text(row.get("protocol"), max_len=40),

        price=to_positive_float(row.get("price")),
        market_cap=to_positive_float(row.get("marketCap")),
        liquidity=to_non_negative_float(row.get("liquidity")),
        volume_1h=to_non_negative_float(row.get("volume")) if is_1h else None,
        count_1h=to_non_negative_int(row.get("count")) if is_1h else None,
        count_1h_buy=to_non_negative_int(row.get("countBuy")) if is_1h else None,
        count_1h_sell=to_non_negative_int(row.get("countSell")) if is_1h else None,

        holders=to_non_negative_int(row.get("holders")),
        kyc_holders=to_non_negative_int(row.get("kycHolders")),
        top10_percent=to_percent(row.get("holdersTop10Percent")),

        net_inflow=to_ratio(row.get("inflow"), limit=1e12),
        smart_money_traders=to_non_negative_int(row.get("traders")),

        audit_risk_level=audit_level,
        audit_risk_codes=audit_codes,
        tags=_collect_tags(row),
    )


def parse_signal_row(chain_id: str, row: dict[str, Any],
                     observed_at: int) -> TokenObservation | None:
    """聪明钱信号。

    关键：exitRate=100 表示聪明钱已全部离场，是强烈的**负面**信号。
    如果只看 smartMoneyCount 就当利好，会在别人跑完之后接盘。
    """
    contract = _contract_of(row)
    if not contract:
        return None

    direction = (to_text(row.get("direction")) or "").lower()

    return TokenObservation(
        chain_id=chain_id,
        contract_address=contract,
        endpoint="signal",
        observed_at=observed_at,
        parser_version=PARSER_VERSION,
        symbol=to_text(row.get("ticker")),
        decimals=to_int(row.get("tokenDecimals")),
        launch_platform=to_text(row.get("launchPlatform"), max_len=40),

        price=to_positive_float(row.get("currentPrice")),
        market_cap=to_positive_float(row.get("currentMarketCap")),

        smart_money_count=to_non_negative_int(row.get("smartMoneyCount")),
        exit_rate=to_percent(row.get("exitRate")),
        max_gain=to_ratio(row.get("maxGain")),
        alert_market_cap=to_positive_float(row.get("alertMarketCap")),
        signal_direction=direction or None,
        signal_type=to_text(row.get("smartSignalType"), max_len=40),
        signal_triggered_at=to_timestamp_ms(row.get("signalTriggerTime")),
        signal_status=to_text(row.get("status"), max_len=40),
        tags=_collect_tags(row),
    )


def parse_social_row(chain_id: str, row: dict[str, Any],
                     observed_at: int) -> TokenObservation | None:
    """社交热度榜。结构是嵌套的 metaInfo / marketInfo / socialHypeInfo。"""
    meta = row.get("metaInfo") if isinstance(row.get("metaInfo"), dict) else {}
    contract = _contract_of(meta) or _contract_of(row)
    if not contract:
        return None

    market = row.get("marketInfo") if isinstance(row.get("marketInfo"), dict) else {}
    hype = row.get("socialHypeInfo") if isinstance(row.get("socialHypeInfo"), dict) else {}

    return TokenObservation(
        chain_id=chain_id,
        contract_address=contract,
        endpoint="social",
        observed_at=observed_at,
        parser_version=PARSER_VERSION,
        symbol=to_text(meta.get("symbol")),
        decimals=to_int(first_not_none(meta.get("decimals"), meta.get("decimal"))),
        # metaInfo.tokenAge 实际是创建时间戳，不是年龄
        launch_time_ms=to_timestamp_ms(meta.get("tokenAge")),

        market_cap=to_positive_float(market.get("marketCap")),
        social_hype=to_non_negative_float(hype.get("socialHype")),
        social_hype_cn=to_non_negative_float(hype.get("socialHypeCn")),
        social_hype_en=to_non_negative_float(hype.get("socialHypeEn")),
        sentiment=to_text(hype.get("sentiment"), max_len=20),
        tags=_collect_tags(row),
    )


def parse_detail(chain_id: str, contract_address: str, data: dict[str, Any],
                 observed_at: int) -> TokenObservation:
    """单币详情（dynamic/info）。字段最全，但对极新的币大量为 null。

    price 经常为 null 而 aggPrice 有值，因此必须回退；
    marketCap 同样可能缺失，由质量层用 price × circulatingSupply 交叉校验。
    """
    return TokenObservation(
        chain_id=chain_id,
        contract_address=contract_address,
        endpoint="detail",
        observed_at=observed_at,
        parser_version=PARSER_VERSION,
        launch_time_ms=to_timestamp_ms(data.get("launchTime")),

        price=to_positive_float(first_not_none(data.get("price"), data.get("aggPrice"))),
        market_cap=to_positive_float(data.get("marketCap")),
        fdv=to_positive_float(data.get("fdv")),
        liquidity=to_non_negative_float(data.get("liquidity")),
        volume_5m=to_non_negative_float(data.get("volume5m")),
        volume_1h=to_non_negative_float(data.get("volume1h")),
        volume_4h=to_non_negative_float(data.get("volume4h")),
        volume_24h=to_non_negative_float(data.get("volume24h")),
        volume_1h_buy=to_non_negative_float(data.get("volume1hBuy")),
        volume_1h_sell=to_non_negative_float(data.get("volume1hSell")),
        count_5m=to_non_negative_int(data.get("count5m")),
        count_1h=to_non_negative_int(data.get("count1h")),
        count_1h_buy=to_non_negative_int(data.get("count1hBuy")),
        count_1h_sell=to_non_negative_int(data.get("count1hSell")),
        pct_change_5m=to_signed_percent(data.get("percentChange5m")),
        pct_change_1h=to_signed_percent(data.get("percentChange1h")),
        pct_change_4h=to_signed_percent(data.get("percentChange4h")),
        pct_change_24h=to_signed_percent(data.get("percentChange24h")),
        price_high_24h=to_positive_float(data.get("priceHigh24h")),
        price_low_24h=to_positive_float(data.get("priceLow24h")),

        circulating_supply=to_positive_float(data.get("circulatingSupply")),
        total_supply=to_positive_float(data.get("totalSupply")),
        max_supply=to_positive_float(data.get("maxSupply")),

        holders=to_non_negative_int(data.get("holders")),
        kyc_holders=to_non_negative_int(data.get("kycHolderCount")),
        top10_percent=to_percent(data.get("top10HoldersPercentage")),
        dev_percent=to_percent(
            first_not_none(data.get("devHoldingPercent"), data.get("holdersDevPercent"))
        ),
        sniper_percent=to_percent(data.get("sniperHoldingPercent")),
        insider_percent=to_percent(data.get("insiderHoldingPercent")),
        bundler_percent=to_percent(data.get("bundlerHoldingPercent")),
        new_wallet_percent=to_percent(data.get("newWalletHoldingPercent")),
        smart_money_percent=to_percent(
            first_not_none(
                data.get("smartMoneyHoldingPercent"), data.get("holdersSmartMoneyPercent")
            )
        ),
        kol_percent=to_percent(data.get("kolHoldingPercent")),
        pro_percent=to_percent(data.get("proHoldingPercent")),

        bonding_progress=to_percent(data.get("progress")),
        migrate_status=to_int(data.get("migrateStatus")),
        migrate_time_ms=to_timestamp_ms(data.get("migrateTime")),
        tags=_collect_tags(data),
    )


def parse_meta(chain_id: str, contract_address: str, data: dict[str, Any],
               observed_at: int) -> TokenObservation:
    """静态元信息，主要用于补齐 symbol / name / decimals / 创建时间。"""
    return TokenObservation(
        chain_id=chain_id,
        contract_address=contract_address,
        endpoint="meta",
        observed_at=observed_at,
        parser_version=PARSER_VERSION,
        symbol=to_text(first_not_none(data.get("symbol"), data.get("originSymbol"))),
        name=to_text(first_not_none(data.get("name"), data.get("originName")), max_len=120),
        decimals=to_int(first_not_none(data.get("decimals"), data.get("decimal"))),
        launch_time_ms=to_timestamp_ms(
            first_not_none(data.get("launchTime"), data.get("createTime"))
        ),
        creator_address=to_text(
            first_not_none(data.get("devAddress"), data.get("creator")), max_len=120
        ),
        total_supply=to_positive_float(data.get("totalSupply")),
        max_supply=to_positive_float(data.get("maxSupply")),
        circulating_supply=to_positive_float(data.get("circulatingSupply")),
        tags=_collect_tags(data),
    )


# 命中即视为致命风险的审计条目关键字（标题为英文文案，做小写包含匹配）
_HONEYPOT_KEYWORDS = ("honeypot", "cannot sell", "can not be sold", "trading disabled")


def parse_audit(chain_id: str, contract_address: str, data: dict[str, Any],
                observed_at: int) -> TokenObservation:
    """安全审计。

    极其重要的语义细节：hasResult=false 或 isSupported=false 时，
    riskLevel 会返回 -1 且 riskItems 为空。这时必须解析成 UNKNOWN，
    绝不能因为"没有命中风险项"就当成安全——刚创建几分钟的币
    几乎全部落在这种状态，若按安全处理等于把风险门整个绕过去。
    """
    has_result = to_bool(data.get("hasResult"))
    is_supported = to_bool(data.get("isSupported"))
    usable = bool(has_result) and bool(is_supported)

    if not usable:
        return TokenObservation(
            chain_id=chain_id,
            contract_address=contract_address,
            endpoint="audit",
            observed_at=observed_at,
            parser_version=PARSER_VERSION,
            audit_available=False,
        )

    level = to_int(data.get("riskLevel"))
    if level is not None and level < 0:
        level = None

    extra = data.get("extraInfo") if isinstance(data.get("extraInfo"), dict) else {}
    hit_titles: list[str] = []
    honeypot = False
    for item in data.get("riskItems") or []:
        if not isinstance(item, dict):
            continue
        for detail in item.get("details") or []:
            if not isinstance(detail, dict) or not to_bool(detail.get("isHit")):
                continue
            title = to_text(detail.get("title")) or ""
            risk_type = (to_text(detail.get("riskType")) or "").upper()
            hit_titles.append(f"{item.get('id')}:{title}")
            lowered = title.lower()
            if risk_type == "RISK" and any(k in lowered for k in _HONEYPOT_KEYWORDS):
                honeypot = True

    return TokenObservation(
        chain_id=chain_id,
        contract_address=contract_address,
        endpoint="audit",
        observed_at=observed_at,
        parser_version=PARSER_VERSION,
        audit_available=True,
        audit_risk_level=level,
        audit_risk_codes=tuple(hit_titles[:20]),
        buy_tax_pct=to_percent(extra.get("buyTax")),
        sell_tax_pct=to_percent(extra.get("sellTax")),
        honeypot=honeypot,
        contract_verified=to_bool(extra.get("isVerified")),
    )


# ─────────────────────────────────────────────────────────────────────────
# Schema drift 检测
# ─────────────────────────────────────────────────────────────────────────

# 各端点已知字段集。新增未知字段本身无害（我们会忽略），
# 但**已知字段消失**意味着解析将静默返回 None，必须立刻告警。
REQUIRED_KEYS: dict[str, frozenset[str]] = {
    "trending": frozenset({"contractAddress", "price", "marketCap", "liquidity", "holders"}),
    "meme_rush": frozenset({"contractAddress", "price", "marketCap", "holders", "progress"}),
    "meme_rank": frozenset({"contractAddress", "price", "marketCap", "holders"}),
    "inflow": frozenset({"ca", "price", "marketCap", "inflow"}),
    "signal": frozenset({"contractAddress", "smartMoneyCount", "exitRate"}),
    "social": frozenset({"metaInfo", "socialHypeInfo"}),
}


def detect_missing_keys(endpoint_name: str, rows: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """检查必需字段是否整体消失（抽样前若干行即可判断）。

    只有"所有采样行都缺该字段"才算 drift；单行缺失是正常的数据稀疏。
    """
    required = REQUIRED_KEYS.get(endpoint_name)
    if not required:
        return ()
    sample = [r for _, r in zip(range(10), rows)]
    if not sample:
        return ()
    missing = tuple(
        sorted(key for key in required if all(key not in row for row in sample))
    )
    return missing
