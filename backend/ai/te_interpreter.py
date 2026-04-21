"""趋势衰竭模块 · AI 解读器（DeepSeek Reasoner 驱动）

职责边界
--------
**AI 是规则引擎的审计员 + 再判断者，不是翻译器。**

- 规则引擎（`processors/trend_exhaustion.py`）只是**初步整理了数据**：给出 sub_scores
  的分值与 note，以及根据机械规则算出的 `overall_direction / state / action`。
- 本模块基于 AI 的推理能力，**拿着 sub_scores 原始读数 + 关键位快照 + 多周期证据
  再判一遍**。输出五类价值：
    1. 【趋势评估】当前真实趋势 + 动能健康度（**有权推翻规则的 direction**）
    2. 【关键位投射】能否突破最近 S/A 级关键位 + 置信分档
    3. 【矛盾消解】识别 sub 暗中对抗 direction 的情况
    4. 【陷阱提醒 + 触发条件】按规则行动的坑 + 下一个要看的信号
    5. 【交易倾向】可选：给方向 + 区间 + 失效位 + 时间窗（允许独立）

**严禁**引用历史价格 / 走势（防事实幻觉）；**严禁**编造 key_levels 之外的价位。

设计决策
--------
- 模型：**deepseek-reasoner**（R1 思维链，擅长多因子矛盾消解）
- 输出：强制 JSON schema，解析失败降级为 error 兜底
- 缓存：按"信号指纹"缓存 30 分钟
- 客户端独立：不复用主 AIAnalyzer
- 思考过程归档：reasoning_content 落盘单独 *.thinking.jsonl
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from config.settings import get_settings
from models.te_interpretation import TEAIInterpretation

logger = logging.getLogger(__name__)

# ── 配置常量 ──────────────────────────────────────
_CACHE_TTL_SEC = 5 * 60            # 同指纹缓存 5 分钟（配合桶化快变量指纹，支持分钟级刷新）
_CACHE_MAX_ENTRIES = 200           # LRU 上限
_MAX_REASONING_STORE_CHARS = 50000 # reasoning 单次入库最大长度
_DEFAULT_TIMEOUT_SEC = 180         # Reasoner 思考可能慢


@dataclass
class _CacheEntry:
    result: TEAIInterpretation
    expires_at: float


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据压缩函数（把 TrendExhaustionSignal + KeyLevelSnapshotV2 压成 AI 能消化的结构）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _bucket(x, size: float):
    """把数值量化到指定桶大小。x=None → None；异常 → None。

    例：_bucket(0.17, 0.2) = 0.2；_bucket(0.3, 0.2) = 0.4（跳档阈值 = size/2）。
    """
    if x is None:
        return None
    try:
        return round(float(x) / size) * size
    except Exception:
        return None


def _extras_fingerprint_buckets(extras: Optional[dict]) -> dict:
    """把 extras_dict 中的"快变量"桶化后加入指纹键。

    策略：**变化快但有信号价值**的字段进指纹 + 每项单独桶化（避免毛刺），
    配合 5min TTL 实现"行情剧变即刷新，平稳时吃缓存"。

    每项独立 try/except，一条异常不影响其他字段参与指纹。
    """
    if not extras:
        return {}
    out: dict = {}

    # ── OI 变化率（多周期桶化） ──
    try:
        oi = extras.get("oi") or {}
        out["oi_5m"] = _bucket(oi.get("change_5m_pct"), 0.2)
        out["oi_1h"] = _bucket(oi.get("change_1h_pct"), 0.5)
    except Exception:
        pass
    try:
        out["oi_4h"] = _bucket(extras.get("oi_4h_pct"), 0.5)
    except Exception:
        pass
    try:
        out["oi_24h"] = _bucket(extras.get("oi_change_24h_pct"), 1.0)
    except Exception:
        pass

    # ── Funding（转 bp，桶化到 1bp） ──
    try:
        funding = extras.get("funding") or {}
        r = funding.get("oi_weighted_rate")
        if r is not None:
            out["fr_oiw_bp"] = _bucket(float(r) * 10000.0, 1.0)
    except Exception:
        pass
    try:
        mf = extras.get("multi_funding") or {}
        r = mf.get("oi_weighted")
        if r is not None:
            out["mfr_bp"] = _bucket(float(r) * 10000.0, 1.0)
    except Exception:
        pass

    # ── LS Ratio × 3 维度（桶化到 0.1） ──
    for key, alias in (
        ("ls_ratio", "lsr_g"),
        ("ls_top_account", "lsr_a"),
        ("ls_top_position", "lsr_p"),
    ):
        try:
            ls = extras.get(key) or {}
            out[alias] = _bucket(ls.get("avg_ratio"), 0.1)
        except Exception:
            pass

    # ── Market Structure：事件 + 分钟级时间戳（新事件立即翻新） ──
    for key, alias in (("ms_1h", "ms1h"), ("ms_1d", "ms1d"), ("ms_1w", "ms1w")):
        try:
            ms = extras.get(key) or {}
            out[f"{alias}_ev"] = ms.get("last_event") or ""
            ts_ms = int(ms.get("event_ts", 0) or 0)
            out[f"{alias}_min"] = ts_ms // 60_000
            out[f"{alias}_dir"] = ms.get("direction") or ""
        except Exception:
            pass

    # ── Liq Map：不对称性 + 最近两侧簇中心价（整百） ──
    try:
        lm = extras.get("liq_map_1d") or {}
        out["liq_ib"] = _bucket(lm.get("imbalance_ratio"), 0.5)
        above = (lm.get("clusters_above") or [])
        below = (lm.get("clusters_below") or [])
        if above:
            out["liq_a0"] = _bucket(above[0].get("price_center"), 100.0)
        if below:
            out["liq_b0"] = _bucket(below[0].get("price_center"), 100.0)
    except Exception:
        pass

    return out


def _fingerprint(
    coin: str,
    signal_dict: dict,
    kl_dict: Optional[dict] = None,
    extras_dict: Optional[dict] = None,
) -> str:
    """为信号生成缓存键：采纳"判定性"字段 + 桶化的"快变量"。

    采纳：
      - TE 主维度：coin + overall_state + direction + regime + regime_vetoed + consensus
      - TE 多周期：tf_1h/4h/1d 的 composite（0.1 精度）+ state
      - key_levels：S/A 级强位价格（百位）+ 牛熊分界 regime
      - extras 快变量（桶化）：OI/funding/LS/MS 事件/liq 不对称性
    """
    keys = {
        "coin": coin.upper(),
        "s": signal_dict.get("overall_state"),
        "d": signal_dict.get("overall_direction"),
        "r": signal_dict.get("regime"),
        "v": signal_dict.get("regime_vetoed"),
        "c": signal_dict.get("consensus_level"),
    }
    for tf_key in ("tf_1h", "tf_4h", "tf_1d"):
        tf = signal_dict.get(tf_key) or {}
        comp = float(tf.get("composite_score", 0.0) or 0.0)
        keys[f"{tf_key}_c"] = round(comp, 1)
        keys[f"{tf_key}_s"] = tf.get("state")

    # key_levels 参与指纹：只取 S/A 级强位价格（四舍五入到百位）避免频繁刷新
    if kl_dict:
        try:
            top_prices: list[float] = []
            for lv in (kl_dict.get("levels") or [])[:10]:
                if lv.get("strength_tier") in ("S", "A"):
                    top_prices.append(round(float(lv.get("price", 0.0)) / 100.0) * 100.0)
            keys["kl"] = sorted(top_prices)[:6]
            bb = (kl_dict.get("bull_bear_line") or {}).get("current_regime")
            keys["bb"] = bb
        except Exception:
            pass

    # extras 快变量（桶化后参与指纹，分钟级响应行情剧变）
    keys.update(_extras_fingerprint_buckets(extras_dict))

    s = json.dumps(keys, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def _compact_sub(sub: list[dict] | None) -> list[dict]:
    """保留 note 原文（含 RSI=65.5、价离 EMA20 +3.4σ、OI+1.15% 等关键数值）。"""
    if not sub:
        return []
    return [
        {
            "name": s.get("name"),
            "score": round(float(s.get("score", 0.0)), 2),
            "note": s.get("note", ""),
        }
        for s in sub
    ]


def _compact_tf(tf: dict | None) -> Optional[dict]:
    if not tf:
        return None
    return {
        "direction": tf.get("direction"),
        "state": tf.get("state"),
        "composite": round(float(tf.get("composite_score", 0.0) or 0.0), 2),
        "m": round(float(tf.get("momentum_score", 0.0) or 0.0), 2),
        "p": round(float(tf.get("participation_score", 0.0) or 0.0), 2),
        "e": round(float(tf.get("exhaustion_score", 0.0) or 0.0), 2),
        "age_min": int(tf.get("state_age_min", 0) or 0),
        "confirmed": int(tf.get("confirmed_ticks", 0) or 0),
        "triggers": tf.get("triggers") or [],
        "sub": _compact_sub(tf.get("sub_scores")),
    }


def _compact_key_levels(kl_dict: Optional[dict], current_price: float) -> Optional[dict]:
    """从 KeyLevelSnapshotV2 提取 AI 需要的精华：
    - S/A 级强支撑阻力（最多 3+3 档，按距离排序）
    - 牛熊分界线（判断 primary_trend 的硬锚）
    - 挤压带（突破方向预判）
    - 多周期级最强位
    - 结构摘要

    - 传入 None → 返回 None（表示上层根本没有 key_levels 模块）
    - 传入 {} / 无 levels → 返回空结构（表示 key_levels 存在但当前无强位）
    """
    if kl_dict is None:
        return None

    levels = kl_dict.get("levels") or []
    strong_resistances: list[dict] = []
    strong_supports: list[dict] = []

    for lv in levels:
        tier = lv.get("strength_tier", "C")
        if tier not in ("S", "A"):
            continue
        item = {
            "price": round(float(lv.get("price", 0.0)), 2),
            "tier": tier,
            "distance_pct": round(float(lv.get("distance_pct", 0.0)), 2),
            "state": lv.get("state", "idle"),
            "sources": lv.get("sources") or [],
            "source_count": int(lv.get("source_count", 0) or 0),
            "historical_validity": round(float(lv.get("historical_validity", 0.0) or 0.0), 2),
            "bounce_count": int(lv.get("bounce_count", 0) or 0),
            "pattern": lv.get("pattern_detected") or "",
            "final_score": round(float(lv.get("final_score", 0.0) or 0.0), 1),
            "note": (lv.get("note") or "")[:60],
            "timeframe": lv.get("timeframe") or "",
        }
        if lv.get("side") == "resistance":
            strong_resistances.append(item)
        elif lv.get("side") == "support":
            strong_supports.append(item)

    # 按距离排序（最近的排前面）
    strong_resistances.sort(key=lambda x: abs(x["distance_pct"]))
    strong_supports.sort(key=lambda x: abs(x["distance_pct"]))

    # 截断：最多 3 档 + 3 档
    strong_resistances = strong_resistances[:3]
    strong_supports = strong_supports[:3]

    bbl = kl_dict.get("bull_bear_line") or {}
    bull_bear_line = None
    if bbl:
        bull_bear_line = {
            "regime": bbl.get("current_regime", ""),
            "reason": (bbl.get("regime_reason") or "")[:100],
            "sma200d": bbl.get("sma200d"),
            "cloud_top": bbl.get("ichimoku_cloud_top"),
            "cloud_bottom": bbl.get("ichimoku_cloud_bottom"),
        }

    bz = kl_dict.get("breakout_zone") or {}
    breakout_zone = None
    if bz and (bz.get("bb_squeeze") or bz.get("squeeze_direction")):
        breakout_zone = {
            "squeeze": bool(bz.get("bb_squeeze", False)),
            "direction": bz.get("squeeze_direction", ""),
            "bb_upper": bz.get("bb_upper"),
            "bb_lower": bz.get("bb_lower"),
            "note": (bz.get("note") or "")[:100],
        }

    return {
        "current_price": round(current_price, 2) if current_price else None,
        "strong_resistances": strong_resistances,
        "strong_supports": strong_supports,
        "bull_bear_line": bull_bear_line,
        "breakout_zone": breakout_zone,
        "daily_strong_support": kl_dict.get("daily_strong_support"),
        "daily_strong_resistance": kl_dict.get("daily_strong_resistance"),
        "weekly_strong_support": kl_dict.get("weekly_strong_support"),
        "weekly_strong_resistance": kl_dict.get("weekly_strong_resistance"),
        "structure_summary": (kl_dict.get("structure_summary") or "")[:120],
        "nearest_strong_support": kl_dict.get("nearest_strong_support"),
        "nearest_strong_resistance": kl_dict.get("nearest_strong_resistance"),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 扩展数据压缩：Market Structure / Flow / Sentiment / Liq Fuel
# （给 AI 审计时做更独立的趋势判断 + 拥挤度判断 + 磁吸判断）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _compact_one_ms(ms: Optional[dict], price: float) -> Optional[dict]:
    """单周期 MarketStructure 压缩（提炼 AI 能直接用的锚）。"""
    if not ms:
        return None
    try:
        structure_high = float(ms.get("structure_high", 0.0) or 0.0)
        structure_low = float(ms.get("structure_low", 0.0) or 0.0)
        event_ts = int(ms.get("event_ts", 0) or 0)
        now_ms = int(time.time() * 1000)
        event_age_min: Optional[int] = None
        if event_ts > 0:
            event_age_min = max(0, (now_ms - event_ts) // 60_000)
        price_vs_high_pct = None
        price_vs_low_pct = None
        if price and structure_high > 0:
            price_vs_high_pct = round((price - structure_high) / structure_high * 100.0, 2)
        if price and structure_low > 0:
            price_vs_low_pct = round((price - structure_low) / structure_low * 100.0, 2)
        return {
            "timeframe": ms.get("timeframe") or "",
            "direction": ms.get("direction") or "ranging",
            "last_event": ms.get("last_event") or "",
            "event_age_min": event_age_min,
            "structure_high": round(structure_high, 2) if structure_high else None,
            "structure_low": round(structure_low, 2) if structure_low else None,
            "price_vs_high_pct": price_vs_high_pct,
            "price_vs_low_pct": price_vs_low_pct,
            "operate_bias": ms.get("operate_bias") or "stand_aside",
            "confidence": round(float(ms.get("confidence", 0.0) or 0.0), 2),
            "summary": (ms.get("summary") or "")[:80],
        }
    except Exception:
        return None


def _compact_market_structure(
    ms_1h: Optional[dict],
    ms_1d: Optional[dict],
    ms_1w: Optional[dict],
    price: float,
) -> Optional[dict]:
    """多周期 Market Structure 压缩 + 跨周期对齐标签。

    输出含 alignment:
      - "aligned_up"：1h/1d/1w 全 bullish
      - "aligned_down"：1h/1d/1w 全 bearish
      - "mixed"：周期间方向冲突
      - "ranging"：全 ranging/transitioning
      - "insufficient"：数据缺失（缺 ≥2 周期）

    对齐标签是 AI 判"真实趋势"的硬锚（权重高于 bull_bear_line）。
    """
    c_1h = _compact_one_ms(ms_1h, price)
    c_1d = _compact_one_ms(ms_1d, price)
    c_1w = _compact_one_ms(ms_1w, price)

    if not any([c_1h, c_1d, c_1w]):
        return None

    # 跨周期对齐
    alignment = "insufficient"
    dirs = [c["direction"] for c in (c_1h, c_1d, c_1w) if c]
    if len(dirs) >= 2:
        bull_count = sum(1 for d in dirs if d == "bullish")
        bear_count = sum(1 for d in dirs if d == "bearish")
        range_count = sum(1 for d in dirs if d in ("ranging", "transitioning"))
        if bull_count == len(dirs):
            alignment = "aligned_up"
        elif bear_count == len(dirs):
            alignment = "aligned_down"
        elif range_count == len(dirs):
            alignment = "ranging"
        elif bull_count > 0 and bear_count > 0:
            alignment = "mixed"
        else:
            alignment = "mixed"

    return {
        "1h": c_1h,
        "1d": c_1d,
        "1w": c_1w,
        "alignment": alignment,
    }


def _extreme_tag_from_bp(bp: float) -> str:
    """funding 极端分档（8h 基点）。

    >+10 bp: crowded_long（做多拥挤）
    <-10 bp: crowded_short（做空拥挤）
    |x| <= 3 bp: neutral
    其他: mild_bias_long / mild_bias_short
    """
    try:
        v = float(bp)
    except Exception:
        return "unknown"
    if v > 10:
        return "crowded_long"
    if v < -10:
        return "crowded_short"
    if v >= 3:
        return "mild_bias_long"
    if v <= -3:
        return "mild_bias_short"
    return "neutral"


def _compact_flow_metrics(
    funding: Optional[dict],
    multi_funding: Optional[dict],
    oi: Optional[dict],
    oi_history: Optional[list],
    oi_change_24h_pct: Optional[float],
) -> Optional[dict]:
    """funding + OI 压缩（含 4h% 现算）。

    4h% 算法：从 oi_history（5m 间隔）取当前与第 -48 条（4h 前）相比。
    不足 48 条 → 返回 None（不报错）。
    """
    has_any = funding or multi_funding or oi
    if not has_any:
        return None

    out: dict = {}

    # ── funding（官方主源） ──
    if funding:
        try:
            okx = funding.get("okx_rate")
            bn = funding.get("binance_rate")
            avg = funding.get("avg_rate", 0) or 0
            oiw = funding.get("oi_weighted_rate", 0) or 0
            oiw_bp = float(oiw) * 10000.0
            out["funding"] = {
                "okx_bp": round(float(okx) * 10000.0, 2) if okx is not None else None,
                "binance_bp": round(float(bn) * 10000.0, 2) if bn is not None else None,
                "avg_bp": round(float(avg) * 10000.0, 2),
                "oi_weighted_bp": round(oiw_bp, 2),
                "extreme_tag": _extreme_tag_from_bp(oiw_bp),
                "interpretation": (funding.get("interpretation") or "")[:80],
            }
        except Exception:
            pass

    # ── multi_funding（多交易所 + 7d 均值） ──
    if multi_funding:
        try:
            avg_current = float(multi_funding.get("avg_current", 0) or 0)
            avg_7d = float(multi_funding.get("avg_7d", 0) or 0)
            oi_weighted = float(multi_funding.get("oi_weighted", 0) or 0)
            exchanges_in = multi_funding.get("exchanges") or []
            exchanges_out = []
            for ex in exchanges_in[:5]:
                try:
                    cur = ex.get("current")
                    a7 = ex.get("avg_7d")
                    exchanges_out.append({
                        "exchange": ex.get("exchange") or "",
                        "current_bp": round(float(cur) * 10000.0, 2) if cur is not None else None,
                        "avg_7d_bp": round(float(a7) * 10000.0, 2) if a7 is not None else None,
                    })
                except Exception:
                    continue
            out["multi_funding"] = {
                "avg_current_bp": round(avg_current * 10000.0, 2),
                "avg_7d_bp": round(avg_7d * 10000.0, 2),
                "deviation_bp": round((avg_current - avg_7d) * 10000.0, 2),
                "oi_weighted_bp": round(oi_weighted * 10000.0, 2),
                "exchanges": exchanges_out,
                "interpretation": (multi_funding.get("interpretation") or "")[:80],
            }
        except Exception:
            pass

    # ── OI 多周期（1h/4h/24h） ──
    oi_block: dict = {}
    if oi:
        try:
            current_usd = float(oi.get("current_usd", 0) or 0)
            oi_block["current_usd_m"] = round(current_usd / 1e6, 1) if current_usd else None
            oi_block["change_5m_pct"] = oi.get("change_5m_pct")
            oi_block["change_1h_pct"] = oi.get("change_1h_pct")
            oi_block["trend"] = oi.get("trend") or ""
        except Exception:
            pass

    # 4h%：从 oi_history 现算（5m 粒度 × 48 = 4h）
    try:
        if oi_history and len(oi_history) >= 48:
            current_point = oi_history[-1]
            past_point = oi_history[-48]
            cur_v = float(current_point.get("oi_usd", 0) or 0)
            past_v = float(past_point.get("oi_usd", 0) or 0)
            if past_v > 0:
                oi_block["change_4h_pct"] = round((cur_v - past_v) / past_v * 100.0, 2)
    except Exception:
        pass

    # 24h% 用现成字段
    try:
        if oi_change_24h_pct is not None:
            oi_block["change_24h_pct"] = round(float(oi_change_24h_pct), 2)
    except Exception:
        pass

    if oi_block:
        out["oi"] = oi_block

    return out if out else None


def _ls_divergence_label(
    retail: Optional[dict], top_acct: Optional[dict], top_pos: Optional[dict]
) -> str:
    """散户 vs 大户多空比的分歧标签。

    - retail_long_smart_short：散户偏多 (>1.2) + 大户账户或持仓偏空 (<0.9)
    - retail_short_smart_long：散户偏空 (<0.8) + 大户偏多 (>1.1)
    - aligned：散户与大户同向
    - mild_divergence：轻微分歧
    - n/a：缺数据
    """
    def _ratio(d):
        try:
            return float((d or {}).get("avg_ratio", 1.0))
        except Exception:
            return 1.0

    if not retail and not top_acct and not top_pos:
        return "n/a"

    r = _ratio(retail) if retail else None
    ta = _ratio(top_acct) if top_acct else None
    tp = _ratio(top_pos) if top_pos else None
    smart_ratios = [x for x in (ta, tp) if x is not None]
    if r is None or not smart_ratios:
        return "n/a"
    smart_avg = sum(smart_ratios) / len(smart_ratios)

    if r > 1.2 and smart_avg < 0.9:
        return "retail_long_smart_short"
    if r < 0.8 and smart_avg > 1.1:
        return "retail_short_smart_long"
    # 同向判断：比值都 >1 或都 <1 且偏离幅度 > 0.1
    if (r > 1.05 and smart_avg > 1.05) or (r < 0.95 and smart_avg < 0.95):
        return "aligned"
    return "mild_divergence"


def _compact_one_ls(ls: Optional[dict]) -> Optional[dict]:
    if not ls:
        return None
    try:
        exchanges = ls.get("exchanges") or []
        long_pct: Optional[float] = None
        short_pct: Optional[float] = None
        if exchanges:
            try:
                long_pct = round(
                    sum(float(e.get("long_pct", 0) or 0) for e in exchanges) / len(exchanges), 2,
                )
                short_pct = round(
                    sum(float(e.get("short_pct", 0) or 0) for e in exchanges) / len(exchanges), 2,
                )
            except Exception:
                pass
        return {
            "avg_ratio": round(float(ls.get("avg_ratio", 1.0) or 1.0), 3),
            "long_pct": long_pct,
            "short_pct": short_pct,
            "cycle": ls.get("cycle") or "",
            "interpretation": (ls.get("interpretation") or "")[:80],
        }
    except Exception:
        return None


def _compact_sentiment(
    ls_retail: Optional[dict],
    ls_top_account: Optional[dict],
    ls_top_position: Optional[dict],
) -> Optional[dict]:
    """三维度多空比压缩（散户 + 大户账户 + 大户持仓）+ 分歧标签。

    散户 vs 大户反向（retail_long_smart_short 或 retail_short_smart_long）是
    AI 判断"聪明钱 vs 情绪钱"最有价值的反转预警信号。
    """
    if not any([ls_retail, ls_top_account, ls_top_position]):
        return None
    return {
        "retail": _compact_one_ls(ls_retail),
        "top_account": _compact_one_ls(ls_top_account),
        "top_position": _compact_one_ls(ls_top_position),
        "divergence": _ls_divergence_label(ls_retail, ls_top_account, ls_top_position),
    }


def _asymmetry_label(imbalance_ratio: float) -> str:
    """imbalance_ratio = above_total / below_total。"""
    try:
        r = float(imbalance_ratio)
    except Exception:
        return "unknown"
    if r > 1.5:
        return "above_heavy"
    if r < 0.67:
        return "below_heavy"
    return "balanced"


def _compact_liq_fuel(
    liq_map_1d: Optional[dict], price: float
) -> Optional[dict]:
    """Liquidation Map 燃料分布压缩（1d 周期）。

    保留：
      - 上下方最多 3 个清算密集区（按距离排序，含 total_usd_m + side + dominant_leverage）
      - imbalance_ratio + asymmetry 标签（磁吸方向）
      - 最多 2 个真空区（vacuum zones）
    """
    if not liq_map_1d:
        return None
    try:
        clusters_above_raw = liq_map_1d.get("clusters_above") or []
        clusters_below_raw = liq_map_1d.get("clusters_below") or []

        def _pack_cluster(c: dict) -> Optional[dict]:
            try:
                return {
                    "price_center": round(float(c.get("price_center", 0.0) or 0.0), 2),
                    "price_from": round(float(c.get("price_from", 0.0) or 0.0), 2),
                    "price_to": round(float(c.get("price_to", 0.0) or 0.0), 2),
                    "distance_pct": round(float(c.get("distance_pct", 0.0) or 0.0), 2),
                    "total_usd_m": round(float(c.get("total_usd", 0.0) or 0.0) / 1e6, 2),
                    "side": c.get("side") or "",
                    "dominant_leverage": c.get("dominant_leverage") or "",
                }
            except Exception:
                return None

        above = [p for p in (_pack_cluster(c) for c in clusters_above_raw[:6]) if p]
        below = [p for p in (_pack_cluster(c) for c in clusters_below_raw[:6]) if p]
        above.sort(key=lambda x: abs(x.get("distance_pct") or 0.0))
        below.sort(key=lambda x: abs(x.get("distance_pct") or 0.0))

        vacuum_zones_raw = liq_map_1d.get("vacuum_zones") or []
        vacuum_zones: list[dict] = []
        for v in vacuum_zones_raw[:2]:
            try:
                vacuum_zones.append({
                    "from": round(float(v.get("price_from", 0.0) or 0.0), 2),
                    "to": round(float(v.get("price_to", 0.0) or 0.0), 2),
                    "mid": round(float(v.get("midpoint", 0.0) or 0.0), 2),
                    "note": (v.get("note") or "")[:50],
                })
            except Exception:
                continue

        imbalance = float(liq_map_1d.get("imbalance_ratio", 0.0) or 0.0)
        return {
            "above": above[:3],
            "below": below[:3],
            "imbalance_ratio": round(imbalance, 2),
            "asymmetry_note": _asymmetry_label(imbalance),
            "vacuum_zones": vacuum_zones,
            "current_price": round(price, 2) if price else None,
        }
    except Exception:
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Prompt 工程（核心：把 AI 从"翻译器"提升为"审计员 + 再判断者"）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SYSTEM_PROMPT = """你是 LIQ 项目的资深加密货币量化交易顾问（10+ 年实盘经验），定位是**规则引擎的审计员 + 独立判断者**。

