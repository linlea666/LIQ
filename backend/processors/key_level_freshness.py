"""Key Level 数据血统/新鲜度引擎（M1 · V3 准备阶段）。

为什么需要：
  V3 关键位评分依赖多源（清算 1d/7d/30d、heatmap、max_pain、footprint、orderbook
  pressure、CVD…）。一旦某源缺失或卡住，旧版仍以满分参与排序 → 用户被误导。

设计：
  1. compute_freshness(state) → DataFreshness：
     扫描 state 上的关键源 ts 字段，计算 age_seconds，对照 SOURCE_TTL 判断是否过期；
     输出 sources_age_seconds / overall_freshness_score / stale_sources / missing_sources
  2. apply_freshness_to_level(lv, freshness)：
     - 主源若 stale → lv.is_stale=True；final_score × 0.85 软衰减
     - 主源若 missing → 从 explain_chips 移除该源的标签
     不会硬下调 tier（避免与 confluence 评分耦合），只在 final_score 末端调节，让用户
     仍然看得到这个位但被标记"⏳ 数据偏旧"。

主源识别：
  根据 KeyLevelV2.sources（白话来源数组）做关键字匹配，映射到 SOURCE_TTL 中的源名；
  例如 "7d清算簇" → "liq_map_7d"，"VWAP" → "vwap_age_unbounded"（永久新鲜）。

注意（保守原则 dev-constraints #6）：
  - 不修改 confluence_score / strength_tier 主公式
  - final_score 只做 0.85 软衰减，可关闭（apply_freshness_to_level 接收 enabled 参数）
"""

from __future__ import annotations

import time
from typing import Any, Optional

from models.key_level import DataFreshness, KeyLevelV2

# 各数据源 TTL（秒）—— 超过即视为 stale
# 设计依据：单源 TTL ≥ poll_interval × 2（保守，避免抖动误报）
SOURCE_TTL: dict[str, float] = {
    "liq_map_1d": 600,         # poll 90s × ~6 = 1d 清算地图新鲜窗口
    "liq_map_7d": 3600,        # poll 90s × 多次但 7d 数据本身冷
    "liq_map_30d": 7200,       # 远期数据，2h 容忍度
    "liq_heatmap_24h": 1200,   # poll 600s
    "liq_heatmap_7d": 3600,    # poll 1800s
    "liq_max_pain": 600,       # poll 300s × 2
    "footprint_contract": 300,
    "orderbook_pressure": 90,  # poll ≤ 60s
    "cvd": 600,
    "oi": 600,
    "ticker": 60,              # ticker 必须实时
    # 永久新鲜源（指标计算自蜡烛，蜡烛新鲜即新鲜）
    "ema_daily": 3600,
    "vwap": 1800,
    "candles_4h": 3600,
    "candles_daily": 7200,
    "candles_weekly": 86400,
    "vp": 1800,                # VP 来自 trades 累积
}


# source_tag → SOURCE_TTL key 的映射（白话→机器键）
# 注意：候选位 source_tag 在 level_discovery.py 内定义
SOURCE_TAG_TO_KEY: dict[str, str] = {
    # 清算簇
    "liq_cluster_below_1d": "liq_map_1d",
    "liq_cluster_above_1d": "liq_map_1d",
    "liq_cluster_below_7d": "liq_map_7d",
    "liq_cluster_above_7d": "liq_map_7d",
    "liq_cluster_below_30d": "liq_map_30d",
    "liq_cluster_above_30d": "liq_map_30d",
    # 微观结构
    "absorption_zone_support": "footprint_contract",
    "absorption_zone_resistance": "footprint_contract",
    "footprint_stacked": "footprint_contract",
    "footprint_poc": "footprint_contract",
    "orderbook_wall": "orderbook_pressure",
    "orderbook_pressure": "orderbook_pressure",
    # 资金/持仓
    "oi_surge_zone": "oi",
    "vp_poc": "vp",
    "vp_val": "vp",
    "vp_vah": "vp",
    "vwap": "vwap",
    # 价格结构（自蜡烛派生 → 走蜡烛 TTL）
    "swing_high_4h": "candles_4h",
    "swing_low_4h": "candles_4h",
    "swing_high_1d": "candles_daily",
    "swing_low_1d": "candles_daily",
    "swing_high_1w": "candles_weekly",
    "swing_low_1w": "candles_weekly",
    "unfilled_wick_low": "candles_daily",
    "unfilled_wick_high": "candles_daily",
    # 数学指标
    "sma_200_1d": "candles_daily",
    "ema_50_1d": "ema_daily",
    "ema_200_1d": "ema_daily",
    # 注：心理关口、Pivot、Fib、Ichimoku 等为永久新鲜（不在此 dict）
}


