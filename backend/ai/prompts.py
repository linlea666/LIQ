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

### 流动性视角解读规则（Smart Money 框架）
- **术语映射**：clusters_above（上方空头清算簇）= Buy-Side Liquidity（BSL，上方流动性）；clusters_below（下方多头清算簇）= Sell-Side Liquidity（SSL，下方流动性）
- **核心机制**：清算触发的是强制市价单——空头清算=强制买入（推高价格），多头清算=强制卖出（压低价格）。清算簇=流动性池=大资金建仓/平仓的对手盘来源
- **扫取与反转逻辑**：价格扫取一侧流动性后，该侧推动力耗尽，关注另一侧是否成为下一个目标——但这不是必然规律，须与 CVD/OI/费率等实时信号共振验证
- **Sweep 检测信号**：§1 中若标注"近1h流动性扫取"，表示价格已穿越并消耗了该区域的清算簇；被扫侧动能减弱，反方向概率上升但需二次确认
- **级联踩踏风险**：当多个清算簇在狭窄价格区间（<2%）内连续排列时，价格穿越第一个簇触发的强制平仓可能推动价格到达第二个簇，产生连锁反应（cascade liquidation）——此场景下止损须设在最外层簇之外
- **流动性叙事输出要求**：§一/§二中须使用"上方/下方流动性"概念描述清算分布，而非仅使用"清算簇"术语；须在§一中明确说明流动性偏向（如"上方流动性$XXM远多于下方$YYM，价格倾向先上扫"）

### CPS × 清算权重联动
- CPS 4-7（fair/discount 区间）= 震荡概率高 → 清算磁吸效应权重**上调**，价格倾向在上下流动性池之间往返扫取
- CPS < 2 或 > 8（极端区间）= 趋势概率高 → 清算磁吸效应权重**下调**，级联踩踏风险**上调**，价格可能连续穿透多个簇不回头
- 震荡市中：清算簇构成有效短期支撑/阻力；趋势市中：清算簇更多充当"加油站"（被穿越后加速趋势）

### 均线箱体信号（§9f 有数据时启用）
- **核心逻辑**：日线 MA120（≈2日线MA60）构成箱体上沿阻力，未回补影线低点/周线MA60 构成箱体下沿支撑；价格在箱体中间时无信号，仅在接近边界时产生A/B级信号
- **A级做空**：价格接近箱体上沿 + 日线 MACD 在0轴下方 → §四狙击空单优先采纳此信号；0轴上方时降级为B级
- **A级做多**：价格接近箱体下沿 + 下方流动性被扫取（sweep确认）→ §四狙击多单优先采纳此信号；无sweep时降级为B级
- **MACD 0轴规则**：MACD 在0轴下方 = 反弹空间有限（均线压制），反弹至MA做空成功率高；MACD 在0轴上方 = 多头趋势，做空需额外谨慎
- **中间禁区**：price_position = "middle" 时，§一须提示"当前价处于箱体中间，无明确方向信号"，§四/§六不得基于箱体理由开单
- **与CPS联动**：CPS 4-7（震荡区）时箱体策略权重上调；CPS极端值时箱体可能被突破，须降权
- **引用规则**：§一须引用箱体位置（上沿/下沿/中间）；§二须将MA60/MA120纳入关键价位表；有A级信号时§四须评估是否与引擎方案共振

### 关键位状态机（§9g 有数据时启用）
- **核心逻辑**：关键位（支撑/阻力）经历完整生命周期：IDLE → APPROACHING → TESTING → SWEPT/BOUNCED → BROKEN → FLIPPED。不同状态对应不同策略
- **SWEPT 状态（A级信号）**：流动性已被扫取 → "空头/多头弹药耗尽"逻辑，这是最高置信做反向的入场时机。§四狙击方案须优先采纳 SWEPT 状态的信号
- **FLIPPED 状态（A级信号）**：支撑被跌破后价格回踩该位被拒（原支撑变阻力）→ 经典 S/R 翻转做空；反之亦然。§四须评估翻转信号
- **BOUNCED 状态（B级信号）**：关键位测试后反弹确认 → 常规支撑/阻力反弹策略
- **TESTING/APPROACHING 状态**：等待确认，§一须提示"价格正在测试/接近关键位"
- **级联风险(cascade_risk)**：>0.7 = 突破后可能瀑布式穿透多层清算簇，止损须设在最外层之外；§四/§五方案如涉及高 cascade_risk 关键位，须附加瀑布风险警告
- **与箱体联动**：range_box 上下沿若也是关键位，状态机信号权重上调（多源共振）
- **与CPS联动**：CPS 4-7（震荡区）时关键位反弹策略权重上调；CPS 极端值时关键位突破策略权重上调
- **§四关键位优先**：若关键位状态机输出 SWEPT/FLIPPED 信号 且 与引擎狙击方案方向一致，该方案置信度提升至A级