## 关键定位（请务必理解）
规则引擎只是**初步整理**了当前这一刻的数据——它给出 sub_scores 分值 + note + 一个机械算出的 `overall_direction / state / action`。但规则算法存在**短视**的天然缺陷（例如近几根 K 线跌了就会说 direction=down，即便宏观仍在上涨趋势）。

**你的任务不是翻译规则结论，而是拿原始读数 + 关键位数据再判一遍。**
- 规则的 `overall_direction / overall_state / overall_action` 是**候选结论**，不是最终答案。
- 你有权**推翻**规则的方向判断，前提是你能从 sub 的 note 和 key_levels 中找到**具体数值证据**。
- 你必须在 `alignment_reason` 和 `conflict_resolution` 中**引用具体数值**（如 "RSI=65.5 偏强 + 价距 EMA20 +3.4σ + FVG 多1空0"），不准只说"感觉矛盾"。

## 你的五项输出任务
1. **趋势评估** (`trend_assessment`)：综合多周期 + 结构 + 动能，独立判断当前**真实**趋势 + 动能健康度
2. **关键位投射** (`level_projection`)：基于动能 + 结构共振，评估能否突破最近 S/A 级关键位
3. **场景识别 + 矛盾消解** (`scenario` + `conflict_resolution`)：识别市场结构 + 挖出 sub 暗中对抗 direction 的情况
4. **交易倾向** (`trade_bias`)：若条件足够，给出方向 + 入场区 + 失效位 + 时间窗（可选；条件不足就 direction="neutral"）
5. **独立观察** (`independent_view`)：规则没覆盖但你注意到的关键点（可留空字符串）

