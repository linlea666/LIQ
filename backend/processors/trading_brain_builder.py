"""交易大脑聚合构建器：把关键位 / 流动性墙 / 清算数据合并为 PriceZone。

根因：
    现有仪表盘各 tab 独立，缺少「同一条价格轴」上的统一解释；本模块仅做只读聚合，
    不引入新打分公式（遵守铁律）。

复用：
    - KeyLevelSnapshotV2 / OrderbookPressureSnapshot / LiquidationMap 已是权威输出
    - 评分字段：wall 侧直接读 SR/SA/break_through_risk；KL 侧用 final_score 与 cascade_risk 做展示映射
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from models.key_level import KeyLevelSnapshotV2, KeyLevelV2, LiqMagnetLevel
from models.liquidation import LiqCluster, LiquidationMap
from models.orderbook_pressure import (
    OrderbookPressureSnapshot,
    SweepTarget,
    WallEvent,
    WallZone,
)
from models.trading_brain import (
    BrainContextChips,
    BrainDataQuality,
    BrainEvent,
    BrainFutBin,
    BrainFutBook,
    BrainFutMagnet,
    BrainPriceZone,
    BrainRankings,
    BrainScenario,
    BrainSpotBook,
    BrainSpotBookItem,
    BrainZoneRoles,
    TradingBrainSnapshot,
)


@dataclass
class _RawPiece:
    anchor: float
    p_lo: float
    p_hi: float
    kind: str  # "wall" | "level" | "liq_cluster" | "magnet"
    wall: Optional[WallZone] = None
    level: Optional[KeyLevelV2] = None
    liq: Optional[LiqCluster] = None
    magnet: Optional[LiqMagnetLevel] = None


@dataclass
class _Cluster:
    pieces: list[_RawPiece] = field(default_factory=list)

    @property
    def p_lo(self) -> float:
        return min(p.p_lo for p in self.pieces)

    @property
    def p_hi(self) -> float:
        return max(p.p_hi for p in self.pieces)

    @property
    def anchor_mid(self) -> float:
        return (self.p_lo + self.p_hi) / 2.0


def merge_tolerance(last_price: float, atr: float) -> float:
    """Q2=A：max(0.5×ATR, 0.3%×价)。ATR 缺失时只用 0.3%。"""
    pct = abs(last_price) * 0.003
    if atr and atr > 0:
        return max(0.5 * atr, pct)
    return pct


def _fmt_usd_short(usd: float) -> str:
    """中文金额格式化（与前端 formatCnUsd 同步口径）。

    亿+万 两档：
      ≥ 1 亿       → "X.XX亿"
      ≥ 100 万     → "X,XXX万"  千分位整数
      ≥ 1 万       → "X.X万"    保留 1 位
      < 1 万       → 千分位整数
    """
    try:
        v = float(usd)
    except (TypeError, ValueError):
        return "0"
    if v == 0:
        return "0"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e8:
        return f"{sign}{a / 1e8:.2f}亿"
    if a >= 1e6:
        return f"{sign}{round(a / 1e4):,}万"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.1f}万"
    return f"{sign}{round(a):,}"


def _kl_to_score_01(lv: KeyLevelV2, *, support_side: bool) -> tuple[float, float]:
    """(support_trust, resistance_trust) 展示映射；不修改原字段。"""
    fs = max(0.0, min(1.0, float(lv.final_score) / 100.0))
    if lv.side == "support" and support_side:
        return fs, 0.0
    if lv.side == "resistance" and not support_side:
        return 0.0, fs
    return 0.0, 0.0


def _wall_spot_supply(w: WallZone) -> bool:
    return bool(
        w.source in ("spot_only", "spot+depth")
        or w.has_spot_confluence
        or w.coinbase_spot_confluence
    )


def _wall_futures_liquidity(w: WallZone) -> bool:
    return bool(
        w.source in ("depth_only", "depth+large_order", "large_order_only", "spot+depth")
    )


def _collect_pieces(
    *,
    walls_above: list[WallZone],
    walls_below: list[WallZone],
    levels: list[KeyLevelV2],
    liq_above: list[LiqCluster],
    liq_below: list[LiqCluster],
    magnets: list[LiqMagnetLevel],
    merge_tol: float,
    last_price: float,
) -> list[_RawPiece]:
    pieces: list[_RawPiece] = []

    for w in list(walls_above) + list(walls_below):
        pieces.append(_RawPiece(
            anchor=w.price_mid,
            p_lo=w.price_low,
            p_hi=w.price_high,
            kind="wall",
            wall=w,
        ))

    half = max(merge_tol * 0.5, last_price * 0.0002)
    for lv in levels:
        p = float(lv.price)
        pieces.append(_RawPiece(
            anchor=p,
            p_lo=p - half,
            p_hi=p + half,
            kind="level",
            level=lv,
        ))

    for c in list(liq_above) + list(liq_below):
        pieces.append(_RawPiece(
            anchor=float(c.price_center),
            p_lo=float(c.price_from),
            p_hi=float(c.price_to),
            kind="liq_cluster",
            liq=c,
        ))

    band = max(merge_tol * 0.35, last_price * 0.00015)
    for m in magnets:
        p = float(m.price)
        pieces.append(_RawPiece(
            anchor=p,
            p_lo=p - band,
            p_hi=p + band,
            kind="magnet",
            magnet=m,
        ))

    return pieces


def _cluster_pieces(pieces: list[_RawPiece], tol: float) -> list[_Cluster]:
    if not pieces:
        return []
    ordered = sorted(pieces, key=lambda x: x.anchor)
    clusters: list[_Cluster] = []
    for p in ordered:
        placed = False
        for cl in clusters:
            if abs(p.anchor - cl.anchor_mid) <= tol:
                cl.pieces.append(p)
                placed = True
                break
        if not placed:
            clusters.append(_Cluster(pieces=[p]))
    return clusters


def _liq_sweep_score(c: LiqCluster) -> float:
    """清算簇 → 扫单吸引力 proxy（0–1），仅展示用。"""
    u = max(0.0, float(c.total_usd))
    raw = u / (200_000_000.0 + u)
    if c.exchange_count >= 3:
        raw = min(1.0, raw + 0.08)
    return round(min(1.0, raw), 3)


def _role_group(dom_role: str) -> str:
    """把 6 类 dominant_role 折叠成 4 类 role_group，作为 zone_id 哈希输入。

    折叠目的：让 zone_id 对"角色微变"也保持稳定。
    例如同一价格区在 contested 与 spot_defense 之间偶尔切换（共振临界点），
    若直接用全 dom_role 字符串做哈希，会导致 zone_id 频繁跳动。

    映射：
      spot_defense / contested        → "defense"   （结构防御簇）
      futures_target / liquidation_magnet → "target"  （目标/磁铁簇）
      key_level_only                  → "key_level" （单一关键位）
      other                           → "mixed"     （弱聚合）
    """
    if dom_role in ("spot_defense", "contested"):
        return "defense"
    if dom_role in ("futures_target", "liquidation_magnet"):
        return "target"
    if dom_role == "key_level_only":
        return "key_level"
    return "mixed"


def _stable_zone_id(
    coin: str, price_mid: float, atr: float, role_group: str,
    *, ref_price: float = 0.0,
) -> str:
    """跨帧稳定的 zone_id（修复 P1-B 伪稳定 bug）。

    旧实现 `f"{coin}|{z_lo:.2f}|{z_hi:.2f}|{idx}"`：
      - z_lo/z_hi 随 piece 集合微动（一个 piece 进出 cluster 即变）
      - idx 随当帧聚类排序漂移
      → 同一价格区跨帧 zone_id 不一致，导致：
        ① 前端 selectedId 跳动；② setup_id 跨帧错配；③ 状态机持久化失效

    新实现：基于 ATR 自适应价格桶 + role_group 联合哈希
      bucket_width = max(0.25 × ATR, 1.0)；ATR 缺失退化到 0.15% × ref_price
      bucket_mid   = round(price_mid / bucket_width) × bucket_width

    关键约束：bucket_width 必须独立于 price_mid，否则 price_mid 跨帧微动会
    传导到 width 再到 bucket → 自我抵消失败。同币种同帧内 ATR 是恒定参考量，
    所以 width 在帧内严格稳定，跨帧也仅以 ATR 自身的更新速率漂移（远低于 zone 微动）。

    role_group 折叠为 4 类（见 _role_group），容忍 dominant_role 在
    contested↔spot_defense 等临界点的微抖。
    """
    if atr and atr > 0:
        width = max(0.25 * atr, 1.0)
    else:
        ref = abs(ref_price or price_mid)
        width = max(ref * 0.0015, 1.0)
    bucket_mid = round(price_mid / width) * width
    digest = hashlib.sha1(
        f"{coin}|{bucket_mid:.4f}|{role_group}".encode()
    ).hexdigest()[:12]
    return f"{coin}_{digest}"


def _build_zone_from_cluster(
    cl: _Cluster,
    *,
    coin: str,
    last_price: float,
    atr: float,
) -> BrainPriceZone:
    z_lo, z_hi = cl.p_lo, cl.p_hi
    z_mid = (z_lo + z_hi) / 2.0
    dist = (z_mid - last_price) / max(last_price, 1e-9) * 100.0

    roles = BrainZoneRoles()
    evidence: list[str] = []
    layer_notes: list[str] = []

    wall_ids: list[str] = []
    kl_prices: list[float] = []

    max_sup = 0.0
    max_res = 0.0
    max_sa = 0.0
    max_btr = 0.0
    dconf_parts: list[float] = []

    for piece in cl.pieces:
        if piece.kind == "wall" and piece.wall:
            w = piece.wall
            if w.wall_zone_id:
                wall_ids.append(w.wall_zone_id)
            spot = _wall_spot_supply(w)
            fut = _wall_futures_liquidity(w)
            roles.spot_supply_wall = roles.spot_supply_wall or spot
            roles.futures_liquidity_wall = roles.futures_liquidity_wall or fut
            roles.coinbase_confluence = roles.coinbase_confluence or w.coinbase_spot_confluence

            side_cn = "买墙（下方支撑候选的流动性证据）" if w.side == "bid" else "卖墙（上方阻力候选的流动性证据）"
            layer = "现货供需层" if spot and not fut else ("双源（现货+合约）层" if w.source == "spot+depth" else "合约流动性层")
            layer_notes.append(f"{layer}：{side_cn}")

            ev = (
                f"[{layer}] 厚度约 {_fmt_usd_short(w.current_usd)} USD · "
                f"信任评分 {w.trust_score:.2f} · 打穿风险评分 {w.break_through_risk:.2f}"
            )
            evidence.append(ev)

            if w.side == "bid":
                max_sup = max(max_sup, float(w.support_resistance_trust_score))
            else:
                max_res = max(max_res, float(w.support_resistance_trust_score))
            max_sa = max(max_sa, float(w.sweep_attractiveness_score))
            max_btr = max(max_btr, float(w.break_through_risk))
            dconf_parts.append(float(w.trust_score))

        elif piece.kind == "level" and piece.level:
            lv = piece.level
            kl_prices.append(float(lv.price))
            roles.key_level = True
            tier = lv.strength_tier
            sup_s, res_s = _kl_to_score_01(lv, support_side=(lv.side == "support"))
            max_sup = max(max_sup, sup_s)
            max_res = max(max_res, res_s)
            max_btr = max(max_btr, float(lv.cascade_risk))
            dconf_parts.append(max(0.0, min(1.0, float(lv.confluence_score) / 100.0)))

            evidence.append(
                f"[关键位] {tier}级{('支撑' if lv.side == 'support' else '阻力')} · "
                f"共振分 {lv.confluence_score:.0f} · 数据来自关键位引擎（未改分）"
            )
            if lv.note:
                evidence.append(f"[关键位说明] {lv.note}")

        elif piece.kind == "liq_cluster" and piece.liq:
            c = piece.liq
            roles.liquidation_magnet = True
            side_cn = "多头" if c.side == "long" else "空头"
            evidence.append(
                f"[清算磁铁] {side_cn}清算密集区 · 约 {_fmt_usd_short(c.total_usd)} USD · "
                f"属扫单/磁吸目标，不作为支撑或阻力"
            )
            max_sa = max(max_sa, _liq_sweep_score(c))
            dconf_parts.append(0.75 if c.exchange_count >= 3 else 0.55)

        elif piece.kind == "magnet" and piece.magnet:
            m = piece.magnet
            roles.liquidation_magnet = True
            evidence.append(
                f"[清算磁铁] {m.note or m.magnet_role} · "
                f"约 {_fmt_usd_short(m.usd)} USD（{m.source}）"
            )
            max_sa = max(max_sa, min(1.0, float(m.usd) / (150_000_000.0 + float(m.usd))))
            dconf_parts.append(0.65)

    data_confidence = round(sum(dconf_parts) / max(len(dconf_parts), 1), 3) if dconf_parts else 0.35

    # dominant_label
    if roles.liquidation_magnet and not (roles.key_level or roles.spot_supply_wall or roles.futures_liquidity_wall):
        dom = "清算磁铁（磁吸/扫单目标位）"
    elif roles.spot_supply_wall and roles.futures_liquidity_wall:
        dom = "多源争夺区（现货供需 + 合约流动性）"
    elif roles.key_level and (roles.spot_supply_wall or roles.futures_liquidity_wall):
        dom = "关键位 + 流动性共振区"
    elif roles.key_level:
        dom = "关键价位区"
    elif roles.spot_supply_wall:
        dom = "现货供需墙区（支撑/阻力候选）"
    elif roles.futures_liquidity_wall:
        dom = "合约流动性墙区"
    else:
        dom = "价格关注区"

    dom_role = _classify_dominant_role(
        roles=roles,
        max_sup=max_sup,
        max_res=max_res,
    )

    # P1-B 修复：zone_id 必须在 dom_role 已定后再算（依赖 role_group）。
    # 注意此处使用 z_mid 而非 (z_lo, z_hi) — 桶哈希对窄幅微动天然稳健，
    # 不依赖 _Cluster 的边界，是 zone_id 跨帧稳定的关键。
    # ref_price=last_price：ATR 缺失时也能给出稳定的 width 参考量。
    zone_id = _stable_zone_id(
        coin, z_mid, atr, _role_group(dom_role), ref_price=last_price,
    )

    scen = BrainScenario(
        if_hold="关注该区是否出现成交吸收、墙厚度是否维持、现货/合约 CVD 是否同向走弱。",
        if_break="关注邻近清算磁铁、打穿风险评分与流动性真空；不作为交易指令。",
        invalidates_if="以关键位失效条件或更高周期收盘结构为准（若本区含关键位，详见关键位卡片）。",
    )

    return BrainPriceZone(
        zone_id=zone_id,
        coin=coin,
        price_low=round(z_lo, 4),
        price_high=round(z_hi, 4),
        price_mid=round(z_mid, 4),
        distance_pct=round(dist, 3),
        roles=roles,
        dominant_label=dom,
        dominant_role=dom_role,
        wall_zone_ids=sorted(set(wall_ids)),
        key_level_prices=sorted(set(kl_prices)),
        support_trust=round(max_sup, 3),
        resistance_trust=round(max_res, 3),
        sweep_attractiveness=round(max_sa, 3),
        break_through_risk=round(max_btr, 3),
        data_confidence=data_confidence,
        evidence=evidence[:12],
        scenario=scen,
        layer_notes=layer_notes[:8],
    )


def _classify_dominant_role(
    *,
    roles: BrainZoneRoles,
    max_sup: float,
    max_res: float,
) -> str:
    """将多 role 标记折叠为单一主导角色，前端按此上色 + 排行分桶。

    优先级（由高到低）：
      1. spot_defense  ：spot_supply_wall ∨ coinbase_confluence ∨ (key_level ∧ trust ≥ 0.55)
                         若同时存在 futures_liquidity_wall ∧ liquidation_magnet → contested
      2. contested     ：现货层与（合约+清算）层并存
      3. futures_target：futures_liquidity_wall ∧ liquidation_magnet（无现货防守）
      4. liquidation_magnet：仅清算磁铁
      5. key_level_only：只有关键位但无墙/磁铁
      6. other         ：其他

    保守原则：spot_defense 必须有"硬证据"（现货墙/Coinbase）或"高 trust 关键位"，
    避免把弱关键位也视为防守位。
    """
    has_spot = bool(roles.spot_supply_wall or roles.coinbase_confluence)
    strong_kl = bool(roles.key_level and max(max_sup, max_res) >= 0.55)
    has_target = bool(roles.futures_liquidity_wall and roles.liquidation_magnet)

    if (has_spot or strong_kl) and (
        roles.futures_liquidity_wall or roles.liquidation_magnet
    ):
        return "contested"
    if has_spot or strong_kl:
        return "spot_defense"
    if has_target:
        return "futures_target"
    if roles.liquidation_magnet:
        return "liquidation_magnet"
    if roles.key_level:
        return "key_level_only"
    return "other"


def _build_summary(zones: list[BrainPriceZone], last_price: float) -> str:
    below = [z for z in zones if z.price_mid < last_price]
    above = [z for z in zones if z.price_mid > last_price]
    below.sort(key=lambda z: abs(z.distance_pct))
    above.sort(key=lambda z: abs(z.distance_pct))

    parts: list[str] = []
    if below:
        b0 = below[0]
        if b0.roles.spot_supply_wall and b0.roles.key_level:
            parts.append(
                f"下方约 {b0.price_mid:,.0f} 出现现货供需墙与关键位证据叠加"
            )
        elif b0.roles.liquidation_magnet:
            parts.append(
                f"下方约 {b0.price_mid:,.0f} 存在清算磁铁/密集区（磁吸目标，非纯粹支撑）"
            )
        elif b0.roles.key_level:
            parts.append(f"下方约 {b0.price_mid:,.0f} 有关键位证据")
    if above:
        a0 = above[0]
        if a0.roles.liquidation_magnet:
            parts.append(
                f"上方约 {a0.price_mid:,.0f} 存在清算相关磁吸区"
            )
    if not parts:
        return "当前价附近暂无强聚合证据，请留意数据质量与更新时间。"
    return "；".join(parts) + "。本页为结构与流动性辅助视图，不含交易指令。"


def _iter_sweep_targets(op: OrderbookPressureSnapshot) -> list[SweepTarget]:
    """聚合扫单磁铁参照：顶层 top_sweep_targets（若将来写入）+ 各墙区 sweep_target。"""
    out: list[SweepTarget] = []
    seen: set[tuple[float, str]] = set()
    for t in list(getattr(op, "top_sweep_targets", None) or []):
        if not isinstance(t, SweepTarget):
            continue
        key = (float(t.magnet_price), str(t.direction))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    for w in list(op.walls_above or []) + list(op.walls_below or []):
        st = getattr(w, "sweep_target", None)
        if st is None:
            continue
        key = (float(st.magnet_price), str(st.direction))
        if key in seen:
            continue
        seen.add(key)
        out.append(st)
    return out


def _wall_event_message(ev: WallEvent) -> str:
    mapping = {
        "wall_appeared": "墙区新出现",
        "wall_strengthened": "墙增厚",
        "wall_weakened": "墙减薄",
        "wall_removed": "墙撤单/结束（未成交部分）",
        "wall_consumed": "墙被成交消耗",
        "wall_reloaded": "同价区疑似重挂",
        "wall_consumed_and_removed": "同帧内既消耗又撤单（结构变化）",
    }
    base = mapping.get(str(ev.event_type), str(ev.event_type))
    side = "买侧" if ev.side == "bid" else "卖侧"
    return f"{side}{base}"


def _build_events(
    op: Optional[OrderbookPressureSnapshot],
    *,
    now_sec: int,
    wall_layer: str = "futures",
) -> list[BrainEvent]:
    if not op or not op.wall_events:
        return []
    out: list[BrainEvent] = []
    for ev in op.wall_events:
        ts = int(ev.ts_sec or 0)
        if ts <= 0 or now_sec - ts > 1800:
            continue
        layer: Any = "spot" if wall_layer == "spot" else "futures"
        out.append(BrainEvent(
            ts=ts,
            layer=layer,
            price_mid=float(ev.price_mid),
            zone_id=str(ev.wall_zone_id or ""),
            message=_wall_event_message(ev),
            source="liquidity_wall_engine",
        ))
    out.sort(key=lambda e: e.ts, reverse=True)
    return out[:40]


def _rankings(zones: list[BrainPriceZone]) -> BrainRankings:
    defenses = [
        z.zone_id for z in sorted(
            [x for x in zones if x.dominant_role == "spot_defense"],
            key=lambda z: (
                -max(z.support_trust, z.resistance_trust),
                abs(z.distance_pct),
            ),
        )[:8]
    ]
    targets = [
        z.zone_id for z in sorted(
            [
                x for x in zones
                if x.dominant_role in ("futures_target", "liquidation_magnet")
            ],
            key=lambda z: (-z.sweep_attractiveness, abs(z.distance_pct)),
        )[:8]
    ]
    contested = [
        z.zone_id for z in sorted(
            [x for x in zones if x.dominant_role == "contested"],
            key=lambda z: (
                -(max(z.support_trust, z.resistance_trust) + z.sweep_attractiveness),
                abs(z.distance_pct),
            ),
        )[:8]
    ]

    sup_ids = [
        z.zone_id for z in sorted(
            [x for x in zones if x.support_trust >= 0.05 and (x.roles.key_level or x.roles.spot_supply_wall)],
            key=lambda z: (-z.support_trust, abs(z.distance_pct)),
        )[:8]
    ]
    res_ids = [
        z.zone_id for z in sorted(
            [x for x in zones if x.resistance_trust >= 0.05 and (x.roles.key_level or x.roles.spot_supply_wall)],
            key=lambda z: (-z.resistance_trust, abs(z.distance_pct)),
        )[:8]
    ]
    sweep_ids = [
        z.zone_id for z in sorted(
            [
                x for x in zones
                if x.roles.liquidation_magnet
                or x.sweep_attractiveness >= 0.45
                or (x.roles.futures_liquidity_wall and x.sweep_attractiveness >= 0.35)
            ],
            key=lambda z: (-z.sweep_attractiveness, abs(z.distance_pct)),
        )[:8]
    ]
    btr_ids = [
        z.zone_id for z in sorted(
            [x for x in zones if x.break_through_risk >= 0.2],
            key=lambda z: (-z.break_through_risk, abs(z.distance_pct)),
        )[:8]
    ]

    return BrainRankings(
        support_trust=sup_ids,
        resistance_trust=res_ids,
        sweep_targets=sweep_ids,
        break_through_risk=btr_ids,
        top_defenses=defenses,
        top_targets=targets,
        top_contested=contested,
    )


# ────────────────────────────────────────────────────────────────────────────
# Phase B：现货订单簿模块（从 walls_above / walls_below 抽取，按距离分层）
# ────────────────────────────────────────────────────────────────────────────

# 距离档位阈值（与产品规则一致）
_BRACKET_NEAR = 0.5    # |dist_pct| ≤ 0.5%
_BRACKET_MID = 2.0     # 0.5% < |dist_pct| ≤ 2.0%
_BRACKET_FAR = 5.0     # 2.0% < |dist_pct| ≤ 5.0%（>5% 截断）

# 各档位每侧上限（与 BrainSpotBook.bracket_caps 默认一致）
_BRACKET_CAP = {"near": 8, "mid": 8, "far": 6}


def _bracket_of(distance_pct: float) -> Optional[str]:
    """返回 'near' / 'mid' / 'far'；超过 _BRACKET_FAR 返回 None（不展示）。"""
    a = abs(distance_pct)
    if a <= _BRACKET_NEAR:
        return "near"
    if a <= _BRACKET_MID:
        return "mid"
    if a <= _BRACKET_FAR:
        return "far"
    return None


def _wall_to_book_item(w: WallZone) -> BrainSpotBookItem:
    """从 WallZone 抽取展示项；不重新计算任何评分字段（铁律）。

    现货厚度 = spot_current_usd（Binance 现货侧 5m 深度共振）
             + coinbase_spot_usd（Coinbase 现货独立链路）
    合约厚度 = max(current_usd - spot_usd, 0)
    """
    spot_usd = float(getattr(w, "spot_current_usd", 0.0) or 0.0) + float(
        getattr(w, "coinbase_spot_usd", 0.0) or 0.0
    )
    total_usd = float(getattr(w, "current_usd", 0.0) or 0.0)
    fut_usd = max(total_usd - spot_usd, 0.0)
    bracket = _bracket_of(w.distance_pct) or "far"
    return BrainSpotBookItem(
        wall_zone_id=w.wall_zone_id or "",
        side="bid" if w.side == "bid" else "ask",
        price=float(w.peak_price or w.price_mid),
        distance_pct=float(w.distance_pct),
        bracket=bracket,  # type: ignore[arg-type]
        total_usd=total_usd,
        spot_usd=spot_usd,
        futures_usd=fut_usd,
        is_dual_source=bool(getattr(w, "dual_source", False)),
        has_coinbase=bool(getattr(w, "coinbase_spot_confluence", False)),
        trust_score=float(getattr(w, "trust_score", 0.0) or 0.0),
        strength_tier=getattr(w, "strength_tier", "C"),
        dominant_role=str(getattr(w, "dominant_role", "ordinary") or "ordinary"),
    )


def _trim_brackets(items: list[BrainSpotBookItem]) -> list[BrainSpotBookItem]:
    """各档位按距离升序排序后截断到 cap，串接成最终列表。"""
    grouped: dict[str, list[BrainSpotBookItem]] = {"near": [], "mid": [], "far": []}
    for it in items:
        grouped.setdefault(it.bracket, []).append(it)
    for k, lst in grouped.items():
        lst.sort(key=lambda x: abs(x.distance_pct))
        cap = _BRACKET_CAP.get(k, 8)
        grouped[k] = lst[:cap]
    return grouped["near"] + grouped["mid"] + grouped["far"]


def _build_spot_book(op: Optional[OrderbookPressureSnapshot]) -> Optional[BrainSpotBook]:
    """从 OrderbookPressureSnapshot 抽出现货订单簿视图。

    数据源缺失或无 wall_zone 时返回 None（前端隐藏模块）。
    """
    if not op:
        return None
    above = list(getattr(op, "walls_above", None) or [])
    below = list(getattr(op, "walls_below", None) or [])
    if not above and not below:
        return None
    asks_raw = [
        _wall_to_book_item(w)
        for w in above
        if _bracket_of(getattr(w, "distance_pct", 0.0)) is not None
    ]
    bids_raw = [
        _wall_to_book_item(w)
        for w in below
        if _bracket_of(getattr(w, "distance_pct", 0.0)) is not None
    ]
    asks = _trim_brackets(asks_raw)
    bids = _trim_brackets(bids_raw)
    return BrainSpotBook(
        asks=asks,
        bids=bids,
        bracket_caps=dict(_BRACKET_CAP),
    )


# ────────────────────────────────────────────────────────────────────────────
# Phase C：合约流动性堆积模块（合约侧厚度 + 磁铁叠加；不重新评分）
# ────────────────────────────────────────────────────────────────────────────


def _wall_to_fut_bin(w: WallZone) -> BrainFutBin:
    """从 WallZone 抽取合约侧 bin。

    合约侧厚度 = current_usd - spot_usd（max 0），其中 spot_usd 来自 dual_source/
    coinbase_spot 字段；纯合约墙（无现货共振）→ futures_usd = current_usd。
    """
    spot_usd = float(getattr(w, "spot_current_usd", 0.0) or 0.0) + float(
        getattr(w, "coinbase_spot_usd", 0.0) or 0.0
    )
    total_usd = float(getattr(w, "current_usd", 0.0) or 0.0)
    fut_usd = max(total_usd - spot_usd, 0.0)
    bracket = _bracket_of(w.distance_pct) or "far"
    return BrainFutBin(
        wall_zone_id=w.wall_zone_id or "",
        side="bid" if w.side == "bid" else "ask",
        price=float(w.peak_price or w.price_mid),
        distance_pct=float(w.distance_pct),
        bracket=bracket,  # type: ignore[arg-type]
        futures_usd=fut_usd,
        total_usd=total_usd,
        persistence_score=float(getattr(w, "persistence_score", 0.0) or 0.0),
        sweep_attractiveness=float(getattr(w, "sweep_attractiveness_score", 0.0) or 0.0),
        break_through_risk=float(getattr(w, "break_through_risk", 0.0) or 0.0),
        dominant_role=str(getattr(w, "dominant_role", "ordinary") or "ordinary"),
    )


def _trim_fut_brackets(items: list[BrainFutBin]) -> list[BrainFutBin]:
    """各档位按 |distance_pct| 升序排序后截断，串接成最终列表。"""
    grouped: dict[str, list[BrainFutBin]] = {"near": [], "mid": [], "far": []}
    for it in items:
        grouped.setdefault(it.bracket, []).append(it)
    for k, lst in grouped.items():
        lst.sort(key=lambda x: abs(x.distance_pct))
        cap = _BRACKET_CAP.get(k, 8)
        grouped[k] = lst[:cap]
    return grouped["near"] + grouped["mid"] + grouped["far"]


def _collect_fut_magnets(
    *,
    liq: Optional[LiquidationMap],
    kl: Optional[KeyLevelSnapshotV2],
    last_price: float,
) -> list[BrainFutMagnet]:
    """从 LiquidationMap.clusters_* 与 KeyLevel.magnet_levels 抽磁铁标记。

    仅保留 |distance_pct| ≤ 5%（与 spot/fut bin 同一展示窗口），按距离排序去重。
    """
    out: list[BrainFutMagnet] = []
    seen: set[tuple[str, int]] = set()  # (kind, price_bucket) 用于去重

    def _push(price: float, side: str, kind: str, usd: float, leverage: str = "", note: str = "") -> None:
        if not price or last_price <= 0:
            return
        dist = (price - last_price) / last_price * 100.0
        if abs(dist) > _BRACKET_FAR:
            return
        # 价格分桶去重：±0.05% 内视为同一磁铁
        bucket = int(price / max(last_price * 0.0005, 1.0))
        key = (kind, bucket)
        if key in seen:
            return
        seen.add(key)
        out.append(BrainFutMagnet(
            price=float(price),
            distance_pct=round(dist, 4),
            side="above" if dist > 0 else "below",  # type: ignore[arg-type]
            magnet_kind=kind,  # type: ignore[arg-type]
            usd=float(usd or 0.0),
            leverage_hint=leverage or "",
            note=note or "",
        ))

    if liq:
        for c in list(getattr(liq, "clusters_above", None) or []):
            _push(c.price_center, "above", "liq_cluster", c.total_usd,
                  leverage=getattr(c, "dominant_leverage", "") or "")
        for c in list(getattr(liq, "clusters_below", None) or []):
            _push(c.price_center, "below", "liq_cluster", c.total_usd,
                  leverage=getattr(c, "dominant_leverage", "") or "")
    if kl:
        for m in list(getattr(kl, "magnet_levels", None) or []):
            kind = getattr(m, "source", "other") or "other"
            kind_norm = kind if kind in (
                "max_pain_long", "max_pain_short", "leverage_magnet"
            ) else "other"
            _push(
                m.price,
                "above" if (m.price - last_price) > 0 else "below",
                kind_norm,
                getattr(m, "usd", 0.0) or 0.0,
                leverage=getattr(m, "leverage_hint", "") or "",
                note=getattr(m, "note", "") or "",
            )
    out.sort(key=lambda x: abs(x.distance_pct))
    return out


def _build_fut_book(
    *,
    op: Optional[OrderbookPressureSnapshot],
    liq: Optional[LiquidationMap],
    kl: Optional[KeyLevelSnapshotV2],
    last_price: float,
) -> Optional[BrainFutBook]:
    """合约堆积视图：bin = 合约侧厚度，磁铁 = 清算簇 + max_pain。

    仅 op 缺失且无任何磁铁数据时返回 None；只有磁铁也允许显示（前端可视化磁铁层）。
    """
    bins_above: list[BrainFutBin] = []
    bins_below: list[BrainFutBin] = []
    if op:
        for w in list(getattr(op, "walls_above", None) or []):
            if _bracket_of(getattr(w, "distance_pct", 0.0)) is None:
                continue
            bins_above.append(_wall_to_fut_bin(w))
        for w in list(getattr(op, "walls_below", None) or []):
            if _bracket_of(getattr(w, "distance_pct", 0.0)) is None:
                continue
            bins_below.append(_wall_to_fut_bin(w))

    magnets = _collect_fut_magnets(liq=liq, kl=kl, last_price=last_price)

    # 标记 bin 是否与磁铁同价区共振（用于前端高亮"扫单目标墙"）
    if magnets and (bins_above or bins_below):
        atr_pct = max(0.10, abs(last_price) * 0.0005 / max(last_price, 1.0) * 100.0)
        for b in bins_above + bins_below:
            for m in magnets:
                if abs(b.distance_pct - m.distance_pct) <= atr_pct:
                    b.is_attached_magnet = True
                    break

    bins_above = _trim_fut_brackets(bins_above)
    bins_below = _trim_fut_brackets(bins_below)

    if not bins_above and not bins_below and not magnets:
        return None
    return BrainFutBook(
        bins_above=bins_above,
        bins_below=bins_below,
        magnets=magnets,
        bracket_caps=dict(_BRACKET_CAP),
    )


def build_trading_brain_snapshot(
    *,
    coin: str,
    last_price: float,
    atr: float,
    kl: Optional[KeyLevelSnapshotV2],
    op: Optional[OrderbookPressureSnapshot],
    liq: Optional[LiquidationMap],
    cvd_contract_trend: str = "",
    cvd_spot_trend: str = "",
    oi_delta_1h_pct: Optional[float] = None,
    funding_interpretation: str = "",
    max_zones: int = 24,
) -> TradingBrainSnapshot:
    """纯函数：由调用方从 CoinState 抽出字段后传入。"""
    now_sec = int(time.time())
    tol = merge_tolerance(last_price, atr)

    walls_above = list(op.walls_above) if op else []
    walls_below = list(op.walls_below) if op else []
    levels = list(kl.levels) if kl else []
    liq_a = list(liq.clusters_above) if liq else []
    liq_b = list(liq.clusters_below) if liq else []
    magnets = list(kl.magnet_levels) if kl else []

    pieces = _collect_pieces(
        walls_above=walls_above,
        walls_below=walls_below,
        levels=levels,
        liq_above=liq_a,
        liq_below=liq_b,
        magnets=magnets,
        merge_tol=tol,
        last_price=last_price,
    )
    clusters = _cluster_pieces(pieces, tol)
    zones = [
        _build_zone_from_cluster(c, coin=coin, last_price=last_price, atr=atr)
        for c in clusters
    ]
    # P1-B：zone_id 已基于价格桶稳定哈希；同桶若被聚类拆出多个 _Cluster
    # （罕见，但理论上会发生），需保留唯一一个（取距现价最近的）。
    _by_id: dict[str, BrainPriceZone] = {}
    for z in zones:
        prev = _by_id.get(z.zone_id)
        if prev is None or abs(z.distance_pct) < abs(prev.distance_pct):
            _by_id[z.zone_id] = z
    zones = list(_by_id.values())
    zones.sort(key=lambda z: abs(z.distance_pct))
    if max_zones > 0:
        zones = zones[:max_zones]

    rankings = _rankings(zones)
    events = _build_events(op, now_sec=now_sec)

    # 数据质量
    dq_notes: list[str] = []
    lq = op.data_quality if op else ""
    ready_count = sum(1 for x in (kl, op, liq) if x is not None)
    total_count = 3
    is_partial_ready = ready_count < total_count
    if not op:
        dq_notes.append("挂单压力/流动性墙快照暂不可用")
    if not kl:
        dq_notes.append("关键位 V2 快照暂不可用")
    if not liq:
        dq_notes.append("清算地图暂不可用")

    if is_partial_ready and not zones:
        summary = (
            f"数据未就绪：{ready_count}/{total_count} 项核心源已接入；"
            f"暖机期间仅展示已到达的字段，请稍候再看。"
        )
    else:
        summary = _build_summary(zones, last_price)

    freshness_score = None
    stale: list[str] = []
    missing: list[str] = []
    if kl and kl.data_freshness:
        freshness_score = kl.data_freshness.overall_freshness_score
        stale = list(kl.data_freshness.stale_sources or [])
        missing = list(kl.data_freshness.missing_sources or [])

    # 最近磁铁价位 — 自 crowding / sweep targets
    mag_above: Optional[float] = None
    mag_below: Optional[float] = None
    if op:
        for t in _iter_sweep_targets(op):
            try:
                mp = float(t.magnet_price)
                if t.direction == "below" and last_price > mp:
                    if mag_below is None or mp > mag_below:
                        mag_below = mp
                if t.direction == "above" and last_price < mp:
                    if mag_above is None or mp < mag_above:
                        mag_above = mp
            except (TypeError, ValueError):
                continue

    ctx = BrainContextChips(
        regime=kl.regime if kl else "",
        regime_description=kl.regime_description if kl else "",
        oi_delta_1h_pct=oi_delta_1h_pct,
        funding_interpretation=funding_interpretation[:120] if funding_interpretation else "",
        cvd_contract_trend=cvd_contract_trend or "",
        cvd_spot_trend=cvd_spot_trend or "",
        nearest_magnet_above=mag_above,
        nearest_magnet_below=mag_below,
    )

    ts = int(op.ts_sec) if op and op.ts_sec else (int(kl.ts) if kl and kl.ts else now_sec)

    dq = BrainDataQuality(
        liquidity_wall_quality=lq or "",
        usd_usdt_basis_pct=op.usd_usdt_basis_pct if op else None,
        overall_freshness_score=freshness_score,
        stale_sources=stale,
        missing_sources=missing,
        notes=dq_notes,
        is_partial_ready=is_partial_ready,
        ready_count=ready_count,
        total_count=total_count,
    )

    # Phase 2：从 zones 派生 TradeSetupCandidate（不输出交易指令）
    from processors.opportunity_engine import build_opportunities
    opportunities = build_opportunities(
        zones=zones, last_price=last_price, atr=atr, ctx=ctx, dq=dq,
    )

    # Phase 4：事件驱动状态机推进（首屏即给准确状态分布）
    if opportunities:
        from processors.opportunity_state_machine import (
            StateTickContext,
            advance_all,
        )
        wall_evts = list(op.wall_events) if op else []
        advance_all(opportunities, StateTickContext(
            last_price=last_price,
            now_sec=now_sec,
            wall_events=wall_evts,
            ctx=ctx,
            dq=dq,
        ))

    spot_book = _build_spot_book(op)
    fut_book = _build_fut_book(op=op, liq=liq, kl=kl, last_price=last_price)

    return TradingBrainSnapshot(
        coin=coin.upper(),
        ts=ts,
        last_price=last_price,
        atr=round(atr, 4),
        summary=summary,
        context=ctx,
        zones=zones,
        rankings=rankings,
        events=events,
        data_quality=dq,
        opportunities=opportunities,
        spot_book=spot_book,
        fut_book=fut_book,
    )