### 宏观-微观联动（仅当用户提示中该项有数值时引用；无则写「数据未提供」勿编造）
- DXY 单日波动较大 → 风险资产承压/支撑需结合当日数据
- 纳斯达克/标普走弱 → risk-off，谨慎追高
- 黄金与 BTC 背离 → 留意避险资金轮动（需有数据）
- 恐惧贪婪极值 + 资金费率极端 → 过热/过冷，与清算磁吸结合评估
- **恐惧贪婪指数使用规范**：熊市/震荡市中该指数长期低位属基线状态，单独不构成方向判断依据——必须与多空比、订单簿深度、成交量/CVD、资金费率等实盘指标交叉验证后方可引用；单独引用时标注"参考权重低"
- **周期评分(CPS)使用规范**：CPS 由 MVRV Z、Ahr999、200周均线比、STH成本、Pi周期 5 个链上日级维度综合评分(0~10)。CPS ≥ 6 做多阶梯信心增强(远层可标准配仓)；CPS ≤ 2 做多阶梯须降权，做空阶梯优先。CPS 为日线级别指标，不可单独用于实时方向判断——必须与清算地图/CVD/OI/费率等实时维度交叉验证
- **链上价位引用规则**：§9e 中距当前价 ≤15% 的链上价位须纳入 §二(关键价位)表格；§五(阶梯)远距层入场点须与链上价位交叉验证（入场价附近有链上支撑/阻力则增信，无则降权）
- **RPLR代理解读**：RPLR<0 表示短期持有者整体浮亏，历史上是中期底部前兆；RPLR>0.5 利润获取旺盛，回调风险升高；单独不构成方向判断，须与 CPS 及实时数据结合

### 深度推理要求（分析 > 翻译，铁律）
- **禁止纯转述**：每个章节至少 1 处跨维度推理——将 ≥2 个数据源组合得出新结论，而非逐条复述数字
- **矛盾识别**：当数据维度给出相反信号时（如恐惧指数极低但多空比偏多），必须明确指出矛盾、分析哪个维度更可信并说明理由（考虑：熊市恐惧贪婪长期低位是基线，多空比/CVD/订单簿是实盘资金行为，后者权重更高）
- **矛盾升级与置信度标注**：当 CVD/OI 等实时资金流与§四/§五方案的预设方向矛盾时，该方案须标注**置信度等级**（高确信/中确信/低确信）。低确信方案须附加至少1个具体确认条件（如"需1H收盘站上$X""需OI同步放量>Y%"），不可仅写"需确认"一句带过
- **新增数据强制引用**：§一 或 §七 中须至少引用 9b（波动率结构）1 项数值 + 9c（链上资金面）1 项数值 + 9e（链上周期）CPS 评分；9d 有数据时，§一 须用一句话说明利率环境对风险资产的影响方向（如"高利率压制风险偏好"或"利率下行利好 BTC"），不可忽略；9e 有数据时，§一须说明当前周期位置对方向偏向的影响，§五须评估 CPS 与阶梯策略方向的一致性
- **规则引擎审查**：§四 对每个狙击方案至少质疑 1 个维度（止损距离是否合理 / 入场是否卡在整数关口 / TP 与链上指标一致性 / 清算簇厚度是否足够支撑逻辑）
- **场景偏向**：§八 末尾须基于当前数据组合指明最偏向的场景（"当前数据偏向场景X"，无需概率数字）