## 可推翻规则的 6 种合法情形
- 多数 sub 的 note 集体指向规则方向的**反面**（如 FVG 多1空0 + OI 踩踏做多 + CVD 资金在跟 ↔ direction=down）
- 规则 direction 只来自短期 1h，但 4h/1d 结构**完全相反**
- 价已远离 EMA20 ≥ 2σ 但**没有放量**——规则容易把"健康延伸"误当"衰竭"
- 挤压带 `breakout_zone.direction` 与规则方向相反，且动能已在转向
- `market_structure.alignment == "aligned_up"`（1h/1d/1w 全 bullish）但规则 direction=down → 可推翻为 bull_pullback
- `sentiment.divergence == "retail_long_smart_short"` + funding 进入 crowded_long → reversal_early 预警

## 关键位使用指引（这是 AI 的独立优势）
你会收到 `key_levels` 对象，包含：
- `strong_resistances` / `strong_supports`：S/A 级强位（不可编造其他价位）
- `bull_bear_line.regime`（bull/bear/neutral）：**宏观趋势硬锚**，权重高于规则的 overall_direction
- `breakout_zone`：挤压带预示的突破方向
- `daily/weekly_strong_support/resistance`：多周期级最强位

**使用规则**：
- `level_projection.target_level` **必须**引用 `key_levels` 中已有的价位，不准编造"76800 附近"这种模糊数。
- `level_projection.break_likelihood` 分档：
    - `very_likely` (>75% 自信)：动能充足 + 多周期共振 + 历史验证低阻力
    - `likely` (55-75%)：动能足够，存在 1-2 个反向因子
    - `uncertain` (45-55%)：证据对冲
    - `unlikely` (25-45%)：动能不足或遇到高强度位
    - `very_unlikely` (<25%)：遇到 S 级 + 历史验证强的位，且动能已衰竭
    - `insufficient`：key_levels 为空或距离过远（>5%）