def _safe_age(state: Any, attr: str, now: float) -> Optional[float]:
    """读 state.{attr} 的时间戳字段计算 age；缺失返回 None。

    时间戳字段名兼容（按优先级）：
      1) BaseModel / 普通对象的 .ts（多数 LIQ 模型）
      2) BaseModel / 普通对象的 .ts_sec（OrderbookPressureSnapshot 等）
      3) BaseModel / 普通对象的 .timestamp
      4) dict 的 'ts' / 'ts_sec' / 'timestamp'
      5) state.{attr} 本身就是 int/float 时间戳
    """
    obj = getattr(state, attr, None)
    if obj is None:
        return None
    ts: Optional[float] = None
    for key in ("ts", "ts_sec", "timestamp"):
        if hasattr(obj, key):
            v = getattr(obj, key, None)
            if v is not None:
                ts = v
                break
    if ts is None and isinstance(obj, dict):
        for key in ("ts", "ts_sec", "timestamp"):
            v = obj.get(key)
            if v is not None:
                ts = v
                break
    if ts is None and isinstance(obj, (int, float)):
        ts = obj
    if ts is None or ts <= 0:
        return None
    if ts > 1e12:
        ts = ts / 1000.0
    return max(0.0, now - float(ts))


def compute_freshness(state: Any) -> DataFreshness:
    """扫描 state 关键源 ts，计算综合新鲜度。

    state 类型：engine.CoinState（不强类型化避免循环 import）；预期字段：
      ticker / liq_maps[1d|7d|30d] / liq_heatmaps[24h|7d] / liq_max_pain[24h] /
      footprint_contract / orderbook_pressure_snapshot / cvd_contract / oi /
      candles_4h / candles_daily / candles_weekly / ema_daily / vp
    """
    now = time.time()
    ages: dict[str, float] = {}
    missing: list[str] = []
    stale: list[str] = []

    # 实时类（走 ticker.ts）
    age = _safe_age(state, "ticker", now)
    if age is None:
        missing.append("ticker")
    else:
        ages["ticker"] = round(age, 1)
        if age > SOURCE_TTL["ticker"]:
            stale.append("ticker")

    # 多周期清算地图
    liq_maps = getattr(state, "liq_maps", None) or {}
    for cycle, key in (("1d", "liq_map_1d"), ("7d", "liq_map_7d"), ("30d", "liq_map_30d")):
        m = liq_maps.get(cycle)
        if m is None:
            missing.append(key)
            continue
        ts = getattr(m, "ts", 0) or 0
        if ts <= 0:
            missing.append(key)
            continue
        a = max(0.0, now - float(ts))
        ages[key] = round(a, 1)
        if a > SOURCE_TTL[key]:
            stale.append(key)

    # 热力图
    liq_heatmaps = getattr(state, "liq_heatmaps", None) or {}
    for hkey, key in (("24h", "liq_heatmap_24h"), ("7d", "liq_heatmap_7d")):
        h = liq_heatmaps.get(hkey)
        if h is None:
            missing.append(key)
            continue
        ts = getattr(h, "ts", 0) or 0
        if ts <= 0:
            missing.append(key)
            continue
        a = max(0.0, now - float(ts))
        ages[key] = round(a, 1)
        if a > SOURCE_TTL[key]:
            stale.append(key)

    # max_pain
    liq_max_pain = getattr(state, "liq_max_pain", None) or {}
    mp = liq_max_pain.get("24h")
    if mp is None:
        missing.append("liq_max_pain")
    else:
        ts = getattr(mp, "ts", 0) or 0
        if ts <= 0:
            missing.append("liq_max_pain")
        else:
            a = max(0.0, now - float(ts))
            ages["liq_max_pain"] = round(a, 1)
            if a > SOURCE_TTL["liq_max_pain"]:
                stale.append("liq_max_pain")

    # 微观结构
    # state.footprint_contract 是 deque[dict]（polls.footprint._parse_bar 写入），
    # 结构 {"ts": int(秒), "buckets": [...]}；ts 偶尔毫秒（13 位）兼容处理
    fp = getattr(state, "footprint_contract", None)
    if not fp:
        missing.append("footprint_contract")
    else:
        try:
            last_bar = fp[-1]
            if isinstance(last_bar, dict):
                ts = last_bar.get("end_ts") or last_bar.get("ts") or 0
            else:
                ts = getattr(last_bar, "end_ts", 0) or getattr(last_bar, "ts", 0) or 0
            if ts and ts > 0:
                ts_sec = float(ts) / 1000.0 if ts > 1e12 else float(ts)
                a = max(0.0, now - ts_sec)
                ages["footprint_contract"] = round(a, 1)
                if a > SOURCE_TTL["footprint_contract"]:
                    stale.append("footprint_contract")
            else:
                missing.append("footprint_contract")
        except (IndexError, AttributeError, TypeError):
            missing.append("footprint_contract")

    age = _safe_age(state, "orderbook_pressure_snapshot", now)
    if age is None:
        missing.append("orderbook_pressure")
    else:
        ages["orderbook_pressure"] = round(age, 1)
        if age > SOURCE_TTL["orderbook_pressure"]:
            stale.append("orderbook_pressure")

    # CVD / OI
    for attr, key in (("cvd_contract", "cvd"), ("oi", "oi")):
        age = _safe_age(state, attr, now)
        if age is None:
            missing.append(key)
        else:
            ages[key] = round(age, 1)
            if age > SOURCE_TTL[key]:
                stale.append(key)

    # 综合分（除去永久新鲜类，仅用上述 8-10 个核心源做分母）
    core_keys = [
        "liq_map_1d", "liq_map_7d", "liq_map_30d",
        "liq_heatmap_24h", "liq_max_pain",
        "footprint_contract", "orderbook_pressure",
        "cvd", "oi",
    ]
    total_core = len(core_keys)
    bad = sum(1 for k in core_keys if k in stale or k in missing)
    overall = round(100.0 * (1 - bad / total_core), 1) if total_core else 100.0

    return DataFreshness(
        ts=int(now),
        sources_age_seconds=ages,
        overall_freshness_score=overall,
        stale_sources=sorted(stale),
        missing_sources=sorted(missing),
    )