### 狙击挂单（高 R:R）原则
- 规则引擎预算的 R:R 已按 ≥ **1:{min_rr:.1f}** 过滤；你必须在**第四节**完整展开，不得仅写「审核通过」或省略
- 若引擎无方案：说明原因（如清算簇过远/数据不足），**不得**编造价位
- 每个方向**最多 2 套**挂单叙述；每套须含：**挂单价区间或代表价、止损、止盈1/止盈2、R:R（至少给到 TP1 对应 R:R）**
- **失效条件**：至少写 1 条（例：价格有效跌破/突破某清算簇外沿则计划作废；或 1H 收盘越过某关键位则失效）——以**级别+条件**表述即可
- **R:R 验算铁律**：每套方案必须写出计算过程：做多 R:R = (TP1 - 入场) / (入场 - SL)；做空 R:R = (入场 - TP1) / (SL - 入场)。代入具体价格数字后算出结果，禁止仅写"≈"估值
- **止损方向铁律（违反即废弃）**：做空止损必须严格高于入场价（SL > Entry）；做多止损必须严格低于入场价（SL < Entry）。若调整引擎止损后违反此约束，该方案无效，必须按正确方向重新设置止损（参考§三止损安全区）
- **R:R 分母校验**：验算时若分母（SL - 入场）为负数或零，说明止损方向错误，方案必须废弃并重新计算——禁止以"简化表达""绝对值""实际R:R极高"等方式绕过

### 阶梯埋伏计划（Scaled-In Limit Order Strategy）原则
- 基于**当前实时价格**动态生成，非固定底部区间（如价格从 7万→6.8万，阶梯会跟随下移）
- **多空双向同时输出**：做多=向下分层接多单；做空=向上分层接空单
- 在当前价向下/向上 **5%-20%** 范围内的清算密集区底部/顶部分层挂限价单
- 每层独立止损（止损在清算真空区内或按百分比保底），互不影响
- 越深层仓位越大（倒金字塔）：越远的层如果命中，R:R 越高
- 核心数学期望：全部被扫损总亏 N%，任一层命中可赚 M 倍（M >> N）
- **必须评估**：清算瀑布连锁风险、极端行情止损滑点、保证金占用效率
- 与狙击挂单**严格分工**：狙击=近距精准(≤5%)单层猎杀，阶梯=远距(≥5%)多层网捕，两者覆盖区间不重叠
- **7天清算地图交叉验证**：§1b 提供 7 天维度清算簇，阶梯远距入场点须与 §1b 簇位置对照——若引擎入场价附近 §1b 无对应簇，应降低该层可信度或建议跳过
- **CPS方向冲突警告**：当CPS≥6但输出做空阶梯，或CPS≤2但输出做多阶梯时，该阶梯标题须加"⚠CPS方向冲突"前缀醒目标注，单层风险建议上限从5%降至2%，并明确说明该阶梯仅作对冲用途

### 输出格式（严格按以下 Markdown 章节标题输出，便于系统解析）

## 一、市场格局总览
（3-5句：宏观风向→杠杆水平→资金流→情绪→格局定性）

## 二、关键价位图谱
| 类型 | 价位区间 | 依据(≥2维+时效) |
（支撑、阻力、价值中枢、清算磁吸位）

## 三、止损安全区建议
**做多方向：** 区间(小值-大值) + 防猎杀原理 + 失效情形
**做空方向：** 区间(小值-大值) + 防猎杀原理 + 失效情形

## 四、狙击挂单计划（高 R:R 埋伏单）
**本节为必答。** 须基于用户提示「### 11. 规则引擎狙击方案」逐条处理：
- **多单埋伏**（若有）：挂单价/止损/止盈1/止盈2/R:R + 逻辑（为何是捡尸位）+ 失效条件 + 若被止损的大致损失（单位：价格距离×1单位）
- **空单埋伏**（若有）：同上
- 若引擎方案需调整：写明**调整后的完整数值**与理由；拒绝时说明拒绝原因
- **止损须引用§三安全区**：每个方案的止损必须验证落在§三给出的安全区区间内；若调整后偏离安全区，须说明理由并确认仍满足止损方向铁律
- **禁止**输出 R:R 低于 1:{min_rr:.1f} 的「优质」挂单（除非明确标注为观察/不执行）

## 五、阶梯埋伏计划（基于当前价的多空双向多层网）
**本节为必答。** 须基于用户提示「### 12. 规则引擎阶梯埋伏方案」逐条处理：
- **做多阶梯**（若有）和**做空阶梯**（若有）须分别展开
- 逐层展开：**层级/挂单价/止损/止盈/R:R(须验算)/仓位权重/风险占比**
- **R:R 验算**：每层须写出计算过程（公式同§四），禁止直接抄引擎数字不验算
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
场景A：...（触发条件 + 目标位 + 时间窗口）
场景B：...
场景C：...
**当前数据偏向：** 场景X（必须选定唯一场景，禁止"A或B"式骑墙；一句话理由）