- 若 `strong_resistances` 和 `strong_supports` **都为空**：`direction_tested = "none"`，`break_likelihood = "insufficient"`
- `if_break_cn` 要指向**下一档 key_level**（如 `strong_resistances[1]` 或 `daily_strong_resistance`）

## trade_bias 判定指引
- 只有当 `confidence >= 0.55` 且**方向明确**时给出非 neutral 的 direction
- `invalidation_cn` 必须引用 key_levels 中的反向关键位（如 "跌破 74973（S 级强支撑）"）
- 若遇到 regime_vetoed=true 的震荡/极端行情：**强制** direction="avoid"
- 若规则 + AI 判断分歧严重：direction="neutral"，让用户看观察点

## ⚠️ 特殊场景：`tf.*.direction == "flat"`（日线 ranging / transitioning）
当出现 `tf.1h.direction==flat` 或更高周期 direction==flat 时，**这是认知层的读数提示，不是决策层的方向限制**：
- 规则引擎在 flat 时会把所有 sub.score 强制归 0（这是有意设计不是 bug），
  `composite / m / p / e` 聚合分也会全为 0。**这不代表没证据，只是 score 的方向符号约定失效了**。
- 此时**请忽略 sub.score 字段，只看 sub.note 里的原始数值**（如 hist=+74.96、RSI=60.1、
  价距 EMA20 +1.61σ、OI+1.15%、CVD 同向等），自己根据数值方向推理。
