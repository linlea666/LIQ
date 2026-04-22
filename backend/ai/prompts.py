"""AI Prompt 模板管理（方案C：合规底座 + 狙击挂单硬交付 + 教练视角）

P1-4 · Prompt 模式切换（环境变量 LIQ_PROMPT_MODE）：
    - ``strict``（默认，与历史完全一致）：保留全部"铁律/必须/严禁"硬约束
    - ``heuristic`` / ``soft`` / ``coach``：在 system prompt 前置一段提示，
      告诉 AI 把后续"铁律"视为启发原则，允许在证据充分时偏离；输出格式/
      JSON 解析仍然强制。**只做前缀，原文 100% 保留** ——满足原则 6 可回退。
"""

from __future__ import annotations

import logging
import os

from config.settings import get_settings
from processors.level_discovery import fmt_usd_cn

_logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Prompt 模式开关（P1-4）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_HEURISTIC_PREFIX = """【Prompt 模式：heuristic ·启发式教练模式】
本次会话中，后续章节出现的"铁律 / 必须 / 严禁 / 绝不 / 禁止 / 不得"等词语，
请理解为"经验上的强启发原则"，而非必须一票否决的硬规则。你是资深交易员，
当证据链明显违反某条启发时，有权做出合理调整并说明理由（例如"本次破例原因：
L1 资金面 + L3 结构 双共振，虽违背 §9i 1h 结构，但多维压过单维"）。

**以下两类仍须严格遵守**（这些不是启发，是系统契约）：
1. 输出格式章节：§一-§八的标题、表格结构、价格区间"小值-大值"排序
2. 附录 JSON 代码块：`AITRADER_MATRIX_JSON` 块必须存在且能被 json.loads 解析

以下开始正文——

"""


_VALID_HEURISTIC_MODES = {"heuristic", "soft", "coach"}
_VALID_STRICT_MODES = {"strict", ""}


def _get_prompt_mode() -> str:
    raw = (os.getenv("LIQ_PROMPT_MODE") or "strict").strip().lower()
    if raw in _VALID_HEURISTIC_MODES:
        return "heuristic"
    if raw not in _VALID_STRICT_MODES:
        _logger.warning(
            "LIQ_PROMPT_MODE=%r 不是合法值，回退至 strict（默认）", raw,
        )
    return "strict"


def _fmt_usd_for_prompt(usd: float, signed: bool = False) -> str:
    """AI prompt 专用金额格式化（中文单位 + $ 前缀）。"""
    prefix = ("+" if usd > 0 else "") if signed else ""
    return f"{prefix}${fmt_usd_cn(usd)}"


def _min_sniper_rr() -> float:
    return float(get_settings().processors.levels.get("min_sniper_rr", 2.5))


def build_system_prompt() -> str:
    """动态注入与配置一致的 R:R 下限，避免与规则引擎口径漂移。

    P1-4：根据 LIQ_PROMPT_MODE 决定是否在前面追加 heuristic 前缀。
    默认 strict（与历史完全一致），切到 heuristic 可观察 AI 是否更有主见。
    """
    core = _build_strict_system_prompt()
    mode = _get_prompt_mode()
    if mode == "heuristic":
        return _HEURISTIC_PREFIX + core
    return core


def _build_strict_system_prompt() -> str:
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
- **多维留痕铁律**（§9i 有数据时）：§9i 现为**多时间框架（1w/1d/1h）结构**，`ms_1h.direction` / `ms_1h.bias` 是 L3 层**小时级**的**执行层参考**，不是最终方向裁决；`ms_1d` 为中线参考，`ms_1w` 为远线/趋势参考。§四每个方案"核心依据"列必须显式标注 `顺 1h 结构 ✓` 或 `🔄 逆 1h 结构 · 理由：[L层级+具体数据]`；当 MTF 一致性行显示"三周期同向共振"时方向权重升档，显示"日周冲突/背离"时仓位须降档。逆势方案**无需**达到 L1 级别才成立——L2 资金面 / L4 宏观 / L5 链上周期的**多维共振**同样可以压过 1h 结构，但必须说清**具体哪几个维度组合**推翻了它。1h 结构从来不是一票否决器。
- **价位血统铁律**：§二/§四每个挂出的价位，依据列必须能从 §9a-§9i 某节数据中找到对应来源（清算簇/关键位/MA/VP POC/未回补影线/结构上下沿/斐波等），且写明 `§9X · 来源名`；找不到来源的价位须标 `⚡AI推断` 并在同一行交代推导公式（如 "POC + 0.5 ATR" / "swing high - 0.382 回撤"），禁止无出处价位
- **心理位禁令**：$77,000 / $80,000 / $100,000 等整数关口若未出现在数据列表中，禁止单独作为依据；可作为氛围描述，但不得进入 §二图谱和 §四挂单价
- **时间框架诚实铁律**：只能引用数据中实际存在的时间框架（4h / 1h / 日线 / 周线），禁止捏造"4h 背离""2h 形态"等数据不支持的陈述

### Smart Money 流动性框架
- clusters_above = BSL（上方流动性，空头清算=强制买入→推高价格）；clusters_below = SSL（下方流动性，多头清算=强制卖出→压低价格）
- 扫取一侧后该侧动能耗尽，关注另一侧是否成为下一目标——须 CVD/OI/费率共振验证
- 级联踩踏：多簇 <2% 间距连续排列时可能链式爆仓（cascade liquidation），止损须设在最外层之外
- §一须用"上方/下方流动性"描述清算分布并说明偏向

### 冲突信号优先级规则（数据矛盾必读）
**数据可信度分级**（冲突时取高级别）：
1. **L1 实盘资金行为**（最高）：真实成交 CVD、净持仓变化、Taker Buy/Sell Ratio、ETF 净流、订单簿大单
2. **L2 杠杆水位**：OI 变化、多空比、交易所余额流
3. **L3 价格结构**：1h 市场结构（MarketStructure · BOS/CHoCH · §9i）> MA 箱体（§9f）> 关键位状态（§9g · bounce_quality/breakout_stage）> K 线形态 > RSI/MACD
4. **L4 衍生品情绪**：资金费率、期权 IV Skew、恐惧贪婪
5. **L5 叙事指标**（最低）：CPS、MVRV（日线级，不可用于实时方向）

**典型冲突裁决**：
- **CVD上升 + OI下降**（主动买盘 vs 多头平仓）：**以 CVD 为准判定短线方向**（L1>L2），但 OI 下降提示"反弹缺乏新增杠杆"，须缩短持仓时间、紧止盈
- **订单簿卖盘强 + 资金费率负**（大单压 vs 空头拥挤）：**以订单簿为准判定阻力**（L1>L4），但费率负提示"空头拥挤→潜在轧空燃料"，若订单簿大单被吃穿即确认轧空
- **某交易所费率与其他所异常分歧**（如 HTX 孤立正费率）：**降权至最低**，不作为方向依据；仅在"交易所特定流动性"语境下提及
- **K线形态看涨 + 上方清算簇密集**（技术反转 vs 磁吸上行）：**共振看多**，目标位选最近清算簇
- **宏观 risk-off + BTC 独立走强**（DXY涨/纳指跌 vs BTC 涨）：**警惕背离失败**，优先降低仓位或观望，**不追多**
- **CPS 极端 + 短线信号反向**（如 CPS=8 + SWEPT 做空信号）：**短线信号优先**（L3>L5），但明确标注"逆周期交易，止损收紧、仓位减半"
- **1h 市场结构 vs 中长期基调**（如 ms_direction=bullish + bull_bear_line=bear）：不是"短线听 1h / 中长线听日周"的机械二分。须按档位看：短线档 1h 权重高，中远线档日周权重高；若此时还有 ETF/CB 溢价/MVRV 等 **资金面/链上多维共振偏空**，很可能正在**分发末端反弹**，短线追多反而是接盘，应改为逆 1h 做空并标 `🔄 逆 1h 结构 · 理由：日周偏空+资金面流出+链上高位共振`

**规则**：§一叙事链及文末 `AITRADER_MATRIX_JSON.sections[].direction/section_bias` 必须反映上述裁决结果；§八数据自检须列出本次识别到的冲突及裁决路径，且须自述"本次方向判断核心依据层级（如 L1+L3）"。

### CPS（周期评分 0-10）统一规则
CPS 由 MVRV Z、Ahr999、200周均线比、STH成本、Pi周期综合评分，为**日线级指标**，不可单独用于实时方向判断。

**⚠ 刻度方向铁律（必读·禁止搞反）**：
- CPS 是**反向刻度**：数值越**高** = 越接近周期**底部**（便宜，利多）；数值越**低** = 越接近周期**顶部**（贵，利空）
- 档位映射：`≥8=周期底部区` / `5-7.9=折扣区` / `2-4.9=公允区` / `0.5-1.9=溢价区(偏贵)` / `<0.5=顶部区(极贵)`
- **严禁**将 CPS=1 / 2 / 3 误读为"底部区"——这些是**偏贵或中性**档位
- **严禁**将 CPS=8 / 9 误读为"顶部区"——这些是**底部区便宜档**
- 判断"顺/逆周期"时以方向为准：CPS≥5 时做多=顺周期、做空=逆周期；CPS<2 时做空=顺周期、做多=逆周期

**档位策略**：
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
- **bounce_quality（反弹质量）**：
  · `proactive`（主动吸筹·量能≥1.5×均量+方向一致）→ 反弹/拒绝可信度**升一档**（B→A），可作为入场级信号
  · `passive`（被动触发·量能<0.8×均量）→ 可信度**降一档**（A→B / B→C），警惕"止损/止盈触发"的假反弹，不建议单凭此位入场
