"""Prompt 铁律分类索引（P2.2 · 本轮只标注，不删）

## 用户反馈（根因）
> 提示词过长、过密、过硬，容易把模型逼进规则森林。不是普通长，而是
> 多层规则叠加 + 例外规则 + 反例裁决 + 输出合约 + JSON 协议同时存在。
> 典型后果：前半段记住了，后半段忘了 / JSON 漏了 / 方案数量超了。

## 策略（用户决策 · 渐进）
> 先标 TODO 分类 → 跑验证 → 再分批删

本模块按五类对 prompt 中的所有"铁律 / 必须 / 严禁 / 禁止"条目做分类标注：

- ``CRITICAL``  合规 & 系统契约（输出格式、JSON 可解析、禁止合规违规表述）
               · 永不删除
- ``SEMANTIC``  防止语义错乱（方向定义、TP1/TP2 区分、刻度方向等）
               · 永不删除，但可压缩表述
- ``HEURISTIC`` 启发式经验（叙事陷阱、权衡顺序、终审规则）
               · P2.2-B 候选压缩 / 合并为"交易员直觉"段落
- ``REDUNDANT`` 与其他条冗余（多处表述同一约束）
               · P2.2-B 候选删除（保留最简表述）
- ``LEGACY``    历史兼容条目（旧版格式防误触发）
               · P2.2-B 候选删除（若验证无回归）

## 落地方式
- 每条铁律由 ``anchor`` 锁定：其 text 片段在 prompts.py 中必须唯一存在
- ``validate_catalog_against_prompt()`` 读 prompts.py 源码，校验每条 anchor
  都能在源码中找到，**防止 prompt 改动时 catalog 漂移**
- 本轮 commit 不删任何条目；下一轮（P2.2-B）删除前先跑一次 AI 分析冷启动，
  验证删减后输出不回归；
- 单测 test_prompt_rule_catalog_coverage 保证 anchor 与源码一致

## 非目标
- 本模块**不重写** prompts.py 正文（保守修改原则 6）
- 不改任何 AI 交互实际行为，仅为后续治理提供元数据
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RuleCategory = Literal["CRITICAL", "SEMANTIC", "HEURISTIC", "REDUNDANT", "LEGACY"]


@dataclass(frozen=True)
class RuleMeta:
    """单条 prompt 规则的分类元数据。"""
    rule_id: str                 # 唯一标识，格式 "section.topic"
    category: RuleCategory       # 分类
    anchor: str                  # 在 prompts.py 中唯一存在的文本片段（用于校验）
    purpose_cn: str              # 中文目的说明（为何存在）
    removal_candidate: bool      # 下一轮是否候选删除
    notes: str = ""              # 补充说明


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CATALOG · 按 prompts.py 章节顺序列出核心铁律
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 注：本 catalog 覆盖 ≈80% 关键铁律；细粒度列举项（如"若...则..."子条目）不列入
#     —— 删减时以本表为基本单元，子项随主条目处理
RULE_CATALOG: list[RuleMeta] = [
    # ── 系统 Prompt · 铁律段 ───────────────────────────────────────
    RuleMeta(
        rule_id="system.iron.decision_tool",
        category="CRITICAL",
        anchor="你是**决策参考工具**",
        purpose_cn="合规底线：禁止保证盈利 / 不给胜率数字",
        removal_candidate=False,
        notes="合规核心，永保留",
    ),
    RuleMeta(
        rule_id="system.iron.cross_verification",
        category="SEMANTIC",
        anchor="关键价位必须 **≥2 维**交叉验证",
        purpose_cn="质量底线：强制跨维度推理",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="system.iron.data_conflict_judge",
        category="SEMANTIC",
        anchor="数据矛盾时必须指出并判断哪个更可信",
        purpose_cn="冲突处理原则（与 L1-L5 分级互为引用）",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="system.iron.macro_presence_check",
        category="LEGACY",
        anchor="若宏观数据中已有恐惧贪婪/DXY/纳指等任一数值",
        purpose_cn="修复历史版本 AI 误报'宏观完全缺失'的 bug",
        removal_candidate=True,
        notes="现已有 DataMeta 机制后可删",
    ),
    RuleMeta(
        rule_id="system.iron.orderbook_zero_depth",
        category="LEGACY",
        anchor="订单簿合计深度为 0 时表述为",
        purpose_cn="修复历史 AI 误报'流动性完全消失'的 bug",
        removal_candidate=True,
        notes="P0.3 完成后可并入数据异常话术",
    ),
    RuleMeta(
        rule_id="system.iron.outlier_warning",
        category="SEMANTIC",
        anchor="数据异常值怀疑",
        purpose_cn="离群数据降权（与 L1-L5 可信度分级互补）",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="system.iron.mtf_multi_trace",
        category="HEURISTIC",
        anchor="多维留痕铁律",
        purpose_cn="强制 §四每方案标注顺/逆 1h 结构 + 理由",
        removal_candidate=False,
        notes="防 AI 盲从 1h 结构，P2.2-B 可精简表述",
    ),
    RuleMeta(
        rule_id="system.iron.price_lineage",
        category="SEMANTIC",
        anchor="价位血统铁律",
        purpose_cn="每价位必须标 §9X 数据来源或 ⚡AI推断公式",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="system.iron.psychological_levels",
        category="SEMANTIC",
        anchor="心理位禁令",
        purpose_cn="$77k/$80k/$100k 等整数关口不得充当数据依据",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="system.iron.timeframe_honesty",
        category="SEMANTIC",
        anchor="时间框架诚实铁律",
        purpose_cn="禁止捏造 4h/2h 等数据中不存在的时间框架",
        removal_candidate=False,
    ),

    # ── CPS 刻度方向 ─────────────────────────────────────────────
    RuleMeta(
        rule_id="system.cps.scale_direction",
        category="SEMANTIC",
        anchor="CPS 是**反向刻度**：数值越**高**",
        purpose_cn="防 AI 把 CPS=8 误读为顶部（真实是底部区）",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="system.cps.archetype_ban_low",
        category="REDUNDANT",
        anchor="严禁**将 CPS=1 / 2 / 3 误读",
        purpose_cn="与 scale_direction 条目部分重叠",
        removal_candidate=True,
        notes="可与上面合并",
    ),
    RuleMeta(
        rule_id="system.cps.archetype_ban_high",
        category="REDUNDANT",
        anchor="严禁**将 CPS=8 / 9 误读",
        purpose_cn="与 scale_direction 条目部分重叠",
        removal_candidate=True,
        notes="可与上面合并",
    ),

    # ── 输出合约 · §四 · TP1/TP2 语义 ─────────────────────────────
    RuleMeta(
        rule_id="output.tp_semantics",
        category="SEMANTIC",
        anchor="TP1 / TP2 语义铁律",
        purpose_cn="防 TP1/TP2 前后错乱导致下单异常",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="output.entry_range_order",
        category="CRITICAL",
        anchor="价格区间**必须小值在前-大值在后**",
        purpose_cn="系统解析契约",
        removal_candidate=False,
    ),

    # ── 输出合约 · 附录 JSON ──────────────────────────────────────
    RuleMeta(
        rule_id="output.json_parsable",
        category="CRITICAL",
        anchor="JSON **必须**能被 `json.loads` 直接解析",
        purpose_cn="系统解析契约（下游 analyzer.py 依赖）",
        removal_candidate=False,
    ),

    # ── 交易员推理框架 / 终审员 ───────────────────────────────────
    RuleMeta(
        rule_id="trader.thinking_like_market_maker",
        category="HEURISTIC",
        anchor="像庄家一样思考",
        purpose_cn="方法论指引（清算地图 → 猎杀路径）",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="trader.final_arbiter",
        category="HEURISTIC",
        anchor="AI 终审员权限",
        purpose_cn="明确 AI 可以推翻规则 & 推翻门槛",
        removal_candidate=False,
        notes="P2.2-B 可压缩表述，但语义核心不可删",
    ),

    # ── P0 修复 · 数据反语义说明（P0.3/P0.6/P0.7）────────────────
    RuleMeta(
        rule_id="fix.p0_3.bid_ask_skew",
        category="CRITICAL",
        anchor="P0.3 · 真实\"买卖力差\"",
        purpose_cn="修 §5/§6 spread 与 skew 语义串味 bug",
        removal_candidate=False,
        notes="P0 修复（保留注释作为 provenance）",
    ),
    RuleMeta(
        rule_id="fix.p0_3.ls_change_label",
        category="CRITICAL",
        anchor="P0.3 · 多空比变化量（ls_chg24）",
        purpose_cn="修 LS ratio 变化量误读为百分比",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="fix.p0_6.whale_filter",
        category="CRITICAL",
        anchor="P0.6 · 展示层兜底过滤",
        purpose_cn="过滤 $0 / <$100k 巨鲸转账",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="fix.p0_7.news_bias_disambiguation",
        category="CRITICAL",
        anchor="btc_price_bias = 该叙事对 BTC 价格方向的判断",
        purpose_cn="澄清 btc_price_bias 与 intensity 解耦",
        removal_candidate=False,
    ),
    RuleMeta(
        rule_id="fix.p1_1.etf_daily_pending",
        category="CRITICAL",
        anchor="P1.1 · ETF 当日 $0 很可能是",
        purpose_cn="ETF 当日 $0 加 pending 标注",
        removal_candidate=False,
    ),

    # ── P2.1 · expectancy 指引 ───────────────────────────────────
    RuleMeta(
        rule_id="fix.p2_1.expectancy_guide",
        category="SEMANTIC",
        anchor="期望值 (Expectancy) 自主评估",
        purpose_cn="取消硬 R:R，引入期望值软底线",
        removal_candidate=False,
    ),
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 查询 API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_removable_rules() -> list[RuleMeta]:
    """返回所有下一轮候选删除的规则。"""
    return [r for r in RULE_CATALOG if r.removal_candidate]


def get_rules_by_category(cat: RuleCategory) -> list[RuleMeta]:
    """按分类检索规则。"""
    return [r for r in RULE_CATALOG if r.category == cat]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 校验：catalog 是否与 prompts.py 源码同步
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _prompts_py_text() -> str:
    """读 prompts.py 源码（不触发 import，避免副作用）。"""
    path = Path(__file__).resolve().parent / "prompts.py"
    return path.read_text(encoding="utf-8")


def validate_catalog_against_prompt() -> list[str]:
    """检查每条 catalog.anchor 在 prompts.py 中确实存在。

    Returns:
        list[str]: 缺失 anchor 的规则 ID 列表（空列表 = 全部匹配）
    """
    src = _prompts_py_text()
    missing = []
    for rule in RULE_CATALOG:
        if rule.anchor not in src:
            missing.append(f"{rule.rule_id} (anchor: {rule.anchor[:30]}...)")
    return missing


def format_catalog_summary() -> str:
    """生成一份人类可读的分类汇总（用于审阅 / 日志）。"""
    lines = ["# Prompt 铁律分类汇总（P2.2）\n"]
    by_cat: dict[str, list[RuleMeta]] = {}
    for r in RULE_CATALOG:
        by_cat.setdefault(r.category, []).append(r)

    total = len(RULE_CATALOG)
    removable = len(get_removable_rules())
    lines.append(f"**总计**: {total} 条铁律 · 候选删除 {removable} 条\n")

    order: list[RuleCategory] = ["CRITICAL", "SEMANTIC", "HEURISTIC", "REDUNDANT", "LEGACY"]
    for cat in order:
        items = by_cat.get(cat, [])
        if not items:
            continue
        lines.append(f"\n## {cat} ({len(items)} 条)\n")
        for r in items:
            star = " 🗑️" if r.removal_candidate else ""
            lines.append(f"- `{r.rule_id}`{star}: {r.purpose_cn}")
    return "\n".join(lines)
