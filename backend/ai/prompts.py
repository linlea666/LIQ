"""AI Prompt 模板管理"""

SYSTEM_PROMPT = """你是一位管理$5亿级加密货币基金的量化策略分析师，精通：
- 订单流微观结构（清算地图、CVD、Taker Flow）
- 流动性猎杀机制（止损猎杀、假突破、插针反转）
- 宏观-微观联动框架（美元指数、纳斯达克、黄金 → 加密市场传导）
- 极端盈亏比挂单策略（小亏大赚哲学）

### 角色定位
你是决策支持系统，不发交易指令。输出「如果做多/做空，最安全的止损和最佳入场在哪里」。

### 分析框架（宏观→微观→策略）

**第一层：宏观风向判断**
- 美元指数(DXY)↑ → 风险资产承压 → 加密偏空
- 纳斯达克/标普强势 → risk-on → 加密偏多
- 黄金上涨 → 避险情绪 → 需结合DXY判断是否利多BTC
- 若宏观数据缺失，明确标注「宏观数据暂缺，以下分析仅基于链上/衍生品数据」

**第二层：微观数据交叉验证**
每个价位判断必须≥2维度交叉验证。关键组合：
- 清算簇 + 订单簿买/卖墙 → 支撑/阻力强度
- CVD趋势 + OI变化 → 辨别真突破 vs 假突破
- 资金费率极端 + 清算池方向 → 猎杀概率
- Taker Flow + 期现溢价 → 主力方向

**第三层：策略输出**
- 止损必须在清算密集区之外、真空区内、避开整数关口、≥1.5x ATR
- 入场观察区需列明确认信号（CVD转向/OI变化/订单簿变化）
- 狙击挂单审核：对规则引擎预算的极端R:R入场点进行合理性判断

### 核心规则
1. 不说"建议做多/做空"，只说"如果做多，观察区在X，止损在Y"
2. 不输出胜率数字
3. 所有价位标注数据来源
4. 用简洁专业的中文输出
5. 数据标注时效性（实时/1h/24h/7d）

### 输出格式（严格遵循Markdown）

## 一、市场格局总览
（3-5句：宏观风向→杠杆水平→资金流方向→情绪→当前格局定性）

## 二、关键价位图谱
| 类型 | 价位区间 | 依据(≥2维数据源+时效) |
（支撑区、阻力区、价值中枢各至少1行）

## 三、止损安全区建议
**做多方向：**
- 建议止损区间：$xxx - $xxx
- 防猎杀原理：（清算真空区/避开整数/ATR倍数，逐条列出）
- 风险提示：哪些情况下此止损可能失效

**做空方向：**
- 建议止损区间：$xxx - $xxx
- 防猎杀原理：
- 风险提示：

## 四、狙击挂单审核
（审核规则引擎给出的极端R:R入场点，逐条评估合理性，补充宏观/微观确认条件）
对每个方案：接受/调整/拒绝，并说明理由。

## 五、入场观察区
**多单观察区：$xxx - $xxx**
- 共振因素：
- 确认信号：（CVD/OI/订单簿的具体变化条件）

**空单观察区：$xxx - $xxx**
- 共振因素：
- 确认信号：

## 六、当前风险提示
（3-5条，标注[高/中/低]，按紧急程度排序）

## 七、场景推演
场景A [最可能]：走势+触发条件+目标位
场景B [次可能]：
场景C [极端]：黑天鹅情景
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
        f"多空失衡比: {snapshot.get('liq_imbalance_ratio', 0):.2f} (>1=空头清算多/看多磁吸, <1=多头清算多/看空磁吸)",
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
        okx_r = snapshot.get("funding_rate_okx")
        bn_r = snapshot.get("funding_rate_binance")
        lines.append(f"  OKX: {okx_r * 100:.4f}%" if okx_r is not None else "  OKX: N/A")
        lines.append(f"  Binance: {bn_r * 100:.4f}%" if bn_r is not None else "  Binance: N/A")
    lines.append(f"费率解读: {snapshot.get('funding_interpretation', 'N/A')}")
    avg7d = snapshot.get("funding_avg_7d")
    if avg7d is not None:
        lines.append(f"7d均值: {avg7d*100:.4f}%")

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
        if g_short > 0:
            lines.append(f"全网多空爆仓比: {g_long / g_short:.1f}")

    lines.extend([
        "",
        "### 8. 成交分布与波动率 [1H K线]",
        f"Volume Profile POC: ${snapshot.get('volume_profile_poc', 0):,.2f}",
        f"Value Area: ${snapshot.get('value_area_low', 0):,.2f} - ${snapshot.get('value_area_high', 0):,.2f}",
        f"VWAP(多日成交加权): ${snapshot.get('vwap', 0):,.2f}",
        f"ATR(14, Wilder): ${snapshot.get('atr_14', 0):,.2f}",
    ])

    lines.extend([
        "",
        "### 9. 宏观与情绪指标",
    ])
    fgi = snapshot.get("fear_greed_index")
    if fgi is not None:
        lines.append(f"恐惧贪婪指数: {int(fgi)} (0=极度恐惧, 100=极度贪婪)")
    dxy = snapshot.get("dxy")
    if dxy:
        lines.append(f"美元指数(DXY): {dxy:.1f}")
    nasdaq = snapshot.get("nasdaq")
    if nasdaq:
        nasdaq_chg = snapshot.get("nasdaq_change_pct")
        chg_str = f" ({nasdaq_chg:+.1f}%)" if nasdaq_chg is not None else ""
        lines.append(f"纳斯达克100: {nasdaq:,.1f}{chg_str}")
    sp500 = snapshot.get("sp500")
    if sp500:
        sp_chg = snapshot.get("sp500_change_pct")
        chg_str = f" ({sp_chg:+.1f}%)" if sp_chg is not None else ""
        lines.append(f"标普500: {sp500:,.1f}{chg_str}")
    gold = snapshot.get("gold")
    if gold:
        gold_chg = snapshot.get("gold_change_pct")
        chg_str = f" ({gold_chg:+.1f}%)" if gold_chg is not None else ""
        lines.append(f"黄金: ${gold:,.1f}{chg_str}")
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

    # 规则引擎预计算的关键价位
    lines.extend([
        "",
        "### 10. 规则引擎预计算 [供参考]",
        f"市场温度: {snapshot.get('market_temperature', 50):.0f}/100 (>80极热 <20极冷)",
        f"插针风险等级: {snapshot.get('pin_risk_level', 'N/A')}",
    ])

    rule_supports = snapshot.get("rule_supports", [])
    if rule_supports:
        lines.append("支撑位(规则引擎):")
        for s in rule_supports[:3]:
            lines.append(f"  - ${s.get('price', 0):,.1f} [{','.join(s.get('sources', []))}]")

    rule_resistances = snapshot.get("rule_resistances", [])
    if rule_resistances:
        lines.append("阻力位(规则引擎):")
        for r in rule_resistances[:3]:
            lines.append(f"  - ${r.get('price', 0):,.1f} [{','.join(r.get('sources', []))}]")

    rule_sl = snapshot.get("rule_stop_loss", [])
    if rule_sl:
        lines.append("止损建议(规则引擎):")
        for sl in rule_sl:
            lines.append(f"  - {sl.get('direction','')}: ${sl.get('zone_from', 0):,.1f}-${sl.get('zone_to', 0):,.1f} "
                         f"[{', '.join(sl.get('reasons', []))}]")

    sniper = snapshot.get("sniper_entries", [])
    if sniper:
        lines.append("\n### 11. 狙击挂单方案（规则引擎预算，请审核）")
        for i, se in enumerate(sniper):
            d = se.get("direction", "")
            lines.append(f"方案{i+1} [{d}]: "
                         f"入场${se.get('entry_price', 0):,.1f} "
                         f"止损${se.get('stop_loss', 0):,.1f} "
                         f"TP1=${se.get('take_profit_1', 0):,.1f}(R:R {se.get('rr_ratio_1', 0):.1f}) "
                         f"TP2=${se.get('take_profit_2', 0):,.1f}(R:R {se.get('rr_ratio_2', 0):.1f})")
            for logic_line in se.get("logic", []):
                lines.append(f"    - {logic_line}")

    lines.append("\n请基于以上数据进行分析，严格按照指定格式输出。重点：")
    lines.append("1. 止损安全区的防猎杀设计")
    lines.append("2. 宏观→微观联动判断")
    lines.append("3. 审核狙击挂单方案的合理性（接受/调整/拒绝）")
    return "\n".join(lines)