def apply_freshness_to_level(
    lv: KeyLevelV2,
    freshness: DataFreshness,
    *,
    decay_factor: float = 0.85,
) -> None:
    """对单个 level 应用新鲜度软衰减。

    设计原则（dev-constraints #6 保守修改）：
      - 不动 confluence_score / strength_tier 主公式
      - 仅在 final_score 末端做 × decay_factor 软衰减
      - 主源 stale 时设 lv.is_stale=True，并记录 primary_source_age_hours
      - 不删除 sources / explain_chips（保留可见性）

    主源识别：
      遍历 lv.sources（白话来源），找到首个匹配 SOURCE_TAG_TO_KEY 的关键字；
      若它对应的源在 freshness.stale_sources 中 → 衰减；
      若在 missing_sources 中 → 不衰减但记 is_stale=True（数据没了，无法判断旧不旧）
    """
    if not freshness:
        return

    primary_key: Optional[str] = None
    # 简单关键字匹配（足够 M1，M2 时 source_tag 直接落到 lv 上避免字符串匹配）
    for src in lv.sources:
        # source 是白话；可能含 "7d清算簇" / "VWAP" / "200日SMA" 等
        if "30d" in src or "30天" in src:
            primary_key = "liq_map_30d"
            break
        if "7d清算" in src or "7天清算" in src:
            primary_key = "liq_map_7d"
            break
        if "清算" in src:
            primary_key = "liq_map_1d"
            break
        if "Footprint" in src or "吸收带" in src:
            primary_key = "footprint_contract"
            break
        if "VWAP" in src:
            primary_key = "vwap"
            break
        if "热力图" in src or "heatmap" in src.lower():
            primary_key = "liq_heatmap_24h"
            break

    if primary_key is None:
        return  # 该 level 主源是永久新鲜类（指标 / Fib / 心理关口），不需衰减

    age = freshness.sources_age_seconds.get(primary_key)
    if age is not None:
        lv.primary_source_age_hours = round(age / 3600.0, 2)

    if primary_key in freshness.stale_sources:
        lv.is_stale = True
        lv.final_score = round(max(0.0, lv.final_score * decay_factor), 1)
    elif primary_key in freshness.missing_sources:
        # 主源缺失 = 数据消失，不衰减分数（可能是临时网络问题），仅打 is_stale 标
        lv.is_stale = True