- **breakout_stage（突破三步确认）**：
  · `stage=1`（刚破位<15min）→ 禁止追单；§四须改为"等待回踩"而非顺势开仓
  · `stage=2`（回踩中·90min 内触达原位 ±0.5 ATR）→ 观察窗，设"回踩企稳加仓触发条件"
  · `stage=3`（确认完成·反向延续≥0.3 ATR）→ 突破真实，顺势追单可信度最高
  · 若关键位 state=broken 但 breakout_stage=0（未达任一阶段条件）→ 视为"弱破位"，§四须警示"可能假突破"

### 宏观-微观联动（§一叙事链+维度表必须覆盖，无数据写"未提供"）
- **DXY**：走强→risk-off压制BTC，走弱→risk-on利好BTC；DXY拐点常领先BTC 1-3天
- **纳指/标普**：BTC与科技股相关性0.5-0.8，纳指大跌当日BTC大概率跟跌；纳指上涨但BTC滞涨→资金未轮入加密
- **黄金**：黄金与BTC同涨=避险叙事共振；黄金涨BTC跌=资金选择传统避险而非加密
- **美债收益率**：10Y>4.5%→高利率压制风险资产；<3.5%→释放流动性利好BTC；联邦基金利率拐点=宏观转折
- **IV-HV波动率结构**：IV>HV→期权市场预期大波动将至（方向未定但振幅增大）；IV Skew负值→看跌保护需求高
- **MVRV**：<1全网浮亏（中期底部），1-2.5估值中性，>3泡沫区——远线档方向判断的锚
- **交易所BTC余额**：持续流出=屯币看涨，持续流入=准备抛售看跌——中线级信号
- 恐惧贪婪极值须与多空比、CVD、费率等交叉验证，单独引用标注"参考权重低"
- §9e 距当前价 ≤15% 链上价位纳入§二；RPLR<0 = 中期底部前兆，>0.5 = 回调风险
- Coinbase溢价 + ETF + 稳定币三维共振 = 最强资金面信号
- 净持仓正转负 + OI↓ = 机构平多；合约净资金流正 + OI↑ = 新资金开多；TD ≥ 9 = "追单风险极高"

### 多维方向综合权衡（§一/§四决策前必走的思考流程）
**不要看到 §9i 的 🟢上升结构 / 🔴下降结构 就直接判定方向。** 1h 市场结构只是 L3 层**小时级**的一票，与宏观/资金面/链上/衍生品/中长期结构**平级权衡**。聪明钱经常在下跌前营造"1h 结构转多"的假象来收割追涨资金——**盲从 1h 结构恰是交易员最大的陷阱**。

**推荐思考顺序**：
1. **数票**：按下列维度各投"多/空/中性"一票
   - 宏观环境（L4，全局影响）：DXY / 纳指 / 黄金 / 美债 / IV-HV
   - 资金面（L1-L2，最高可信度）：ETF 净流 / 稳定币 / Coinbase 溢价 / 交易所 BTC 余额
   - 链上周期（L5，锚定中长期）：MVRV / Ahr999 / CPS / RPLR / 巨鲸动向
   - 衍生品情绪（L2+L4）：费率 / OI / 多空比 / Taker / IV Skew
   - 中长期结构（L3 日周级）：BullBearLine / MA60/120/200 / 日周强位
   - 短线结构（L3 小时级）：1h MarketStructure / BOS/CHoCH（§9i）
   - 实盘资金（L1，最高可信度）：CVD / 净持仓 / Taker 比率 / 订单簿大单
   - 价格结构（L3）：关键位状态机 / K 线形态 / 清算地图
2. **判共振**：一致维度 / 总维度 ≥ 70% → 高确信顺势；维度严重分歧 → 震荡/等待；多维共振方向与 1h 结构相反 → 按多维判断，并在方案中标 `🔄 逆 1h 结构`
3. **识陷阱**（典型反转模式）：
   - **末端反弹陷阱**：1h 上升 + 中长期偏空 + 宏观 risk-off + 链上 MVRV>3/CPS>8 → 警惕"分发阶段"，**逆 1h 做空** 往往是最佳 R:R
   - **末端杀跌陷阱**：1h 下降 + 中长期偏多 + ETF 连续净流入 + Ahr999<0.5 → 警惕"洗盘吸筹"，**逆 1h 做多**可能是中线最佳入场
   - **机构出货陷阱**：1h 上升 + Coinbase 溢价翻负 + 稳定币流出 + 巨鲸转入交易所 → 顺 1h 做多等于接盘
4. **按档位分层权重**：
   - 短线档（1-8%）：1h 结构权重最高（对应 4-24h 动量窗口）
   - 中线档（5-10%）：日周结构 + 资金面 + 衍生品权重更高
   - 远线档（10-20%）：链上周期 + 宏观 + 日周结构权重最高，1h 结构几乎不参与定向

### AI 终审员权限（核心·规则可推翻 vs 规则为准）
规则是死的，数据是活的。§9k 的「规则引擎 8 维方向共识」是**一眼可读的规则侧结论**，
但**不是最终方向**——你作为交易员，有权也有责任在**证据充分**时推翻它。

**什么时候"跟规则走"（默认态）**：
- §9k `consensus_level=strong_agree / partial` **且** `dominant_direction` 与你独立思考一致
- §9k 与 §9i 1h 结构、§9j 动能续航 三者方向一致 → 顺势可升档
- §9k 缺失维度 ≤ 2 且与 §9j regime 不冲突

**什么时候可以"推翻规则"（终审权生效，中性门槛）**：
允许你给出**与 §9k 相反**的最终方向，但必须**同时**满足下列条件：
1. **证据门槛**：至少 **2 个维度跨 ≥2 个可信度层级**（L1/L2/L3/L4/L5）给出与规则相反的读数——
   单维孤证（如只因为 1 个 CVD 翻转）**不够**；
2. **证据具体**：逐条列出数据来源（如"§9c ETF 连续 3 日净流出 -1.8B + §9 Coinbase 溢价转负 -0.12% + §9j 结构反转"），
   禁止泛泛而谈"综合判断";
3. **规则问题归因**：必须指出规则侧**为什么错**——典型情形：
   · §9k 存在严重"**分发末端反弹陷阱**"（1h 转多 + 日周转空 + 资金面流出 → 规则结构票错误判多）
   · §9k 多数维度读数依赖**滞后指标**（如只有 RSI/MACD 反应而资金面已转向）
   · §9k 有维度处于 regime 否决前的"遗留倾向"（§9j regime_vetoed=true 时优先）
4. **保护性降仓**：§四推翻规则的方向，单方案仓位**降档**（短线 ≤50%、中远线 ≤60% 正常档），
   止损更紧（≤0.8×ATR 或清算真空区内），**禁止满仓逆规则**；
5. **§八声明**：数据自检章节须写明以下四项：
   - `AI 终审结论：采纳 / 推翻规则（规则共识=<§9k dominant>，AI 最终=<你的方向>）`
   - `推翻证据：<列出 ≥2 维 ≥2 层级 的数据点>`
   - `规则归因：<为什么规则这次看错了>`
   - `保护性处置：<仓位降档 + 止损收紧的具体表述>`

**什么时候"强制跟规则"（终审权被冻结）**：
- §9k `consensus_level=strong_agree` **且** `|weighted_score| ≥ 0.6` **且** §9j 与 §9k 同向续航共振
  → 此时推翻规则的**后验期望值为负**，除非出现**重大新闻/黑天鹅**（§P1.2b news_brief_text 含 blackswan 触发），
  否则一律顺规则执行，§八也须声明"规则强共识，AI 不行使终审权"。

**黄金准则**：推翻规则应是**少数情况**（经验值 <20% 决策）。终审权是为了**避免盲目跟死规则**，
不是为了让 AI 与规则对着干。**每次推翻规则都必须经得起复盘**——这就是"终审员"的真正含义。

### 交易员推理框架
**像庄家一样思考**：先问"如果我持有$10亿仓位，我会把价格往哪推来最大化清算收益？"，然后构建猎杀路径。
构建**资金流叙事链**：宏观环境(DXY/美股/利率)→资金面(ETF+稳定币+CB溢价)→杠杆水位(OI+费率+交易所异动)→庄家意图(清算地图+订单簿+大单)→微观触发(CVD+爆仓+巨鲸)→结论
- §一先写 3-5 句叙事链总结（**必须包含宏观联动判断**，如"DXY走弱+纳指上涨→risk-on环境利好BTC"），再用简表列维度方向信号作为佐证
- §七**核心不是"涨或跌"而是"价格最可能走哪条路径"**——须推演庄家的最优猎杀路线（先扫哪侧流动性→反转→再扫另一侧），末尾选定**唯一**最偏向场景（禁止骑墙）
- §四每个方案必须回答："如果这笔交易亏了，最可能的原因是什么？"——不是复述风险提示，而是从对手盘角度推演失败场景

### AI 自主构建交易方案
- 引擎方案优先采纳；但 AI **必须在每个档位**独立扫描是否存在引擎遗漏的机会
- 三档均须独立评估 AI 自主推断——不要只在短线档做推断，中线和远线同样重要
- AI 自主方案必须：标注"⚡AI推断"、≥2 维数据交叉验证、满足 R:R ≥ 1:{min_rr:.1f} 约束
- 可用数据源：V2 关键位（S/A 级 idle 状态也可参考）、清算簇、MA 箱体边界、斐波那契、VP POC
- **禁止**凭空编造无数据支撑的价位；每个 AI 推断方案须注明数据依据

