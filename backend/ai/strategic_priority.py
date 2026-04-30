"""Strategic AI · 数据排序优先级公式与分组排序。

设计动机：
    Strategic prompt §0-§14 中"数据章节内部排序"是 GPT 反馈的关键缺陷修复点。
    此前 prompt 把数据按"类别"罗列，AI 容易被远端清算簇 / 大周期关键位先入为主，
    忽略离当前价 0.3% 的争夺区。本模块按"决策相关性优先"（近场优先 + 强度加权 +
    数据新鲜度）做章节内部排序，让 AI 看到的 Top N 都是当下决策最相关的样本。

核心公式（5 类对象，对应 prompt §4 / §5 / §6 / §7 / §8）：
  1. PriceZone（§4 价格区地图）
  2. Opportunity / TradeSetupCandidate（§5 高盈亏比候选）
  3. KeyLevel V3（§6 关键位）
  4. WallZone（§7 现货/合约挂单墙）
  5. LiqCluster（§8 清算地图 / 止损带）

设计纪律：
  - 所有 priority 函数返回 [0, 1] 区间分数；越高越靠前。
  - proximity_score 统一用"线性衰减"曲线：0% 距离 = 1.0，5% 距离 = 0.0，超出 = 0
    （简单可解释；不引入复杂高斯/指数曲线，避免 AI prompt 中难以描述）
  - 排序函数返回 list[T]，不就地修改输入；上层在渲染前调用一次。
  - **不"魔法过滤"**——RR < 1.5 的 opportunity / weight=low 的 wall 仍可保留在结果
    末尾，由 prompt 的 Top N 截断决定是否进入最终 prompt。这样保留排序 + 截断分离，
    便于测试。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from models.key_level import KeyLevelV2
    from models.liquidation import LiqCluster
    from models.orderbook_pressure import WallZone
    from models.trading_brain import BrainPriceZone, TradeSetupCandidate


# ────────────────────────────────────────────────────────────────────────────
# 共享辅助
# ────────────────────────────────────────────────────────────────────────────

def proximity_score(distance_pct: float, max_pct: float = 5.0) -> float:
    """距离当前价的接近度评分（线性衰减）。

    Args:
        distance_pct: 距当前价百分比（含符号；本函数仅用绝对值）
        max_pct: 视为"远场"的截止百分比，超过时返回 0

    曲线：
      |0%|=1.0 → |2.5%|=0.5 → |5%|=0.0 → |>5%|=0
    """
    d = abs(distance_pct)
    if d >= max_pct:
        return 0.0
    return max(0.0, 1.0 - d / max_pct)


def _clip01(v: float) -> float:
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


# ────────────────────────────────────────────────────────────────────────────
# 1. PriceZone 排序（§4）
# ────────────────────────────────────────────────────────────────────────────

# 角色权重表（对应 prompt §4 设计）。spot_defense / contested 是当前系统核心
# "决策地图"概念，权重最高；liquidation_magnet 是流动性目标，权重次之；
# key_level_only 是单纯结构（无现货/合约共振），权重再次。
_ZONE_ROLE_WEIGHT = {
    "spot_defense": 1.00,
    "contested": 1.00,
    "futures_target": 0.75,
    "liquidation_magnet": 0.70,
    "key_level_only": 0.60,
    "other": 0.30,
}


def price_zone_priority(zone: "BrainPriceZone") -> float:
    """PriceZone 决策相关性评分。

    公式（与 prompt §4 同源）：
      0.30 × proximity_score
      + 0.20 × role_importance
      + 0.20 × max(support_trust, resistance_trust, sweep_attractiveness)
      + 0.10 × data_confidence
      + 0.10 × confluence_score   ← 来自 wall_zone_ids + key_level_prices 计数
      + 0.10 × evidence_recency   ← 简化版：evidence 列表长度归一化（无时间戳）

    设计取舍：
      - confluence_score：用"墙数 + 关键位数"的简化总数（max 5）替代真正的"共振分"，
        避免引入新的子打分。对于 prompt 排序而言，差异化 5 档足够。
      - evidence_recency：BrainPriceZone 没有时间戳字段，用 evidence 列表长度归一
        作为"信息丰富度"代理（max 6）。简单可解释。
    """
    role_w = _ZONE_ROLE_WEIGHT.get(zone.dominant_role, 0.30)
    strength = max(
        getattr(zone, "support_trust", 0.0) or 0.0,
        getattr(zone, "resistance_trust", 0.0) or 0.0,
        getattr(zone, "sweep_attractiveness", 0.0) or 0.0,
    )
    confluence_count = (
        len(getattr(zone, "wall_zone_ids", []) or [])
        + len(getattr(zone, "key_level_prices", []) or [])
    )
    confluence = min(1.0, confluence_count / 5.0)
    evidence_count = len(getattr(zone, "evidence", []) or [])
    evidence_recency = min(1.0, evidence_count / 6.0)

    score = (
        0.30 * proximity_score(zone.distance_pct)
        + 0.20 * role_w
        + 0.20 * _clip01(strength)
        + 0.10 * _clip01(getattr(zone, "data_confidence", 0.0) or 0.0)
        + 0.10 * confluence
        + 0.10 * evidence_recency
    )
    return _clip01(score)


def sort_price_zones(
    zones: list["BrainPriceZone"],
) -> tuple[list["BrainPriceZone"], list["BrainPriceZone"], list["BrainPriceZone"]]:
    """按 prompt §4 三向分组排序：(current, above, below)。

    分组规则：
      - current  : |distance_pct| < 0.30
      - above    : distance_pct >= 0.30
      - below    : distance_pct <= -0.30

    每组内部按 price_zone_priority 降序。
    """
    cur: list["BrainPriceZone"] = []
    above: list["BrainPriceZone"] = []
    below: list["BrainPriceZone"] = []
    for z in zones:
        d = z.distance_pct
        if abs(d) < 0.30:
            cur.append(z)
        elif d >= 0.30:
            above.append(z)
        else:
            below.append(z)
    cur.sort(key=price_zone_priority, reverse=True)
    above.sort(key=price_zone_priority, reverse=True)
    below.sort(key=price_zone_priority, reverse=True)
    return cur, above, below


# ────────────────────────────────────────────────────────────────────────────
# 2. Opportunity 排序（§5）
# ────────────────────────────────────────────────────────────────────────────

# 状态权重（与 SetupStateName 枚举对应，prompt §5 设计要求）。confirmed = 满分；
# missed/cancelled/invalidated 仍允许进入排序但权重接近 0，由 Top N 自然过滤。
_OPP_STATE_WEIGHT = {
    "confirmed": 1.00,
    "confirmation_pending": 0.90,
    "triggered": 0.85,
    "waiting_for_trigger": 0.75,
    "forming": 0.50,
    "cooldown": 0.25,
    "missed": 0.10,
    "cancelled": 0.10,
    "invalidated": 0.10,
}


def _opportunity_max_rr(opp: "TradeSetupCandidate") -> float:
    targets = getattr(opp, "targets", []) or []
    if not targets:
        return 0.0
    rrs = [float(getattr(t, "rr", 0) or 0) for t in targets]
    return max(rrs) if rrs else 0.0


def opportunity_priority(opp: "TradeSetupCandidate") -> float:
    """Opportunity 决策相关性评分。

    公式：
      0.25 × state_weight
      + 0.25 × opportunity_score
      + 0.20 × asymmetry_score
      + 0.15 × rr_score          ← max(targets.rr) / 5，上限 1
      + 0.10 × data_confidence
      + 0.05 × proximity_to_entry  ← 简化为 setup 已在 entry 区时给满分（state >= triggered）

    proximity_to_entry 简化策略：
      - 状态 confirmed / confirmation_pending / triggered → 已经触达入场，给 1.0
      - 状态 waiting_for_trigger / forming → 0.5（尚未触达）
      - 其他终态 → 0.0
    避免引入新的距离查询（需要从 zone 反查 entry_zone），保持公式纯数据。
    """
    state_name = getattr(getattr(opp, "state", None), "name", "forming") or "forming"
    state_w = _OPP_STATE_WEIGHT.get(state_name, 0.10)
    rr_norm = min(1.0, _opportunity_max_rr(opp) / 5.0)
    if state_name in ("confirmed", "confirmation_pending", "triggered"):
        proximity = 1.0
    elif state_name in ("waiting_for_trigger", "forming"):
        proximity = 0.5
    else:
        proximity = 0.0

    score = (
        0.25 * state_w
        + 0.25 * _clip01(getattr(opp, "opportunity_score", 0.0) or 0.0)
        + 0.20 * _clip01(getattr(opp, "asymmetry_score", 0.0) or 0.0)
        + 0.15 * rr_norm
        + 0.10 * _clip01(getattr(opp, "data_confidence", 0.0) or 0.0)
        + 0.05 * proximity
    )
    return _clip01(score)


def sort_opportunities(
    opps: list["TradeSetupCandidate"],
) -> list["TradeSetupCandidate"]:
    """按 opportunity_priority 降序。"""
    return sorted(opps, key=opportunity_priority, reverse=True)


# ────────────────────────────────────────────────────────────────────────────
# 3. KeyLevel V3 排序（§6）
# ────────────────────────────────────────────────────────────────────────────

# 时间框架权重（V3 设计：长周期权重高但被距离反向打折）。
# 权重表与 prompt §6 设计同步。
_KL_TIMEFRAME_WEIGHT = {
    "1w": 1.00,
    "1d": 0.85,
    "4h": 0.70,
    "1h": 0.55,
    "15m": 0.40,
}

# 强度档位映射（KeyLevelV2.s_class："S/A/B/C"）。S 是顶档。
_KL_STRENGTH_BY_TIER = {
    "S": 1.00,
    "A": 0.80,
    "B": 0.60,
    "C": 0.40,
}


def _kl_strength_score(kl: "KeyLevelV2") -> float:
    """从 s_class（"S/A/B/C"）映射；缺失时退化到 final_score / 100。"""
    tier = (getattr(kl, "s_class", "") or "").upper()
    if tier in _KL_STRENGTH_BY_TIER:
        return _KL_STRENGTH_BY_TIER[tier]
    final = float(getattr(kl, "final_score", 0.0) or 0.0)
    return _clip01(final / 100.0)


def _kl_distance_pct(kl: "KeyLevelV2", current_price: float) -> float:
    """KeyLevel 距当前价的百分比（绝对值）。"""
    if current_price <= 0:
        return 999.0
    p = float(getattr(kl, "price", 0) or 0)
    if p <= 0:
        return 999.0
    return abs((p - current_price) / current_price) * 100.0


def key_level_priority(kl: "KeyLevelV2", current_price: float) -> float:
    """KeyLevel V3 决策相关性评分。

    公式：
      0.35 × proximity_score
      + 0.25 × strength_score（s_class 映射 / final_score 退化）
      + 0.20 × timeframe_weight（1w=1.0 / 1d=0.85 / 4h=0.70 / 1h=0.55 / 15m=0.40）
      + 0.10 × confluence_score（同区证据组数 / 4）
      + 0.10 × lifecycle_activity（lifecycle_events 列表长度 / 6）

    设计纪律：
      - 距离权重最大（0.35），确保"近场 4h S 级"优先于"远端 1w S 级"
      - confluence 用 evidence_groups 计数代理（max 4 = 现货/合约/清算/历史 全占）
      - lifecycle_activity 反映该位最近被触达频率（V3 升级字段）
    """
    distance = _kl_distance_pct(kl, current_price)
    tf = (getattr(kl, "timeframe", "") or "").lower()
    tf_w = _KL_TIMEFRAME_WEIGHT.get(tf, 0.40)
    evidence_groups = getattr(kl, "evidence_groups", None)
    if evidence_groups is not None:
        try:
            confluence = min(1.0, len(evidence_groups) / 4.0)
        except TypeError:
            confluence = 0.0
    else:
        confluence = 0.0
    lifecycle = getattr(kl, "lifecycle_events", []) or []
    lifecycle_score = min(1.0, len(lifecycle) / 6.0)

    score = (
        0.35 * proximity_score(distance)
        + 0.25 * _kl_strength_score(kl)
        + 0.20 * tf_w
        + 0.10 * confluence
        + 0.10 * lifecycle_score
    )
    return _clip01(score)


def sort_key_levels(
    kls: list["KeyLevelV2"],
    current_price: float,
) -> tuple[list["KeyLevelV2"], list["KeyLevelV2"], list["KeyLevelV2"]]:
    """按 prompt §6 三向分组排序：(near=±1%, above, below)。

    每组内部按 key_level_priority 降序。
    """
    near: list["KeyLevelV2"] = []
    above: list["KeyLevelV2"] = []
    below: list["KeyLevelV2"] = []
    for kl in kls:
        p = float(getattr(kl, "price", 0) or 0)
        if p <= 0 or current_price <= 0:
            continue
        diff = (p - current_price) / current_price * 100.0
        if abs(diff) < 1.0:
            near.append(kl)
        elif diff >= 1.0:
            above.append(kl)
        else:
            below.append(kl)
    sort_key = lambda x: key_level_priority(x, current_price)
    near.sort(key=sort_key, reverse=True)
    above.sort(key=sort_key, reverse=True)
    below.sort(key=sort_key, reverse=True)
    return near, above, below


# ────────────────────────────────────────────────────────────────────────────
# 4. WallZone 排序（§7）
# ────────────────────────────────────────────────────────────────────────────

def wall_priority(wall: "WallZone") -> float:
    """WallZone 决策相关性评分。

    公式（与 prompt §7 同源）：
      0.30 × proximity_score
      + 0.30 × trust_score
      + 0.20 × size_score          ← log10(current_usd / 1M) / 2，clamp 到 [0,1]
      + 0.15 × persistence_score   ← visible_minutes / 60（1h+ 持续 = 满分）
      + 0.05 × coinbase_bonus      ← coinbase_spot_confluence ? 1.0 : 0.0

    设计取舍：
      - size_score 用 log 而非线性：1M / 10M / 100M 大单尺度对数差异更合理
      - 原 event_recency（0.10 权重）依赖上层预填 wall._recent_event_score，
        但实际从未被预填，权重对所有墙都是 0；移除并按"距离/trust 是决策最关键"
        把 0.10 平摊到 proximity 与 trust 上，避免无效权重稀释信号
      - coinbase_bonus 单独加 0.05 而非作为 trust 加权——确保 Coinbase 共振墙
        在距离/trust 接近时优先于纯合约墙（机构资金 footprint 优势）
    """
    import math
    distance = abs(getattr(wall, "distance_pct", 0.0) or 0.0)
    current_usd = float(getattr(wall, "current_usd", 0.0) or 0.0)
    if current_usd > 0:
        size_raw = math.log10(max(1.0, current_usd / 1_000_000)) / 2.0
        size = _clip01(size_raw)
    else:
        size = 0.0
    persistence = min(1.0, (float(getattr(wall, "visible_minutes", 0.0) or 0.0)) / 60.0)
    coinbase_bonus = 1.0 if getattr(wall, "coinbase_spot_confluence", False) else 0.0

    score = (
        0.30 * proximity_score(distance)
        + 0.30 * _clip01(getattr(wall, "trust_score", 0.0) or 0.0)
        + 0.20 * size
        + 0.15 * persistence
        + 0.05 * coinbase_bonus
    )
    return _clip01(score)


def sort_walls(walls: list["WallZone"]) -> list["WallZone"]:
    """按 wall_priority 降序排列（不分组；侧别由调用方决定）。"""
    return sorted(walls, key=wall_priority, reverse=True)


# ────────────────────────────────────────────────────────────────────────────
# 5. LiqCluster 排序（§8）
# ────────────────────────────────────────────────────────────────────────────

def liq_cluster_priority(
    cluster: "LiqCluster",
    timeframe_weight: float = 1.0,
    vacuum_gap_pct: float = 0.0,
) -> float:
    """LiqCluster 流动性目标评分。

    公式（与 prompt §8 同源）：
      0.30 × proximity_score
      + 0.30 × amount_score        ← log10(total_usd / 100k) / 3，clamp [0,1]
      + 0.15 × vacuum_gap_score    ← vacuum_gap_pct / 1.5（1.5% 真空 = 满分）
      + 0.10 × crowding_score      ← exchange_count / 4（4 所共振 = 满分）
      + 0.10 × leverage_intensity  ← LiqCluster.leverage_intensity（已 0-1）
      + 0.05 × timeframe_weight    ← 调用方传入：1d=1.0 / 7d=0.65 / 30d=0.40

    timeframe_weight 由调用方决定（按本 cluster 来自哪个 LiquidationMapBlock 给值）。
    vacuum_gap_pct 同样由调用方查询（cluster 自身不含真空区信息）。
    """
    import math
    distance = abs(getattr(cluster, "distance_pct", 0.0) or 0.0)
    total_usd = float(getattr(cluster, "total_usd", 0.0) or 0.0)
    if total_usd > 0:
        amount = _clip01(math.log10(max(1.0, total_usd / 100_000)) / 3.0)
    else:
        amount = 0.0
    vacuum_score = min(1.0, max(0.0, vacuum_gap_pct) / 1.5)
    ex_count = int(getattr(cluster, "exchange_count", 1) or 1)
    crowding = min(1.0, ex_count / 4.0)
    leverage = _clip01(getattr(cluster, "leverage_intensity", 0.0) or 0.0)

    score = (
        0.30 * proximity_score(distance)
        + 0.30 * amount
        + 0.15 * vacuum_score
        + 0.10 * crowding
        + 0.10 * leverage
        + 0.05 * _clip01(timeframe_weight)
    )
    return _clip01(score)


# 时间窗权重（按 prompt §8 设计："近场 + 当下可触达"优先于长周期）
LIQ_TIMEFRAME_WEIGHT = {
    "1d": 1.00,
    "7d": 0.65,
    "30d": 0.40,
}


def sort_liq_clusters(
    clusters: list["LiqCluster"],
    timeframe: str,
    vacuum_zones: Optional[list] = None,
) -> list["LiqCluster"]:
    """按 liq_cluster_priority 排序。

    Args:
        clusters: 单侧（above 或 below）的 LiqCluster 列表
        timeframe: "1d" / "7d" / "30d"，决定 timeframe_weight
        vacuum_zones: 同侧的 VacuumZone 列表，用于计算每个 cluster 邻近的
            vacuum_gap_pct（无邻近真空区时为 0）
    """
    tf_w = LIQ_TIMEFRAME_WEIGHT.get(timeframe, 0.40)
    vz_list = vacuum_zones or []

    def _gap_for(c: "LiqCluster") -> float:
        """从 vacuum_zones 列表里查找 cluster 邻近的真空区"宽度百分比"。

        计算方式：VacuumZone 模型只含 price_from / price_to / midpoint，没有
        gap_pct 字段，需现场计算 (price_to - price_from) / midpoint × 100。
        若 cluster 价位落在某 vacuum_zone ±1% 邻域内，取该 zone 的宽度百分比；
        无邻近真空区时返回 0。
        """
        cp = float(getattr(c, "price_center", 0.0) or 0.0)
        if cp <= 0 or not vz_list:
            return 0.0
        best = 0.0
        for vz in vz_list:
            vz_low = float(getattr(vz, "price_from", 0.0) or 0.0)
            vz_high = float(getattr(vz, "price_to", 0.0) or 0.0)
            if vz_low <= 0 or vz_high <= 0:
                continue
            mid = float(getattr(vz, "midpoint", 0.0) or 0.0) or (vz_low + vz_high) / 2.0
            if mid <= 0:
                continue
            if abs((mid - cp) / mid) < 0.01:
                width_pct = (vz_high - vz_low) / mid * 100.0
                if width_pct > best:
                    best = width_pct
        return best

    return sorted(
        clusters,
        key=lambda c: liq_cluster_priority(
            c, timeframe_weight=tf_w, vacuum_gap_pct=_gap_for(c),
        ),
        reverse=True,
    )


# ────────────────────────────────────────────────────────────────────────────
# Stop Liquidity Band 聚合（GPT 反馈 G-3 必修）
# ────────────────────────────────────────────────────────────────────────────
#
# 根因：
#   §8 给 AI 的 1d / 7d / 30d 各 8 个 cluster = 24 个 100 美元 bins。AI 看到
#   76,400 / 76,500 / 76,600 / 76,700 / 76,800 / 77,000 这种连续点，无法形成
#   "上方有一条 600 美元宽的扫空带"的整体认知。这是反骑墙规则能否真正落地
#   的前提——AI 不知道扫空带边缘在哪，就没法判断"扫前 / 扫中 / 扫后"。
#
# 算法：单 pass 距离阈值聚类
#   1. 把 clusters 按 price_center 升序排序
#   2. 维护当前带 [low, high]，下一个 cluster 与 high 的价差 ≤ threshold 即合并
#   3. threshold = max(0.4 × ATR, 0.4% × last_price)（无 ATR 时退化到 0.4%）
#
# 注意：不跨周期合并——每个 cycle (1d/7d/30d) 各自聚合，保留周期信号差异。
# 跨周期共识合并是更高级的优化，可单独 PR 处理。


def aggregate_stop_bands(
    clusters: list["LiqCluster"],
    last_price: float,
    atr: float,
) -> list[dict]:
    """把按距离从近到远排序的 LiqCluster 列表聚合成 stop liquidity bands。

    Args:
        clusters: 单侧（above 或 below）的 LiqCluster 列表（任意顺序，函数内
            会按 price_center 升序）
        last_price: 当前价（用于阈值计算与百分比距离）
        atr: ATR 绝对值（与 last_price 同单位；为 0 时退化到纯百分比阈值）

    Returns:
        list of dict（按 price_low 升序）：
            {
              "price_low": float,
              "price_high": float,
              "peak_price": float,        # 带内 USD 最大的 cluster 价位
              "peak_usd": float,
              "total_usd": float,
              "distance_low_pct": float,  # 带 lower 边距当前价百分比（带符号：above=正/below=负）
              "distance_high_pct": float,
              "bin_count": int,
              "exchange_count_max": int,  # 带内最高的 exchange_count
              "constituents_count": int,  # = bin_count（语义别名，前端可用）
              "side": str,                # 取自首个 cluster.side（"long" | "short"）
            }
    """
    if not clusters or last_price <= 0:
        return []

    # 阈值：max(0.4 × ATR, 0.4% × last_price)
    pct_thresh = 0.004 * last_price
    atr_thresh = 0.4 * (atr or 0.0)
    threshold = max(pct_thresh, atr_thresh)
    if threshold <= 0:
        return []

    # 按 price_center 升序（聚合需要邻接判断）
    sorted_cs = sorted(
        clusters,
        key=lambda c: float(getattr(c, "price_center", 0.0) or 0.0),
    )

    bands: list[dict] = []
    cur: Optional[dict] = None

    for c in sorted_cs:
        cp = float(getattr(c, "price_center", 0.0) or 0.0)
        if cp <= 0:
            continue
        usd = float(getattr(c, "total_usd", 0.0) or 0.0)
        ex_cnt = int(getattr(c, "exchange_count", 1) or 1)
        side = (getattr(c, "side", "") or "").lower()

        if cur is None:
            cur = _new_band(c, cp, usd, ex_cnt, side)
            continue

        # 是否与当前带相邻
        if (cp - cur["price_high"]) <= threshold:
            cur["price_high"] = max(cur["price_high"], cp)
            cur["total_usd"] += usd
            cur["bin_count"] += 1
            if usd > cur["peak_usd"]:
                cur["peak_usd"] = usd
                cur["peak_price"] = cp
            cur["exchange_count_max"] = max(cur["exchange_count_max"], ex_cnt)
        else:
            bands.append(_finalize_band(cur, last_price))
            cur = _new_band(c, cp, usd, ex_cnt, side)

    if cur is not None:
        bands.append(_finalize_band(cur, last_price))

    return bands


def _new_band(c, cp: float, usd: float, ex_cnt: int, side: str) -> dict:
    return {
        "price_low": cp,
        "price_high": cp,
        "peak_price": cp,
        "peak_usd": usd,
        "total_usd": usd,
        "bin_count": 1,
        "exchange_count_max": ex_cnt,
        "side": side,
    }


def _finalize_band(b: dict, last_price: float) -> dict:
    """填充 distance_*_pct（带符号：above 自然得正，below 自然得负）。

    注意：与 §6/§7 渲染一致，above（side=short）距离为正，below（side=long）距离为负。
    无需根据 side 分支——price - last_price 自然反映符号；这与 LiqCluster.distance_pct
    旧契约（恒为绝对值）的差异由调用方知晓即可。
    """
    b["distance_low_pct"] = round((b["price_low"] - last_price) / last_price * 100.0, 3)
    b["distance_high_pct"] = round((b["price_high"] - last_price) / last_price * 100.0, 3)
    b["constituents_count"] = b["bin_count"]
    return b
