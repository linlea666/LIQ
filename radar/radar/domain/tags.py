"""归一化标签常量。

刻意使用我们自己的内部标签名，而不是直接沿用币安返回的展示文案。
原因很实际：币安把 "Wash Trading" 改成 "Suspicious Volume" 这类文案调整
随时可能发生，若风险规则直接匹配原文，改版当天洗盘过滤就会**静默失效**——
系统不报错，只是再也拦不住洗盘币。

因此解析层负责把原始文案映射到这里的常量，风险配置只引用这些常量。
"""

from __future__ import annotations

# 风险类
TAG_DEV_WASH = "DEV_WASH_TRADING"
TAG_INSIDER_WASH = "INSIDER_WASH_TRADING"
TAG_HIGH_TAX = "HIGH_TAX"

# 中性/正向类
TAG_DEV_BURNED = "DEV_BURNED_TOKEN"
TAG_PUMPFUN_LIVING = "PUMPFUN_LIVING"
TAG_CMC_BOOST = "CMC_BOOST"
TAG_DEX_PAID = "DEX_PAID"

# 币安原始文案（小写）→ 内部标签
RAW_TAG_MAP: dict[str, str] = {
    "high tax token": TAG_HIGH_TAX,
    "dex paid": TAG_DEX_PAID,
    "update dexscreener social": TAG_DEX_PAID,
}

# 布尔型 tagXxx 字段名 → 内部标签
BOOL_TAG_FIELDS: tuple[tuple[str, str], ...] = (
    ("tagDevWashTrading", TAG_DEV_WASH),
    ("tagInsiderWashTrading", TAG_INSIDER_WASH),
    ("tagDevBurnedToken", TAG_DEV_BURNED),
    ("tagPumpfunLiving", TAG_PUMPFUN_LIVING),
    ("tagCmcBoost", TAG_CMC_BOOST),
)