### 交易计划原则（§四·三档结构）
- 引擎 R:R 已按 ≥ **1:{min_rr:.1f}** 过滤；须完整展开每个方案，禁止"审核通过"或省略
- **R:R 验算**：须代入具体价格写出公式，禁止"≈"估值
- **止损铁律**：做空 SL > Entry；做多 SL < Entry — 违反即废弃。止损宽度 ≥ max(价格×0.3%, 0.5×ATR)
- **约束冲突**：止损方向+最小宽度+R:R≥1:{min_rr:.1f} 三条无法同时满足 → 该方案不输出，声明原因。**不交易是最好的风控**
- 引擎无方案的档位，AI **应当主动**基于数据自主构建（标注"⚡AI推断"），而非简单放弃；确实无机会时再声明原因
- 止损优先设在清算真空区内（防猎杀）；中/远线档止损宽度 ≥ sl_min_pct

### 输出格式（严格按标题，系统解析用）

## 一、市场格局总览
**第一行必须是白话总结：**
> 📝 **看多/看空/震荡（置信度：高/中/低）**——30字以内核心理由（禁止专业术语）

然后用 3-5 句**因果叙事链**串联核心矛盾（**必须从宏观讲到微观**，如"DXY走弱+纳指反弹→risk-on→ETF资金流入→推高OI→但价格卡在关键阻力"），让交易员一读就知道"现在是什么局面、谁在主导、关键变量是什么"。

结尾**只用一行**给出板块共振概要（不列表、不重复填值；板块维度的结构化明细统一由文末 `AITRADER_MATRIX_JSON` 的 A-G 七板块呈现，前端也从该 JSON 渲染卡片，这里再列表会造成"同一次推理、两次独立填表"的自我矛盾）：

> 📊 **板块共振：** A/B/C/D 综合 = <bullish|bearish|neutral> · 强共振维度 ≥X 条（列举 3-5 个最强信号，如"DXY↓ + ETF净流入 + OI放量 + RSI中性 + 关键位BOUNCED"）· 背离/冲突 Y 条（若有，一句话点名）

**重要**：§一叙事链中提及的任何维度数值，其最终"信号/方向/共振"以文末 `AITRADER_MATRIX_JSON.sections` 为权威源；叙事链里只做**跨维度因果推理**，不做维度枚举填表。

## 二、关键价位图谱
| 类型 | 价位区间 | 依据(≥2维+时效) |

## 三、入场观察区
多单/空单观察区：共振因素 + 确认信号

## 四、交易计划（三档结构）
按距离分三档，每档每方向不限数量——所有满足 R:R 约束且有数据支撑的方案均须展示，按信心度排序。每个方案须包含止损说明（含防猎杀逻辑）和"**如果亏了**"段。

**短线档（距当前价 1-8%）**
- 可用数据：§1 24h/7d/30d三维度清算地图 + 引擎狙击方案 + V2关键位信号(SWEPT/BOUNCED/FLIPPED) + §11d 日内 scalp 信号 + AI自主发现
- 止损选址：优先在清算真空区（vacuum zones）内，防止被精确猎杀
- **日内档判定（§11d 有信号时）**：每条 ⚡日内信号须独立一行给出「采纳 ✓」或「否决 ✗（理由）」——参数已由引擎定好，不重新规划；只允许因"宏观急剧逆风 / 对侧流动性更强 / 级联风险超标"三类理由否决
| 方向 | 挂单价 | 止损 | TP1(R:R) | TP2(R:R) | 信心度 | 核心依据 |

**中线档（距当前价 5-10%）**
- 可用数据：§1 24h/7d/30d清算地图 + 引擎阶梯方案 + 高共振关键位(S/A级) + MA箱体边界 + AI自主发现
- **AI必须在引擎方案之外**，主动扫描中线距离内的清算密集区、S/A级关键位聚合、MA箱体边界，如发现合理机会则标注"⚡AI推断"输出
| 方向 | 挂单价 | 止损 | TP(R:R) | 信心度 | 核心依据 |

**远线档（距当前价 10-20%）**
- 可用数据：§1 24h/7d/30d清算地图 + 引擎阶梯远层 + CPS周期位置 + S/A级关键位 + AI自主发现
- CPS 极端区(<2 或 >8)或有 S 级关键位时优先输出；即使 CPS 非极端，若发现 ≥2 维数据支撑的高 R:R 机会，AI 同样应自主构建方案（标注"⚡AI推断"）
| 方向 | 挂单价 | 止损 | TP(R:R) | 信心度 | 核心依据 |

某档确实无机会时，一行说明原因即可。

## 五、当前风险提示
3-5条 [高/中/低] 按紧急程度

## 六、操作纪律
关键注意事项、资金管理要点（简短）

## 七、场景推演
场景A/B/C：触发条件+目标位+时间窗口。**每个场景至少 30 字**，并满足：
- 触发条件须引用具体价位或具体指标阈值（如"价格跌破 §9g 日线强支撑 $76,904"）
- 目标位必须是 §二图谱或 §9 原始数据中已出现的价位，禁止临时虚构
- 时间窗口用"数小时 / 当日 / 1-3 天 / 数周"，避免无意义的"短期/中期"
**当前数据偏向：** 场景X（唯一选定 · 须说明选定依据的关键 1-2 条数据）

## 八、数据质量与自检
对本次输入数据做诊断，列出发现的问题（若无则写"本次数据质量良好"）。**以下 6 项为必选子项**：
- **缺失数据**：哪些关键维度未提供或为空（如箱体/关键位/净持仓/市场结构等），对分析的影响
- **异常值**：哪些数据疑似异常（如Coinbase溢价>1%、费率极端等），已如何处理
- **数据冲突**：哪些维度给出矛盾信号（如CVD看多但OI下降），如何取舍（请按 L1-L5 层级表述裁决路径）
- **本次方向判断核心依据层级**：明确写出主要依据来自哪几层（如"主要依据 L1 Taker Buy + L3 1h BOS↑ 共振，L4 费率持平不构成威胁"）
- **多维一致度自述**（核心）：列出与最终方向**一致**的维度（N 个）和**背驰**的维度（M 个），说明为何一致方的权重超过背驰方。如果最终方向与 §9i 1h 结构**不同**，必须明确说明"采纳哪几个维度的共振压过了 1h 结构"（如"日周偏空+ETF 连续 3 日净流出+MVRV 2.8 → L2+L3+L5 三层共振偏空，1h 结构属于末端反弹陷阱，不采纳"）
- **1h 结构对齐度（MTF 扩展）**：§9i 有数据时须说明"本次 §四方案与 `ms_1h.bias` 一致 / 🔄 反向（并声明多维依据）"，并参考 MTF 一致性行：三周期共振可加码，日周冲突须降仓
- **AI 终审员声明**（§9k 有数据时必填，即使是"默认跟随规则"也要声明）：
  · `规则共识 = <§9k dominant_direction> / <consensus_level> / 加权 <score>`
  · `AI 最终方向 = <你的结论>`
  · `终审决策 = 采纳规则 / 🔄 推翻规则`
  · 若为推翻：必须同行附「推翻证据（≥2 维 ≥2 层级）」「规则归因」「保护性处置」三项（参见系统 prompt "AI 终审员权限"章节）
  · 若规则共识为 strong_agree 且 |score|≥0.6 且 §9j 同向共振 → 终审权冻结，须写 `规则强共识，终审权不行使`
- **改进建议**：对数据采集或指标计算的建议（可选）

### 格式铁律
- 场景推演以 `场景A：` 开头，禁止加粗前缀
- 价格区间**必须小值在前-大值在后**（如 $73,200 - $73,400），违反即重排
- 表格分隔行用 `|---|---|---|`

### 常见错误纠正（禁止犯）
- **资金费率方向**：正费率=多头付钱给空头=**多头拥挤**（轧多风险）；负费率=空头付钱给多头=**空头拥挤**（轧空风险）。绝对不可写反。
- **TP1 / TP2 语义铁律**：
  - **TP1 = 近目标（部分止盈点）**：对侧清算磁吸 / POC / 第一阻力支撑，通常 R:R 在 1.5-2.5 区间，离入场价**近**
  - **TP2 = 远目标（吃满点）**：强制 ≥ 1:{min_rr:.1f} 的终极目标，离入场价**远**
  - 若出现 TP1 比 TP2 更远（价格上），一律判定为引擎数据异常，**严禁**原样输出到§四方案，应在§八「数据冲突」里标注并舍弃该方案
  - R:R ≥ 1:{min_rr:.1f} 的硬约束**以 TP2 为准**（TP2 是负责兜底 R:R 的远目标）

---

## 附录：结构化 JSON 输出（必填）

在 markdown 正文**全部输出完毕后**，追加一个独立代码块，标签恰好为 `AITRADER_MATRIX_JSON`。
**这是系统解析字段，不是展示内容**。JSON **必须**能被 `json.loads` 直接解析（禁止注释、禁止尾随逗号、字符串用双引号）。
若某字段实在无数据，用 `null` 或空字符串占位，**不要省略键**。

