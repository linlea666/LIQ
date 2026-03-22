"""AI Prompt 模板管理"""

SYSTEM_PROMPT = """你是一位华尔街级别的加密货币衍生品市场分析师，精通订单流分析、流动性微观结构和反止损猎杀策略。
你的核心使命是通过多维实时数据，帮助交易员识别庄家猎杀意图，计算安全止损位和最优入场区。

### 核心规则
1. 你是决策支持工具，不发交易指令（不说"建议做多/做空"）
2. 不输出"胜率"数字
3. 所有价位必须有≥2个数据维度交叉验证
4. 止损位必须满足：(a)在清算密集区之外 (b)避开整数关口 (c)位于真空区内
5. 用简洁专业的中文输出
6. 每段数据标注时效性（如"资金费率7d均值"vs"实时"）

### 止损安全区专项指引（防猎杀核心）
- 做多止损：必须放在下方清算密集区的下沿之下，优先选择真空区内
- 做空止损：必须放在上方清算密集区的上沿之上，优先选择真空区内
- 绝对禁止：将止损放在清算密集区内部（会被连带爆仓）
- 避开陷阱：$X000, $X500 等整数价位是猎杀热区，偏移至少 0.1%
- ATR 缓冲：止损距离不低于 1.5x ATR，高波动时提高到 2x

### 输出格式（严格遵循）

## 一、市场格局总览
（3-4句总结：多空倾向、杠杆水平、资金流方向、情绪指数、宏观环境）

## 二、关键价位图谱
| 类型 | 价位 | 强度(1-5) | 依据(≥2维度) |

## 三、止损安全区建议
做多方向：
- 建议止损区间：$xxx - $xxx
- 防猎杀原理：（说明为何安全：清算真空区/避开整数/ATR倍数）
- 风险提示：哪些情况下此止损可能失效

做空方向：
- 建议止损区间：$xxx - $xxx
- 防猎杀原理：
- 风险提示：

## 四、入场观察区
多单观察区：$xxx - $xxx
- 共振因素：（列出支撑依据）
- 确认信号：（CVD转正/OI企稳/订单簿买墙出现等）

空单观察区：$xxx - $xxx
- 共振因素：
- 确认信号：

## 五、当前风险提示
（3-5条，按紧急程度排序，每条标注[高/中/低]）

## 六、场景推演
场景A [概率定性：最可能]：（走势+触发条件+目标位）
场景B [概率定性：次可能]：
场景C [概率定性：极端]：（黑天鹅/突发事件情景）
"""


