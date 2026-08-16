"""安全类型转换。

币安接口的数值几乎全是字符串，且精度极高（30+ 位小数）；
部分字段在同一接口内既可能是字符串又可能是数字。
所有转换失败一律返回 None（UNKNOWN），绝不返回 0——
把"没拿到"写成 0 会直接污染评分和后续所有研究结论。
"""

from __future__ import annotations

import math
from typing import Any

# 明显异常的数值上界：超过则视为解析错误而非真实值
_MAX_ABS = 1e18


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() in ("null", "none", "nan", "-"):
            return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result) or abs(result) > _MAX_ABS:
        return None
    return result


def to_int(value: Any) -> int | None:
    result = to_float(value)
    if result is None:
        return None
    try:
        return int(result)
    except (OverflowError, ValueError):
        return None


def to_positive_float(value: Any) -> float | None:
    """价格、市值、流动性等必须为正；<=0 视为无数据。

    这里把 0 当作 UNKNOWN 是有意的：币安对刚创建的代币经常返回 0，
    而"市值 0"在业务上不可能成立，若当真会让所有比率特征爆炸。
    """
    result = to_float(value)
    if result is None or result <= 0:
        return None
    return result


def to_non_negative_float(value: Any) -> float | None:
    """成交量、净流入绝对值等允许为 0（真实含义就是没有成交）。"""
    result = to_float(value)
    if result is None or result < 0:
        return None
    return result


def to_non_negative_int(value: Any) -> int | None:
    result = to_int(value)
    if result is None or result < 0:
        return None
    return result


def to_percent(value: Any, *, max_value: float = 100.0) -> float | None:
    """0–100 标度的百分比字段。

    超出 [0, max_value] 的值视为解析错误：接口偶发返回
    离谱数值时，宁可当作 UNKNOWN，也不能让它污染筹码集中度判断。
    """
    result = to_float(value)
    if result is None:
        return None
    if result < 0 or result > max_value:
        return None
    return result


def to_signed_percent(value: Any, *, limit: float = 100000.0) -> float | None:
    """涨跌幅百分比，可正可负。Meme 币动辄 +1000%，上限放宽。"""
    result = to_float(value)
    if result is None or abs(result) > limit:
        return None
    return result


def to_ratio(value: Any, *, limit: float = 10000.0) -> float | None:
    """比率型字段（如 maxGain 的 0.1626 表示 +16.26%）。"""
    result = to_float(value)
    if result is None or abs(result) > limit:
        return None
    return result


def to_timestamp_ms(value: Any) -> int | None:
    """毫秒时间戳。识别并拒绝明显不合理的值（如秒级、0、未来过远）。"""
    result = to_int(value)
    if result is None or result <= 0:
        return None
    # 2015-01-01 ~ 2100-01-01 的毫秒范围
    if result < 1_420_070_400_000 or result > 4_102_444_800_000:
        # 可能是秒级时间戳，尝试换算
        if 1_420_070_400 <= result <= 4_102_444_800:
            return result * 1000
        return None
    return result


def to_text(value: Any, *, max_len: int = 200) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none"):
        return None
    return text[:max_len]


def to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y"):
            return True
        if lowered in ("false", "0", "no", "n"):
            return False
    return None


def first_not_none(*values: Any) -> Any:
    """按优先级取第一个非 None 值（用于同义字段回退，如 price → aggPrice）。"""
    for value in values:
        if value is not None:
            return value
    return None