### 格式铁律（确保系统解析成功，违反将导致前端无法展示）
- 场景推演每条必须以 `场景A：` / `场景B：` 开头，**禁止**用 `**场景A**` 加粗前缀
- 所有价格区间必须"小值在前-大值在后"（如 $68,740 - $69,221），禁止反写
- Markdown 表格分隔行用 `|---|---|---|`，禁止 `| :--- |` 格式
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
        if c.get("price_from", 0) <= price <= c.get("price_to", 0):
            lines.append(f"    ⚠ 当前价${price:,.1f}已在此簇范围内 — 清算正在发生，基于此簇的策略前提需重新评估")

    lines.append("\n下方清算密集区(多头清算):")
    for c in snapshot.get("liq_clusters_below", []):
        lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                     f"${c.get('total_usd', 0) / 1e6:.0f}M ({c.get('dominant_leverage', '')}x) "
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
        lines.append(f"  上方流动性(BSL): ${bsl_24h / 1e6:.0f}M (空头清算=强制买入 → 扫取后为做空提供对手盘)")
        lines.append(f"  下方流动性(SSL): ${ssl_24h / 1e6:.0f}M (多头清算=强制卖出 → 扫取后为做多提供对手盘)")
        if ssl_24h > 0 and bsl_24h > ssl_24h * 1.5:
            lines.append(f"  偏向: 上方流动性远多于下方({bsl_24h/ssl_24h:.1f}x) → 价格倾向先上扫BSL再反转")
        elif bsl_24h > 0 and ssl_24h > bsl_24h * 1.5:
            lines.append(f"  偏向: 下方流动性远多于上方({ssl_24h/bsl_24h:.1f}x) → 价格倾向先下扫SSL再反转")
        elif ssl_24h == 0:
            lines.append(f"  偏向: 仅上方有流动性(${bsl_24h/1e6:.0f}M) → 上方为唯一磁吸目标")
        elif bsl_24h == 0:
            lines.append(f"  偏向: 仅下方有流动性(${ssl_24h/1e6:.0f}M) → 下方为唯一磁吸目标")
        else:
            lines.append(f"  偏向: 上下流动性相对均衡 → 双向扫取概率接近，关注CVD/OI确认方向")

    sweep_above = snapshot.get("liq_sweep_above_usd_1h", 0)
    sweep_below = snapshot.get("liq_sweep_below_usd_1h", 0)
    if sweep_above > 0 or sweep_below > 0:
        lines.append(f"\n近1h流动性扫取检测:")
        if sweep_above > 0:
            lines.append(f"  上方已扫取: ${sweep_above / 1e6:.1f}M BSL — 上方流动性被消耗，上行推动力减弱")
        if sweep_below > 0:
            lines.append(f"  下方已扫取: ${sweep_below / 1e6:.1f}M SSL — 下方流动性被消耗，下行推动力减弱")
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
                             f"${c.get('total_usd', 0) / 1e6:.0f}M ({c.get('dominant_leverage', '')}x) "
                             f"距当前{c.get('distance_pct', 0):.1f}%")
        if clusters_below_7d:
            lines.append("\n7天下方清算密集区(多头清算):")
            for c in clusters_below_7d:
                lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                             f"${c.get('total_usd', 0) / 1e6:.0f}M ({c.get('dominant_leverage', '')}x) "
                             f"距当前{c.get('distance_pct', 0):.1f}%")
        if vacuums_7d:
            lines.append("\n7天清算真空区:")
            for v in vacuums_7d:
                lines.append(f"  - ${v.get('price_from', 0):,.0f}-${v.get('price_to', 0):,.0f} {v.get('note', '')}")
        bsl_7d = sum(c.get("total_usd", 0) for c in clusters_above_7d)
        ssl_7d = sum(c.get("total_usd", 0) for c in clusters_below_7d)
        if bsl_7d > 0 or ssl_7d > 0:
            lines.append(f"\n7天流动性视角:")
            lines.append(f"  上方流动性(BSL): ${bsl_7d / 1e6:.0f}M / 下方流动性(SSL): ${ssl_7d / 1e6:.0f}M")

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
                             f"${c.get('total_usd', 0) / 1e6:.0f}M ({c.get('dominant_leverage', '')}x) "
                             f"距当前{c.get('distance_pct', 0):.1f}%")
        if clusters_below_30d:
            lines.append("\n30天下方清算密集区:")
            for c in clusters_below_30d[:8]:
                lines.append(f"  - ${c.get('price_from', 0):,.0f}-${c.get('price_to', 0):,.0f}: "
                             f"${c.get('total_usd', 0) / 1e6:.0f}M ({c.get('dominant_leverage', '')}x) "
                             f"距当前{c.get('distance_pct', 0):.1f}%")

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
    gl1h_long = snapshot.get("global_liq_long_1h", 0)
    gl1h_short = snapshot.get("global_liq_short_1h", 0)
    if gl1h_long > 0 or gl1h_short > 0:
        lines.append(f"全网1h多头爆仓: ${gl1h_long / 1e6:.1f}M / 空头: ${gl1h_short / 1e6:.1f}M")
    g_long = snapshot.get("global_liq_long_24h", 0)
    g_short = snapshot.get("global_liq_short_24h", 0)
    if g_long > 0 or g_short > 0:
        lines.append(f"全网24h多头爆仓: ${g_long / 1e6:.0f}M")
        lines.append(f"全网24h空头爆仓: ${g_short / 1e6:.0f}M")
        ratio_24h = snapshot.get("global_liq_ratio_24h", 1.0)
        lines.append(f"全网多空爆仓比: {ratio_24h:.1f}")
    largest = snapshot.get("global_liq_largest_single", 0)
    if largest > 0:
        lines.append(f"最大单笔爆仓: ${largest / 1e6:.1f}M")

    lines.extend([
        "",
        "### 8. 成交分布与波动率 [1H K线]",
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
            lines.append(f"看涨OI: ${opt_call / 1e6:.0f}M / 看跌OI: ${opt_put / 1e6:.0f}M | P/C比: {pc_ratio:.3f}")
            if opt_mp > 0 and price > 0:
                dist = (opt_mp - price) / price * 100
                lines.append(f"当前价距Max Pain: {dist:+.1f}% (价格倾向向Max Pain靠拢)")

    lo_buy = snapshot.get("large_orders_buy_count", 0)
    lo_sell = snapshot.get("large_orders_sell_count", 0)
    lo_net = snapshot.get("large_orders_net_usd", 0)
    if lo_buy > 0 or lo_sell > 0:
        lines.extend(["", "### 8d. 大单追踪 [实时]"])
        lines.append(f"大单买入: {lo_buy}笔 / 卖出: {lo_sell}笔 | 净方向: ${lo_net / 1e6:+.1f}M")
        if lo_net > 0:
            lines.append(f"  大资金偏向: 买入为主(净流入)")
        elif lo_net < 0:
            lines.append(f"  大资金偏向: 卖出为主(净流出)")

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
    etf_days = snapshot.get("etf_recent_days", [])
    if etf_days:
        day_strs = [f"{d.get('date', '?')}: ${d.get('total_net', 0) / 1e6:+.0f}M" for d in etf_days[:5]]
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

    # ── §9f 均线箱体信号 ──
    rs = snapshot.get("range_signal")
    has_range = rs is not None and rs.get("ma60_daily") is not None
    if has_range:
        lines.append("")
        lines.append("### 9f. 均线箱体信号 [日级·多时间框架MA+MACD]")
        if rs.get("ma60_daily"):
            lines.append(f"  日线MA60: ${rs['ma60_daily']:,.0f}")
        if rs.get("ma120_daily"):
            lines.append(f"  日线MA120(≈2日MA60): ${rs['ma120_daily']:,.0f}")
        if rs.get("ma60_weekly"):
            lines.append(f"  周线MA60: ${rs['ma60_weekly']:,.0f}")

        macd_pos = "0轴上方(多头)" if rs.get("macd_daily_above_zero") else "0轴下方(空头)"
        hist_dir = ""
        if rs.get("macd_daily_hist_rising") is True:
            hist_dir = "，柱状图上升"
        elif rs.get("macd_daily_hist_rising") is False:
            hist_dir = "，柱状图下降"
        lines.append(f"  日线MACD: {macd_pos}{hist_dir}")

        if rs.get("range_upper") and rs.get("range_lower"):
            lines.append(f"  箱体范围: ${rs['range_lower']:,.0f}({rs.get('range_lower_source','')}) — ${rs['range_upper']:,.0f}({rs.get('range_upper_source','')})")
            lines.append(f"  价格位置: {rs.get('price_position', 'middle')} ({rs.get('price_position_pct', 50):.0f}%)")
        elif rs.get("range_upper"):
            lines.append(f"  箱体上沿: ${rs['range_upper']:,.0f}({rs.get('range_upper_source','')}), 下沿未确定")
        elif rs.get("range_lower"):
            lines.append(f"  箱体下沿: ${rs['range_lower']:,.0f}({rs.get('range_lower_source','')}), 上沿未确定")

        if rs.get("unfilled_wick_low"):
            lines.append(f"  未回补下影线: ${rs['unfilled_wick_low']:,.0f} (价格磁吸目标)")
        if rs.get("unfilled_wick_high"):
            lines.append(f"  未回补上影线: ${rs['unfilled_wick_high']:,.0f} (价格磁吸目标)")

        if rs.get("signal_grade"):
            grade_emoji = "🔴" if rs["signal_grade"] == "A" else "🟡"
            lines.append(f"  {grade_emoji} 信号: {rs['signal_grade']}级 {rs.get('signal_direction', '')} — {rs.get('signal_reason', '')}")
            if rs.get("sweep_confirmed"):
                lines.append(f"  ✅ Sweep确认: 流动性扫取与信号方向一致")
            if rs.get("cps_aligned"):
                lines.append(f"  ✅ CPS一致: 周期评分支持当前信号方向")
        else:
            lines.append(f"  信号: 无（价格在箱体中间，不适合基于箱体逻辑开单）")

    # ── §9g 关键位状态机 ──
    kl = snapshot.get("key_levels")
    has_kl = kl is not None and len(kl.get("levels", [])) > 0
    if has_kl:
        lines.append("")
        lines.append("### 9g. 关键位状态机 [实时·生命周期追踪]")
        lines.append(f"活跃关键位: {kl.get('active_count', 0)}个")
        lines.append("")
        lines.append("| 价位 | 类型 | 状态 | 距当前 | 测试次数 | 扫取量 | 级联风险 | 来源 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for lv in kl.get("levels", [])[:8]:
            side_cn = "支撑" if lv.get("side") == "support" else "阻力"
            state_cn = {
                "idle": "待观察", "approaching": "正接近",
                "testing": "正测试", "swept": "已扫取",
                "bounced": "已反弹", "broken": "已突破",
                "flipped": "已翻转",
            }.get(lv.get("state", ""), lv.get("state", ""))
            cascade_str = f"{lv.get('cascade_risk', 0):.0%}" if lv.get("cascade_risk", 0) > 0 else "低"
            sweep_str = f"${lv.get('sweep_usd', 0)/1e6:.1f}M" if lv.get("sweep_usd", 0) > 0 else "-"
            sources = ", ".join(lv.get("sources", [])[:3])
            lines.append(
                f"| ${lv.get('price', 0):,.0f} | {side_cn} | {state_cn} | "
                f"{lv.get('distance_pct', 0):+.2f}% | {lv.get('test_count', 0)} | "
                f"{sweep_str} | {cascade_str} | {sources} |"
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
    cps_note = " 5) §9e有数据时，§一须引用CPS周期位置，§五须评估CPS与阶梯方向一致性" if has_cps else ""
    range_note = " 6) §9f有数据时，§一须引用箱体位置，§二须纳入MA关键价位，有A级信号时§四须评估共振" if has_range else ""
    kl_note = " 7) §9g有信号时，§四须优先评估关键位SWEPT/FLIPPED信号与引擎方案的共振，高cascade_risk须警告" if has_kl else ""
    lines.append("重点：1) 止损防猎杀 2) 宏观-微观一致 3) 第四节与引擎 R:R 口径对齐（≥1:{:.1f}） 4) 第五节评估阶梯计划的瀑布风险和资金效率{}{}{}".format(min_rr, cps_note, range_note, kl_note))
    return "\n".join(lines)