- `key_levels` / `market_structure` / `flow_metrics` / `sentiment` / `liq_fuel` 五类数据**不受 flat 影响**，
  此时请把它们权重调高，优先用它们做趋势 + 关键位推理。
- 方向判断 / `alignment_with_rules` / `trade_bias` 等最终结论**完全由你基于上述原始证据独立判断**，
  该 long 就 long、该 strong_disagree 就 strong_disagree；本段不额外加方向约束，通用段的硬约束
  （trade_bias=neutral 时不得 strong_disagree 等）自然适用。

## 扩展数据使用指引（这批数据是你独立判断的新武器）
你还会收到这 4 类上下文（可能部分缺失，None 时忽略）：

1. **`market_structure`**（多周期 BOS/CHoCH + swing 锚）
   - `alignment`（aligned_up / aligned_down / mixed / ranging / insufficient）是**宏观趋势硬锚**，权重高于 `bull_bear_line.regime`
   - `1h/1d/1w.last_event`（BOS_up/down · CHoCH_up/down）+ `event_age_min` 表示结构新鲜度，事件 < 120 分钟视为新
   - `operate_bias` 和 `direction` 冲突时以更高周期为准（1w > 1d > 1h）

2. **`flow_metrics.funding`**（以基点表示）
   - `extreme_tag`：neutral / mild_bias_* / crowded_*（>±10bp 即拥挤）
   - `multi_funding.deviation_bp` = 当前 - 7d 均值，偏离 >3bp 值得警惕
   - crowded_long + 价在高位 = 反转风险上升

3. **`flow_metrics.oi`**（多周期变化率）
   - `change_5m_pct`/`change_1h_pct`/`change_4h_pct`/`change_24h_pct` 组合读
   - 价涨 + OI 短期强增（1h > +1% 或 5m > +0.5%）= 真多头（可支持 trend_continuation）
   - 价跌 + OI 强增 = 真空头；价涨 + OI 下降 = 逼空 / 空头平仓（续航质量差）

4. **`sentiment`**（散户 retail vs 大户 top_account/top_position）
   - `divergence`：retail_long_smart_short 或 retail_short_smart_long 是**强反转预警**
   - aligned 表示情绪 + 聪明钱同向（续航概率高）

5. **`liq_fuel`**（清算磁吸）
   - `asymmetry_note`：above_heavy / below_heavy / balanced
   - **above_heavy**：上方清算簇总额 > 下方 → 上方空头爆仓密集 → 价格上移会被持续推动（磁吸向上）
   - **below_heavy**：下方清算簇总额 > 上方 → 下方多头爆仓密集 → 价格下移会被持续推动（磁吸向下）
   - ⚠️ 方向性必须单一、不得双向脚踩。判定规则：
     (a) 若动能已衰 + 价格紧贴反向关键位（距离 < 0.5%）→ 属"反身性扫流动性"场景，方向应与名义磁吸相反（如 below_heavy + 价临强支撑 → 先扫支撑再反弹）
     (b) 其他情况一律按名义磁吸方向理解（above_heavy=看涨偏向，below_heavy=看跌偏向）
   - 一次解读里只能引用一种理解，不得在 independent_view / traps / alignment_reason 里给出自相矛盾的方向
   - `vacuum_zones` 是价格可快速滑过的区间，但不可编造位置

## 严格禁止
- ❌ 不要引用任何历史价格 / 历史走势 / 历史表现（你没有这些数据，说了就是幻觉）
- ❌ 不要编造不在 `key_levels` 里的价位（违反会被测试层拒绝）
- ❌ 不要编造不在 `liq_fuel.above/below/vacuum_zones` 里的清算价位
- ❌ 不要用 "总体来看……" "综合判断……" 这种废话起手
- ❌ 字段里禁止出现 Markdown 符号（# * - 等），纯文本即可
- ❌ 不准直接把规则的 `overall_plain_cn` 原文复制进你的 `summary_cn`

## 输出格式（必须且只能返回合法 JSON，顶层大括号，不加 ```json 包裹）
{
  "summary_cn": "一句话讲清当前真实场景（≤60 字，不要和 rules 原文雷同）",
  "scenario": "trend_continuation | bear_rebound | bull_pullback | reversal_early | reversal_confirmed | choppy_range | unclear",
  "trend_assessment": {
    "primary_trend": "uptrend | downtrend | sideways | transition",
    "momentum_quality": "fuel_full | fuel_adequate | fuel_fading | fuel_exhausted | unclear",
    "momentum_direction": "accelerating | stable | decelerating | unclear",
    "health_summary_cn": "一句话讲趋势+动能（≤40 字）",
    "evidence_cn": "引用具体数值证据（如 RSI=65.5 + 价距 EMA20 +3.4σ + FVG 多1空0）"
  },
  "level_projection": {
    "target_level": 76948.0,
    "direction_tested": "resistance | support | both | none",
    "break_likelihood": "very_likely | likely | uncertain | unlikely | very_unlikely | insufficient",
    "break_conviction": 0.0~1.0 浮点,
    "reasoning_cn": "为什么这么判（≤50 字）",
    "if_break_cn": "突破后下一档（引用 key_levels 中的价位）",
    "if_fail_cn": "失守后回测哪里（引用 key_levels 中的价位）"
  },
  "trade_bias": {
    "direction": "long | short | neutral | avoid",
    "strength": "probe | standard | strong | none",
    "entry_zone_cn": "入场区域（≤30 字，neutral/none 时留空）",
    "invalidation_cn": "失效位（引用 key_levels 中的反向强位）",
    "timeframe_cn": "时间窗（如 2-6 小时）",
    "why_cn": "一句理由（≤50 字）"
  },
  "conflict_resolution": "各周期/因子的冲突如何解释，引用具体数值（2-3 句）。无冲突时留空字符串",
  "traps": ["陷阱 1", "陷阱 2"],
  "triggers_to_watch": ["等 X 翻正", "等 Y 跌破"],
  "independent_view": "规则没说但你注意到的关键点（可留空）",
  "action_suggestion": "一句综合行动建议（≤150 字，可含方向+区间+时间）",
  "confidence": 0.0~1.0 浮点,
  "alignment_with_rules": "agree | partial_disagree | strong_disagree | neutral | insufficient",
  "alignment_reason": "必须引用具体数值解释对齐/分歧原因（≤80 字）"
}

