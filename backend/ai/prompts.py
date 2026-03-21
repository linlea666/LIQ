"""AI Prompt 模板管理"""

SYSTEM_PROMPT = """你是一位专业的加密货币衍生品市场分析师，专注于通过订单流和流动性数据识别市场微观结构。
你的任务是根据提供的多维实时数据，为交易员输出关键价位分析和市场格局解读。

重要规则：
1. 你只提供信息分析和价位参考，不提供交易指令（不说"建议做多/做空"）
2. 不输出任何"胜率"数字，因为你不是概率计算器
3. 所有价位必须有明确的数据依据
4. 用简洁专业的中文输出
5. 必须严格按照以下 section 结构输出

输出格式要求（严格遵循）：

## 一、市场格局总览
（2-3句话总结当前市场状态，包括多空倾向、杠杆水平、资金流方向）

## 二、关键价位图谱
（表格形式列出所有关键支撑和阻力位，每个价位附带依据）
| 类型 | 价位 | 强度 | 依据 |

## 三、止损安全区建议
做多方向：
- 建议止损区间：$xxx - $xxx
- 依据：（列出具体原因）

做空方向：
- 建议止损区间：$xxx - $xxx
- 依据：（列出具体原因）

## 四、入场观察区
多单观察区：$xxx - $xxx
- 共振因素：
- 确认信号：

空单观察区：$xxx - $xxx
- 共振因素：
- 确认信号：

## 五、当前风险提示
（列出 2-4 条需要注意的风险点）

## 六、场景推演
场景A：（描述最可能的走势场景）
场景B：（描述次可能的场景）
场景C：（描述极端场景）
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
        "### 清算地图数据",
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

    lines.append("\n清算真空区:")
    for v in snapshot.get("vacuum_zones", []):
        lines.append(f"  - ${v.get('price_from', 0):,.0f}-${v.get('price_to', 0):,.0f} {v.get('note', '')}")

    lines.extend([
        "",
        "### 资金流数据",
        f"合约CVD趋势(1h): {snapshot.get('cvd_contract_trend', 'N/A')} (delta: ${snapshot.get('cvd_contract_delta_1h', 0) / 1e6:.1f}M)",
        f"现货CVD趋势(1h): {snapshot.get('cvd_spot_trend', 'N/A')} (delta: ${snapshot.get('cvd_spot_delta_1h', 0) / 1e6:.1f}M)",
        f"CVD背离: {snapshot.get('cvd_divergence', '无')}",
        "",
        "### 持仓与费率",
        f"OI总量: ${snapshot.get('oi_current_usd', 0) / 1e9:.2f}B",
        f"OI变化(1h): {snapshot.get('oi_change_1h_pct', 0):+.2f}%",
        f"OI变化(5m): {snapshot.get('oi_change_5m_pct', 0):+.2f}%",
        f"OI趋势: {snapshot.get('oi_trend', 'N/A')}",
        f"资金费率(OKX): {snapshot.get('funding_rate_okx', 'N/A')}",
        f"资金费率(Binance): {snapshot.get('funding_rate_binance', 'N/A')}",
        f"费率解读: {snapshot.get('funding_interpretation', 'N/A')}",
        f"期现溢价: {snapshot.get('basis_pct', 0):+.4f}%",
        "",
        "### 订单簿",
        "主要买墙:",
    ])

    for w in snapshot.get("orderbook_bid_walls", []):
        lines.append(f"  - ${w.get('price', 0):,.1f}: {w.get('size', 0):.1f} (${w.get('size_usd', 0) / 1e6:.1f}M)")

    lines.append("主要卖墙:")
    for w in snapshot.get("orderbook_ask_walls", []):
        lines.append(f"  - ${w.get('price', 0):,.1f}: {w.get('size', 0):.1f} (${w.get('size_usd', 0) / 1e6:.1f}M)")

    lines.extend([
        "",
        "### 爆仓与成交分布",
        f"近30分钟多头爆仓: ${snapshot.get('recent_liq_30m_long_usd', 0) / 1e6:.1f}M",
        f"近30分钟空头爆仓: ${snapshot.get('recent_liq_30m_short_usd', 0) / 1e6:.1f}M",
        f"Volume Profile POC: ${snapshot.get('volume_profile_poc', 0):,.2f}",
        f"Value Area: ${snapshot.get('value_area_low', 0):,.2f} - ${snapshot.get('value_area_high', 0):,.2f}",
        f"VWAP: ${snapshot.get('vwap', 0):,.2f}",
        f"ATR(14): ${snapshot.get('atr_14', 0):,.2f}",
        "",
        f"### 综合评估",
        f"市场温度: {snapshot.get('market_temperature', 50):.0f}/100",
        f"插针风险等级: {snapshot.get('pin_risk_level', 'N/A')}",
    ])

    # v2.0 预留: 宏观数据段
    # macro = snapshot.get("macro_context")
    # if macro:
    #     lines.extend(["", "### 宏观环境", ...])

    lines.append("\n请基于以上数据进行分析，严格按照指定格式输出。")
    return "\n".join(lines)
