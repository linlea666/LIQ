"""新闻规则过滤 · Layer 1（D08·规则层）

职责：
  - 接收 RawNewsItem 列表 → 快速规则判定 keep/drop
  - 目标：过滤 70-90% 噪音（通用行情播报/广告/工具类），留 10-30% 进入 AI 层
  - 零 LLM 调用，纯 Python 规则

规则集（默认值内置，可由 config 覆盖）：
  A. 黑名单：广告/工具类关键词 → 直接 drop
  B. 白名单：FOMC/SEC/关税/war/监管/黑客/ETF/黄金/法币/暴跌/暴涨 → 强制 keep
  C. 热度门槛：view_count < N AND reliability < M → drop
  D. 语义分类：给每条附加 tier（blackswan/major/normal/minor）
  E. 地缘专用：war/ceasefire/nuclear/sanction → 强制 tier=major 最低

落实日志锚点：
  - D.D08_NEWS_PIPELINE：每次 filter 上报 input / kept / pass_rate / top drop reason
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from models.common_enums import NewsTier
from models.news_event import RawNewsItem

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 默认规则集（可被 config 覆盖）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DEFAULT_CONFIG: dict = {
    # 关键词分档
    "blackswan_keywords": [
        "战争", "全面冲突", "核武", "核战", "交易所跑路", "破产",
        "hack", "exploit", "bankruptcy", "war declared", "nuclear",
        "crash", "暴雷", "遭黑客", "协议漏洞", "资不抵债",
    ],
    "major_keywords": [
        "FOMC", "CPI", "利率", "加息", "降息", "非农", "PPI",
        "SEC", "美联储", "Fed", "Powell", "鲍威尔", "监管",
        "ETF", "现货 ETF", "批准", "关税", "tariff",
        "制裁", "sanction", "停火", "ceasefire", "空袭",
        "军事", "military", "冲突", "conflict",
        "CFTC", "政府关门", "shutdown",
        "暴跌", "暴涨", "闪崩",
    ],
    "minor_keywords": [
        "K线", "行情播报", "行情解读", "走势分析", "教程",
        "扫码", "领取", "空投领取", "抽奖", "活动",
    ],
    # 黑名单（立即 drop）
    "blacklist_keywords": [
        "扫码", "领取福利", "添加微信", "VIP群",
        "推广", "广告", "合作请联系", "加群",
        "AD·", "【推广】", "新手教程",
    ],
    # 白名单来源/作者（即便低热度也保留）
    "whitelist_authors": [
        "OKX-Industry", "BlockBeats", "PANews", "ForesightNews",
        "theblock", "CoinDesk", "Reuters",
    ],
    # 白名单关键词（强制 keep）
    "whitelist_keywords": [
        "BTC", "ETH", "bitcoin", "ethereum", "比特币", "以太坊",
        "美联储", "FOMC", "SEC", "ETF", "war", "监管",
    ],
    # 热度门槛
    "heat_threshold_view": 100,        # view_count 下限
    "heat_threshold_reliability": 0.70,  # 低于此可信度的源才适用热度门槛
    "min_title_len": 4,                  # 标题过短 drop
    "min_content_len": 0,                # 内容过短 drop（0 表示不检查）
    # 地缘主题关键词（命中至少 major）
    "geopolitical_keywords": [
        "war", "ceasefire", "nuclear", "sanction", "airstrike",
        "战争", "停火", "制裁", "空袭", "核武", "海峡",
        "冲突", "conflict", "military", "军事",
    ],
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 统计
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class FilterStats:
    """过滤统计（调用方可读）"""
    input_count: int = 0
    kept_count: int = 0
    dropped_by_blacklist: int = 0
    dropped_by_heat: int = 0
    dropped_by_length: int = 0
    dropped_by_dedupe: int = 0

    # 分档
    blackswan: int = 0
    major: int = 0
    normal: int = 0
    minor: int = 0

    # 最常见的 drop 原因（前 3）
    top_drop_reasons: list[tuple[str, int]] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if self.input_count <= 0:
            return 0.0
        return round(self.kept_count / self.input_count, 4)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def filter_news_layer1(
    items: list[RawNewsItem],
    config: Optional[dict] = None,
) -> tuple[list[RawNewsItem], dict[str, NewsTier], FilterStats]:
    """规则层过滤。

    返回：
      kept           — 通过的条目
      tier_map       — {external_id: tier}  供 Layer2 决定是否调用 AI
      stats          — 统计对象
    """
    cfg = _merge_config(config)
    stats = FilterStats()
    stats.input_count = len(items)

    kept: list[RawNewsItem] = []
    tier_map: dict[str, NewsTier] = {}
    drop_reasons: Counter[str] = Counter()
    seen_titles: set[str] = set()

    for item in items:
        drop, reason = _should_drop(item, cfg, seen_titles)
        if drop:
            drop_reasons[reason] += 1
            if reason.startswith("blacklist"):
                stats.dropped_by_blacklist += 1
            elif reason.startswith("heat"):
                stats.dropped_by_heat += 1
            elif reason.startswith("length") or reason.startswith("non_crypto"):
                stats.dropped_by_length += 1
            elif reason.startswith("dupe"):
                stats.dropped_by_dedupe += 1
            continue

        tier = _classify_tier(item, cfg)
        kept.append(item)
        tier_map[item.external_id] = tier
        _tally_tier(stats, tier)

        norm = _normalize_title(item.title)
        if norm:
            seen_titles.add(norm)

    stats.kept_count = len(kept)
    stats.top_drop_reasons = drop_reasons.most_common(3)

    _mark_d08(stats)
    return kept, tier_map, stats


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部规则
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _classify_tier(item: RawNewsItem, config: dict) -> NewsTier:
    """根据关键词 + 热度 + 来源给条目打 tier"""
    text = f"{item.title}\n{item.content}\n{item.translated_title}\n{item.translated_content}"
    lt = text.lower()

    # 多关键词命中取最严格
    if _match_any(lt, config["blackswan_keywords"]):
        return "blackswan"
    if _match_any(lt, config["major_keywords"]):
        return "major"
    # 地缘主题强制至少 major
    if _match_any(lt, config["geopolitical_keywords"]):
        return "major"
    if _match_any(lt, config["minor_keywords"]):
        # 但若高热度/高可信度则放回 normal
        if item.view_count >= 5_000 or item.source_reliability >= 0.8:
            return "normal"
        return "minor"
    return "normal"


def _should_drop(
    item: RawNewsItem,
    config: dict,
    seen_titles: set[str],
) -> tuple[bool, str]:
    """判断是否 drop

    返回 (drop, reason)
    """
    text = f"{item.title}\n{item.content}"
    lt = text.lower()

    # 白名单：优先级最高（即便命中黑名单也保留 —— 但黑名单强度高的仍先判）
    whitelisted = _is_whitelisted(item, config)

    # A. 黑名单
    if _match_any(lt, config["blacklist_keywords"]):
        # 同时白名单命中（如"比特币扫码 FOMC"这种奇葩）优先 keep
        if not whitelisted:
            return True, "blacklist:ad"

    # B. 标题过短 / 内容过短
    if len(item.title.strip()) < int(config["min_title_len"]):
        return True, "length_too_short"
    if config["min_content_len"] > 0 and len(item.content.strip()) < int(config["min_content_len"]):
        if not whitelisted:
            return True, "length_content_too_short"

    # C. 标题去重（同 batch 内）
    norm = _normalize_title(item.title)
    if norm and norm in seen_titles:
        return True, "dupe:title"

    # D. 热度门槛（仅对非白名单 & 低可信源）
    if (
        not whitelisted
        and item.source_reliability < float(config["heat_threshold_reliability"])
        and item.view_count < int(config["heat_threshold_view"])
    ):
        return True, f"heat_below:view_{int(config['heat_threshold_view'])}"

    # E. 非加密/宏观/地缘相关（粗过滤）
    if not whitelisted and not _looks_relevant(item, config):
        return True, "non_crypto_relevance"

    return False, ""


def _is_whitelisted(item: RawNewsItem, config: dict) -> bool:
    """白名单命中（强制 keep）

    关键词命中 或 source_author in whitelist 或 raw_tags 含核心币
    """
    if item.source_author in config["whitelist_authors"]:
        return True

    text = f"{item.title}\n{item.content}\n{item.translated_title}\n{item.translated_content}"
    if _match_any(text.lower(), config["whitelist_keywords"]):
        return True

    core_tokens = {"btc", "eth", "sol", "bnb", "bitcoin", "ethereum"}
    for tag in (item.raw_tags or []):
        if tag.lower() in core_tokens:
            return True

    return False


def _looks_relevant(item: RawNewsItem, config: dict) -> bool:
    """粗判条目是否与加密/宏观/地缘相关。

    策略：命中 whitelist 或 major 或 blackswan 或 geopolitical 关键词之一即视为相关。
    （这里重复 whitelist 判断是为了让兜底逻辑更清晰）
    """
    text = f"{item.title}\n{item.content}\n{item.translated_title}\n{item.translated_content}".lower()
    for key in ("whitelist_keywords", "major_keywords", "blackswan_keywords", "geopolitical_keywords"):
        if _match_any(text, config[key]):
            return True
    # 有 cashTagList 标签（$BTC 这种）则相关
    core_tokens = {"btc", "eth", "sol", "bnb", "bitcoin", "ethereum"}
    for tag in (item.raw_tags or []):
        if tag.lower() in core_tokens:
            return True
    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_NORM_RE = re.compile(r"[\s\-_—·•\.,。，；：!?!?()（）\[\]【】\"'“”‘’]+")


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    return _NORM_RE.sub("", title.strip().lower())


def _match_any(haystack: str, needles: list[str]) -> bool:
    if not haystack or not needles:
        return False
    for n in needles:
        if not n:
            continue
        if n.lower() in haystack:
            return True
    return False


def _merge_config(overrides: Optional[dict]) -> dict:
    if not overrides:
        return dict(_DEFAULT_CONFIG)
    merged = dict(_DEFAULT_CONFIG)
    for k, v in overrides.items():
        if k in merged and isinstance(merged[k], list) and isinstance(v, list):
            merged[k] = list(v)
        else:
            merged[k] = v
    return merged


def _tally_tier(stats: FilterStats, tier: NewsTier) -> None:
    if tier == "blackswan":
        stats.blackswan += 1
    elif tier == "major":
        stats.major += 1
    elif tier == "minor":
        stats.minor += 1
    else:
        stats.normal += 1


def _mark_d08(stats: FilterStats) -> None:
    try:
        from utils.decision_tracker import D, get_tracker
        get_tracker().mark(
            D.D08_NEWS_PIPELINE,
            status="ok" if stats.input_count == 0 or stats.kept_count > 0 else "warn",
            log=False,
            input=stats.input_count,
            kept=stats.kept_count,
            pass_rate=stats.pass_rate,
            blackswan=stats.blackswan,
            major=stats.major,
            normal=stats.normal,
            minor=stats.minor,
            top_drop=[{"reason": r, "count": c} for r, c in stats.top_drop_reasons[:3]],
        )
    except Exception:  # noqa: BLE001
        logger.debug("[D08] mark failed", exc_info=True)