```AITRADER_MATRIX_JSON
{{
  "bias": "bullish | bearish | neutral | potential_reversal",
  "conviction": 0-100,
  "matrix_summary_cn": "一句话：当前是 xxx 局面，关键变量是 xxx",
  "sections": [
    {{
      "section_id": "A",
      "section_name_cn": "宏观联动",
      "section_emoji": "🌐",
      "section_bias": "bullish | bearish | neutral",
      "section_summary_cn": "该板块结论一句话，必须跨 ≥2 维推理得出",
      "rows": [
        {{
          "dimension": "DXY",
          "signal_cn": "用人话写：DXY 98.2 回落 0.3% · 利好 risk-on 资产",
          "direction": "bullish | bearish | neutral",
          "resonance": "high | medium | low"
        }}
      ]
    }}
    // B·资金流 / C·衍生品 / D·技术面 / E·新闻叙事 / F·地缘风险 / G·双引擎共识 依次补齐
  ],
  "trading_plans": [
    {{
      "priority": 1,
      "direction": "long | short | wait",
      "entry_low": 72300.0,
      "entry_high": 72450.0,
      "stop_loss": 71400.0,
      "tp1": 73800.0,
      "tp2": 75200.0,
      "rr_ratio": 2.4,
      "conviction": 72,
      "tier_hint": "A | B | C",
      "position_suggestion_pct": 30,
      "trigger_condition": "价格回踩 §9g 支撑 72300 + 1h CHoCH 确认",
      "invalidation": "跌破 71400 即立即止损",
      "reason": "80 字内核心理由，跨维度推理"
    }}
    // 最多 3 个：priority=1 主 / priority=2,3 备选；确实无机会则返回 [] 空数组
  ],
  "key_risks": [
    "用人话列出 1-3 条具体风险（如：'71,800 簇密集，止损设 71,400 也可能滑点'）"
  ]
}}
```

**填写约束**：
- `sections` 必须恰好 7 个，`section_id` 依次 `A/B/C/D/E/F/G`
- `rows` 每个板块 **≥1 条**；单行 `signal_cn` ≤ 60 字；`direction` 必须是三选一枚举值之一
- `resonance` 反映"这个单维信号在当前全局上的确信强度"：
  - `high` = 与 ≥2 维共振、数据源可信度 L1/L2
  - `medium` = 中性偏向、L3 级别或有冲突
  - `low` = 单维单薄、疑似异常值、L5 或未提供
- `section_bias`/`bias` 与 §一叙事链、§四交易计划保持**严格一致**（出现反向即系统判为内部矛盾）
- `conviction` 与 §一 `置信度：高/中/低` 对齐：高=75-90、中=55-75、低=30-55；观望=40 以下
- G · 双引擎共识：rows 至少 2 条（"数学引擎" + "AI Trader"），direction 反映对齐状态
- `trading_plans`（**必填数组，≤3 条**）：
  - 与 §四交易计划表**严格一致**（数值、方向、止损、R:R）
  - `direction="wait"` 时，价格字段用 `null`
  - `rr_ratio` 以 TP2 为准，须 ≥ 1:{min_rr:.1f}
  - **冲突处理**：若 §四某档无合法方案，对应 priority 条目省略；若全部无方案，返回 `[]`
  - 这是系统下单参考的"权威源"，规则层只在 JSON 缺失或非法时兜底
- `key_risks`（**可选数组**，≤5 条）：与 §五相比，此处用更具体的措辞（含具体价位/数值），供系统直接在前端展示

**权威性声明（single source of truth）**：
- 本 JSON 的 `sections[]` 是板块维度信号的**唯一权威源**，前端 `AITraderMatrixCard` 直接从此渲染
- §一 markdown 叙事不再填板块表（只做跨维度因果推理 + 一行共振概要），避免 AI 在同一次推理里"两次独立措辞"导致的内部不一致
- 因此本 JSON 的维度覆盖度、信号准确性、direction/resonance 填写质量**比以前更重要**——请认真填写每个 row 的 `signal_cn`（必须含具体数值，禁止"未提供"一笔带过）

