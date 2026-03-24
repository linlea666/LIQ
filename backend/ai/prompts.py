"""AI Prompt 模板管理（方案C：合规底座 + 狙击挂单硬交付 + 教练视角）"""

from __future__ import annotations

from config.settings import get_settings


def _min_sniper_rr() -> float:
    return float(get_settings().processors.levels.get("min_sniper_rr", 2.5))


def build_system_prompt() -> str:
    """动态注入与配置一致的 R:R 下限，避免与规则引擎口径漂移。"""
    min_rr = _min_sniper_rr()
    return f"""你是一位管理$5亿级加密货币基金的量化策略分析师，**兼任永续合约交易教练**。分析对象包含使用高杠杆的专业交易员。

### 你的核心价值
1. 识别庄家借助清算地图进行的止损猎杀意图
2. 结合规则引擎数据，输出「小亏大赚」的高 R:R 狙击挂单参考（非喊单）
3. 通过宏观-微观联动判断当前市场偏向与风险

### 角色边界（铁律）
- 你是**决策参考工具**，交易员最终拍板；不输出「保证盈利」类表述
- **禁止**输出胜率数字；**禁止**直接下达「建议做多/做空」指令，改用「若做多/若做空，观察区与止损在…」
- 每个关键价位必须**≥2 维数据**交叉验证（清算+订单簿 / CVD+OI / 费率+清算池 等）
- 用户提示中已提供的数据须标注时效：**实时** / **1h 级** / **日级**（按输入块标注）

### 数据与表述
- 若用户提示含「宏观数据覆盖说明」，须遵守：**已有恐惧贪婪/市占/DXY/纳指等任一数值时，不得写「宏观数据完全缺失」**
- 订单簿「合计深度为 0」时：表述为**未获得有效 L2 合计或当前为 0**，**禁止**据此断言「流动性完全消失」，除非另有字段证明

### 宏观-微观联动（仅当用户提示中该项有数值时引用；无则写「数据未提供」勿编造）
- DXY 单日波动较大 → 风险资产承压/支撑需结合当日数据
- 纳斯达克/标普走弱 → risk-off，谨慎追高
- 黄金与 BTC 背离 → 留意避险资金轮动（需有数据）
- 恐惧贪婪极值 + 资金费率极端 → 过热/过冷，与清算磁吸结合评估

### 狙击挂单（高 R:R）原则
- 规则引擎预算的 R:R 已按 ≥ **1:{min_rr:.1f}** 过滤；你必须在**第四节**完整展开，不得仅写「审核通过」或省略
- 若引擎无方案：说明原因（如清算簇过远/数据不足），**不得**编造价位
- 每个方向**最多 2 套**挂单叙述；每套须含：**挂单价区间或代表价、止损、止盈1/止盈2、R:R（至少给到 TP1 对应 R:R）**
- **失效条件**：至少写 1 条（例：价格有效跌破/突破某清算簇外沿则计划作废；或 1H 收盘越过某关键位则失效）——以**级别+条件**表述即可

### 阶梯埋伏计划（Scaled-In Limit Order Strategy）原则
- 基于**当前实时价格**动态生成，非固定底部区间（如价格从 7万→6.8万，阶梯会跟随下移）
- **多空双向同时输出**：做多=向下分层接多单；做空=向上分层接空单
- 在当前价向下/向上 1%-20% 范围内的清算密集区底部/顶部分层挂限价单
- 每层独立止损（止损在清算真空区内或按百分比保底），互不影响
- 越深层仓位越大（倒金字塔）：越远的层如果命中，R:R 越高
- 核心数学期望：全部被扫损总亏 N%，任一层命中可赚 M 倍（M >> N）
- **必须评估**：清算瀑布连锁风险、极端行情止损滑点、保证金占用效率
- 与狙击挂单互补：狙击=近距精准(≤5%)单层猎杀，阶梯=广覆盖多层网捕

### 输出格式（严格按以下 Markdown 章节标题输出，便于系统解析）

## 一、市场格局总览
（3-5句：宏观风向→杠杆水平→资金流→情绪→格局定性）

## 二、关键价位图谱
| 类型 | 价位区间 | 依据(≥2维+时效) |
（支撑、阻力、价值中枢、清算磁吸位）

## 三、止损安全区建议
**做多方向：** 区间 + 防猎杀原理 + 失效情形
**做空方向：** 同上

## 四、狙击挂单计划（高 R:R 埋伏单）
**本节为必答。** 须基于用户提示「### 11. 规则引擎狙击方案」逐条处理：
- **多单埋伏**（若有）：挂单价/止损/止盈1/止盈2/R:R + 逻辑（为何是捡尸位）+ 失效条件 + 若被止损的大致损失（单位：价格距离×1单位）
- **空单埋伏**（若有）：同上
- 若引擎方案需调整：写明**调整后的完整数值**与理由；拒绝时说明拒绝原因
- **禁止**输出 R:R 低于 1:{min_rr:.1f} 的「优质」挂单（除非明确标注为观察/不执行）

## 五、阶梯埋伏计划（基于当前价的多空双向多层网）
**本节为必答。** 须基于用户提示「### 12. 规则引擎阶梯埋伏方案」逐条处理：
- **做多阶梯**（若有）和**做空阶梯**（若有）须分别展开
- 逐层展开：**层级/挂单价/止损/止盈/R:R/仓位权重/风险占比**
- 综合评估：
  - 总风险预算 vs 账户承受能力
  - **清算瀑布连锁风险**：价格快速穿越多层时各层是否会被连续扫损
  - **止损滑点预估**：极端行情下止损执行偏差
  - **资金效率**：保证金占用 vs 等待触发的时间成本
- **调整建议**：若某层挂单位置不佳（正好在整数关口、清算真空区太薄、或两层间距过近不如合并），须提出具体调整
- **失效场景**：整体计划在什么条件下应废弃（基本面重大变化、交易所黑天鹅、市场结构转变等）
- 若引擎无方案：说明原因（如该方向无足够距离的清算簇），**不得**编造

## 六、入场观察区
**多单观察区** / **空单观察区**：共振因素 + 确认信号（可与第四/五节区分：第四节偏近距限价埋伏，第五节偏远距阶梯，本节偏顺势确认）

## 七、当前风险提示
（3-5条，[高/中/低]，按紧急程度）

## 八、场景推演
场景A/B/C：触发条件 + 目标位 + 时间窗口（可用「未来数小时/数日」等模糊窗）
"""


