"""领域模型：解析输出、代币视图、特征、评分。

这是解析器 → 特征引擎 → 评分器 → 存储 之间的唯一契约。
Replay 引擎重放历史时也走同一套模型，保证"线上"与"回测"
不会各写一套特征代码而产生口径分叉。

关键约定：
  - 所有指标字段默认 None，代表 UNKNOWN；绝不用 0 表示"没拿到"。
  - 观测按「字段组」记录新鲜度，使得"聪明钱接口挂了 20 分钟"
    能够精确地只降低相关维度的可信度，而不是一刀切。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FieldGroup(str, Enum):
    """观测字段分组。DataQuality 按组判断新鲜度与缺失。"""

    MARKET = "market"              # 价格、市值、流动性、成交
    HOLDERS = "holders"            # 持有人数
    DISTRIBUTION = "distribution"  # Top10 / dev / sniper / insider / bundler
    SMART_MONEY = "smart_money"    # 聪明钱数量、exitRate、净流入
    SOCIAL = "social"              # 社交热度、搜索量
    AUDIT = "audit"                # 安全审计、税率
    SUPPLY = "supply"              # 供应量


class TokenState(str, Enum):
    """机器状态机。与用户工作流状态（警报的 review_state）严格分离。"""

    DISCOVERED = "DISCOVERED"
    WATCHING = "WATCHING"
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    # V1 状态机不会产出 MOMENTUM：S2 + DISTRIBUTION 已覆盖可操作的生命周期，
    # 再插一个同级状态只会增加抖动面。保留枚举值是为了 V1.5 引入
    # "S2 中的强势扩张子状态"时，历史数据与前端不需要做状态值迁移。
    MOMENTUM = "MOMENTUM"
    DISTRIBUTION = "DISTRIBUTION"
    DORMANT = "DORMANT"          # 沉寂，可复活
    DEAD = "DEAD"                # 硬性死亡：蜜罐/Rug/LP 清空
    BLOCKED = "BLOCKED"          # 命中 Execution Blocker

    @property
    def rank(self) -> int:
        return _STATE_RANK.get(self, 0)

    @property
    def is_active(self) -> bool:
        return self not in (TokenState.DORMANT, TokenState.DEAD, TokenState.BLOCKED)


_STATE_RANK: dict[TokenState, int] = {
    TokenState.DEAD: -2,
    TokenState.BLOCKED: -2,
    TokenState.DORMANT: -1,
    TokenState.DISCOVERED: 0,
    TokenState.WATCHING: 1,
    TokenState.S0: 2,
    TokenState.S1: 3,
    TokenState.S2: 4,
    TokenState.MOMENTUM: 4,
    TokenState.DISTRIBUTION: 3,
}


@dataclass(slots=True)
class TokenObservation:
    """一次归一化后的观测。

    来自列表类接口时字段较少（但已足够做被动更新），
    来自单币详情接口时字段最全。合并策略在 TokenView.apply。
    """

    chain_id: str
    contract_address: str
    endpoint: str
    observed_at: int
    source_at: int | None = None
    latency_ms: int | None = None
    response_hash: str | None = None
    parser_version: str = ""

    # ── 身份 ────────────────────────────────────────────────────────────
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    launch_time_ms: int | None = None
    creator_address: str | None = None
    launch_platform: str | None = None
    stage: str | None = None            # new | finalizing | migrated
    pool_address: str | None = None
    quote_asset: str | None = None
    protocol: str | None = None

    # ── 市场 ────────────────────────────────────────────────────────────
    price: float | None = None
    market_cap: float | None = None
    fdv: float | None = None
    liquidity: float | None = None
    volume_5m: float | None = None
    volume_1h: float | None = None
    volume_4h: float | None = None
    volume_24h: float | None = None
    volume_1h_buy: float | None = None
    volume_1h_sell: float | None = None
    count_5m: int | None = None
    count_1h: int | None = None
    count_1h_buy: int | None = None
    count_1h_sell: int | None = None
    unique_trader_5m: int | None = None
    unique_trader_1h: int | None = None
    unique_trader_24h: int | None = None
    pct_change_5m: float | None = None
    pct_change_1h: float | None = None
    pct_change_4h: float | None = None
    pct_change_24h: float | None = None
    # trending 的 chart1h 一分钟序列提取出的区间极值（回填轮询间隙）
    interval_high: float | None = None
    interval_low: float | None = None
    interval_volume: float | None = None
    # detail 返回的 24 小时极值；对上线不足 24h 的币即历史极值
    price_high_24h: float | None = None
    price_low_24h: float | None = None
    # meme_rush / meme_rank 的 volume/count 无时间窗标注，
    # 对新币等于"自上线累计"，单独存放避免与 1h 口径混淆
    volume_agg: float | None = None
    count_agg: int | None = None
    count_agg_buy: int | None = None
    count_agg_sell: int | None = None
    pct_change_agg: float | None = None

    # ── 供应 ────────────────────────────────────────────────────────────
    circulating_supply: float | None = None
    total_supply: float | None = None
    max_supply: float | None = None

    # ── 持有人与筹码 ────────────────────────────────────────────────────
    holders: int | None = None
    kyc_holders: int | None = None
    top10_percent: float | None = None
    dev_percent: float | None = None
    sniper_percent: float | None = None
    insider_percent: float | None = None
    bundler_percent: float | None = None
    new_wallet_percent: float | None = None
    smart_money_percent: float | None = None
    kol_percent: float | None = None
    pro_percent: float | None = None

    # ── 生命周期 ────────────────────────────────────────────────────────
    bonding_progress: float | None = None   # bonding curve 进度 0-100
    migrate_status: int | None = None        # 0=未迁移 1=已迁移
    migrate_time_ms: int | None = None
    sniper_count: int | None = None
    dev_sell_percent: float | None = None
    twitter_followers: int | None = None
    binance_score: float | None = None       # meme_rank 的币安综合分

    # ── 聪明钱信号 ──────────────────────────────────────────────────────
    smart_money_count: int | None = None
    smart_money_traders: int | None = None
    exit_rate: float | None = None
    max_gain: float | None = None
    alert_market_cap: float | None = None
    net_inflow: float | None = None
    signal_direction: str | None = None      # buy | sell
    signal_type: str | None = None           # SMART_MONEY | SMART_KOL
    signal_triggered_at: int | None = None
    signal_status: str | None = None

    # ── 社交 ────────────────────────────────────────────────────────────
    social_hype: float | None = None
    social_hype_cn: float | None = None
    social_hype_en: float | None = None
    kol_count: int | None = None
    search_count_24h: int | None = None
    sentiment: str | None = None

    # ── 审计 ────────────────────────────────────────────────────────────
    # audit_available=False 表示币安明确"没有审计结果"，
    # 与"审计通过"是完全不同的事实，风险门必须区别对待
    audit_available: bool | None = None
    audit_risk_level: int | None = None
    audit_risk_codes: tuple[str, ...] = ()
    buy_tax_pct: float | None = None
    sell_tax_pct: float | None = None
    honeypot: bool | None = None
    contract_verified: bool | None = None

    # ── 标签与来源 ──────────────────────────────────────────────────────
    tags: tuple[str, ...] = ()
    # 本次观测是否出现在热门榜（用于 Signal Lead Time）
    seen_on_trending: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.chain_id, self.contract_address)


# 各字段组包含的观测字段名，用于判定"本次观测刷新了哪些组"
_GROUP_FIELDS: dict[FieldGroup, tuple[str, ...]] = {
    FieldGroup.MARKET: (
        "price", "market_cap", "fdv", "liquidity",
        "volume_5m", "volume_1h", "volume_4h", "volume_24h",
        "volume_1h_buy", "volume_1h_sell", "volume_agg",
        "count_5m", "count_1h", "count_1h_buy", "count_1h_sell",
        "count_agg", "count_agg_buy", "count_agg_sell",
        "unique_trader_5m", "unique_trader_1h", "unique_trader_24h",
        "pct_change_5m", "pct_change_1h", "pct_change_4h", "pct_change_24h",
        "pct_change_agg", "interval_high", "interval_low", "interval_volume",
        "price_high_24h", "price_low_24h",
        "bonding_progress", "migrate_status", "migrate_time_ms", "binance_score",
    ),
    FieldGroup.HOLDERS: ("holders", "kyc_holders"),
    FieldGroup.DISTRIBUTION: (
        "top10_percent", "dev_percent", "sniper_percent",
        "insider_percent", "bundler_percent", "new_wallet_percent",
        "kol_percent", "pro_percent", "sniper_count", "dev_sell_percent",
    ),
    FieldGroup.SMART_MONEY: (
        "smart_money_count", "smart_money_traders", "exit_rate", "net_inflow",
        "smart_money_percent", "max_gain", "alert_market_cap",
        "signal_direction", "signal_type", "signal_triggered_at", "signal_status",
    ),
    FieldGroup.SOCIAL: (
        "social_hype", "social_hype_cn", "social_hype_en", "sentiment",
        "search_count_24h", "kol_count", "twitter_followers",
    ),
    FieldGroup.AUDIT: (
        "audit_available", "audit_risk_level", "buy_tax_pct", "sell_tax_pct",
        "honeypot", "contract_verified",
    ),
    FieldGroup.SUPPLY: ("circulating_supply", "total_supply", "max_supply"),
}

# 字段 → 所属组。刻意从 _GROUP_FIELDS 派生（而不是再维护一份平铺清单），
# 保证"新增字段却漏加分组"或"分组里写了不存在的字段"这两类错误无法发生。
_FIELD_TO_GROUP: dict[str, FieldGroup] = {
    fname: group for group, fields in _GROUP_FIELDS.items() for fname in fields
}

# 所有可合并的指标字段（身份字段单独处理）
_MERGEABLE_FIELDS: tuple[str, ...] = tuple(_FIELD_TO_GROUP)

_IDENTITY_FIELDS: tuple[str, ...] = (
    "symbol", "name", "decimals", "launch_time_ms", "creator_address",
    "launch_platform", "pool_address", "quote_asset", "protocol",
)


# 稀疏历史序列的最小采样间隔：28 个点 × 5 分钟 ≈ 覆盖 2.3 小时
COARSE_HISTORY_SPACING_MS = 300_000


@dataclass(slots=True)
class HistoryPoint:
    """紧凑历史点：只保留计算速度/加速度真正需要的量。"""

    ts: int
    price: float | None
    market_cap: float | None
    holders: int | None
    liquidity: float | None
    net_inflow: float | None
    smart_money_count: int | None
    social_hype: float | None
    top10_percent: float | None
    unique_trader_1h: int | None


@dataclass
class TokenView:
    """代币在内存中的当前视图（合并多接口观测的结果）。

    历史点有界，避免长时间运行后内存增长；完整历史在 SQLite 里。
    """

    chain_id: str
    contract_address: str
    token_id: int | None = None
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    launch_time_ms: int | None = None
    creator_address: str | None = None
    launch_platform: str | None = None
    stage: str | None = None
    pool_address: str | None = None
    quote_asset: str | None = None
    protocol: str | None = None

    values: dict[str, Any] = field(default_factory=dict)
    tags: set[str] = field(default_factory=set)
    audit_risk_codes: tuple[str, ...] = ()

    # 每个字段组最近一次被刷新的时间（毫秒）
    group_updated_at: dict[str, int] = field(default_factory=dict)
    # 各字段的最后来源端点，便于诊断"这个数是哪来的"
    field_source: dict[str, str] = field(default_factory=dict)

    first_seen_ms: int = 0
    last_observed_ms: int = 0
    last_snapshot_ms: int = 0
    observation_count: int = 0

    state: TokenState = TokenState.DISCOVERED
    state_since_ms: int = 0
    # 状态退出确认计数（滞回：需连续多个周期低于退出阈值）
    exit_streak: dict[str, int] = field(default_factory=dict)

    # 两级历史缓冲：密集序列保证短窗（5m/15m）精度，
    # 稀疏序列以极小的内存代价把覆盖范围拉到 2 小时以上。
    # 若只用一个 maxlen=48 的密集序列，热门币每 30 秒采样一次时
    # 历史只覆盖 24 分钟，1h 窗口的所有特征会永久为 None——
    # 不报错、不告警，只是长周期信号静默消失。
    history: deque[HistoryPoint] = field(default_factory=lambda: deque(maxlen=32))
    history_coarse: deque[HistoryPoint] = field(default_factory=lambda: deque(maxlen=28))

    # 最近一次评分结果（供"与上周期对比"以及降级判断使用）
    last_scores: dict[str, float] = field(default_factory=dict)
    last_features: dict[str, Any] = field(default_factory=dict)
    last_snapshot_id: int | None = None

    # 风险门结果缓存
    blocked: bool = False
    block_reason: str = ""
    gate_blocked: bool = False
    gate_reasons: tuple[str, ...] = ()

    # 数据质量是否处于降级状态。只在状态翻转时发事件，避免每轮重复告警
    quality_degraded: bool = False
    # 审计缓存时间，避免重复消耗配额
    audit_checked_at: int = 0
    # 首次出现在热门榜的时间（Signal Lead Time 的参照点）
    trending_seen_at: int = 0
    # 是否已被选为拒绝样本（持续低频追踪，用于反事实研究）
    is_reject_sample: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.chain_id, self.contract_address)

    # ── 取值 ────────────────────────────────────────────────────────────
    def get(self, name: str) -> Any:
        return self.values.get(name)

    def getf(self, name: str) -> float | None:
        v = self.values.get(name)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def geti(self, name: str) -> int | None:
        v = self.getf(name)
        return int(v) if v is not None else None

    def age_sec(self, now_ms: int) -> int | None:
        """代币年龄（秒）。

        必须显式传入 now_ms 而不是内部读墙上时钟：
        Replay 引擎重放 3 天前的数据时，如果这里用 time.time()，
        算出的年龄会是"3 天前的币到今天"的年龄，
        于是 Top10 年龄分档全部落到最宽松档——
        回测结果会与线上系统当时的判断完全不同，而且不会报任何错。
        """
        if not self.launch_time_ms:
            return None
        return max(0, int((now_ms - self.launch_time_ms) / 1000))

    def group_age_sec(self, group: FieldGroup, now_ms: int) -> float | None:
        """字段组的数据年龄（秒）。None 表示该组从未有过数据。"""
        ts = self.group_updated_at.get(group.value)
        if not ts:
            return None
        return max(0.0, (now_ms - ts) / 1000.0)

    # ── 合并观测 ────────────────────────────────────────────────────────
    def apply(self, obs: TokenObservation) -> set[str]:
        """把一次观测合并进当前视图，返回本次刷新的字段组名集合。

        合并规则：只有非 None 的字段才覆盖，因此列表接口的稀疏数据
        不会把详情接口拿到的完整数据擦掉。
        """
        if not self.first_seen_ms:
            self.first_seen_ms = obs.observed_at
        self.last_observed_ms = max(self.last_observed_ms, obs.observed_at)
        self.observation_count += 1

        for fname in _IDENTITY_FIELDS:
            value = getattr(obs, fname, None)
            if value is not None and getattr(self, fname, None) is None:
                setattr(self, fname, value)
        # stage 会推进（new → finalizing → migrated），允许覆盖
        if obs.stage:
            self.stage = obs.stage

        touched: set[str] = set()
        for fname in _MERGEABLE_FIELDS:
            value = getattr(obs, fname, None)
            if value is None:
                continue
            self.values[fname] = value
            self.field_source[fname] = obs.endpoint
            group = _FIELD_TO_GROUP.get(fname, FieldGroup.MARKET)
            touched.add(group.value)

        for group_name in touched:
            self.group_updated_at[group_name] = obs.observed_at

        if obs.tags:
            self.tags.update(obs.tags)
        if obs.audit_risk_codes:
            self.audit_risk_codes = obs.audit_risk_codes
        if obs.seen_on_trending and not self.trending_seen_at:
            self.trending_seen_at = obs.observed_at

        return touched

    @property
    def history_depth(self) -> int:
        """历史点总数（去掉两级缓冲的重叠部分）。"""
        dense_oldest = self.history[0].ts if self.history else None
        if dense_oldest is None:
            return len(self.history_coarse)
        older = sum(1 for p in self.history_coarse if p.ts < dense_oldest)
        return len(self.history) + older

    def push_history(self, ts: int) -> None:
        """把当前视图压入历史，供下一次计算速度/加速度使用。"""
        point = HistoryPoint(
            ts=ts,
            price=self.getf("price"),
            market_cap=self.getf("market_cap"),
            holders=self.geti("holders"),
            liquidity=self.getf("liquidity"),
            net_inflow=self.getf("net_inflow"),
            smart_money_count=self.geti("smart_money_count"),
            social_hype=self.getf("social_hype"),
            top10_percent=self.getf("top10_percent"),
            unique_trader_1h=self.geti("unique_trader_1h"),
        )
        self.history.append(point)
        # 稀疏序列按最小间隔抽样，同一个对象被两个 deque 共享，不额外占内存
        if (not self.history_coarse
                or ts - self.history_coarse[-1].ts >= COARSE_HISTORY_SPACING_MS):
            self.history_coarse.append(point)

    def history_at_or_before(self, target_ts: int) -> HistoryPoint | None:
        """取目标时刻之前最近的历史点（As-of 语义，绝不使用未来数据）。

        先查密集序列（时间上更贴近目标）；密集序列还不够老时退到稀疏序列。
        两者都没有比目标更老的点时返回 None——**绝不退化成"拿最老的点凑一个"**，
        否则标着 "1h 增长" 的特征实际上算的是 24 分钟增长，
        而这种错误在日志里完全看不出来。
        """
        best = _latest_before(self.history, target_ts)
        if best is not None:
            return best
        return _latest_before(self.history_coarse, target_ts)


@dataclass(slots=True)
class QualityReport:
    """数据质量评估结果（第五维评分）。

    与 Confidence 分离：Confidence 描述"证据是否充分"，
    DataQuality 描述"我们拿到的数据本身是否可信"。
    """

    score: float
    stale_groups: tuple[str, ...] = ()
    missing_groups: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    penalties: dict[str, float] = field(default_factory=dict)
    mc_deviation_ratio: float | None = None
    computed_market_cap: float | None = None
    # 数据质量不足时禁止晋升到交易型状态
    block_s2: bool = False
    block_s1: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "stale_groups": list(self.stale_groups),
            "missing_groups": list(self.missing_groups),
            "conflicts": list(self.conflicts),
            "penalties": {k: round(v, 2) for k, v in self.penalties.items()},
            "mc_deviation_ratio": (
                round(self.mc_deviation_ratio, 4)
                if self.mc_deviation_ratio is not None else None
            ),
            "block_s1": self.block_s1,
            "block_s2": self.block_s2,
        }


@dataclass
class FeatureSet:
    """派生特征。

    每个变化率同时提供相对、绝对、log 三种形式：
    holders 1→3（+200%）和 1000→1500（+50%）不能用同一把尺子衡量。
    """

    values: dict[str, float | None] = field(default_factory=dict)
    # 计算过程中的说明，用于前端"为什么"展示
    notes: dict[str, str] = field(default_factory=dict)

    def get(self, name: str, default: float | None = None) -> float | None:
        v = self.values.get(name)
        return v if v is not None else default

    def set(self, name: str, value: float | None) -> None:
        self.values[name] = value

    def as_dict(self) -> dict[str, Any]:
        return {
            k: (round(v, 6) if isinstance(v, float) else v)
            for k, v in self.values.items()
        }


@dataclass(slots=True)
class FactorScore:
    """单个因子的得分明细，供警报邮件与前端可解释性展示。"""

    name: str
    label: str
    score: float
    max_score: float
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "score": round(self.score, 2),
            "max": self.max_score,
            "detail": self.detail,
        }


@dataclass
class ScoreResult:
    """五维评分结果。"""

    opportunity: float
    confidence: float
    data_quality: float
    rug_risk: float
    distribution: float
    factors: list[FactorScore] = field(default_factory=list)
    risk_flags: dict[str, Any] = field(default_factory=dict)
    distribution_reasons: list[str] = field(default_factory=list)

    def as_scores_dict(self) -> dict[str, float]:
        return {
            "opportunity": round(self.opportunity, 2),
            "confidence": round(self.confidence, 2),
            "data_quality": round(self.data_quality, 2),
            "rug_risk": round(self.rug_risk, 2),
            "distribution": round(self.distribution, 2),
        }

    def factors_as_list(self) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self.factors]


def _latest_before(points: deque[HistoryPoint], target_ts: int) -> HistoryPoint | None:
    best: HistoryPoint | None = None
    for point in points:
        if point.ts <= target_ts:
            best = point
        else:
            break
    return best