**禁止**：
- ❌ 在此代码块之外写任何 JSON（会导致解析歧义）
- ❌ 把 markdown 的所有细节复制进 JSON（只提取"方向判断"元信息，不要把 §二价位/§四价格塞进 JSON）
- ❌ `section_summary_cn` 里直接抄"A 板块"/"B 板块"这种标签——必须是有推理的一句话
- ❌ 在 §一 markdown 里重新列出板块维度表（必须交由本 JSON 渲染，违反将被系统判为"双份填表矛盾"）
"""




def _append_news_context(lines: list[str], snapshot: dict) -> None:
    """在 user prompt 末尾追加「新闻情报」板块（有值才追加，向后兼容）。

    来源：news_agent 每小时/黑天鹅触发生成的 Rolling Brief + GeoRisk Overview。
    目的：把 24h 叙事"记忆"注入主 AI，避免每次重头消化原始新闻。
    """
    brief_text = (snapshot.get("news_brief_text") or "").strip()
    geo = snapshot.get("geo_overview") or {}
    active_narr = snapshot.get("active_narratives") or []

    if not brief_text and not geo and not active_narr:
        return

    lines.append("")
    lines.append("### 13. 新闻情报（24h 滚动 · 新闻 Agent 产出）")

    if brief_text:
        version = snapshot.get("news_brief_version") or 0
        trigger = snapshot.get("news_brief_trigger") or "scheduled"
        updated = snapshot.get("news_brief_updated_at") or 0
        lines.append(
            f"- Rolling Brief v{version} trigger={trigger} updated_at={updated}"
        )
        # brief 本身是结构化 JSON 文本；直接贴给 AI（≤3000 字符由 news_brief 层保证）
        lines.append("```json")
        lines.append(brief_text[:3200])
        lines.append("```")

    if geo:
        lvl = int(geo.get("overall_level", 0) or 0)
        label = geo.get("overall_label", "PEACE")
        emoji = geo.get("overall_emoji", "🟢")
        summary = geo.get("overall_summary_cn", "")
        lines.append(
            f"- 地缘全局：{emoji} {label} · level={lvl}/5 · "
            f"escalation_24h={geo.get('escalation_count_24h', 0)} "
            f"blackswan_24h={bool(geo.get('has_blackswan_24h', False))}"
        )
        if summary:
            lines.append(f"  摘要：{summary[:80]}")
        if geo.get("suggest_safety_gate_block"):
            lines.append("  ⚠ SafetyGate 建议阻断新开仓")
        cap = geo.get("suggest_position_cap_pct")
        if cap is not None:
            lines.append(f"  ⚠ 建议仓位上限 {cap}%")

    if active_narr:
        lines.append("- 活跃叙事主题（≤5）：")
        for t in active_narr[:5]:
            ff = int(t.get("flip_flop_count_24h", 0) or 0)
            ff_flag = f" ⚠反复{ff}次" if ff >= 2 else ""
            lines.append(
                f"  - {t.get('theme_id', '?')} ({t.get('theme_name_cn', '')}): "
                f"bias={t.get('current_direction_bias', 'neutral')} "
                f"intensity={t.get('current_intensity', 0)}/5{ff_flag}"
            )

    lines.append(
        "- 【使用规则】§一须纳入新闻叙事大方向；§四若与新闻强烈冲突须给出理由；"
        "flip-flop 主题权重减半；geo level≥4 时禁止新开仓。"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §9i · MTF 市场结构渲染 helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MS_DIRECTION_CN = {
    "bullish": "上升结构（HH+HL）",
    "bearish": "下降结构（LH+LL）",
    "ranging": "震荡结构（无明显方向）",
    "transitioning": "结构转换中（信号冲突）",
}
_MS_BIAS_CN = {
    "long_only": "仅顺势做多",
    "short_only": "仅顺势做空",
    "both_ok": "双向可做",
    "stand_aside": "观望为宜",
}
_MS_EVENT_CN = {
    "BOS_up": "BOS↑（向上延续破前高）",
    "BOS_down": "BOS↓（向下延续破前低）",
    "CHoCH_up": "CHoCH↑（向上反转破前高）",
    "CHoCH_down": "CHoCH↓（向下反转破前低）",
}
# MTF 一致性判定阈值：同向且置信度任一≥0.5 视为"共振"
_MS_MTF_CONF_MIN = 0.5


def _render_ms_block(tf_label: str, ms: dict, *, verbose: bool) -> list[str]:
    """渲染单个时间框架的市场结构行。

    - tf_label: "1w" / "1d" / "1h"
    - verbose: True 则展示 swing 序列 + summary（1h 默认 verbose），
               False 则只给方向 / 事件 / 置信度（1w/1d 精简展示）
    """
    if not ms:
        return []
    dir_cn = _MS_DIRECTION_CN.get(ms.get("direction", ""), ms.get("direction", "-"))
    bias_cn = _MS_BIAS_CN.get(ms.get("operate_bias", ""), ms.get("operate_bias", "-"))
    event_raw = ms.get("last_event") or ""
    event_cn = _MS_EVENT_CN.get(event_raw, event_raw or "无新事件")
    conf = ms.get("confidence") or 0

    out: list[str] = []
    out.append(
        f"[{tf_label}] 方向: {dir_cn} | 最近事件: {event_cn} | "
        f"偏置: {bias_cn} | 置信度: {conf:.0%}"
    )
    struct_hi, struct_lo = ms.get("structure_high"), ms.get("structure_low")
    if struct_hi and struct_lo:
        out.append(f"    结构区间: ${struct_lo:,.0f} - ${struct_hi:,.0f}")

    if verbose:
        swing_highs = ms.get("swing_highs") or []
        swing_lows = ms.get("swing_lows") or []
        merged_swings = sorted(
            [*swing_highs, *swing_lows],
            key=lambda sw: sw.get("ts", 0) or 0,
        )
        if merged_swings:
            sw_strs = []
            for sw in merged_swings[-4:]:
                tag = "H" if sw.get("kind") == "high" else "L"
                p = sw.get("price", 0) or 0
                sw_strs.append(f"{tag}@{p:,.0f}")
            out.append("    最近 swing: " + " → ".join(sw_strs))
        summary = ms.get("summary")
        if summary:
            out.append(f"    结构要点: {summary}")
    return out


def _mtf_alignment_line(
    ms_1w: dict | None, ms_1d: dict | None, ms_1h: dict | None,
) -> str | None:
    """输出 MTF 一致性判定行。

    4 种典型组合：
      - 三周期同向共振（1w/1d/1h 都 bullish 或都 bearish，置信度达标）→ 高胜率窗口
      - 日周同向 + 1h 相反 → 回调/反弹：1h 作执行层短打
      - 日周分歧（1w ≠ 1d）→ 结构转换中，宜降仓
      - 数据不足（任一 TF 缺失或都 ranging/transitioning）→ 结构不明
    """
    have = [ms for ms in (ms_1w, ms_1d, ms_1h) if ms]
    if not have:
        return None

    def _dir_of(ms: dict | None) -> str:
        if not ms:
            return "missing"
        conf = ms.get("confidence") or 0
        d = ms.get("direction", "")
        if d in ("bullish", "bearish") and conf >= _MS_MTF_CONF_MIN:
            return d
        return "unclear"  # ranging / transitioning / 置信度不足

    w, d, h = _dir_of(ms_1w), _dir_of(ms_1d), _dir_of(ms_1h)

    # 三 TF 同向且都达置信度门槛
    if w == d == h and w in ("bullish", "bearish"):
        side = "多" if w == "bullish" else "空"
        return (
            f"🎯 MTF 一致性: **三周期同向共振（1w/1d/1h 全 {side}）** — "
            f"**高胜率窗口**，可考虑加大仓位/延长持有。"
        )

    # 日周同向 & 1h 相反
    if w == d and w in ("bullish", "bearish") and h in ("bullish", "bearish") and h != w:
        big_side = "多" if w == "bullish" else "空"
        small_side = "空" if h == "bearish" else "多"
        return (
            f"🔄 MTF 分歧: **日周{big_side}头 vs 1h {small_side}头** — "
            f"1h 视为**回调/反弹执行层**，短线可做 1h 方向但目标位须参考日周结构关键位；"
            "中远线主导方向仍以日周为准。"
        )

    # 日周分歧（结构转换中 / 趋势变化期）
    if w in ("bullish", "bearish") and d in ("bullish", "bearish") and w != d:
        return (
            "⚠ MTF 冲突: **周线与日线方向相反** — "
            "结构转换期，宜**降低仓位 / 只做短线**，避免逆大周期重仓。"
        )

    # 剩余情况（含 unclear / missing）
    return (
        "ℹ MTF 提示: 周/日/小时结构未形成明确共振（含震荡或置信度不足），"
        "本次以单 TF 结构 + 关键位共振为主，MTF 对齐度作为次要参考。"
    )


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
        "### 1. 清算地图数据 [24h]",
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
        lines.extend(["", "### 1b. 清算地图数据 [7天]"])
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
        lines.extend(["", "### 1c. 清算地图数据 [30天]"])
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
        f"OI变化(24h): {snapshot.get('oi_change_24h_pct', 0):+.2f}%" if snapshot.get('oi_change_24h_pct') is not None else "OI变化(24h): 暂缺",
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
    if avg7d is not None and avg7d != 0:
        lines.append(f"7d均值: {avg7d*100:.4f}%")
    if funding_exchanges:
        extreme_fr = [fe for fe in funding_exchanges
                      if fe.get("current") is not None and abs(fe["current"]) > 0.0005]
        if extreme_fr:
            names = ", ".join(fe.get("exchange", "") for fe in extreme_fr)
            lines.append(f"  ⚠ {names} 费率绝对值>0.05%，属于极端水平，需警惕轧空/轧多风险")
        outlier_fr = [fe for fe in funding_exchanges
                      if fe.get("current") is not None and abs(fe["current"]) > 0.01]
        if outlier_fr:
            names = ", ".join(fe.get("exchange", "") for fe in outlier_fr)
            lines.append(f"  ⚠⚠ {names} 费率绝对值>1%，疑似数据异常/小所流动性不足，已从平均值中剔除，请降低权重")

    lines.extend([
        f"期现溢价: {snapshot.get('basis_pct', 0):+.4f}%",
        "",
        "### 5. 多空比 [各交易所]",
    ])
    ls = snapshot.get("ls_ratio")
    ls_long = snapshot.get("ls_ratio_long_pct")
    ls_short = snapshot.get("ls_ratio_short_pct")
    ls_chg24 = snapshot.get("ls_ratio_change_24h")
    # P0.3 · 多空比变化量（ls_chg24）是 ratio 数值差（今 2.23 ↔ 昨 2.20 → +0.0273），
    # 非百分比。旧展示仅写 "24h变化: +0.0273"，AI 易误读为 "+2.73%" 并与 §6 买卖力差串味。
    # 这里显式标注"比值差"并同步给出等效百分比，彻底消除二义性。
    def _fmt_ls_chg(base: float | None, chg: float | None) -> str:
        if chg is None:
            return ""
        if base is not None and abs(base) > 1e-6:
            eff_pct = chg / base * 100.0
            return f" | 24h 比值差 {chg:+.4f}(≈ {eff_pct:+.2f}% 环比, 非买卖力差)"
        return f" | 24h 比值差 {chg:+.4f}(比值差, 非百分比)"

    if ls is not None:
        pct_str = f" (多{ls_long:.1f}%/空{ls_short:.1f}%)" if ls_long is not None else ""
        chg_str = _fmt_ls_chg(ls, ls_chg24)
        lines.append(f"全局账户多空比: {ls:.2f}{pct_str}{chg_str} ({snapshot.get('ls_ratio_interpretation', '')})")
    else:
        lines.append("全局账户多空比: 数据暂缺")
    ls_ta = snapshot.get("ls_ratio_top_account")
    ls_tp = snapshot.get("ls_ratio_top_position")
    ta_long = snapshot.get("ls_top_acct_long_pct")
    ta_short = snapshot.get("ls_top_acct_short_pct")
    ta_chg24 = snapshot.get("ls_top_acct_change_24h")
    if ls_ta is not None:
        ta_label = "大户偏多" if ls_ta > 1.1 else ("大户偏空" if ls_ta < 0.9 else "大户中性")
        pct_str = f" (多{ta_long:.1f}%/空{ta_short:.1f}%)" if ta_long is not None else ""
        chg_str = _fmt_ls_chg(ls_ta, ta_chg24)
        lines.append(f"大户账户多空比: {ls_ta:.2f}{pct_str}{chg_str} → {ta_label}")
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
    # P0.3 · 真实"买卖力差"= 买卖总量不对称度，不是买一/卖一价差(spread)
    # 旧版错把 orderbook_spread_pct（spread = 衡量流动性紧致度）当作买卖力差展示，
    # 会出现"买盘$3.1B / 卖盘$3.2B → 买卖力差 +2.73%"这种符号/量级都荒谬的结果。
    bid_ask_skew_pct = 0.0
    if (bid_tot + ask_tot) > 0:
        bid_ask_skew_pct = (bid_tot - ask_tot) / (bid_tot + ask_tot) * 100.0
    # §6 只展示"聚合深度"——这是 Coinglass 订单簿快照的真实产出
    # 大单/墙体详情统一移到 §8d「大单追踪」，避免同一份 large_orders 数据
    # 在两个板块重复展示造成 AI 困惑
    lines.extend([
        "",
        "### 6. 订单簿聚合深度 [数据源: Coinglass · 聚合 Binance/OKX/Bybit 订单簿快照]",
        f"近档位合计深度(USD): 买盘 {_fmt_usd_for_prompt(bid_tot)} / 卖盘 {_fmt_usd_for_prompt(ask_tot)} | 买卖力差 {bid_ask_skew_pct:+.2f}%（(买-卖)/(买+卖)）",
        f"盘口价差 spread: {ob_spread:+.4f}%（买一/卖一价差，衡量流动性紧致度，非买卖力对比）",
        f"买卖力对比: {'买盘强于卖盘(支撑偏强)' if bid_tot > ask_tot * 1.1 else '卖盘强于买盘(抛压偏重)' if ask_tot > bid_tot * 1.1 else '买卖均衡'}",
        "说明: 本节仅含聚合深度总额。具体的大额挂单/墙体请参见 §8d「大单追踪」。",
    ])

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
    if opt_mp is not None and opt_mp > 0:
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
    # 注：§6 的 orderbook_*_walls 本质上就是这份 large_orders 按方向聚合后的 Top 10
    # 展开展示——让 AI 直接看到大单价格/规模，便于和关键位、清算簇做共振分析
    bid_walls = snapshot.get("orderbook_bid_walls", [])
    ask_walls = snapshot.get("orderbook_ask_walls", [])
    # 空节友好降级：即使全为空也渲染章节 + 解释，避免 AI 把"无活跃大单"误报为"数据缺失"
    lines.extend(["", "### 8d. 大单追踪 [数据源: Coinglass 大单监控·实时]"])
    if lo_buy > 0 or lo_sell > 0 or bid_walls or ask_walls:
        if lo_buy > 0 or lo_sell > 0:
            lines.append(f"大单买入: {lo_buy}笔 / 卖出: {lo_sell}笔 | 净方向: {_fmt_usd_for_prompt(lo_net)}")
            if lo_net > 0:
                lines.append(f"  大资金偏向: 买入为主(净流入)")
            elif lo_net < 0:
                lines.append(f"  大资金偏向: 卖出为主(净流出)")

        if bid_walls:
            lines.append("Top 买方大单（按金额排序）:")
            for w in bid_walls:
                price_val = w.get("price", 0)
                usd_val = w.get("size_usd", 0)
                rel_pct = ((price_val - price) / price * 100) if price > 0 else 0
                lines.append(
                    f"  - ${price_val:,.1f} (距现价 {rel_pct:+.2f}%): "
                    f"{_fmt_usd_for_prompt(usd_val)}"
                )
        else:
            lines.append("Top 买方大单: 近期下方无活跃大额买单挂单 → 机构承接意愿分散，下跌时缓冲有限")

        if ask_walls:
            lines.append("Top 卖方大单（按金额排序）:")
            for w in ask_walls:
                price_val = w.get("price", 0)
                usd_val = w.get("size_usd", 0)
                rel_pct = ((price_val - price) / price * 100) if price > 0 else 0
                lines.append(
                    f"  - ${price_val:,.1f} (距现价 {rel_pct:+.2f}%): "
                    f"{_fmt_usd_for_prompt(usd_val)}"
                )
        else:
            lines.append("Top 卖方大单: 近期上方无活跃大额卖单挂单 → 机构压盘不显著，上行阻力主要来自清算簇/技术位")
    else:
        lines.append(
            "当前无超阈值大单活跃（Coinglass 阈值: 现货≥$100K / 合约≥$1M）。"
        )
        lines.append(
            "→ 非数据缺失，解读为常态信号：大资金观望 / 订单簿静默期 / 本周期无异常挂单"
        )
        lines.append(
            "→ 此时 §6 聚合深度（买卖力差）与 §1 清算簇/ §9g 关键位仍是有效的阻力/支撑依据"
        )

    w_alerts = snapshot.get("whale_hl_alerts_count", 0)
    w_transfers = snapshot.get("whale_transfers_count", 0)
    w_dir = snapshot.get("whale_net_direction", "")
    hl_positions = snapshot.get("whale_hl_positions", [])
    w_inflow = snapshot.get("whale_transfer_inflow_usd", 0.0)
    w_outflow = snapshot.get("whale_transfer_outflow_usd", 0.0)
    w_net = snapshot.get("whale_transfer_net_usd", 0.0)
    w_top = snapshot.get("whale_top_transfers", [])
    # 空节友好降级：章节始终渲染，避免 AI 把"无巨鲸活动"误报为"数据缺失"
    lines.extend(["", "### 8e. 巨鲸追踪 [链上+Hyperliquid]"])
    if not (w_alerts > 0 or w_transfers > 0 or hl_positions):
        lines.append(
            "近期无巨鲸活动采集到（Hyperliquid 无警报/持仓 + 链上无 >$1M 转账）。"
        )
        lines.append(
            "→ 非数据缺失，解读为常态信号：大额资金冷静期 / 采集窗口尚短"
        )
    else:
        if w_alerts > 0:
            lines.append(f"Hyperliquid 巨鲸警报: {w_alerts}条")

        # 链上转账：从"仅笔数"升级为"USD 流向 + Top 转账"
        if w_transfers > 0 or abs(w_net) > 0 or w_top:
            if w_inflow > 0 or w_outflow > 0:
                lines.append(
                    f"链上巨鲸转账: 共{w_transfers}笔 | "
                    f"充入交易所 {_fmt_usd_for_prompt(w_inflow)} / "
                    f"提出交易所 {_fmt_usd_for_prompt(w_outflow)} | "
                    f"净流向 {_fmt_usd_for_prompt(w_net, signed=True)}"
                )
                if w_net > 1e7:
                    lines.append("  → 净流入交易所 >$10M：巨鲸囤积到交易所，抛压意图明显(偏空)")
                elif w_net < -1e7:
                    lines.append("  → 净流出交易所 >$10M：巨鲸撤单到冷钱包，惜售囤币(偏多)")
                elif w_net != 0:
                    lines.append(f"  → 净流向规模较小，方向弱信号{'(偏空)' if w_net > 0 else '(偏多)'}")
            else:
                lines.append(f"链上巨鲸转账: {w_transfers}笔 (转账标签为钱包间转账，未涉及交易所充提)")
            if w_top:
                lines.append("Top 转账（按金额排序）:")
                direction_zh = {
                    "inflow": "充入交易所",
                    "outflow": "提出交易所",
                    "ex_to_ex": "交易所间调拨",
                    "wallet_to_wallet": "钱包间转账",
                }
                for t in w_top:
                    lines.append(
                        f"  - {_fmt_usd_for_prompt(t.get('amount_usd', 0))} "
                        f"{direction_zh.get(t.get('direction', ''), t.get('direction', ''))} "
                        f"({t.get('from_label', '?')} → {t.get('to_label', '?')})"
                    )
        if w_dir:
            lines.append(f"巨鲸笔数方向(按转账计数): {w_dir}")
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
        fgi_int = int(fgi)
        fgi_prev = snapshot.get("fear_greed_prev")
        if fgi_prev is not None:
            delta = fgi_int - fgi_prev
            trend = "↑回暖" if delta > 0 else ("↓恶化" if delta < 0 else "→持平")
            lines.append(f"恐惧贪婪指数: {fgi_int} (前值{fgi_prev}, {delta:+d}{trend}) (0=极度恐惧, 100=极度贪婪)")
        else:
            lines.append(f"恐惧贪婪指数: {fgi_int} (0=极度恐惧, 100=极度贪婪)")
    dxy = snapshot.get("dxy")
    if dxy:
        dxy_chg = snapshot.get("dxy_change_pct")
        dxy_chg_str = f" ({dxy_chg:+.2f}%)" if dxy_chg is not None else ""
        lines.append(f"美元指数(DXY): {dxy:.2f}{dxy_chg_str}")
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
    ex_chg_abs = snapshot.get("exchange_btc_change_24h")
    cb_prem = snapshot.get("coinbase_btc_premium")
    usdt_prem = snapshot.get("usdt_otc_premium")
    if any(v is not None for v in (mvrv, ahr, ex_btc, cb_prem)):
        lines.append("")
        lines.append("### 9c. 链上与资金面（AITRADER_MATRIX_JSON · B板块 须引用本节数值）")
        if mvrv is not None:
            mvrv_label = "全网浮亏(底部区域)" if mvrv < 1 else ("估值中性" if mvrv < 2.5 else "估值过热")
            lines.append(f"MVRV 比率: {mvrv:.3f} → {mvrv_label}")
        if ahr is not None:
            ahr_label = "适合抄底" if ahr < 0.45 else ("适合定投" if ahr < 1.2 else "估值偏高")
            lines.append(f"ahr999 囤币指数: {ahr:.4f} → {ahr_label}")
        if ex_btc is not None:
            parts = [f"主要交易所 BTC 余额合计: {ex_btc:,.0f} BTC"]
            if ex_chg_abs is not None:
                parts.append(f"24h变化: {ex_chg_abs:+,.0f} BTC")
            if ex_chg is not None:
                parts.append(f"({ex_chg:+.2f}%)")
            lines.append(" | ".join(parts))
            if ex_chg is not None and ex_chg < -0.5:
                lines.append("  → 余额下降=BTC 被提走屯币，看涨信号")
            elif ex_chg is not None and ex_chg > 0.5:
                lines.append("  → 余额上升=BTC 充入准备卖出，看跌信号")
        if cb_prem is not None:
            lines.append(f"Coinbase BTC 溢价: {cb_prem:.4%} ({'正溢价=美国买盘活跃' if cb_prem > 0 else '负溢价=美国买盘弱'})")
        if usdt_prem is not None:
            lines.append(f"USDT 场外溢价: {usdt_prem:.3f} ({'>1=场外买盘活跃' if usdt_prem > 1 else '<1=场外卖盘'})")

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
        cps_val = cp['cps']
        cps_label_val = cp.get('cps_label', '')
        # CPS 是反向刻度：高分=底部便宜、低分=顶部贵
        # AI 容易被"1/10 小数"常识误导，必须显式标注刻度方向 + 当前档位解读
        if cps_val >= 8:
            cps_intent = "偏多(便宜区·优先做多)"
        elif cps_val >= 5:
            cps_intent = "偏多(折扣区·多头有利)"
        elif cps_val >= 2:
            cps_intent = "中性(估值合理)"
        elif cps_val >= 0.5:
            cps_intent = "偏空(溢价区·多头谨慎)"
        else:
            cps_intent = "强空(顶部区·禁止追多)"
        lines.append(f"周期评分(CPS): {cps_val:.1f}/10 → {cps_label_val}")
        lines.append(
            f"  刻度说明: 10=周期底部区(极便宜) / 0=顶部区(极贵)；"
            f"当前 {cps_val:.1f} → {cps_intent}"
        )

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
        lines.append(
            "[字段语义] 反弹质量/突破阶段为**状态感知字段**，仅在关键位处于对应状态时激活："
            "`反弹质量=-` 表示该 level 尚未发生反弹（state ≠ testing/bounced），属**预期空值**；"
            "`突破阶段=-` 表示该 level 尚未突破（state ≠ broken/flipped），亦属**预期空值**。"
            "⚠ 不要将这些 `-` 解读为「数据缺失/未采集」而触发数据质量警报。"
        )
        lines.append("")
        lines.append("| 价位 | 级别 | 类型 | 状态 | 反弹质量 | 突破阶段 | 距当前 | 共振分 | 来源数 | 测试 | 扫取量 | 级联风险 | 来源 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        bq_cn = {
            "proactive": "主动(量能≥1.5×·升档)",
            "passive": "被动(量能<0.8×·降档)",
            "": "-",
        }
        bs_cn = {
            0: "-",
            1: "stage1(<15m·禁追)",
            2: "stage2(回踩中·观察)",
            3: "stage3(已确认·可追)",
        }
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
            bq_str = bq_cn.get(lv.get("bounce_quality", ""), "-")
            bs_str = bs_cn.get(lv.get("breakout_stage", 0), "-")
            lines.append(
                f"| ${lv.get('price', 0):,.0f} | {tier} | {side_cn} | {state_cn} | "
                f"{bq_str} | {bs_str} | "
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
                    "scalp_long": "⚡日内做多", "scalp_short": "⚡日内做空",
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

    # ── §9i 价格结构（多时间框架 · Price Action / SMC · BOS/CHoCH） ──
    # MTF 改造（2026-04）：原来只渲染 1h，现扩展到 1w/1d/1h 三行 + 一致性判定行
    # - 1w / 1d 数据由 recompute_market_structure_weekly / _daily 产出
    # - 周线数据量少 (70 bars)，置信度低时只展示不要求 AI 过度依赖
    ms_1h = snapshot.get("market_structure")
    ms_1d = snapshot.get("market_structure_1d")
    ms_1w = snapshot.get("market_structure_1w")
    if ms_1h or ms_1d or ms_1w:
        # 标题保留"1h 市场结构"语义（系统 prompt L3 分级表用的即该措辞），
        # 同时附注 MTF 扩展，供 AI 理解 1w/1d 为中/远线参考层。
        lines.extend(
            ["", "### 9i. 1h 市场结构（MTF 扩展 · 1w/1d/1h）[核心·结构决定偏向]"]
        )
        # 逐 TF 渲染（大周期先展示，引导 AI 自上而下判定）
        if ms_1w:
            lines.extend(_render_ms_block("1w", ms_1w, verbose=False))
        if ms_1d:
            lines.extend(_render_ms_block("1d", ms_1d, verbose=False))
        if ms_1h:
            lines.extend(_render_ms_block("1h", ms_1h, verbose=True))

        # MTF 一致性判定行（非硬约束，仅作为 AI 方向偏置输入）
        mtf_line = _mtf_alignment_line(ms_1w, ms_1d, ms_1h)
        if mtf_line:
            lines.append(mtf_line)

        # 1h 参考提示（保留原有教学指引，但语义升级为 MTF 视角）
        if ms_1h:
            conf_1h = (ms_1h.get("confidence") or 0)
            if conf_1h >= 0.6 and ms_1h.get("direction") in ("bullish", "bearish"):
                lines.append(
                    "参考提示: 1h 结构是 L3 小时级的**短线执行层单一参考**，不是最终方向裁决。"
                )
                lines.append(
                    "  · 档位对齐：短线（1-8%）主要看 1h；中线（8-15%）看 1d；"
                    "远线（>15%）看 1w。MTF 同向共振 = 高胜率窗口；MTF 背离 = 降仓/观望。"
                )
                lines.append(
                    "  · 若多维共振（宏观+资金面+链上+日周结构）与 1h 相反——"
                    "如『1h 上升 + 1d 下降 + 1w 下降 + ETF 净流出 + MVRV 高位』这类分发末端反弹——"
                    "**优先采纳多维判断**，方案核心依据列标 `🔄 逆 1h 结构 · 理由：[维度组合]`；"
                    "1h 结构从来不是一票否决器。"
                )

    # ── §9j 动能衰竭 / 续航（TrendExhaustion · MTF 3 维 12 子项）──
    # 原因：该模块已由 engine 注入 AISnapshot，但历史上未在 prompt 渲染 → AI 看不见，
    #       相当于「规则层跑了 1288 行，却在决策时整张表不上桌」。
    # 语义：与 §9i 结构方向**正交**——§9i 说「方向往哪」,9j 说「这个方向还能不能撑」。
    te = snapshot.get("trend_exhaustion")
    if te and isinstance(te, dict):
        overall_state = te.get("overall_state") or "neutral"
        overall_dir = te.get("overall_direction") or "flat"
        consensus = te.get("consensus_level") or "neutral"
        overall_action = te.get("overall_action") or "stand_aside"
        regime_vetoed = bool(te.get("regime_vetoed"))
        data_q = te.get("data_quality") or "insufficient"
        plain = te.get("overall_plain_cn") or ""
        tip = te.get("overall_tip_cn") or ""
        reason = te.get("overall_reason_cn") or ""
        lines.extend(["", "### 9j. 动能衰竭 / 续航（MTF · 与 §9i 结构正交）[核心·方向的续航体检]"])
        state_cn = {
            "healthy_continuation": "🟢 健康续航",
            "momentum_fading":      "🟡 动能衰减",
            "exhaustion_warn":      "🟠 衰竭警戒",
            "structural_reversal":  "🔴 结构反转",
            "neutral":              "⚪ 中性",
        }.get(overall_state, overall_state)
        dir_cn = {"up": "上", "down": "下", "flat": "平"}.get(overall_dir, overall_dir)
        cons_cn = {
            "strong_agree": "MTF 强共振",
            "partial":      "MTF 部分共振",
            "conflict":     "MTF 冲突",
            "neutral":      "MTF 中性",
        }.get(consensus, consensus)
        lines.append(f"状态: {state_cn} · 方向视角: {dir_cn} · 共识: {cons_cn} · 建议动作: {overall_action}")
        if regime_vetoed:
            lines.append("⚠ regime 已否决（震荡/极端环境），本节建议动作自动降级为 stand_aside；"
                         "方向结论仅供参考，**不得单独作为入场理由**。")
        if data_q != "ok":
            miss = te.get("missing_inputs") or []
            if miss:
                lines.append(f"数据质量: {data_q}（缺失: {', '.join(map(str, miss[:3]))}）")
        if plain:
            lines.append(f"白话: {plain}")
        if tip:
            lines.append(f"行动提示: {tip}")
        if reason:
            lines.append(f"MTF 来源: {reason}")
        # 三周期子态（triggers 只取前 3 个，防 prompt 膨胀）
        for tf_key, tf_label in (("tf_1d", "1d"), ("tf_4h", "4h"), ("tf_1h", "1h")):
            tf = te.get(tf_key)
            if not tf:
                continue
            trigs = tf.get("triggers") or []
            composite = tf.get("composite_score") or 0
            lines.append(
                f"  · {tf_label}: state={tf.get('state','')} · composite={composite:+.2f} · "
                f"方向={tf.get('direction','')} · triggers={','.join(trigs[:3]) or '-'}"
            )
        lines.append(
            "使用原则: §9j 与 §9i 组合 → "
            "**续航×方向明确** 才构成顺势加仓依据；"
            "**衰竭/反转×方向明确** 是逆势 / 减仓信号（§四须对应降仓或标 ⚠动能背离）；"
            "**MTF conflict/neutral** 时降低 §四信心度 1 档。"
        )

    # ── §9k 规则引擎 8 维方向共识（DirectionVoteSummary）──
    # 原因：规则侧原本 8 维读数分散在 §9a~§9i，AI 每次要自己点票且容易被单维噪声带偏；
    #       此节把 structure/mtf/momentum/range/key_level/flow/positioning/exhaustion 统一
    #       归一化 → direction + strength + weight，给 AI 一份「规则侧一眼结论」。
    # 注意：本节是**规则侧的票数**，不是最终方向——AI 有终审权（见 system prompt）。
    dv = snapshot.get("direction_vote")
    if dv and isinstance(dv, dict):
        lines.extend(["", "### 9k. 规则引擎 8 维方向共识（AI 可作为「规则侧结论」参考，有终审权）"])
        dom = dv.get("dominant_direction") or "neutral"
        cons = dv.get("consensus_level") or "low_signal"
        score = float(dv.get("weighted_score") or 0)
        bulls = int(dv.get("bullish_votes") or 0)
        bears = int(dv.get("bearish_votes") or 0)
        neutrals = int(dv.get("neutral_votes") or 0)
        active = int(dv.get("active_dimensions") or 0)
        missing = dv.get("missing_dimensions") or []
        summary_cn = dv.get("summary_cn") or ""
        dom_cn = {"bullish": "🟢 偏多", "bearish": "🔴 偏空", "neutral": "⚪ 中性"}.get(dom, dom)
        cons_cn = {
            "strong_agree": "强共识",
            "partial":      "部分共识",
            "conflict":     "多空分歧",
            "low_signal":   "信号弱",
        }.get(cons, cons)
        lines.append(
            f"规则侧结论: {dom_cn} · {cons_cn} · "
            f"加权得分 {score:+.2f}（-1~+1） · "
            f"票型 {bulls}多/{bears}空/{neutrals}中 · 参与维度 {active}/8"
        )
        if summary_cn:
            lines.append(f"一行总结: {summary_cn}")
        if missing:
            lines.append(f"缺失维度: {', '.join(missing[:6])}（本节按剩余维度重新归一化加权）")
        # 8 维明细表（按输出稳定顺序渲染）
        lines.append("| 维度 | 方向 | 强度 | 权重 | 依据 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for v in dv.get("votes") or []:
            d = v.get("direction") or "neutral"
            d_sym = {"bullish": "🟢多", "bearish": "🔴空", "neutral": "⚪中"}.get(d, d)
            lines.append(
                f"| {v.get('name_cn') or v.get('key','')} | {d_sym} | "
                f"{float(v.get('strength') or 0):.2f} | "
                f"{float(v.get('weight') or 0):.2f} | "
                f"{v.get('note','')} |"
            )
        top_b = dv.get("top_bullish") or []
        top_s = dv.get("top_bearish") or []
        if top_b or top_s:
            lines.append(
                f"贡献最大: top_bullish={','.join(top_b) or '-'} · "
                f"top_bearish={','.join(top_s) or '-'}"
            )
        lines.append(
            "使用原则: 1) 与规则同向 → §一共振概要 / JSON.sections 可引用 `§9k 规则共识 = {}` 作为加分项；"
            "2) **想逆规则共识做反向** → 必须在 §八自检中按「AI 终审员」流程声明"
            "（≥2 维跨层级新证据 + 规则推翻理由 + 降仓保护）；"
            "3) 规则 consensus_level=conflict/low_signal → 方向天然低确信，§四降档。"
            .format(dom_cn)
        )

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
            np_chg_24h = snapshot.get("net_position_change_24h")
            # Coinglass v2 net-position 单位为基础币计数（coin），非 USD
            # → 绝对值对 AI 无直接参考价值，核心判断依据是"趋势方向 + 24h 相对变化 + 显著性标签"
            # 不再用 _fmt_usd_for_prompt() 渲染 raw 数值，避免 AI 误把"$1万"当做资金规模解读
            np_str = f"净持仓(v2): {np_trend or '趋势待定'}"
            if np_chg_24h is not None:
                # 净持仓是带符号累积值（净多 - 净空 coin 数），24h 内可从 +300 翻到 -200。
                # 旧版用 abs(latest) 做分母会产生 -276% 这类脱离 [-100,100] 的诡异值。
                # 新版：用 24h 内端点的较大幅值做分母，并 clamp 到 ±200% 内防越界。
                # 若 base 太小（接近方向翻转期），直接退化为"绝对变化量 + 趋势标签"。
                np_vals_start = float(np_latest) - float(np_chg_24h)  # 24h 前端点
                base = max(abs(float(np_latest)), abs(np_vals_start))
                chg_pct = None  # None = 基数过小，不输出百分比
                if base >= 1.0:  # 避免除零 + 避免分母接近 0 的荒诞值
                    raw_pct = (float(np_chg_24h) / base) * 100.0
                    chg_pct = max(-200.0, min(200.0, raw_pct))

                # 显著性 tag 阈值沿用历史经验值（5%/2%），仅分母口径做了健壮化
                if chg_pct is not None:
                    if abs(chg_pct) >= 5:
                        tag = "**显著**"
                    elif abs(chg_pct) >= 2:
                        tag = "温和"
                    else:
                        tag = "微幅"
                    direction_label = '增持' if np_chg_24h > 0 else '减持'
                    np_str += (
                        f" · 24h 变化 {chg_pct:+.1f}% ({np_chg_24h:+,.0f} coin) "
                        f"{tag}{direction_label}"
                    )
                else:
                    # base 太小（方向翻转期 / 持仓近 0）：不输出百分比，避免误导 AI
                    np_str += (
                        f" · 24h 变化量 {np_chg_24h:+,.0f} coin"
                        f"（方向翻转期，基数过小不计百分比）"
                    )
            lines.append(np_str)
            lines.append(
                "  [数据说明] 数值单位为 Coinglass 合约净多空差 coin 计数（非 USD 金额），"
                "核心解读以**趋势方向 + 百分比变化**为准，绝对数值仅作趋势判定输入。"
                "百分比分母已改为 24h 端点较大幅值（避免方向翻转期分母接近 0 产生荒诞数值）；"
                "若标注『方向翻转期，基数过小』则以绝对变化量为准。"
            )
            # 方向语义解读：明确告诉 AI 这意味着什么
            if np_trend:
                if "上升" in np_trend or "多头增仓" in np_trend:
                    lines.append("  → 多头持仓递增：看多燃料补充中，但注意需要现货/资金面共振才能续涨")
                elif "下降" in np_trend or "多头减仓" in np_trend:
                    lines.append("  → 多头持仓递减：多头离场或空头建仓，常见于高位派发或趋势减弱")
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

    has_crypto_sent = fgi is not None or dom is not None
    has_trad = any(snapshot.get(k) for k in ("dxy", "nasdaq", "sp500", "gold"))
    lines.extend([
        "",
        "【宏观数据覆盖说明】（请严格按此表述，避免与上文矛盾）",
        f"- 加密侧情绪/结构: {'已提供（恐惧贪婪/市占等）' if has_crypto_sent else '未提供'}",
        f"- 传统外盘(DXY/纳指/标普/黄金): {'已提供（见§9数值）' if has_trad else '当前未提供，请勿编造或推测具体数值'}",
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
    lines.append("*说明: TP1=近目标(部分止盈·清算磁吸/POC/第一阻力支撑)，TP2=远目标(吃满·满足 R:R≥1:{:.1f})*".format(min_rr))
    if sniper:
        for i, se in enumerate(sniper):
            d = se.get("direction", "")
            dist_pct = abs(se.get("entry_price", 0) - price) / price * 100 if price > 0 else 0
            lines.append(f"方案{i+1} [{d}] 距{dist_pct:.1f}%: "
                         f"入场${se.get('entry_price', 0):,.1f} "
                         f"止损${se.get('stop_loss', 0):,.1f} "
                         f"TP1近=${se.get('take_profit_1', 0):,.1f}(R:R {se.get('rr_ratio_1', 0):.1f}) "
                         f"TP2远=${se.get('take_profit_2', 0):,.1f}(R:R {se.get('rr_ratio_2', 0):.1f})")
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

    # ── §11d 日内极小止损档（引擎 scalp 信号；AI 做方向否决而非规划）──
    # 设计说明：该档为引擎基于 S/A 级关键位 + 15m 影线确认 + 极小止损（≥0.2% 或 ≤0.5×ATR）
    # + R:R ≥ 1.5 硬筛选后的确定性信号；时间窗 30 分钟内有效。
    # AI 推理耗时 150-230s，无法规划此档，**仅需判定是否否决**（宏观急剧不利 / 对侧有更强阻挡）。
    if has_kl:
        scalp_sigs = [s for s in kl.get("signals", [])
                      if s.get("action") in ("scalp_long", "scalp_short")]
        if scalp_sigs:
            lines.append("")
            lines.append("**11d. 日内极小止损档（引擎 scalp；AI 只做方向否决）**")
            lines.append("  规则：已通过 S/A 级关键位 + 15m 影线 + 极小止损 + R:R≥1.5 硬筛；")
            lines.append("  AI 任务：在§四短线档中逐条判定"
                         "是否【否决】（标注理由，如宏观 risk-off / 对侧 SSL-BSL 更强 / 级联风险过高），"
                         "**未否决者默认采纳**，不要重复规划参数。")
            for sig in scalp_sigs:
                side_cn = "做多" if sig.get("action") == "scalp_long" else "做空"
                entry = sig.get("entry_price", 0) or 0
                sl = sig.get("stop_loss", 0) or 0
                tp1 = sig.get("tp1", 0) or 0
                rr = sig.get("rr_ratio", 0) or 0
                sl_pct = abs((entry - sl) / entry * 100) if entry > 0 else 0
                lines.append(
                    f"  - {sig.get('confidence', 'B')}级 ⚡{side_cn} @${sig.get('level_price', 0):,.0f} "
                    f"入场${entry:,.0f} 止损${sl:,.0f}({sl_pct:.2f}%) TP1=${tp1:,.0f} R:R={rr:.1f} "
                    f"| {sig.get('reason', '')}"
                )
                for w in sig.get("warnings", []):
                    lines.append(f"    ⚠ {w}")

    # ── P1.2b · 新闻简报 + 地缘 + 活跃叙事（有值才追加） ──
    _append_news_context(lines, snapshot)

    lines.append("")
    lines.append("请基于以上数据输出，**必须包含八个章节**（一~八），第四节「交易计划」按三档结构输出（短线/中线/远线），第八节「数据质量与自检」对输入数据做诊断。")
    cps_note = " 5) §9e有数据时，§一须引用CPS周期位置，§四中远线档须评估CPS与方向一致性" if has_cps else ""
    range_note = " 6) §9f有数据时，§一须引用箱体位置，§二须纳入MA关键价位，有A级信号时§四须评估共振" if has_range else ""
    kl_note = " 7) §9g有信号时，§四须优先评估关键位SWEPT/FLIPPED信号与引擎方案的共振，高cascade_risk须警告" if has_kl else ""
    scalp_note = " 8) §11d有 scalp 信号时，§四短线档须**逐条**给出「采纳/否决」判定（不重新规划参数）" if has_kl and any(s.get("action","").startswith("scalp_") for s in kl.get("signals", [])) else ""
    lines.append("重点：1) 每个方案止损须含防猎杀说明 2) 宏观-微观一致 3) §四与引擎 R:R 口径对齐（≥1:{:.1f}） 4) 引擎未覆盖的档位AI可自主构建标注⚡AI推断{}{}{}{}".format(min_rr, cps_note, range_note, kl_note, scalp_note))
    return "\n".join(lines)