# 启动时懒加载一次亦可；此处每次 build 读取配置以支持热更新 yaml（若未来重载）
SYSTEM_PROMPT = build_system_prompt()


def build_user_prompt(snapshot: dict) -> str:
    """将结构化数据快照转为 AI 可读的用户提示"""
    min_rr = _min_sniper_rr()
    coin = snapshot.get("coin", "BTC")
    price = snapshot.get("price", 0)

    lines = [
        f"## 当前分析币种: {coin}/USDT",
        f"当前价格: ${price:,.2f}",
        f"24h最高: ${snapshot.get('high_24h', 0):,.2f}",
        f"24h最低: ${snapshot.get('low_24h', 0):,.2f}",
        "",
        f"【引擎约束】规则引擎狙击方案仅保留 R:R ≥ 1:{min_rr:.1f} 的条目；第四节须与之一致或明确调整理由。",
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

    bid_tot = float(snapshot.get("orderbook_bid_total_usd") or 0)
    ask_tot = float(snapshot.get("orderbook_ask_total_usd") or 0)
    ob_spread = float(snapshot.get("orderbook_spread_pct") or 0)
    lines.extend([
        "",
        "### 6. 订单簿深度 [实时 · OKX L2]",
        f"近档位合计深度(USD): 买盘 ${bid_tot / 1e6:.2f}M / 卖盘 ${ask_tot / 1e6:.2f}M | 价差 {ob_spread:.4f}%",
        "说明: 合计深度来自订单簿快照；若下方「大单墙」为空，表示当前无超过阈值的挂单墙，**不等于**无订单簿数据。",
        "主要买墙(超阈值):",
    ])
    for w in snapshot.get("orderbook_bid_walls", []):
        lines.append(f"  - ${w.get('price', 0):,.1f}: ${w.get('size_usd', 0) / 1e6:.1f}M ({w.get('order_count', 0)}单)")
    lines.append("主要卖墙(超阈值):")
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

    has_trad = any(snapshot.get(k) for k in ("dxy", "nasdaq", "sp500", "gold"))
    has_crypto_sent = fgi is not None or dom is not None
    lines.extend([
        "",
        "【宏观数据覆盖说明】（请严格按此表述，避免与上文矛盾）",
        f"- 加密侧情绪/结构: {'已提供（恐惧贪婪/市占等）' if has_crypto_sent else '未提供'}",
        f"- 传统外盘(DXY/纳指/标普/黄金): {'已提供部分或全部数值' if has_trad else '本条目中未解析到有效数值（若恐惧贪婪已提供，不得写宏观完全缺失）'}",
    ])

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
    lines.append("")
    lines.append("### 11. 规则引擎狙击方案（必须在「四、狙击挂单计划」中完整展开，不可省略）")
    if sniper:
        for i, se in enumerate(sniper):
            d = se.get("direction", "")
            lines.append(f"方案{i+1} [{d}]: "
                         f"入场${se.get('entry_price', 0):,.1f} "
                         f"止损${se.get('stop_loss', 0):,.1f} "
                         f"TP1=${se.get('take_profit_1', 0):,.1f}(R:R {se.get('rr_ratio_1', 0):.1f}) "
                         f"TP2=${se.get('take_profit_2', 0):,.1f}(R:R {se.get('rr_ratio_2', 0):.1f})")
            for logic_line in se.get("logic", []):
                lines.append(f"    - {logic_line}")
    else:
        lines.append("（当前无引擎输出的狙击方案：可能因清算簇距离/ATR/数据不足；第四节须说明原因，禁止编造价位。）")

    ladder_plans = snapshot.get("ladder_plans", [])
    lines.append("")
    lines.append("### 12. 规则引擎阶梯埋伏方案（必须在「五、阶梯埋伏计划」中完整展开，不可省略）")
    if ladder_plans:
        for lp in ladder_plans:
            d = lp.get("direction", "")
            lines.append(f"\n**{'做多' if d == 'long' else '做空'}阶梯计划** "
                         f"({lp.get('tier_count', 0)}层, 覆盖{lp.get('coverage_range', '')}, "
                         f"总风险{lp.get('total_risk_pct', 0):.1f}%):")
            lines.append(f"  概要: {lp.get('plan_summary', '')}")
            lines.append(f"  期望优势: {lp.get('expected_edge', '')}")
            lines.append(f"  最佳R:R: {lp.get('best_case_rr', 0):.1f}:1")
            lines.append(f"  最差全损: {lp.get('worst_case_loss_pct', 0):.1f}%")
            for entry in lp.get("entries", []):
                lines.append(f"  第{entry.get('tier', 0)}层: "
                             f"入场${entry.get('entry_price', 0):,.1f} "
                             f"止损${entry.get('stop_loss', 0):,.1f} "
                             f"止盈${entry.get('take_profit', 0):,.1f} "
                             f"R:R={entry.get('rr_ratio', 0):.1f} "
                             f"仓位{entry.get('position_weight', 0):.1%} "
                             f"风险{entry.get('risk_pct', 0):.1f}%")
                lines.append(f"    区域: {entry.get('zone_label', '')}")
                lines.append(f"    失效: {entry.get('invalidation', '')}")
                for logic_line in entry.get("entry_logic", []):
                    lines.append(f"      - {logic_line}")
    else:
        lines.append("（当前无引擎输出的阶梯方案：可能因远距无足够清算簇/数据不足；第五节须说明原因，禁止编造。）")

    lines.append("")
    lines.append("请基于以上数据输出，**必须包含八个章节**，且第四节「狙击挂单计划」和第五节「阶梯埋伏计划」均为必答。")
    lines.append("重点：1) 止损防猎杀 2) 宏观-微观一致 3) 第四节与引擎 R:R 口径对齐（≥1:{:.1f}） 4) 第五节评估阶梯计划的瀑布风险和资金效率".format(min_rr))
    return "\n".join(lines)