def build_user_prompt(snapshot: dict) -> str:
    """将结构化数据快照转为 AI 可读的用户提示"""
    coin = snapshot.get("coin", "BTC")
    price = snapshot.get("price", 0)

    lines = [
        f"## 当前分析币种: {coin}/USDT",
        f"当前价格: ${price:,.2f}",
        f"24h最高: ${snapshot.get('high_24h', 0):,.2f}",
        f"24h最低: ${snapshot.get('low_24h', 0):,.2f}",
        "",
        "### 1. 清算地图数据 [实时]",
        f"多空失衡比: {snapshot.get('liq_imbalance_ratio', 0):.2f} (>1=空头清算多/看多倾向, <1=多头清算多/看空倾向)",
    ]

    lines.append("\n上方清算密集区(空头清算):")
    for c in snapshot.get("liq_clusters_above", []):
        lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                     f"${c.get('total_usd', 0) / 1e6:.0f}M ({c.get('dominant_leverage', '')}x) "
                     f"距当前{c.get('distance_pct', 0):.1f}%")

    lines.append("\n下方清算密集区(多头清算):")
    for c in snapshot.get("liq_clusters_below", []):
        lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                     f"${c.get('total_usd', 0) / 1e6:.0f}M ({c.get('dominant_leverage', '')}x) "
                     f"距当前{c.get('distance_pct', 0):.1f}%")

    lines.append("\n清算真空区(止损安全区域):")
    for v in snapshot.get("vacuum_zones", []):
        lines.append(f"  - ${v.get('price_from', 0):,.0f}-${v.get('price_to', 0):,.0f} {v.get('note', '')}")

    lines.extend([
        "",
        "### 2. 资金流数据 [实时]",
        f"合约CVD趋势(1h): {snapshot.get('cvd_contract_trend', 'N/A')} (净delta: ${snapshot.get('cvd_contract_delta_1h', 0) / 1e6:.1f}M)",
        f"现货CVD趋势(1h): {snapshot.get('cvd_spot_trend', 'N/A')} (净delta: ${snapshot.get('cvd_spot_delta_1h', 0) / 1e6:.1f}M)",
        f"CVD背离信号: {snapshot.get('cvd_divergence', '无') or '无'}",
    ])

    taker_buy = snapshot.get("taker_buy_ratio")
    if taker_buy is not None:
        lines.append(f"Taker买卖力量: 买方{taker_buy:.0%} / 卖方{1-taker_buy:.0%} → {snapshot.get('taker_dominant', '')}")

    lines.extend([
        "",
        "### 3. 持仓与杠杆 [实时]",
        f"OI总量: ${snapshot.get('oi_current_usd', 0) / 1e9:.2f}B",
        f"OI变化(1h): {snapshot.get('oi_change_1h_pct', 0):+.2f}%",
        f"OI变化(5m): {snapshot.get('oi_change_5m_pct', 0):+.2f}%",
        f"OI趋势: {snapshot.get('oi_trend', 'N/A')}",
    ])

    lines.extend([
        "",
        "### 4. 资金费率 [多交易所]",
    ])
    funding_exchanges = snapshot.get("funding_exchanges", [])
    if funding_exchanges:
        for fe in funding_exchanges:
            curr = fe.get("current")
            avg7 = fe.get("avg_7d")
            curr_str = f"{curr*100:.4f}%" if curr is not None else "N/A"
            avg7_str = f"{avg7*100:.4f}%" if avg7 is not None else "-"
            lines.append(f"  {fe.get('exchange','')}: 当前{curr_str} | 7d均{avg7_str}")
    else:
        lines.append(f"  OKX: {snapshot.get('funding_rate_okx', 'N/A')}")
        lines.append(f"  Binance: {snapshot.get('funding_rate_binance', 'N/A')}")
    lines.append(f"费率解读: {snapshot.get('funding_interpretation', 'N/A')}")

    lines.extend([
        f"期现溢价: {snapshot.get('basis_pct', 0):+.4f}%",
        "",
        "### 5. 多空比 [各交易所]",
    ])
    ls = snapshot.get("ls_ratio")
    if ls is not None:
        lines.append(f"综合多空比: {ls:.2f} ({snapshot.get('ls_ratio_interpretation', '')})")
    else:
        lines.append("数据暂缺")

    lines.extend([
        "",
        "### 6. 订单簿深度 [实时]",
        "主要买墙:",
    ])
    for w in snapshot.get("orderbook_bid_walls", []):
        lines.append(f"  - ${w.get('price', 0):,.1f}: ${w.get('size_usd', 0) / 1e6:.1f}M ({w.get('order_count', 0)}单)")
    lines.append("主要卖墙:")
    for w in snapshot.get("orderbook_ask_walls", []):
        lines.append(f"  - ${w.get('price', 0):,.1f}: ${w.get('size_usd', 0) / 1e6:.1f}M ({w.get('order_count', 0)}单)")

    lines.extend([
        "",
        "### 7. 爆仓数据",
        f"近30m多头爆仓(OKX): ${snapshot.get('recent_liq_30m_long_usd', 0) / 1e6:.1f}M",
        f"近30m空头爆仓(OKX): ${snapshot.get('recent_liq_30m_short_usd', 0) / 1e6:.1f}M",
    ])
    g_long = snapshot.get("global_liq_long_24h", 0)
    g_short = snapshot.get("global_liq_short_24h", 0)
    if g_long > 0 or g_short > 0:
        lines.append(f"全网24h多头爆仓: ${g_long / 1e6:.0f}M")
        lines.append(f"全网24h空头爆仓: ${g_short / 1e6:.0f}M")
        lines.append(f"全网多空爆仓比: {g_long / g_short:.1f}" if g_short > 0 else "")

    lines.extend([
        "",
        "### 8. 成交分布 [1H K线]",
        f"Volume Profile POC: ${snapshot.get('volume_profile_poc', 0):,.2f}",
        f"Value Area: ${snapshot.get('value_area_low', 0):,.2f} - ${snapshot.get('value_area_high', 0):,.2f}",
        f"VWAP: ${snapshot.get('vwap', 0):,.2f}",
        f"ATR(14): ${snapshot.get('atr_14', 0):,.2f}",
    ])

    lines.extend([
        "",
        "### 9. 情绪与宏观",
    ])
    fgi = snapshot.get("fear_greed_index")
    if fgi is not None:
        lines.append(f"恐惧贪婪指数: {int(fgi)} (0=极度恐惧, 100=极度贪婪)")
    etf_3d = snapshot.get("etf_net_3d")
    if etf_3d is not None:
        lines.append(f"BTC ETF 3日净流: ${etf_3d / 1e6:.0f}M ({snapshot.get('etf_trend', '')})")
    max_pain = snapshot.get("btc_max_pain")
    if max_pain:
        lines.append(f"BTC 期权 Max Pain: ${max_pain:,.0f}")
    dvol = snapshot.get("btc_dvol")
    if dvol:
        lines.append(f"BTC DVOL(隐含波动率): {dvol:.1f}%")
    dom = snapshot.get("btc_dominance")
    if dom:
        lines.append(f"BTC Dominance: {dom:.1f}%")
    dxy = snapshot.get("dxy")
    if dxy:
        lines.append(f"美元指数(DXY): {dxy:.1f}")

    lines.extend([
        "",
        "### 10. 综合评估",
        f"市场温度: {snapshot.get('market_temperature', 50):.0f}/100 (>80极热 <20极冷)",
        f"插针风险等级: {snapshot.get('pin_risk_level', 'N/A')}",
    ])

    lines.append("\n请基于以上 10 组数据进行分析，严格按照指定格式输出。重点关注止损安全区的防猎杀设计。")
    return "\n".join(lines)
