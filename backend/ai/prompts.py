"""AI Prompt 模板管理（方案C：合规底座 + 狙击挂单硬交付 + 教练视角）"""

from __future__ import annotations

from config.settings import get_settings
from processors.level_discovery import fmt_usd_cn


def _fmt_usd_for_prompt(usd: float) -> str:
    """AI prompt 专用金额格式化（中文单位 + $ 前缀）。"""
    return f"${fmt_usd_cn(usd)}"


def _min_sniper_rr() -> float:
    return float(get_settings().processors.levels.get("min_sniper_rr", 2.5))


def build_system_prompt() -> str:
    """动态注入与配置一致的 R:R 下限，避免与规则引擎口径漂移。"""
    min_rr = _min_sniper_rr()
    return f"""你是一位管理$5亿级永续合约基金的实盘交易员兼教练，10年加密衍生品经验。你不是在写研报，你是在给即将下单的高杠杆交易员做最后的战前推演。

你的每一句话都要回答三个问题：**"所以应该怎么做？为什么？如果错了怎么办？"**
你的核心能力：像庄家一样思考——看穿清算地图背后的止损猎杀意图，判断"谁会被收割、价格最可能走哪条路径"，输出高 R:R 狙击参考（非喊单）。

### 铁律
- 你是**决策参考工具**，交易员最终拍板；禁止"保证盈利"类表述、禁止输出胜率数字
- 关键价位必须 **≥2 维**交叉验证；每个章节至少 1 处跨维度推理（≥2 个数据源→新结论）
- 数据矛盾时必须指出并判断哪个更可信（实盘资金行为 > 情绪指标）
- 若宏观数据中已有恐惧贪婪/DXY/纳指等任一数值，不得写"宏观数据完全缺失"
- 订单簿合计深度为 0 时表述为"未获得有效 L2 数据"，禁止断言"流动性完全消失"
- **数据异常值怀疑**：遇到标注"⚠ 极端异常值"的数据，必须在引用时标注可能不准确，权重降至最低，不得作为方向判断的核心依据。交易员不信任离群数据点。

### Smart Money 流动性框架
- clusters_above = BSL（上方流动性，空头清算=强制买入→推高价格）；clusters_below = SSL（下方流动性，多头清算=强制卖出→压低价格）
- 扫取一侧后该侧动能耗尽，关注另一侧是否成为下一目标——须 CVD/OI/费率共振验证
- 级联踩踏：多簇 <2% 间距连续排列时可能链式爆仓（cascade liquidation），止损须设在最外层之外
- §一须用"上方/下方流动性"描述清算分布并说明偏向

### CPS（周期评分 0-10）统一规则
CPS 由 MVRV Z、Ahr999、200周均线比、STH成本、Pi周期综合评分，为**日线级指标**，不可单独用于实时方向判断。
- **4-7（震荡区）**：清算磁吸权重上调、箱体/关键位反弹策略权重上调
- **<2 或 >8（极端区）**：趋势概率高、级联踩踏风险上调；<2 时做空阶梯优先，≥6 时做多阶梯信心增强
- **方向冲突**：CPS≥6 输出做空阶梯 或 CPS≤2 输出做多阶梯 → 标题加"⚠CPS方向冲突"，单层风险上限降至 2%

### 均线箱体（§9f 有数据时）
- **核心箱体（MA 骨架）**：MA60D / MA120D / MA60W 三条均线，上方最近 MA 为上沿、下方最近 MA 为下沿
- **微观区间**：关键位模块 S/A/B 级最近支撑阻力，作为日内参考
- MACD 0轴下方 → 反弹至 MA 做空优先；0轴上方 → 做空需额外谨慎
- price_position = "middle" 时：§一须提示无明确方向，§四禁止基于箱体开单
- §一引用箱体位置及 MA 名称；§二纳入 MA60/MA120/MA60W；A/S 级信号时§四须评估共振

### 关键位 V2 状态机（§9g 有数据时）
关键位经历 IDLE→APPROACHING→TESTING→SWEPT/BOUNCED→BROKEN→FLIPPED 生命周期，V2 含 strength_tier(S/A/B/C)、confluence_score、多来源维度。
- **SWEPT（A级）**：流动性已被扫取→最高置信反向入场，§四须优先采纳
- **FLIPPED（A级）**：经典 S/R 翻转，§四须评估
- **BOUNCED（B级）**：测试后反弹确认
- cascade_risk >0.7 = 瀑布穿透风险，止损须设最外层之外
- SWEPT/FLIPPED 与引擎狙击方案方向一致 → 置信度提升至A级
- K线形态（pin bar/engulfing/doji）确认时进一步提升信号可信度

### 宏观-微观联动（仅当数据中有值时引用，无则写"数据未提供"）
- DXY/纳指/标普走弱 → risk-off 谨慎追高；黄金与 BTC 背离 → 避险轮动
- 恐惧贪婪极值须与多空比、CVD、费率等交叉验证，单独引用标注"参考权重低"
- §9e 距当前价 ≤15% 链上价位纳入§二；RPLR<0 = 中期底部前兆，>0.5 = 回调风险
- Coinbase溢价 + ETF + 稳定币三维共振 = 最强资金面信号
- 净持仓正转负 + OI↓ = 机构平多；合约净资金流正 + OI↑ = 新资金开多；TD ≥ 9 = "追单风险极高"

### 交易员推理框架
**像庄家一样思考**：先问"如果我持有$10亿仓位，我会把价格往哪推来最大化清算收益？"，然后构建猎杀路径。
构建**资金流叙事链**：资金面(ETF+稳定币+CB溢价)→杠杆水位(OI+费率+交易所异动)→庄家意图(清算地图+订单簿+大单)→微观触发(CVD+爆仓+巨鲸)→结论
- §一先写 3-5 句叙事链总结（用因果逻辑串联多维数据，如"因为X所以Y导致Z"），再用简表列 ≥7 维方向信号作为佐证
- §七**核心不是"涨或跌"而是"价格最可能走哪条路径"**——须推演庄家的最优猎杀路线（先扫哪侧流动性→反转→再扫另一侧），末尾选定**唯一**最偏向场景（禁止骑墙）
- §四每个方案必须回答："如果这笔交易亏了，最可能的原因是什么？"——不是复述风险提示，而是从对手盘角度推演失败场景

### AI 自主构建交易方案
- 引擎方案优先采纳；引擎未覆盖的方向/距离档，AI 可基于数据**自主构建**方案
- AI 自主方案必须：标注"⚡AI推断"、≥2 维数据交叉验证、满足 R:R ≥ 1:{min_rr:.1f} 约束
- 可用数据源：V2 关键位（S/A 级 idle 状态也可参考）、清算簇、MA 箱体边界、斐波那契、VP POC
- **禁止**凭空编造无数据支撑的价位；每个 AI 推断方案须注明数据依据

### 交易计划原则（§四·三档结构）
- 引擎 R:R 已按 ≥ **1:{min_rr:.1f}** 过滤；须完整展开每个方案，禁止"审核通过"或省略
- **R:R 验算**：须代入具体价格写出公式，禁止"≈"估值
- **止损铁律**：做空 SL > Entry；做多 SL < Entry — 违反即废弃。止损宽度 ≥ max(价格×0.3%, 0.5×ATR)
- **约束冲突**：止损方向+最小宽度+R:R≥1:{min_rr:.1f} 三条无法同时满足 → 该方案不输出，声明原因。**不交易是最好的风控**
- 引擎无方案的档位，AI 可基于数据自主构建（标注"⚡AI推断"），或声明该档位暂无机会
- 短线档止损须设在清算真空区内（防猎杀）；中/远线档止损宽度 ≥ sl_min_pct

### 输出格式（严格按标题，系统解析用）

## 一、市场格局总览
**第一行必须是白话总结：**
> 📝 **看多/看空/震荡（置信度：高/中/低）**——30字以内核心理由（禁止专业术语）

然后用 3-5 句**因果叙事链**串联核心矛盾（如"ETF资金持续流入→推高OI→但价格卡在关键阻力→说明多头在堆仓但还没突破"），让交易员一读就知道"现在是什么局面、谁在主导、关键变量是什么"。
结尾附简表（≥7 维方向信号 + 共振强度），作为叙事链的量化佐证。

## 二、关键价位图谱
| 类型 | 价位区间 | 依据(≥2维+时效) |

## 三、入场观察区
多单/空单观察区：共振因素 + 确认信号

## 四、交易计划（三档结构）
按距离分三档，每档每方向不限数量——所有满足 R:R 约束且有数据支撑的方案均须展示，按信心度排序。每个方案须包含止损说明（含防猎杀逻辑）和"**如果亏了**"段。

**短线档（距当前价 1-8%）**
- 数据来源：引擎狙击方案 + V2 关键位信号(SWEPT/BOUNCED/FLIPPED) + AI自主发现
| 方向 | 挂单价 | 止损 | TP1(R:R) | TP2(R:R) | 信心度 | 核心依据 |

**中线档（距当前价 5-10%）**
- 数据来源：引擎阶梯前层 + 高共振关键位(S/A级) + AI自主发现（标注⚡AI推断）
| 方向 | 挂单价 | 止损 | TP(R:R) | 信心度 | 核心依据 |

**远线档（距当前价 10-20%）**
- 数据来源：引擎阶梯远层 + 7d/30d清算地图 + CPS周期位置
- 仅在 CPS 极端区(<2 或 >8)或有 S 级关键位时输出
| 方向 | 挂单价 | 止损 | TP(R:R) | 信心度 | 核心依据 |

某档无机会时，一行说明原因即可。

## 五、当前风险提示
3-5条 [高/中/低] 按紧急程度

## 六、操作纪律
关键注意事项、资金管理要点（简短）

## 七、场景推演
场景A/B/C：触发条件+目标位+时间窗口
**当前数据偏向：** 场景X（唯一选定）

## 八、数据质量与自检
对本次输入数据做诊断，列出发现的问题（若无则写"本次数据质量良好"）：
- **缺失数据**：哪些关键维度未提供或为空（如箱体/关键位/净持仓等），对分析的影响
- **异常值**：哪些数据疑似异常（如Coinbase溢价>1%、费率极端等），已如何处理
- **数据冲突**：哪些维度给出矛盾信号（如CVD看多但OI下降），如何取舍
- **改进建议**：对数据采集或指标计算的建议（可选）

### 格式铁律
- 场景推演以 `场景A：` 开头，禁止加粗前缀
- 价格区间**必须小值在前-大值在后**（如 $73,200 - $73,400），违反即重排
- 表格分隔行用 `|---|---|---|`

### 常见错误纠正（禁止犯）
- **资金费率方向**：正费率=多头付钱给空头=**多头拥挤**（轧多风险）；负费率=空头付钱给多头=**空头拥挤**（轧空风险）。绝对不可写反。
- **R:R 展示顺序**：§四每个方案须先展示 TP1 的 R:R（主目标），再展示 TP2 的 R:R（保守目标）。R:R≥1:{min_rr:.1f} 的达标判定以 TP1 为准。
"""




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
        "### 1. 清算地图数据 [核心·实时]",
        f"多空失衡比: {snapshot.get('liq_imbalance_ratio', 0):.2f} (>1=空头清算多/看多磁吸, <1=多头清算多/看空磁吸)",
    ]

    lines.append("\n上方清算密集区(空头清算):")
    for c in snapshot.get("liq_clusters_above", []):
        lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                     f"{_fmt_usd_for_prompt(c.get('total_usd', 0))} ({c.get('dominant_leverage', '')}x) "
                     f"距当前{c.get('distance_pct', 0):.1f}%")
        if c.get("price_from", 0) <= price <= c.get("price_to", 0):
            lines.append(f"    ⚠ 当前价${price:,.1f}已在此簇范围内 — 清算正在发生，基于此簇的策略前提需重新评估")

    lines.append("\n下方清算密集区(多头清算):")
    for c in snapshot.get("liq_clusters_below", []):
        lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                     f"{_fmt_usd_for_prompt(c.get('total_usd', 0))} ({c.get('dominant_leverage', '')}x) "
                     f"距当前{c.get('distance_pct', 0):.1f}%")
        if c.get("price_from", 0) <= price <= c.get("price_to", 0):
            lines.append(f"    ⚠ 当前价${price:,.1f}已在此簇范围内 — 清算正在发生，基于此簇的策略前提需重新评估")

    lines.append("\n清算真空区(止损安全区域):")
    for v in snapshot.get("vacuum_zones", []):
        lines.append(f"  - ${v.get('price_from', 0):,.0f}-${v.get('price_to', 0):,.0f} {v.get('note', '')}")

    bsl_24h = sum(c.get("total_usd", 0) for c in snapshot.get("liq_clusters_above", []))
    ssl_24h = sum(c.get("total_usd", 0) for c in snapshot.get("liq_clusters_below", []))
    if bsl_24h > 0 or ssl_24h > 0:
        lines.append(f"\n24h流动性视角:")
        lines.append(f"  上方流动性(BSL): {_fmt_usd_for_prompt(bsl_24h)} (空头清算=强制买入 → 扫取后为做空提供对手盘)")
        lines.append(f"  下方流动性(SSL): {_fmt_usd_for_prompt(ssl_24h)} (多头清算=强制卖出 → 扫取后为做多提供对手盘)")
        if ssl_24h > 0 and bsl_24h > ssl_24h * 1.5:
            lines.append(f"  偏向: 上方流动性远多于下方({bsl_24h/ssl_24h:.1f}x) → 价格倾向先上扫BSL再反转")
        elif bsl_24h > 0 and ssl_24h > bsl_24h * 1.5:
            lines.append(f"  偏向: 下方流动性远多于上方({ssl_24h/bsl_24h:.1f}x) → 价格倾向先下扫SSL再反转")
        elif ssl_24h == 0:
            lines.append(f"  偏向: 仅上方有流动性({_fmt_usd_for_prompt(bsl_24h)}) → 上方为唯一磁吸目标")
        elif bsl_24h == 0:
            lines.append(f"  偏向: 仅下方有流动性({_fmt_usd_for_prompt(ssl_24h)}) → 下方为唯一磁吸目标")
        else:
            lines.append(f"  偏向: 上下流动性相对均衡 → 双向扫取概率接近，关注CVD/OI确认方向")

    sweep_above = snapshot.get("liq_sweep_above_usd_1h", 0)
    sweep_below = snapshot.get("liq_sweep_below_usd_1h", 0)
    if sweep_above > 0 or sweep_below > 0:
        lines.append(f"\n近1h流动性扫取检测:")
        if sweep_above > 0:
            lines.append(f"  上方已扫取: {_fmt_usd_for_prompt(sweep_above)} BSL — 上方流动性被消耗，上行推动力减弱")
        if sweep_below > 0:
            lines.append(f"  下方已扫取: {_fmt_usd_for_prompt(sweep_below)} SSL — 下方流动性被消耗，下行推动力减弱")
        if sweep_above > 0 and sweep_below > 0:
            lines.append(f"  解读: 上下流动性均被扫取 → 市场剧烈波动，双侧动能消耗，关注新流动性积累方向")
        elif sweep_above > 0:
            lines.append(f"  解读: 价格已扫取上方流动性 → 关注下方SSL是否成为下一个目标（需CVD/OI确认）")
        elif sweep_below > 0:
            lines.append(f"  解读: 价格已扫取下方流动性 → 关注上方BSL是否成为下一个目标（需CVD/OI确认）")

    clusters_above_7d = snapshot.get("liq_clusters_above_7d", [])
    clusters_below_7d = snapshot.get("liq_clusters_below_7d", [])
    vacuums_7d = snapshot.get("vacuum_zones_7d", [])
    imb_7d = snapshot.get("liq_imbalance_ratio_7d", 0)
    if clusters_above_7d or clusters_below_7d:
        lines.extend(["", "### 1b. 清算地图数据 [7天·阶梯策略核心]"])
        lines.append(f"7天多空失衡比: {imb_7d:.2f}")
        if clusters_above_7d:
            lines.append("\n7天上方清算密集区(空头清算):")
            for c in clusters_above_7d:
                lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                             f"{_fmt_usd_for_prompt(c.get('total_usd', 0))} ({c.get('dominant_leverage', '')}x) "
                             f"距当前{c.get('distance_pct', 0):.1f}%")
        if clusters_below_7d:
            lines.append("\n7天下方清算密集区(多头清算):")
            for c in clusters_below_7d:
                lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                             f"{_fmt_usd_for_prompt(c.get('total_usd', 0))} ({c.get('dominant_leverage', '')}x) "
                             f"距当前{c.get('distance_pct', 0):.1f}%")
        if vacuums_7d:
            lines.append("\n7天清算真空区:")
            for v in vacuums_7d:
                lines.append(f"  - ${v.get('price_from', 0):,.0f}-${v.get('price_to', 0):,.0f} {v.get('note', '')}")
        bsl_7d = sum(c.get("total_usd", 0) for c in clusters_above_7d)
        ssl_7d = sum(c.get("total_usd", 0) for c in clusters_below_7d)
        if bsl_7d > 0 or ssl_7d > 0:
            lines.append(f"\n7天流动性视角:")
            lines.append(f"  上方流动性(BSL): {_fmt_usd_for_prompt(bsl_7d)} / 下方流动性(SSL): {_fmt_usd_for_prompt(ssl_7d)}")

    clusters_above_30d = snapshot.get("liq_clusters_above_30d", [])
    clusters_below_30d = snapshot.get("liq_clusters_below_30d", [])
    imb_30d = snapshot.get("liq_imbalance_ratio_30d", 0)
    if clusters_above_30d or clusters_below_30d:
        lines.extend(["", "### 1c. 清算地图数据 [30天·超远距阶梯参考]"])
        lines.append(f"30天多空失衡比: {imb_30d:.2f}")
        if clusters_above_30d:
            lines.append("\n30天上方清算密集区:")
            for c in clusters_above_30d[:8]:
                lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                             f"{_fmt_usd_for_prompt(c.get('total_usd', 0))} ({c.get('dominant_leverage', '')}x) "
                             f"距当前{c.get('distance_pct', 0):.1f}%")
        if clusters_below_30d:
            lines.append("\n30天下方清算密集区:")
            for c in clusters_below_30d[:8]:
                lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                             f"{_fmt_usd_for_prompt(c.get('total_usd', 0))} ({c.get('dominant_leverage', '')}x) "
                             f"距当前{c.get('distance_pct', 0):.1f}%")

    hotspots = snapshot.get("liq_heatmap_hotspots", [])
    if hotspots:
        lines.extend(["", "### 1d. 清算热力图 [价格-时间密度峰值·Top5]"])
        for hs in hotspots:
            pct = hs.get("pct_above", 0)
            if pct > 0:
                pos_str = f"上方{pct:.1f}%"
            elif pct < 0:
                pos_str = f"下方{abs(pct):.1f}%"
            else:
                pos_str = "当前价附近"
            lines.append(
                f"  - ${hs.get('price', 0):,.0f} ({pos_str}): "
                f"密度 {_fmt_usd_for_prompt(hs.get('total_usd', 0))}"
            )
        lines.append("说明: 热力图反映价格-时间维度的清算订单密集程度，密度越高=该价位附近被清算的概率越大=更强的磁吸效应。")

    lines.extend([
        "",
        "### 2. 资金流数据 [核心·实时]",
        f"合约CVD趋势(1h): {snapshot.get('cvd_contract_trend', 'N/A')} (净delta: {_fmt_usd_for_prompt(snapshot.get('cvd_contract_delta_1h', 0))})",
        f"现货CVD趋势(1h): {snapshot.get('cvd_spot_trend', 'N/A')} (净delta: {_fmt_usd_for_prompt(snapshot.get('cvd_spot_delta_1h', 0))})",
        f"CVD背离信号: {snapshot.get('cvd_divergence', '无') or '无'}",
    ])

    taker_buy = snapshot.get("taker_buy_ratio")
    if taker_buy is not None:
        lines.append(f"Taker买卖力量: 买方{taker_buy:.0%} / 卖方{1-taker_buy:.0%} → {snapshot.get('taker_dominant', '')}")

    lines.extend([
        "",
        "### 3. 持仓与杠杆 [核心·实时]",
        f"OI总量: ${snapshot.get('oi_current_usd', 0) / 1e9:.2f}B",
        f"OI变化(1h): {snapshot.get('oi_change_1h_pct', 0):+.2f}%",
        f"OI变化(5m): {snapshot.get('oi_change_5m_pct', 0):+.2f}%",
        f"OI趋势: {snapshot.get('oi_trend', 'N/A')}",
    ])

    lines.extend([
        "",
        "### 4. 资金费率 [核心·多交易所]",
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
    lines.append("  (提醒: 正费率=多头付钱=多头拥挤; 负费率=空头付钱=空头拥挤，不可写反)")
    avg7d = snapshot.get("funding_avg_7d")
    if avg7d is not None:
        lines.append(f"7d均值: {avg7d*100:.4f}%")
    if funding_exchanges:
        extreme_fr = [fe for fe in funding_exchanges
                      if fe.get("current") is not None and abs(fe["current"]) > 0.001]
        if extreme_fr:
            names = ", ".join(fe.get("exchange", "") for fe in extreme_fr)
            lines.append(f"  ⚠ {names} 费率绝对值>0.1%，属于极端水平，需警惕轧空/轧多风险")

    lines.extend([
        f"期现溢价: {snapshot.get('basis_pct', 0):+.4f}%",
        "",
        "### 5. 多空比 [各交易所]",
    ])
    ls = snapshot.get("ls_ratio")
    if ls is not None:
        lines.append(f"全局账户多空比: {ls:.2f} ({snapshot.get('ls_ratio_interpretation', '')})")
    else:
        lines.append("全局账户多空比: 数据暂缺")
    ls_ta = snapshot.get("ls_ratio_top_account")
    ls_tp = snapshot.get("ls_ratio_top_position")
    if ls_ta is not None:
        ta_label = "大户偏多" if ls_ta > 1.1 else ("大户偏空" if ls_ta < 0.9 else "大户中性")
        lines.append(f"大户账户多空比: {ls_ta:.2f} → {ta_label}")
    if ls_tp is not None:
        tp_label = "大户持仓偏多" if ls_tp > 1.1 else ("大户持仓偏空" if ls_tp < 0.9 else "大户持仓中性")
        lines.append(f"大户持仓多空比: {ls_tp:.2f} → {tp_label}")
    if ls is not None and ls_ta is not None:
        if (ls > 1.1 and ls_ta < 0.9) or (ls < 0.9 and ls_ta > 1.1):
            lines.append(f"  ⚠ 散户vs大户多空分歧：散户{'偏多' if ls > 1 else '偏空'}而大户{'偏多' if ls_ta > 1 else '偏空'}，关注大户方向")
    okx_ls = snapshot.get("okx_ls_ratio_btc")
    bn_ls = snapshot.get("binance_ls_ratio_btc")
    if okx_ls is not None or bn_ls is not None:
        parts = []
        if okx_ls is not None:
            parts.append(f"OKX {okx_ls:.2f}")
        if bn_ls is not None:
            parts.append(f"Binance {bn_ls:.2f}")
        lines.append(f"各所多空比: {' / '.join(parts)}")

    bid_tot = float(snapshot.get("orderbook_bid_total_usd") or 0)
    ask_tot = float(snapshot.get("orderbook_ask_total_usd") or 0)
    ob_spread = float(snapshot.get("orderbook_spread_pct") or 0)
    lines.extend([
        "",
        "### 6. 订单簿深度 [Coinglass聚合·多交易所]",
        f"近档位合计深度(USD): 买盘 {_fmt_usd_for_prompt(bid_tot)} / 卖盘 {_fmt_usd_for_prompt(ask_tot)} | 买卖力差 {ob_spread:+.2f}%",
        f"买卖力对比: {'买盘强于卖盘(支撑偏强)' if bid_tot > ask_tot * 1.1 else '卖盘强于买盘(抛压偏重)' if ask_tot > bid_tot * 1.1 else '买卖均衡'}",
        "说明: 聚合深度来自 Binance/OKX/Bybit 订单簿快照。",
        "主要买墙(超阈值):",
    ])
    for w in snapshot.get("orderbook_bid_walls", []):
        lines.append(f"  - ${w.get('price', 0):,.1f}: {_fmt_usd_for_prompt(w.get('size_usd', 0))} ({w.get('order_count', 0)}单)")
    lines.append("主要卖墙(超阈值):")
    for w in snapshot.get("orderbook_ask_walls", []):
        lines.append(f"  - ${w.get('price', 0):,.1f}: {_fmt_usd_for_prompt(w.get('size_usd', 0))} ({w.get('order_count', 0)}单)")

    lines.extend([
        "",
        "### 7. 爆仓数据（24h聚合）",
        f"24h多头爆仓: {_fmt_usd_for_prompt(snapshot.get('recent_liq_24h_long_usd', snapshot.get('recent_liq_30m_long_usd', 0)))}",
        f"24h空头爆仓: {_fmt_usd_for_prompt(snapshot.get('recent_liq_24h_short_usd', snapshot.get('recent_liq_30m_short_usd', 0)))}",
    ])
    gl1h_long = snapshot.get("global_liq_long_1h", 0)
    gl1h_short = snapshot.get("global_liq_short_1h", 0)
    if gl1h_long > 0 or gl1h_short > 0:
        lines.append(f"全网1h多头爆仓: {_fmt_usd_for_prompt(gl1h_long)} / 空头: {_fmt_usd_for_prompt(gl1h_short)}")
    g_long = snapshot.get("global_liq_long_24h", 0)
    g_short = snapshot.get("global_liq_short_24h", 0)
    if g_long > 0 or g_short > 0:
        lines.append(f"全网24h多头爆仓: {_fmt_usd_for_prompt(g_long)}")
        lines.append(f"全网24h空头爆仓: {_fmt_usd_for_prompt(g_short)}")
        ratio_24h = snapshot.get("global_liq_ratio_24h", 1.0)
        lines.append(f"全网多空爆仓比: {ratio_24h:.1f}")
    largest = snapshot.get("global_liq_largest_single", 0)
    if largest > 0:
        lines.append(f"最大单笔爆仓: {_fmt_usd_for_prompt(largest)}")

    lines.extend([
        "",
        "### 8. 成交分布与波动率 [参考·1H K线]",
        f"Volume Profile POC: ${snapshot.get('volume_profile_poc', 0):,.2f}",
        f"Value Area: ${snapshot.get('value_area_low', 0):,.2f} - ${snapshot.get('value_area_high', 0):,.2f}",
        f"VWAP(多日成交加权): ${snapshot.get('vwap', 0):,.2f}",
        f"ATR(14, Wilder): ${snapshot.get('atr_14', 0):,.2f}",
    ])

    rsi = snapshot.get("rsi_14")
    macd_hist = snapshot.get("macd_histogram")
    macd_az = snapshot.get("macd_above_zero")
    boll_u = snapshot.get("boll_upper")
    boll_m = snapshot.get("boll_middle")
    boll_l = snapshot.get("boll_lower")
    ema20_v = snapshot.get("ema20")
    ma60_v = snapshot.get("ma60_daily")
    ma120_v = snapshot.get("ma120_daily")
    if any(v is not None for v in (rsi, macd_hist, boll_u, ema20_v, ma60_v)):
        lines.extend(["", "### 8b. 技术指标 [Coinglass·日线级]"])
        if rsi is not None:
            rsi_label = "超卖" if rsi < 30 else ("偏弱" if rsi < 45 else ("中性" if rsi < 55 else ("偏强" if rsi < 70 else "超买")))
            lines.append(f"RSI(14): {rsi:.1f} → {rsi_label}")
        if macd_hist is not None:
            macd_pos = "0轴上方(多头)" if macd_az else "0轴下方(空头)"
            hist_dir = "正值(动能增强)" if macd_hist > 0 else "负值(动能减弱)"
            lines.append(f"MACD: {macd_pos} | 柱状图={macd_hist:+.2f} {hist_dir}")
        if boll_u and boll_m and boll_l:
            lines.append(f"布林带: 上轨${boll_u:,.0f} / 中轨${boll_m:,.0f} / 下轨${boll_l:,.0f}")
            if price > 0:
                boll_pos = "超买区" if price > boll_u else ("超卖区" if price < boll_l else "中轨附近" if abs(price - boll_m) / boll_m < 0.01 else "中轨上方" if price > boll_m else "中轨下方")
                lines.append(f"  当前价位置: {boll_pos}")
        if ema20_v is not None:
            lines.append(f"EMA(20): ${ema20_v:,.0f}")
        if ma60_v is not None:
            lines.append(f"MA(60): ${ma60_v:,.0f}")
        if ma120_v is not None:
            lines.append(f"MA(120): ${ma120_v:,.0f}")

    opt_mp = snapshot.get("option_max_pain_price")
    opt_expiry = snapshot.get("option_nearest_expiry", "")
    opt_call = snapshot.get("option_call_oi")
    opt_put = snapshot.get("option_put_oi")
    if opt_mp is not None:
        lines.extend(["", "### 8c. 期权数据 [Coinglass]"])
        lines.append(f"最近到期 Max Pain: ${opt_mp:,.0f} ({opt_expiry})")
        if opt_call is not None and opt_put is not None:
            total_oi = opt_call + opt_put
            pc_ratio = opt_put / opt_call if opt_call > 0 else 0
            lines.append(f"看涨OI: {_fmt_usd_for_prompt(opt_call)} / 看跌OI: {_fmt_usd_for_prompt(opt_put)} | P/C比: {pc_ratio:.3f}")
            if opt_mp > 0 and price > 0:
                dist = (opt_mp - price) / price * 100
                lines.append(f"当前价距Max Pain: {dist:+.1f}% (价格倾向向Max Pain靠拢)")

    lo_buy = snapshot.get("large_orders_buy_count", 0)
    lo_sell = snapshot.get("large_orders_sell_count", 0)
    lo_net = snapshot.get("large_orders_net_usd", 0)
    if lo_buy > 0 or lo_sell > 0:
        lines.extend(["", "### 8d. 大单追踪 [实时]"])
        lines.append(f"大单买入: {lo_buy}笔 / 卖出: {lo_sell}笔 | 净方向: {_fmt_usd_for_prompt(lo_net)}")
        if lo_net > 0:
            lines.append(f"  大资金偏向: 买入为主(净流入)")
        elif lo_net < 0:
            lines.append(f"  大资金偏向: 卖出为主(净流出)")

    w_alerts = snapshot.get("whale_hl_alerts_count", 0)
    w_transfers = snapshot.get("whale_transfers_count", 0)
    w_dir = snapshot.get("whale_net_direction", "")
    hl_positions = snapshot.get("whale_hl_positions", [])
    if w_alerts > 0 or w_transfers > 0 or hl_positions:
        lines.extend(["", "### 8e. 巨鲸追踪 [链上+Hyperliquid]"])
        if w_alerts > 0:
            lines.append(f"Hyperliquid 巨鲸警报: {w_alerts}条")
        if w_transfers > 0:
            lines.append(f"链上巨鲸转账: {w_transfers}笔")
        if w_dir:
            lines.append(f"巨鲸方向: {w_dir}")
        if hl_positions:
            long_usd = sum(p.get("size_usd", 0) for p in hl_positions if p.get("side") == "long")
            short_usd = sum(p.get("size_usd", 0) for p in hl_positions if p.get("side") == "short")
            lines.append(f"Hyperliquid 巨鲸{coin}仓位: 多{_fmt_usd_for_prompt(long_usd)} / 空{_fmt_usd_for_prompt(short_usd)}")
            for p in hl_positions[:5]:
                pnl_str = f"{'盈利' if p.get('pnl', 0) > 0 else '亏损'}{_fmt_usd_for_prompt(abs(p.get('pnl', 0)))}"
                lines.append(f"  - {p.get('side','?')} {_fmt_usd_for_prompt(p.get('size_usd',0))} 入场${p.get('entry',0):,.0f} {p.get('leverage',0)}x | {pnl_str}")
            if long_usd > short_usd * 1.5:
                lines.append("  聪明钱倾向: 多头主导 → 大资金看涨")
            elif short_usd > long_usd * 1.5:
                lines.append("  聪明钱倾向: 空头主导 → 大资金看跌")

    cb_premium = snapshot.get("coinbase_premium", 0)
    cb_trend = snapshot.get("coinbase_premium_trend", "")
    if cb_premium != 0 or cb_trend:
        lines.extend(["", "### 8f. Coinbase 溢价 [核心·机构买盘信号·实时]"])
        prem_pct = cb_premium * 100 if abs(cb_premium) < 1 else cb_premium
        lines.append(f"溢价率: {prem_pct:+.3f}%")
        if abs(prem_pct) > 1.0:
            lines.append(f"  ⚠ 极端异常值（正常范围 ±0.5%）！可能为数据采集误差或流动性异常时点报价，**必须大幅降低此指标权重**，不得作为核心判断依据。")
        elif prem_pct > 0.05:
            lines.append("  正溢价=美股时段机构/散户净买入, 价格上行压力")
        elif prem_pct < -0.05:
            lines.append("  负溢价=Coinbase端卖出偏重, 机构可能在减仓")
        if cb_trend:
            lines.append(f"  近1h趋势: {cb_trend}")

    sc_mcap = snapshot.get("stablecoin_total_mcap", 0)
    sc_chg = snapshot.get("stablecoin_7d_change_pct", 0)
    if sc_mcap > 0:
        lines.extend(["", "### 8g. 稳定币市值 [场外资金·日级]"])
        lines.append(f"稳定币总市值: ${sc_mcap / 1e9:.1f}B | 7日变化: {sc_chg:+.2f}%")
        if sc_chg > 0.5:
            lines.append("  市值增长=场外新资金入场, 买盘弹药增加")
        elif sc_chg < -0.5:
            lines.append("  市值缩减=资金流出加密市场, 需警惕")

    oi_rank = snapshot.get("oi_exchange_rank", [])
    if oi_rank:
        lines.extend(["", "### 8h. 交易所持仓占比 [杠杆分布·实时]"])
        for ex_info in oi_rank[:5]:
            ex_name = ex_info.get("exchange", "")
            ex_oi = ex_info.get("oi_usd", 0)
            ex_chg_1h = ex_info.get("change_1h", 0)
            ex_chg_24h = ex_info.get("change_24h", 0)
            lines.append(f"  {ex_name}: ${ex_oi/1e9:.2f}B | 1h:{ex_chg_1h:+.1f}% 24h:{ex_chg_24h:+.1f}%")
        anomalies = [e for e in oi_rank[:5] if abs(e.get("change_1h", 0)) > 3]
        if anomalies:
            lines.append(f"  ⚠ {', '.join(e['exchange'] for e in anomalies)} 1h持仓异动 > 3%, 关注该所爆仓风险")

    lines.extend([
        "",
        "### 9. 宏观与情绪指标 [参考·日级]",
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
        lines.append(f"BTC ETF 3日净流: {_fmt_usd_for_prompt(etf_3d)} ({snapshot.get('etf_trend', '')})")
    etf_days = snapshot.get("etf_recent_days", [])
    if etf_days:
        day_strs = [f"{d.get('date', '?')}: {_fmt_usd_for_prompt(d.get('total_net', 0))}" for d in etf_days[:5]]
        lines.append(f"ETF 每日明细: {' | '.join(day_strs)}")
    max_pain = snapshot.get("btc_max_pain")
    if max_pain:
        lines.append(f"BTC 期权 Max Pain: ${max_pain:,.0f}")
    dvol = snapshot.get("btc_dvol")
    if dvol:
        lines.append(f"BTC DVOL(隐含波动率): {dvol:.1f}%")
    dom = snapshot.get("btc_dominance")
    if dom:
        lines.append(f"BTC Dominance: {dom:.1f}%")

    # ── 波动率与链上指标（Phase 5） ──
    hist_vol = snapshot.get("btc_hist_vol")
    imp_vol = snapshot.get("btc_implied_vol")
    iv_skew = snapshot.get("btc_iv_skew_1m")
    if hist_vol is not None or imp_vol is not None:
        lines.append("")
        lines.append("### 9b. 波动率结构")
        if hist_vol is not None:
            lines.append(f"BTC 历史波动率(年化): {hist_vol:.2%}")
        if imp_vol is not None:
            lines.append(f"BTC 隐含波动率(OKX期权): {imp_vol:.2%}")
        if hist_vol and imp_vol:
            spread = imp_vol - hist_vol
            label = "期权市场预期波动放大" if spread > 0.02 else ("期权市场预期平静" if spread < -0.02 else "隐含≈历史，无方向性偏差")
            lines.append(f"IV-HV 价差: {spread:+.2%} → {label}")
        if iv_skew is not None:
            skew_label = "看跌需求>看涨(偏恐慌)" if iv_skew < -0.01 else ("看涨需求>看跌(偏乐观)" if iv_skew > 0.01 else "中性")
            lines.append(f"1M IV Skew: {iv_skew:+.4f} → {skew_label}")
        pc_oi = snapshot.get("btc_put_call_oi")
        if pc_oi is not None:
            pc_label = "看跌保护需求高" if pc_oi > 0.7 else ("看涨情绪主导" if pc_oi < 0.4 else "多空均衡")
            lines.append(f"BTC Put/Call OI 比: {pc_oi:.3f} → {pc_label}")

    mvrv = snapshot.get("btc_mvrv")
    ahr = snapshot.get("ahr999")
    ex_btc = snapshot.get("exchange_btc_total")
    ex_chg = snapshot.get("exchange_btc_change_pct")
    sc_dom = snapshot.get("stablecoin_dominance")
    cb_prem = snapshot.get("coinbase_btc_premium")
    usdt_prem = snapshot.get("usdt_otc_premium")
    if any(v is not None for v in (mvrv, ahr, ex_btc, sc_dom)):
        lines.append("")
        lines.append("### 9c. 链上与资金面")
        if mvrv is not None:
            mvrv_label = "全网浮亏(底部区域)" if mvrv < 1 else ("估值中性" if mvrv < 2.5 else "估值过热")
            lines.append(f"MVRV 比率: {mvrv:.3f} → {mvrv_label}")
        if ahr is not None:
            ahr_label = "适合抄底" if ahr < 0.45 else ("适合定投" if ahr < 1.2 else "估值偏高")
            lines.append(f"ahr999 囤币指数: {ahr:.4f} → {ahr_label}")
        if ex_btc is not None:
            chg_str = f" ({ex_chg:+.2f}%)" if ex_chg is not None else ""
            lines.append(f"主要交易所 BTC 余额合计: {ex_btc:,.0f} BTC{chg_str}")
            if ex_chg is not None and ex_chg < -0.5:
                lines.append("  → 余额下降=BTC 被提走屯币，看涨信号")
            elif ex_chg is not None and ex_chg > 0.5:
                lines.append("  → 余额上升=BTC 充入准备卖出，看跌信号")
        if sc_dom is not None:
            lines.append(f"稳定币市占率: {sc_dom:.2f}% (高=干火药多/观望资金多)")
        if cb_prem is not None:
            lines.append(f"Coinbase BTC 溢价: {cb_prem:.4%} ({'正溢价=美国买盘活跃' if cb_prem > 0 else '负溢价=美国买盘弱'})")
        if usdt_prem is not None:
            lines.append(f"USDT 场外溢价: {usdt_prem:.3f} ({'>1=场外买盘活跃' if usdt_prem > 1 else '<1=场外卖盘'})")
        usdt_mcap = snapshot.get("usdt_market_cap")
        if usdt_mcap is not None:
            lines.append(f"USDT 市值: ${usdt_mcap / 1e9:.1f}B")
        hashrate = snapshot.get("btc_hashrate")
        if hashrate is not None:
            lines.append(f"BTC 全网算力: {hashrate:.1f} EH/s")

    us_10y = snapshot.get("us_10y_yield")
    fed_r = snapshot.get("fed_rate")
    if us_10y is not None or fed_r is not None:
        lines.append("")
        lines.append("### 9d. 利率环境")
        if us_10y is not None:
            lines.append(f"美国10年期国债收益率: {us_10y:.3f}%")
        if fed_r is not None:
            lines.append(f"美联储联邦基金利率: {fed_r:.2f}%")
        if us_10y is not None:
            if us_10y > 4.5:
                lines.append("  → 高利率压制风险资产，BTC 承压")
            elif us_10y < 3.5:
                lines.append("  → 低利率利好风险资产，BTC 受益")

    # ── §9e 链上周期画像 (CPS) ──
    cp = snapshot.get("cycle_position")
    has_cps = cp is not None and cp.get("cps") is not None
    if has_cps:
        lines.append("")
        lines.append("### 9e. 链上周期画像 [日级·BTC全局状态机]")
        lines.append(f"周期评分(CPS): {cp['cps']:.1f}/10 → {cp.get('cps_label', '')}")

        mvrv_z = cp.get("mvrv_z_score")
        if mvrv_z is not None:
            mvrv_l = "全网浮亏" if mvrv_z < 0 else ("估值中性偏低" if mvrv_z < 2 else ("估值中性" if mvrv_z < 4 else "估值过热"))
            lines.append(f"  MVRV Z-Score: {mvrv_z:.2f} → {mvrv_l} (贡献{cp.get('mvrv_z_contribution', 0):+.0f})")

        cp_ahr = cp.get("ahr999_value")
        if cp_ahr is not None:
            ahr_l = "适合抄底" if cp_ahr < 0.45 else ("适合定投" if cp_ahr < 1.2 else "估值偏高")
            lines.append(f"  Ahr999: {cp_ahr:.4f} → {ahr_l} (贡献{cp.get('ahr999_contribution', 0):+.0f})")

        sma = cp.get("sma_200w")
        sma_ratio = cp.get("price_vs_200w_ratio")
        if sma and sma_ratio:
            pct_200w = (sma_ratio - 1) * 100
            lines.append(f"  200周均线: ${sma:,.0f} → 当前价{'高出' if pct_200w >= 0 else '低于'}{abs(pct_200w):.1f}% (贡献{cp.get('price_vs_200w_contribution', 0):+.0f})")

        sth_l = cp.get("price_vs_sth_label", "")
        if sth_l:
            sth_parts = []
            for k, label in [("sth_cost_1d", "v1"), ("sth_cost_1w", "v2"), ("sth_cost_1m", "v3"), ("sth_cost_3m", "v4")]:
                v = cp.get(k)
                if v:
                    sth_parts.append(f"{label}=${v:,.0f}")
            lines.append(f"  STH成本: {' / '.join(sth_parts)} → {sth_l} (贡献{cp.get('price_vs_sth_contribution', 0):+.0f})")

        pi_350 = cp.get("pi_350dma")
        pi_111 = cp.get("pi_111dma_x2")
        pi_ratio = cp.get("pi_cycle_ratio")
        if pi_ratio is not None and pi_350 and pi_111:
            pi_l = "距顶部极远" if pi_ratio < 0.6 else ("趋近中性" if pi_ratio < 0.85 else "接近顶部交叉")
            lines.append(f"  Pi周期: 350DMA=${pi_350:,.0f} / 111DMA×2=${pi_111:,.0f} → 比值{pi_ratio:.3f}, {pi_l} (贡献{cp.get('pi_cycle_contribution', 0):+.0f})")

        rplr = cp.get("rplr_proxy")
        if rplr is not None:
            rplr_l = "短期持有者整体浮亏(底部区域信号)" if rplr < 0 else ("中性" if rplr < 0.2 else "获利丰厚(顶部压力)")
            lines.append(f"  RPLR代理: {rplr:+.4f} → {rplr_l}")

        rsi = cp.get("btc_rsi_daily")
        if rsi is not None:
            rsi_l = "超卖" if rsi < 30 else ("偏弱" if rsi < 45 else ("中性" if rsi < 55 else ("偏强" if rsi < 70 else "超买")))
            lines.append(f"  BTC日线RSI(14): {rsi:.1f} → {rsi_l}")

        onchain_levels = []
        for val, src, nature in [
            (cp.get("cvdd"), "CVDD", "极底支撑"),
            (cp.get("sma_200w"), "200周均线", "周期极强支撑"),
            (cp.get("sth_cost_1d"), "STH成本v1", "短期盈亏线"),
            (cp.get("sth_cost_1w"), "STH成本v2", "1周持有者成本"),
            (cp.get("sth_cost_1m"), "STH成本v3", "1-3月持有者成本"),
            (cp.get("sth_cost_3m"), "STH成本v4", "3-6月持有者成本"),
            (cp.get("pi_350dma"), "Pi 350DMA", "中期目标/阻力"),
        ]:
            if val and val > 0:
                dist = (val - price) / price * 100
                side = "支撑" if val < price else "阻力"
                onchain_levels.append((val, src, f"{side}({nature})", dist))

        if onchain_levels:
            onchain_levels.sort(key=lambda x: x[0])
            lines.append("\n链上关键价位（规则引擎已融合，阶梯策略参考）:")
            lines.append("| 价位 | 来源 | 性质 | 距当前 |")
            lines.append("|---|---|---|---|")
            for val, src, nature, dist in onchain_levels:
                lines.append(f"| ${val:,.0f} | {src} | {nature} | {dist:+.1f}% |")

    # ── §9f 箱体信号 V2 ──
    rs = snapshot.get("range_signal")
    has_range = rs is not None and (rs.get("range_upper") or rs.get("ma60_daily"))
    if has_range:
        lines.append("")
        lines.append("### 9f. 箱体信号 [核心·多维共振箱体+状态机+突破概率]")

        if rs.get("range_upper") and rs.get("range_lower"):
            lines.append(f"  核心箱体(MA骨架): ${rs['range_lower']:,.0f}({rs.get('range_lower_source','')}) — ${rs['range_upper']:,.0f}({rs.get('range_upper_source','')})")
            lines.append(f"  价格位置: {rs.get('price_position', 'middle')} ({rs.get('price_position_pct', 50):.0f}%)")
            lines.append(f"  箱体宽度: {rs.get('box_width_pct', 0):.1f}%")

        if rs.get("micro_upper") and rs.get("micro_lower"):
            lines.append(f"  微观区间: ${rs['micro_lower']:,.0f}({rs.get('micro_lower_tier','')}) — ${rs['micro_upper']:,.0f}({rs.get('micro_upper_tier','')}) 宽{rs.get('micro_width_pct', 0):.1f}%")

        box_state = rs.get("box_state", "none")
        state_map = {"none": "未形成", "forming": "形成中", "confirmed": "已确认",
                     "mature": "成熟", "squeeze": "挤压蓄力", "breaking_up": "向上突破中",
                     "breaking_down": "向下突破中", "broken": "已突破"}
        lines.append(f"  箱体状态: {state_map.get(box_state, box_state)} (质量{rs.get('box_quality', 0)}分)")
        if rs.get("box_age_hours", 0) > 0:
            lines.append(f"  存续时长: {rs['box_age_hours']:.0f}h")

        bp = rs.get("breakout_probability", 0)
        if bp > 0:
            bias = {"up": "偏向上破", "down": "偏向下破", "neutral": "方向不明"}.get(rs.get("breakout_direction_bias", ""), "")
            lines.append(f"  突破概率: {bp:.0%} {bias}")
            if rs.get("breakout_reason"):
                lines.append(f"    原因: {rs['breakout_reason']}")

        if rs.get("ma60_daily"):
            lines.append(f"  日线MA60: ${rs['ma60_daily']:,.0f}")
        if rs.get("ma120_daily"):
            lines.append(f"  日线MA120: ${rs['ma120_daily']:,.0f}")
        if rs.get("ma60_weekly"):
            lines.append(f"  周线MA60: ${rs['ma60_weekly']:,.0f}")

        macd_pos = "0轴上方(多头)" if rs.get("macd_daily_above_zero") else "0轴下方(空头)"
        hist_dir = ""
        if rs.get("macd_daily_hist_rising") is True:
            hist_dir = "，柱状图上升"
        elif rs.get("macd_daily_hist_rising") is False:
            hist_dir = "，柱状图下降"
        lines.append(f"  日线MACD: {macd_pos}{hist_dir}")

        if rs.get("unfilled_wick_low"):
            lines.append(f"  未回补下影线: ${rs['unfilled_wick_low']:,.0f} (价格磁吸目标)")
        if rs.get("unfilled_wick_high"):
            lines.append(f"  未回补上影线: ${rs['unfilled_wick_high']:,.0f} (价格磁吸目标)")

        if rs.get("signal_grade"):
            grade_map = {"S": "🔴", "A": "🟠", "B": "🟡", "C": "⚪"}
            emoji = grade_map.get(rs["signal_grade"], "⚪")
            lines.append(f"  {emoji} 信号: {rs['signal_grade']}级 {rs.get('signal_direction', '')} — {rs.get('signal_reason', '')}")
            confirms = []
            if rs.get("sweep_confirmed"):
                confirms.append("Sweep确认")
            if rs.get("cps_aligned"):
                confirms.append("CPS一致")
            if rs.get("bb_squeeze"):
                confirms.append("BB挤压")
            if rs.get("oi_buildup"):
                confirms.append("OI堆积")
            if rs.get("volume_declining"):
                confirms.append("量缩")
            if confirms:
                lines.append(f"  共振因子({rs.get('confluence_count', 0)}): {', '.join(confirms)}")
        else:
            lines.append(f"  信号: 无（价格在箱体中间/箱体未形成）")

    # ── §9g 关键位状态机 V2 ──
    kl = snapshot.get("key_levels")
    has_kl = kl is not None and len(kl.get("levels", [])) > 0
    if has_kl:
        lines.append("")
        lines.append("### 9g. 关键位状态机 [核心·V2·多维共振+生命周期追踪]")
        lines.append(f"活跃关键位: {kl.get('active_count', 0)}个")

        struct = kl.get("structure_summary")
        if struct:
            lines.append(f"结构摘要: {struct}")
        ds = kl.get("daily_strong_support")
        dr = kl.get("daily_strong_resistance")
        ws = kl.get("weekly_strong_support")
        wr = kl.get("weekly_strong_resistance")
        if ds or dr:
            lines.append(f"日线最强: 支撑{ds or '-'} / 阻力{dr or '-'}")
        if ws or wr:
            lines.append(f"周线最强: 支撑{ws or '-'} / 阻力{wr or '-'}")

        bb = kl.get("bull_bear_line")
        if bb:
            regime = {"bull": "偏多", "bear": "偏空", "neutral": "震荡"}.get(bb.get("current_regime", ""), "待定")
            lines.append(f"多空分界: {regime} — {bb.get('regime_reason', '')}")
            if bb.get("sma200d"):
                lines.append(f"  200日SMA: ${bb['sma200d']:,.0f}")

        bz = kl.get("breakout_zone")
        if bz and bz.get("bb_squeeze"):
            lines.append(f"突破蓄力: BB Squeeze {bz.get('squeeze_direction', '')} {bz.get('note', '')}")

        lines.append("")
        lines.append("| 价位 | 级别 | 类型 | 状态 | 距当前 | 共振分 | 来源数 | 测试 | 扫取量 | 级联风险 | 来源 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for lv in kl.get("levels", [])[:15]:
            side_cn = "支撑" if lv.get("side") == "support" else "阻力"
            state_cn = {
                "idle": "待观察", "approaching": "正接近",
                "testing": "正测试", "swept": "已扫取",
                "bounced": "已反弹", "broken": "已突破",
                "flipped": "已翻转",
            }.get(lv.get("state", ""), lv.get("state", ""))
            tier = lv.get("strength_tier", "C")
            score = lv.get("confluence_score", 0)
            src_cnt = lv.get("source_count", 0)
            cascade_str = f"{lv.get('cascade_risk', 0):.0%}" if lv.get("cascade_risk", 0) > 0 else "低"
            sweep_usd = lv.get("sweep_usd", 0)
            sweep_str = _fmt_usd_for_prompt(sweep_usd) if sweep_usd > 0 else "-"
            sources = ", ".join(lv.get("sources", [])[:3])
            lines.append(
                f"| ${lv.get('price', 0):,.0f} | {tier} | {side_cn} | {state_cn} | "
                f"{lv.get('distance_pct', 0):+.2f}% | {score:.0f} | {src_cnt} | "
                f"{lv.get('test_count', 0)} | {sweep_str} | {cascade_str} | {sources} |"
            )

        kl_signals = kl.get("signals", [])
        if kl_signals:
            lines.append("")
            lines.append("关键位信号:")
            for sig in kl_signals:
                action_cn = {
                    "snipe_long": "狙击做多", "snipe_short": "狙击做空",
                    "flip_long": "翻转做多", "flip_short": "翻转做空",
                    "wait_sweep": "等待扫取", "wait_approach": "等待接近",
                }.get(sig.get("action", ""), sig.get("action", ""))
                entry_str = f"入场${sig['entry_price']:,.0f}" if sig.get("entry_price") else ""
                sl_str = f"止损${sig['stop_loss']:,.0f}" if sig.get("stop_loss") else ""
                rr_str = f"R:R={sig['rr_ratio']:.1f}" if sig.get("rr_ratio") else ""
                parts = [p for p in [entry_str, sl_str, rr_str] if p]
                lines.append(
                    f"  {sig.get('confidence', 'C')}级 {action_cn} @${sig.get('level_price', 0):,.0f}: "
                    f"{sig.get('reason', '')} {'| ' + ' '.join(parts) if parts else ''}"
                )
                for w in sig.get("warnings", []):
                    lines.append(f"    ⚠ {w}")

    # ── §9g2 K 线形态检测 ──
    cp_name = snapshot.get("candlestick_pattern_name", "")
    cp_side = snapshot.get("candlestick_pattern_side", "")
    cp_strength = snapshot.get("candlestick_pattern_strength", 0)
    if cp_name:
        side_cn = "看涨" if cp_side == "support" else "看跌"
        lines.extend(["", "### 9g2. 最新4H K线形态"])
        lines.append(f"形态: {cp_name}（{side_cn}反转，强度 {cp_strength:.0%}）")
        lines.append("说明: 该形态为入场确认加分项。若与关键位 SWEPT/FLIPPED/BOUNCED 共振，信号可信度提升一档。")

    # ── §9h 净持仓 + 合约资金流 + TD序列 ──
    np_trend = snapshot.get("net_position_trend")
    np_latest = snapshot.get("net_position_latest")
    nf_1h = snapshot.get("futures_coin_netflow_1h")
    nf_trend = snapshot.get("futures_coin_netflow_trend")
    td_cnt = snapshot.get("td_sequential_count")
    td_dir = snapshot.get("td_sequential_direction")
    pf = snapshot.get("poll_failures", {})
    has_9h_data = any(v is not None and v != "" for v in (np_latest, np_trend, nf_trend, td_cnt))
    has_9h_failures = any(k in pf for k in ("net_position", "coin_netflow", "td_sequential"))
    if has_9h_data or has_9h_failures:
        lines.extend(["", "### 9h. 净持仓 + 合约资金流 + TD序列"])
        if np_latest is not None:
            lines.append(f"净持仓(v2): {_fmt_usd_for_prompt(np_latest)} ({np_trend or ''})")
        elif "net_position" in pf:
            lines.append(f"净持仓(v2): ⚠ 采集失败（{pf['net_position']}），本次分析不含此维度")
        if nf_1h is not None:
            nf_label = "资金净流入合约" if nf_1h > 0 else "资金净流出合约"
            lines.append(f"合约净资金流(1h): {_fmt_usd_for_prompt(nf_1h)} → {nf_label} ({nf_trend or ''})")
        elif "coin_netflow" in pf:
            lines.append(f"合约净资金流(1h): ⚠ 采集失败（{pf['coin_netflow']}），本次分析不含此维度")
        if td_cnt is not None and td_dir:
            td_label = f"TD{td_dir}计数={td_cnt}"
            if td_cnt >= 9:
                td_label += " ⚠反转信号"
            elif td_cnt >= 7:
                td_label += " 接近反转"
            lines.append(f"TD序列: {td_label}")
        elif "td_sequential" in pf:
            lines.append(f"TD序列: ⚠ 采集失败（{pf['td_sequential']}），本次分析不含此维度")

    has_trad = any(snapshot.get(k) for k in ("dxy", "nasdaq", "sp500", "gold"))
    has_crypto_sent = fgi is not None or dom is not None
    lines.extend([
        "",
        "【宏观数据覆盖说明】（请严格按此表述，避免与上文矛盾）",
        f"- 加密侧情绪/结构: {'已提供（恐惧贪婪/市占等）' if has_crypto_sent else '未提供'}",
        f"- 传统外盘(DXY/纳指/标普/黄金): {'已提供部分或全部数值' if has_trad else '本条目中未解析到有效数值（若恐惧贪婪已提供，不得写宏观完全缺失）'}",
        "- 链上周期(CPS): " + (f"已提供(CPS={cp['cps']:.1f})" if has_cps else "未提供"),
        f"- 均线箱体: " + (f"已提供(信号={rs.get('signal_grade', '无')}级)" if has_range else "未提供"),
        f"- 关键位状态机: " + (f"已提供(活跃{kl.get('active_count', 0)}个)" if has_kl else "未提供"),
        f"- 净持仓/资金流/TD序列: " + ("已提供" if any(snapshot.get(k) for k in ("net_position_trend", "futures_coin_netflow_trend", "td_sequential_count")) else "未提供"),
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
        for s in rule_supports[:5]:
            lines.append(f"  - ${s.get('price', 0):,.1f} [{','.join(s.get('sources', []))}]")

    rule_resistances = snapshot.get("rule_resistances", [])
    if rule_resistances:
        lines.append("阻力位(规则引擎):")
        for r in rule_resistances[:5]:
            lines.append(f"  - ${r.get('price', 0):,.1f} [{','.join(r.get('sources', []))}]")

    rule_sl = snapshot.get("rule_stop_loss", [])
    if rule_sl:
        lines.append("止损建议(规则引擎):")
        for sl in rule_sl:
            lines.append(f"  - {sl.get('direction','')}: ${sl.get('zone_from', 0):,.1f}-${sl.get('zone_to', 0):,.1f} "
                         f"[{', '.join(sl.get('reasons', []))}]")

    # ── §11 统一引擎交易方案（三档合一）──
    sniper = snapshot.get("sniper_entries", [])
    ladder_plans = snapshot.get("ladder_plans", [])
    lines.append("")
    lines.append("### 11. 引擎交易方案（必须在「四、交易计划」中按三档展开）")

    lines.append("")
    lines.append("**11a. 短线档方案（引擎狙击，距当前价 1-8%）**")
    if sniper:
        for i, se in enumerate(sniper):
            d = se.get("direction", "")
            dist_pct = abs(se.get("entry_price", 0) - price) / price * 100 if price > 0 else 0
            lines.append(f"方案{i+1} [{d}] 距{dist_pct:.1f}%: "
                         f"入场${se.get('entry_price', 0):,.1f} "
                         f"止损${se.get('stop_loss', 0):,.1f} "
                         f"TP1=${se.get('take_profit_1', 0):,.1f}(R:R {se.get('rr_ratio_1', 0):.1f}) "
                         f"TP2=${se.get('take_profit_2', 0):,.1f}(R:R {se.get('rr_ratio_2', 0):.1f})")
            for logic_line in se.get("logic", []):
                lines.append(f"    - {logic_line}")
    else:
        lines.append("（当前无引擎狙击方案：可能因清算簇距离/ATR/数据不足。AI 可基于数据自主构建，标注⚡AI推断。）")

    lines.append("")
    lines.append("**11b. 中远线档方案（引擎阶梯，距当前价 5-20%）**")
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
        lines.append("（当前无引擎阶梯方案：可能因远距无足够清算簇/数据不足。AI 可基于数据自主构建，标注⚡AI推断。）")

    # ── §11c 前瞻观察：高共振 idle 关键位 ──
    if has_kl:
        idle_sa = [lv for lv in kl.get("levels", [])
                   if lv.get("state") == "idle"
                   and lv.get("strength_tier") in ("S", "A")
                   and abs(lv.get("distance_pct", 0)) <= 15]
        if idle_sa:
            lines.append("")
            lines.append("**11c. 前瞻观察位（高共振 idle 关键位，AI 可自主构建方案）**")
            for lv in idle_sa:
                side_cn = "支撑" if lv.get("side") == "support" else "阻力"
                lines.append(
                    f"  - {lv.get('strength_tier', 'C')}级{side_cn} ${lv.get('price', 0):,.0f} "
                    f"距{lv.get('distance_pct', 0):+.1f}% 共振{lv.get('confluence_score', 0):.0f}分 "
                    f"来源: {', '.join(lv.get('sources', [])[:3])}"
                )

    lines.append("")
    lines.append("请基于以上数据输出，**必须包含八个章节**（一~八），第四节「交易计划」按三档结构输出（短线/中线/远线），第八节「数据质量与自检」对输入数据做诊断。")
    cps_note = " 5) §9e有数据时，§一须引用CPS周期位置，§四中远线档须评估CPS与方向一致性" if has_cps else ""
    range_note = " 6) §9f有数据时，§一须引用箱体位置，§二须纳入MA关键价位，有A级信号时§四须评估共振" if has_range else ""
    kl_note = " 7) §9g有信号时，§四须优先评估关键位SWEPT/FLIPPED信号与引擎方案的共振，高cascade_risk须警告" if has_kl else ""
    lines.append("重点：1) 每个方案止损须含防猎杀说明 2) 宏观-微观一致 3) §四与引擎 R:R 口径对齐（≥1:{:.1f}） 4) 引擎未覆盖的档位AI可自主构建标注⚡AI推断{}{}{}".format(min_rr, cps_note, range_note, kl_note))
    return "\n".join(lines)
