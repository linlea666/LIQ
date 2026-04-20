"""安全护栏 · L5 Safety Gate

5 道护栏（全部独立，并行判定）：
  G1 极端波动    ATR% > 某阈值（无 percentile 时用 atr_14/price 硬阈值） → block/warn
  G2 宏观事件    FOMC/CPI/NFP 事件前后 4h                                 → warn (P1 接入)
  G3 爆仓混乱    24h 爆仓量 vs 基线                                       → warn/block
  G4 API 降级    关键源连续失败 / last_success > 1h                       → warn/block
  G5 黑天鹅/地缘 GeoRisk overall_level >= 4                               → block

每道护栏输出 pass/warn/block：
  pass  — 无影响
  warn  — 打分减 5-10 / 仓位减半
  block — 禁止开仓（force traffic_light=red, action=wait）

落实日志锚点：
  - D.D03_SAFETY_GATE：每次 evaluate 上报 5 个 gate 各自状态 + triggered
  - 触发 block 时额外 warn 级别日志

复用决策：
  - 不改现有 poll_failures / source_health 结构
  - 宏观事件源 P1 对接 macro.events（当前占位 pass）
  - 地缘对接新增 GeoRiskOverview（当前 overall_level 可读即 ok）
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from models.execution_plan import SafetyGateResult
from models.geo_risk import GeoRiskOverview
from models.snapshot import AISnapshot, SourceHealth

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 阈值常量（保守值；可随回测再调整）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ATR_PCT_PERCENTILE_BLOCK = 95.0
_ATR_PCT_PERCENTILE_WARN = 85.0

# percentile 缺失时的硬阈值兜底（与 market_regime 协同）
_ATR_PCT_HARD_BLOCK = 6.0
_ATR_PCT_HARD_WARN = 4.0

_HOUR_CHANGE_BLOCK_PCT = 5.0

_LIQ_BLOCK_RATIO = 5.0
_LIQ_WARN_RATIO = 3.0
_LIQ_HARD_BLOCK_24H_USD = 5e9

_KEY_SOURCES = {"coinglass", "binance_futures", "bbx"}
_SOURCE_STALE_SEC = 3600


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def evaluate_safety_gates(
    coin: str,
    snapshot: Optional[AISnapshot] = None,
    *,
    geo_overview: Optional[GeoRiskOverview] = None,
    source_health: Optional[list[SourceHealth]] = None,
    atr_pct_percentile: Optional[float] = None,
    # 轻量调用路径（没有 AISnapshot 时直接提供关键字段）
    price: Optional[float] = None,
    atr_14: Optional[float] = None,
    hour_change_pct: Optional[float] = None,
    liq_24h_total_usd: Optional[float] = None,
    liq_7d_avg_usd: Optional[float] = None,
) -> SafetyGateResult:
    """评估 5 道安全护栏。

    - snapshot=None 且 kwargs 缺省时，5 道 gate 全部降级到 pass（但 mark warn）
    - 所有 gate 独立失败处理：任一异常 → 该 gate 置 warn，reason 记录
    """
    gates: dict[str, tuple[str, str]] = {}

    # G1 ── 极端波动
    try:
        gates["g1"] = _g1_extreme_volatility(
            snapshot=snapshot,
            atr_pct_percentile=atr_pct_percentile,
            price=price, atr_14=atr_14, hour_change_pct=hour_change_pct,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[G1] eval failed: %s", e)
        gates["g1"] = ("warn", f"G1 异常：{e}")

    # G2 ── 宏观事件（P1 启用，当前 pass）
    try:
        gates["g2"] = _g2_macro_event(snapshot)
    except Exception as e:  # noqa: BLE001
        logger.debug("[G2] eval failed: %s", e)
        gates["g2"] = ("warn", f"G2 异常：{e}")

    # G3 ── 爆仓混乱
    try:
        gates["g3"] = _g3_liquidation_chaos(
            snapshot=snapshot,
            liq_24h_total_usd=liq_24h_total_usd,
            liq_7d_avg_usd=liq_7d_avg_usd,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[G3] eval failed: %s", e)
        gates["g3"] = ("warn", f"G3 异常：{e}")

    # G4 ── API 降级
    try:
        gates["g4"] = _g4_api_degradation(source_health)
    except Exception as e:  # noqa: BLE001
        logger.debug("[G4] eval failed: %s", e)
        gates["g4"] = ("warn", f"G4 异常：{e}")

    # G5 ── 黑天鹅 / 地缘
    try:
        gates["g5"] = _g5_blackswan_geo(geo_overview)
    except Exception as e:  # noqa: BLE001
        logger.debug("[G5] eval failed: %s", e)
        gates["g5"] = ("warn", f"G5 异常：{e}")

    result = aggregate_to_result(gates)

    # D03 上报
    try:
        from utils.decision_tracker import D, get_tracker
        # P0-h：统计 G4 接入的 source_health 覆盖情况（便于看"是否真在跑"）
        src_total = 0
        src_key_connected = 0
        if source_health:
            def _get(obj, key, default=None):
                if isinstance(obj, dict):
                    return obj.get(key, default)
                return getattr(obj, key, default)
            for sh in source_health:
                src_total += 1
                name = str(_get(sh, "name", "") or "").lower()
                status = str(_get(sh, "status", "") or "").lower()
                if name in _KEY_SOURCES and status == "connected":
                    src_key_connected += 1

        get_tracker().mark(
            D.D03_SAFETY_GATE,
            status="warn" if result.triggered else "ok",
            log=bool(result.block_reason),  # 触发 block 时打一行
            coin=coin,
            triggered=result.triggered,
            g1=gates["g1"][0], g2=gates["g2"][0],
            g3=gates["g3"][0], g4=gates["g4"][0],
            g5=gates["g5"][0],
            block_reason=result.block_reason[:120] if result.block_reason else "",
            src_total=src_total,
            src_key_connected=src_key_connected,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[D03] safety_gate tracker mark failed", exc_info=True)

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 独立 gate 实现
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _g1_extreme_volatility(
    snapshot: Optional[AISnapshot] = None,
    atr_pct_percentile: Optional[float] = None,
    *,
    price: Optional[float] = None,
    atr_14: Optional[float] = None,
    hour_change_pct: Optional[float] = None,
) -> tuple[str, str]:
    """G1：极端波动

    优先用百分位；无则用 ATR% 硬阈值；再兜底 1h 涨跌幅
    """
    # 1) percentile
    if atr_pct_percentile is not None:
        if atr_pct_percentile >= _ATR_PCT_PERCENTILE_BLOCK:
            return "block", f"ATR% 百分位 {atr_pct_percentile:.0f} ≥ {_ATR_PCT_PERCENTILE_BLOCK:.0f}，极端波动熔断"
        if atr_pct_percentile >= _ATR_PCT_PERCENTILE_WARN:
            return "warn", f"ATR% 百分位 {atr_pct_percentile:.0f} ≥ {_ATR_PCT_PERCENTILE_WARN:.0f}，波动偏高"

    # 2) ATR% 硬阈值
    p = price
    a = atr_14
    if snapshot is not None:
        p = p or float(snapshot.price or 0)
        a = a or float(snapshot.atr_14 or 0)
    if p and a and p > 0:
        atr_pct = (a / p) * 100.0
        if atr_pct >= _ATR_PCT_HARD_BLOCK:
            return "block", f"ATR% {atr_pct:.2f}% ≥ {_ATR_PCT_HARD_BLOCK}%，极端波动"
        if atr_pct >= _ATR_PCT_HARD_WARN:
            return "warn", f"ATR% {atr_pct:.2f}% ≥ {_ATR_PCT_HARD_WARN}%，波动偏高"

    # 3) 1h 涨跌兜底
    if hour_change_pct is not None and abs(hour_change_pct) >= _HOUR_CHANGE_BLOCK_PCT:
        return "block", f"1h 涨跌 {hour_change_pct:.2f}% ≥ ±{_HOUR_CHANGE_BLOCK_PCT}%"

    return "pass", ""


def _g2_macro_event(snapshot: Optional[AISnapshot]) -> tuple[str, str]:
    """G2：宏观事件（P1 启用，当前 pass）

    数据源：snapshot 预留的 macro.events。当前项目尚未接入结构化宏观事件流，
    为保持 P0 范围纪律，当前固定 pass；P1 接入后在此扩展。
    """
    return "pass", ""


def _g3_liquidation_chaos(
    snapshot: Optional[AISnapshot] = None,
    *,
    liq_24h_total_usd: Optional[float] = None,
    liq_7d_avg_usd: Optional[float] = None,
) -> tuple[str, str]:
    """G3：爆仓混乱

    比值判定：
      ratio > 5 → block；> 3 → warn
    无 7d 均值时用硬 USD 阈值（5B USD 以上视为极端）兜底
    """
    liq_24h = liq_24h_total_usd
    if liq_24h is None and snapshot is not None:
        liq_24h = float(snapshot.global_liq_long_24h or 0) + float(snapshot.global_liq_short_24h or 0)

    if not liq_24h or liq_24h <= 0:
        return "pass", ""

    if liq_7d_avg_usd and liq_7d_avg_usd > 0:
        ratio = liq_24h / liq_7d_avg_usd
        if ratio >= _LIQ_BLOCK_RATIO:
            return "block", f"24h 爆仓 / 7d 均值 = {ratio:.2f}x ≥ {_LIQ_BLOCK_RATIO}x，市场混乱"
        if ratio >= _LIQ_WARN_RATIO:
            return "warn", f"24h 爆仓 / 7d 均值 = {ratio:.2f}x ≥ {_LIQ_WARN_RATIO}x，注意情绪失衡"

    # 兜底硬阈值
    if liq_24h >= _LIQ_HARD_BLOCK_24H_USD:
        return "block", f"24h 爆仓 {liq_24h/1e9:.2f}B USD ≥ {_LIQ_HARD_BLOCK_24H_USD/1e9:.1f}B，极端清算"

    return "pass", ""


def _g4_api_degradation(
    source_health: Optional[list],  # list[SourceHealth] 或 list[dict]
) -> tuple[str, str]:
    """G4：API 降级

    关键源白名单：coinglass / binance_futures / bbx
    任一 disconnected 且 last_success_ts 超过 1h → warn
    ≥2 个关键源同时 disconnected → block
    支持 SourceHealth 或 dict 两种形式（duck typing）
    """
    if not source_health:
        return "pass", ""

    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    now = int(time.time())
    stale_keys: list[str] = []
    for sh in source_health:
        name = str(_get(sh, "name", "") or "").lower()
        if name not in _KEY_SOURCES:
            continue
        status = str(_get(sh, "status", "") or "").lower()
        last_ok = int(_get(sh, "last_success_ts", 0) or 0)
        age = now - last_ok if last_ok > 0 else _SOURCE_STALE_SEC + 1

        if status == "disconnected" and age > _SOURCE_STALE_SEC:
            stale_keys.append(name)
        elif status == "degraded" and age > _SOURCE_STALE_SEC * 2:
            stale_keys.append(name)

    if len(stale_keys) >= 2:
        return "block", f"关键源 {','.join(stale_keys)} 多路降级/断连"
    if len(stale_keys) == 1:
        return "warn", f"关键源 {stale_keys[0]} 降级/断连 > 1h"
    return "pass", ""


def _g5_blackswan_geo(
    geo_overview: Optional[GeoRiskOverview],
) -> tuple[str, str]:
    """G5：黑天鹅 / 地缘升级"""
    if geo_overview is None:
        return "pass", ""

    try:
        level = int(getattr(geo_overview, "overall_level", 0) or 0)
    except Exception:  # noqa: BLE001
        level = 0

    has_blackswan = bool(getattr(geo_overview, "has_blackswan_24h", False))

    if has_blackswan or level >= 4:
        return "block", f"GeoRisk overall_level={level} / blackswan={has_blackswan}"
    if level == 3:
        return "warn", "GeoRisk CRISIS（level=3）"
    return "pass", ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 结果聚合
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def aggregate_to_result(
    gates: dict[str, tuple[str, str]],
) -> SafetyGateResult:
    """把 5 道 gate 结果聚合为 SafetyGateResult"""
    res = SafetyGateResult()
    res.g1_extreme_vol = gates.get("g1", ("pass", ""))[0]  # type: ignore[assignment]
    res.g2_macro_event = gates.get("g2", ("pass", ""))[0]  # type: ignore[assignment]
    res.g3_liq_chaos   = gates.get("g3", ("pass", ""))[0]  # type: ignore[assignment]
    res.g4_api_degrade = gates.get("g4", ("pass", ""))[0]  # type: ignore[assignment]
    res.g5_blackswan   = gates.get("g5", ("pass", ""))[0]  # type: ignore[assignment]

    triggered = False
    warnings: list[str] = []
    block_reason = ""
    for _, (status, reason) in gates.items():
        if status != "pass":
            triggered = True
        if status == "block" and not block_reason and reason:
            block_reason = reason
        if status in ("warn", "block") and reason:
            warnings.append(reason)

    res.triggered = triggered
    res.block_reason = block_reason
    res.warnings = warnings
    return res


# 向后兼容别名：方便外部调用时传 dict 形式
def evaluate_safety_gates_from_dict(coin: str, data: dict[str, Any]) -> SafetyGateResult:  # pragma: no cover
    """便捷入口：传入 {price, atr_14, hour_change_pct, liq_24h_total_usd, ...}"""
    return evaluate_safety_gates(
        coin=coin,
        price=data.get("price"),
        atr_14=data.get("atr_14"),
        hour_change_pct=data.get("hour_change_pct"),
        liq_24h_total_usd=data.get("liq_24h_total_usd"),
        liq_7d_avg_usd=data.get("liq_7d_avg_usd"),
        atr_pct_percentile=data.get("atr_pct_percentile"),
    )