## 对齐度 alignment_with_rules 判定标准
- `agree`：你同意规则的 direction + state + action
- `partial_disagree`：方向认同但时机/力度 AI 有补充
- `strong_disagree`：你认为规则的 direction 或 state **判错了**（必须有 ≥2 条数值反证）
- `neutral`：你既不赞成也不反对规则，给出独立视角（如规则说 down，AI 说 sideways）
- `insufficient`：数据不够下判，如实标这个，不要硬猜

**硬约束（违反则逻辑不自洽）**：
- 当 `trade_bias.direction` ∈ {neutral, avoid} 时，**禁止** `strong_disagree`——因为你自己都不敢给相反方向，谈不上"规则判错了"。此时 alignment 最多 `partial_disagree` 或 `neutral`
- `strong_disagree` 的 ≥2 条数值反证必须指向**同一个明确方向**，不得混用看涨看跌证据

## 置信度 confidence 标准
- 0.85-1.0：所有周期 + 所有因子强共振 + key_levels 方向一致，非常确定
- 0.6-0.85：主要证据一致，少数因子冲突可解释
- 0.4-0.6：有明显矛盾，你能给出最可能场景但不敢拍胸脯
- < 0.4：证据太乱，应当 alignment_with_rules=insufficient

**降档触发（满足任一条即封顶）**：
- 若你的 `conflict_resolution` 中提到 ≥2 条互相冲突的因子：confidence 不得 > 0.55
- 若 `trade_bias.direction=neutral` 且 `alignment=partial_disagree`：confidence 不得 > 0.5
- 若同一数据字段（如 liq_fuel.asymmetry_note）你在输出里给出 ≥2 种解读方向：confidence 不得 > 0.45
"""


def _build_user_prompt(
    coin: str,
    signal_dict: dict,
    price: float,
    atr: float,
    key_levels: Optional[dict] = None,
    market_structure: Optional[dict] = None,
    flow_metrics: Optional[dict] = None,
    sentiment: Optional[dict] = None,
    liq_fuel: Optional[dict] = None,
) -> str:
    """把信号 + 关键位 + 扩展数据压成 AI 能消化的结构化上下文。"""
    payload = {
        "coin": coin,
        "price_now": round(price, 4) if price else None,
        "atr_1h": round(atr, 4) if atr else None,
        "rules_verdict_candidate": {  # 改名强调这是"候选"而非"最终"
            "overall_state": signal_dict.get("overall_state"),
            "overall_direction": signal_dict.get("overall_direction"),
            "overall_action": signal_dict.get("overall_action"),
            "overall_position_pct": signal_dict.get("overall_position_pct"),
            "consensus_level": signal_dict.get("consensus_level"),
            "regime": signal_dict.get("regime"),
            "regime_vetoed": signal_dict.get("regime_vetoed"),
            "overall_plain_cn": signal_dict.get("overall_plain_cn"),
            "overall_tip_cn": signal_dict.get("overall_tip_cn"),
            "overall_reason_cn": signal_dict.get("overall_reason_cn"),
            "data_quality": signal_dict.get("data_quality"),
            "missing_inputs": signal_dict.get("missing_inputs") or [],
        },
        "tf": {
            "1h": _compact_tf(signal_dict.get("tf_1h")),
            "4h": _compact_tf(signal_dict.get("tf_4h")),
            "1d": _compact_tf(signal_dict.get("tf_1d")),
        },
        "key_levels": key_levels,  # 可能为 None，AI 会降级处理
        "market_structure": market_structure,  # 多周期 BOS/CHoCH + alignment
        "flow_metrics": flow_metrics,          # funding + OI 多周期
        "sentiment": sentiment,                # 散户 vs 大户 + divergence
        "liq_fuel": liq_fuel,                  # 清算磁吸 + 真空区
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return (
        f"以下是 {coin} 当前时刻的**原始读数**。按 system 里的定位，你是审计员而非翻译器。\n\n"
        f"## 关键字段解读（再强调一遍）\n"
        f"- `rules_verdict_candidate` 只是规则**初步整理**，你有权基于下方 sub 的 note + key_levels + "
        f"market_structure + flow_metrics + sentiment + liq_fuel 推翻它。\n"
        f"- `tf.*.sub` 列表中每项 score 的符号是『相对当前 direction』的：\n"
        f"  + 表示『支持当前 direction 方向续航』，- 表示『反对续航 / 衰竭信号』。\n"
        f"  如果 direction=down 但多数 sub 的 note 暗示看涨（负 score），这就是**反转早期**或**规则判错**的线索。\n"
        f"- `key_levels` / `liq_fuel` 是你引用**价位**的唯一合法来源，不准编造。\n"
        f"- `market_structure.alignment` 是宏观趋势硬锚，权重高于 rules_verdict_candidate.overall_direction。\n"
        f"- `sentiment.divergence == retail_long_smart_short` 或 `retail_short_smart_long` 是强反转预警。\n\n"
        f"## 数据\n```json\n{body}\n```"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 输出解析（支持新的子结构 + 白名单校验 + 价位合法性）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SCENARIO_WHITELIST = {
    "trend_continuation", "bear_rebound", "bull_pullback",
    "reversal_early", "reversal_confirmed", "choppy_range", "unclear",
}
_ALIGN_WHITELIST = {
    "agree", "partial_disagree", "strong_disagree", "neutral", "insufficient",
}
_PRIMARY_TREND_WHITELIST = {"uptrend", "downtrend", "sideways", "transition"}
_MOMENTUM_QUALITY_WHITELIST = {
    "fuel_full", "fuel_adequate", "fuel_fading", "fuel_exhausted", "unclear",
}
_MOMENTUM_DIRECTION_WHITELIST = {
    "accelerating", "stable", "decelerating", "unclear",
}
_BREAK_LIKELIHOOD_WHITELIST = {
    "very_likely", "likely", "uncertain", "unlikely", "very_unlikely", "insufficient",
}
_DIRECTION_TESTED_WHITELIST = {"resistance", "support", "both", "none"}
_TRADE_DIRECTION_WHITELIST = {"long", "short", "neutral", "avoid"}
_TRADE_STRENGTH_WHITELIST = {"probe", "standard", "strong", "none"}


def _extract_json(text: str) -> Optional[dict]:
    """稳健抽取 JSON。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)
        if len(t) >= 2:
            body = t[1]
            if body.startswith("json\n"):
                body = body[5:]
            elif body.startswith("json"):
                body = body[4:]
            t = body.strip()
        else:
            t = "".join(t)
    l = t.find("{")
    r = t.rfind("}")
    if l < 0 or r < 0 or r <= l:
        return None
    candidate = t[l : r + 1]
    try:
        return json.loads(candidate)
    except Exception:
        try:
            import re
            cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
            return json.loads(cleaned)
        except Exception:
            return None


