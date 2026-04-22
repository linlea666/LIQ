"""P2.1 · R:R 硬约束 → expectancy 软底线 单测

验证 prompt 模板侧：
  - 已取消 "AI 自主方案必须满足 R:R ≥ min_rr 约束" 的硬性条目
  - 已引入 "期望值 (Expectancy) 自主评估" 指引
  - 软底线 rr≥1.0 替代硬底线 min_rr
  - 规则引擎侧的 min_rr 过滤信息保留（用户保护）

注意：本测不跑完整 prompt 渲染（依赖图太深），只对关键文案片段做断言。
"""
from __future__ import annotations


def _load_prompts_src() -> str:
    """一次性读入 prompts.py 源码做文本断言"""
    import pathlib
    p = pathlib.Path(__file__).resolve().parent.parent / "ai" / "prompts.py"
    return p.read_text(encoding="utf-8")


class TestExpectancyPromptMigration:
    """P2.1 · prompt 文案迁移的关键断言"""

    def test_expectancy_guide_present(self):
        """expectancy 指引已加入 prompt"""
        src = _load_prompts_src()
        assert "期望值 (Expectancy)" in src, "缺少 expectancy 指引核心段"
        assert "胜率" in src and "盈亏比" in src, "应显式提及胜率×盈亏比"
        assert "禁止给出具体胜率数字" in src, "需约束 AI 只讲直觉不给数字"

    def test_soft_floor_rr_10(self):
        """软底线 rr≥1.0 已写入（替代硬底线 min_rr）"""
        src = _load_prompts_src()
        assert "软底线" in src, "缺少 '软底线' 概念"
        assert "rr≥1.0" in src or "rr ≥ 1.0" in src or "R:R ≥ 1.0" in src, (
            "应显式写出 rr≥1.0 软底线"
        )

    def test_hard_rr_constraint_relaxed(self):
        """硬性 R:R 要求 'AI 自主方案必须...满足 R:R ≥ 1:X 约束' 已取消"""
        src = _load_prompts_src()
        # 核对特定硬约束句式已不再出现
        assert "AI 自主方案必须：标注\"⚡AI推断\"、≥2 维数据交叉验证、满足 R:R ≥ 1:" not in src, (
            "仍存在硬性 R:R 要求，未按 P2.1 软化"
        )
        # 新版句式：AI 自主方案要求（数据源 + 标注 + 跨维验证），不含 R:R 硬条目
        assert "AI 自主方案要求：标注\"⚡AI推断\"、≥2 维数据交叉验证" in src, (
            "未替换为软化后的 AI 自主方案要求句式"
        )

    def test_rule_engine_min_rr_preserved(self):
        """规则引擎侧 min_rr 过滤信息仍保留（告知 AI 规则层保护）"""
        src = _load_prompts_src()
        # 应仍提及"引擎...按 ≥ 1:{min_rr"格式，告诉 AI 规则层已做保护
        assert "引擎" in src and "min_rr" in src, "规则引擎 min_rr 信息意外丢失"
        # 应提及"预过滤"或"规则层保护"等措辞
        assert "预过滤" in src or "规则层" in src or "规则引擎侧" in src, (
            "需表达规则引擎侧 min_rr 过滤是保护性措施（不是 AI 硬约束）"
        )

    def test_tp2_no_forced_constraint(self):
        """TP2 不再 '强制 ≥ 1:X'，改为 '软底线 R:R ≥ 1.0'"""
        src = _load_prompts_src()
        assert "TP2 = 远目标（吃满点）**：强制 ≥ 1:" not in src, (
            "TP2 仍有硬性 '强制 ≥ min_rr' 要求，未软化"
        )
        assert "TP2 = 远目标（吃满点）**：软底线 R:R ≥ 1.0" in src, (
            "TP2 新文案未按 P2.1 替换"
        )

    def test_expectancy_sorting_hint(self):
        """§四排序指引改为 '期望值优先、信心度次之'"""
        src = _load_prompts_src()
        assert "期望值优先" in src, "缺少 '期望值优先' 排序指引"

    def test_footer_rr_rule_softened(self):
        """用户 prompt 文末「重点」行不再直接硬约束 R:R"""
        src = _load_prompts_src()
        # 旧版 "3) §四与引擎 R:R 口径对齐（≥1:...）" 已不再是单纯口径约束
        assert "§四与引擎 R:R 口径对齐" not in src, (
            "文末「重点」行仍存在旧 R:R 口径硬约束句式"
        )
        # 新版应显式提及"期望值"
        assert "期望值" in src and "软底线 rr≥1.0" in src, (
            "文末「重点」行未按 P2.1 引入期望值语义"
        )