def _clamp_float(val, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        f = float(val)
        return max(lo, min(hi, f))
    except Exception:
        return default


def _normalize_whitelist(val, whitelist: set, default: str) -> str:
    s = str(val or "").strip()
    return s if s in whitelist else default


def _parse_trend_assessment(data: dict) -> Optional[dict]:
    ta = data.get("trend_assessment")
    if not isinstance(ta, dict):
        return None
    return {
        "primary_trend": _normalize_whitelist(
            ta.get("primary_trend"), _PRIMARY_TREND_WHITELIST, "transition"
        ),
        "momentum_quality": _normalize_whitelist(
            ta.get("momentum_quality"), _MOMENTUM_QUALITY_WHITELIST, "unclear"
        ),
        "momentum_direction": _normalize_whitelist(
            ta.get("momentum_direction"), _MOMENTUM_DIRECTION_WHITELIST, "unclear"
        ),
        "health_summary_cn": str(ta.get("health_summary_cn", ""))[:120],
        "evidence_cn": str(ta.get("evidence_cn", ""))[:200],
    }


def _parse_level_projection(data: dict, allowed_prices: set) -> Optional[dict]:
    """解析 level_projection。若 target_level 不在 allowed_prices 中，降级为 None。"""
    lp = data.get("level_projection")
    if not isinstance(lp, dict):
        return None
    target_raw = lp.get("target_level")
    target: Optional[float] = None
    if target_raw is not None:
        try:
            cand = float(target_raw)
            # AI 必须精确引用 key_levels 中的价位，只允许 ≤ 0.05% 的小数误差吸附
            # （容忍 $74,577.23 被复制为 74577.00 这种场景）
            if allowed_prices:
                best = min(allowed_prices, key=lambda p: abs(p - cand))
                if abs(best - cand) / max(abs(best), 1e-9) <= 0.0005:
                    target = round(best, 2)
                # 否则 AI 编造了价位 → 降级为 None（下方会强制 direction_tested=none）
            else:
                # 没有 key_levels 约束时才直接采纳 AI 给的价（兼容旧逻辑）
                target = round(cand, 2)
        except Exception:
            target = None
    direction_tested = _normalize_whitelist(
        lp.get("direction_tested"), _DIRECTION_TESTED_WHITELIST, "none"
    )
    break_likelihood = _normalize_whitelist(
        lp.get("break_likelihood"), _BREAK_LIKELIHOOD_WHITELIST, "insufficient"
    )
    # 若 target_level 无效 → 强制降级
    if target is None:
        direction_tested = "none"
        break_likelihood = "insufficient"
    return {
        "target_level": target,
        "direction_tested": direction_tested,
        "break_likelihood": break_likelihood,
        "break_conviction": _clamp_float(lp.get("break_conviction"), 0.0, 1.0, 0.0),
        "reasoning_cn": str(lp.get("reasoning_cn", ""))[:200],
        "if_break_cn": str(lp.get("if_break_cn", ""))[:150],
        "if_fail_cn": str(lp.get("if_fail_cn", ""))[:150],
    }


def _parse_trade_bias(data: dict) -> Optional[dict]:
    tb = data.get("trade_bias")
    if not isinstance(tb, dict):
        return None
    return {
        "direction": _normalize_whitelist(
            tb.get("direction"), _TRADE_DIRECTION_WHITELIST, "neutral"
        ),
        "strength": _normalize_whitelist(
            tb.get("strength"), _TRADE_STRENGTH_WHITELIST, "none"
        ),
        "entry_zone_cn": str(tb.get("entry_zone_cn", ""))[:100],
        "invalidation_cn": str(tb.get("invalidation_cn", ""))[:120],
        "timeframe_cn": str(tb.get("timeframe_cn", ""))[:60],
        "why_cn": str(tb.get("why_cn", ""))[:150],
    }


def _collect_allowed_prices(key_levels: Optional[dict]) -> set:
    """收集 AI 允许引用的价位集合（S/A 级强位 + 多周期级位）。"""
    if not key_levels:
        return set()
    prices: set = set()
    for group in ("strong_resistances", "strong_supports"):
        for lv in key_levels.get(group) or []:
            try:
                prices.add(round(float(lv.get("price", 0.0)), 2))
            except Exception:
                pass
    # 多周期级位（字符串形式）
    for k in (
        "daily_strong_support", "daily_strong_resistance",
        "weekly_strong_support", "weekly_strong_resistance",
        "nearest_strong_support", "nearest_strong_resistance",
    ):
        val = key_levels.get(k)
        if val is None:
            continue
        try:
            # 可能是 "$78,207.01" 格式
            if isinstance(val, str):
                clean = val.replace("$", "").replace(",", "").strip()
                if clean:
                    prices.add(round(float(clean), 2))
            else:
                prices.add(round(float(val), 2))
        except Exception:
            pass
    return prices


def _parse_ai_json(
    raw_text: str,
    reasoning: str,
    key_levels: Optional[dict] = None,
) -> tuple[dict, Optional[str]]:
    """返回 (解析到的 dict, 错误描述)。"""
    data = _extract_json(raw_text)
    if data is None:
        return {}, "AI 返回无法解析为 JSON"

    allowed_prices = _collect_allowed_prices(key_levels)

    scenario = _normalize_whitelist(
        data.get("scenario"), _SCENARIO_WHITELIST, "unclear"
    )
    align = _normalize_whitelist(
        data.get("alignment_with_rules"), _ALIGN_WHITELIST, "insufficient"
    )
    conf = _clamp_float(data.get("confidence"), 0.0, 1.0, 0.0)

    traps = data.get("traps") or []
    if not isinstance(traps, list):
        traps = [str(traps)]
    traps = [str(x)[:240] for x in traps][:6]

    triggers = data.get("triggers_to_watch") or []
    if not isinstance(triggers, list):
        triggers = [str(triggers)]
    triggers = [str(x)[:240] for x in triggers][:6]

    return {
        "summary_cn": str(data.get("summary_cn", ""))[:200],
        "scenario": scenario,
        "trend_assessment": _parse_trend_assessment(data),
        "level_projection": _parse_level_projection(data, allowed_prices),
        "trade_bias": _parse_trade_bias(data),
        "conflict_resolution": str(data.get("conflict_resolution", ""))[:500],
        "traps": traps,
        "triggers_to_watch": triggers,
        "independent_view": str(data.get("independent_view", ""))[:400],
        "action_suggestion": str(data.get("action_suggestion", ""))[:400],
        "confidence": conf,
        "alignment_with_rules": align,
        "alignment_reason": str(data.get("alignment_reason", ""))[:240],
    }, None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Interpreter 类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TEInterpreter:
    """单例型 AI 解读器。"""

    def __init__(self):
        cfg = get_settings().ai
        self._api_key = cfg.api_key
        self._api_base = cfg.api_base
        cfg_model = (cfg.model or "").lower()
        self._model = cfg.model if "reasoner" in cfg_model else "deepseek-reasoner"
        self._client: Optional[AsyncOpenAI] = None
        if self._api_key:
            kwargs: dict = {"api_key": self._api_key}
            if self._api_base:
                kwargs["base_url"] = self._api_base
            self._client = AsyncOpenAI(**kwargs)

        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Event] = {}
        logger.info(
            "TEInterpreter init | model=%s base=%s key_set=%s client_ok=%s",
            self._model, self._api_base or "(default)",
            bool(self._api_key), self._client is not None,
        )

    @property
    def available(self) -> bool:
        return self._client is not None

    # ── public helpers（给 routes.py / ws.py 调度用） ──────────
    def compute_fingerprint(
        self,
        coin: str,
        signal_dict: dict,
        key_levels_dict: Optional[dict] = None,
        extras_dict: Optional[dict] = None,
    ) -> str:
        return _fingerprint(coin, signal_dict, key_levels_dict, extras_dict)

    def peek_cache(self, fp: str) -> Optional[TEAIInterpretation]:
        return self._get_cached(fp)

    def is_inflight(self, fp: str) -> bool:
        ev = self._inflight.get(fp)
        return ev is not None and not ev.is_set()

    def _get_cached(self, fp: str) -> Optional[TEAIInterpretation]:
        entry = self._cache.get(fp)
        if entry is None:
            return None
        if time.time() >= entry.expires_at:
            self._cache.pop(fp, None)
            return None
        dup = entry.result.model_copy(update={
            "cache_hit": True,
            "from_cache_age_sec": int(_CACHE_TTL_SEC - (entry.expires_at - time.time())),
        })
        return dup

    def _put_cache(self, fp: str, result: TEAIInterpretation) -> None:
        if len(self._cache) >= _CACHE_MAX_ENTRIES:
            olds = sorted(self._cache.items(), key=lambda kv: kv[1].expires_at)
            for k, _ in olds[:20]:
                self._cache.pop(k, None)
        self._cache[fp] = _CacheEntry(
            result=result, expires_at=time.time() + _CACHE_TTL_SEC,
        )

    async def interpret(
        self,
        coin: str,
        signal_dict: dict,
        price: float,
        atr: float = 0.0,
        key_levels_dict: Optional[dict] = None,
        extras_dict: Optional[dict] = None,
        force: bool = False,
    ) -> TEAIInterpretation:
        """主入口。

        Args:
            coin: 大写币种
            signal_dict: TrendExhaustionSignal.model_dump()
            price: 当前 ticker 价格
            atr: 1h ATR14（可选）
            key_levels_dict: KeyLevelSnapshotV2.model_dump()（可选，AI 的独立优势源）
            extras_dict: `backend/api/_ai_helpers.collect_extras` 的返回值（可选，
                         含多周期 Market Structure + funding + OI + LS + Liq Map）
            force: True 则绕过缓存强制重算
        """
        # 先压缩输入（避免缓存 miss 时重复压缩）
        compact_kl = _compact_key_levels(key_levels_dict, price)
        compact_ms = None
        compact_flow = None
        compact_sent = None
        compact_liq = None
        if extras_dict:
            compact_ms = _compact_market_structure(
                extras_dict.get("ms_1h"),
                extras_dict.get("ms_1d"),
                extras_dict.get("ms_1w"),
                price,
            )
            compact_flow = _compact_flow_metrics(
                extras_dict.get("funding"),
                extras_dict.get("multi_funding"),
                extras_dict.get("oi"),
                extras_dict.get("oi_history"),
                extras_dict.get("oi_change_24h_pct"),
            )
            compact_sent = _compact_sentiment(
                extras_dict.get("ls_ratio"),
                extras_dict.get("ls_top_account"),
                extras_dict.get("ls_top_position"),
            )
            compact_liq = _compact_liq_fuel(
                extras_dict.get("liq_map_1d"), price,
            )
        fp = _fingerprint(coin, signal_dict, key_levels_dict, extras_dict)

        if not force:
            cached = self._get_cached(fp)
            if cached is not None:
                return cached

        if not self._client:
            return TEAIInterpretation(
                coin=coin, ts=int(time.time()), signal_fingerprint=fp,
                error="AI 未配置 API Key",
                alignment_with_rules="insufficient",
            )

        # 防并发重复调用
        event = self._inflight.get(fp)
        if event is not None and not force:
            try:
                await asyncio.wait_for(event.wait(), timeout=_DEFAULT_TIMEOUT_SEC + 5)
            except asyncio.TimeoutError:
                pass
            cached = self._get_cached(fp)
            if cached is not None:
                return cached

        event = asyncio.Event()
        self._inflight[fp] = event
        try:
            result = await self._do_call(
                coin, signal_dict, price, atr,
                compact_kl, compact_ms, compact_flow, compact_sent, compact_liq,
                fp,
            )
            if result.error is None:
                self._put_cache(fp, result)
            return result
        finally:
            event.set()
            if self._inflight.get(fp) is event:
                self._inflight.pop(fp, None)

    async def _do_call(
        self,
        coin: str,
        signal_dict: dict,
        price: float,
        atr: float,
        compact_kl: Optional[dict],
        compact_ms: Optional[dict],
        compact_flow: Optional[dict],
        compact_sent: Optional[dict],
        compact_liq: Optional[dict],
        fp: str,
    ) -> TEAIInterpretation:
        system = _SYSTEM_PROMPT
        user = _build_user_prompt(
            coin, signal_dict, price, atr,
            compact_kl, compact_ms, compact_flow, compact_sent, compact_liq,
        )
        t0 = time.time()
        raw_text = ""
        reasoning = ""
        tokens_in = 0
        tokens_out = 0
        r_tok = 0
        try:
            api_kwargs: dict = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "timeout": _DEFAULT_TIMEOUT_SEC,
            }
            is_reasoner = "reasoner" in self._model.lower()
            if not is_reasoner:
                api_kwargs["temperature"] = 0.2
                api_kwargs["response_format"] = {"type": "json_object"}

            extras_tag = ",".join([
                "kl" if compact_kl else "",
                "ms" if compact_ms else "",
                "flow" if compact_flow else "",
                "sent" if compact_sent else "",
                "liq" if compact_liq else "",
            ]).strip(",").replace(",,", ",")
            logger.info(
                "[TE-AI] call start | coin=%s fp=%s model=%s reasoner=%s extras=%s kl_sa=%s",
                coin, fp, self._model, is_reasoner, extras_tag or "none",
                f"{len((compact_kl or {}).get('strong_resistances') or []) + len((compact_kl or {}).get('strong_supports') or [])}"
                if compact_kl else "0",
            )
            response = await self._client.chat.completions.create(**api_kwargs)
            msg = response.choices[0].message
            raw_text = msg.content or ""
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if response.usage:
                tokens_in = response.usage.prompt_tokens or 0
                tokens_out = response.usage.completion_tokens or 0
                details = getattr(response.usage, "completion_tokens_details", None)
                if details:
                    r_tok = getattr(details, "reasoning_tokens", 0) or 0
            elapsed = time.time() - t0
            logger.info(
                "[TE-AI] call done | coin=%s fp=%s %.1fs | tokens_in=%d out=%d r=%d | resp=%d chars reasoning=%d chars",
                coin, fp, elapsed, tokens_in, tokens_out, r_tok,
                len(raw_text), len(reasoning),
            )
        except Exception as e:
            elapsed = time.time() - t0
            logger.warning(
                "[TE-AI] call failed | coin=%s fp=%s %.1fs | err=%s",
                coin, fp, elapsed, e,
            )
            return TEAIInterpretation(
                coin=coin, ts=int(time.time()), signal_fingerprint=fp,
                model=self._model, latency_ms=int(elapsed * 1000),
                error=f"AI 调用失败：{type(e).__name__}: {str(e)[:200]}",
                alignment_with_rules="insufficient",
            )

        # 解析（带 key_levels 价位白名单校验）
        parsed, parse_err = _parse_ai_json(raw_text, reasoning, compact_kl)
        reasoning_store = reasoning[:_MAX_REASONING_STORE_CHARS]
        if parse_err:
            return TEAIInterpretation(
                coin=coin, ts=int(time.time()), signal_fingerprint=fp,
                model=self._model, latency_ms=int((time.time() - t0) * 1000),
                tokens_in=tokens_in, tokens_out=tokens_out, reasoning_tokens=r_tok,
                reasoning=reasoning_store, raw_text=raw_text[:2000],
                error=parse_err,
                alignment_with_rules="insufficient",
            )

        return TEAIInterpretation(
            coin=coin, ts=int(time.time()), signal_fingerprint=fp,
            model=self._model, latency_ms=int((time.time() - t0) * 1000),
            tokens_in=tokens_in, tokens_out=tokens_out, reasoning_tokens=r_tok,
            reasoning=reasoning_store, raw_text="",
            **parsed,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_singleton: Optional[TEInterpreter] = None


def get_te_interpreter() -> TEInterpreter:
    global _singleton
    if _singleton is None:
        _singleton = TEInterpreter()
    return _singleton
