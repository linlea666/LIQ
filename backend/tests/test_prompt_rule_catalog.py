"""P2.2 · Prompt 铁律分类 catalog 单测

核心保障：
  1. catalog 中每条 anchor 都能在 prompts.py 源码中唯一存在
     （防止 prompt 修改时 catalog 漂移）
  2. 分类覆盖五类（CRITICAL/SEMANTIC/HEURISTIC/REDUNDANT/LEGACY），
     每类至少 1 条
  3. 所有候选删除条目均非 CRITICAL（安全锁：不允许删合规铁律）
  4. 查询 API 基本正确性
"""
from __future__ import annotations

from ai.prompt_rule_catalog import (
    RULE_CATALOG,
    RuleCategory,
    format_catalog_summary,
    get_removable_rules,
    get_rules_by_category,
    validate_catalog_against_prompt,
)


class TestCatalogAnchorsSync:
    """每条 catalog.anchor 必须在 prompts.py 中存在（防漂移锁）"""

    def test_all_anchors_present(self):
        missing = validate_catalog_against_prompt()
        assert missing == [], f"Catalog 漂移，缺失 anchor: {missing}"

    def test_rule_ids_unique(self):
        ids = [r.rule_id for r in RULE_CATALOG]
        assert len(ids) == len(set(ids)), "存在重复 rule_id"


class TestCategoryCoverage:
    """五类分类每类必须至少有 1 条（用户要求的全覆盖标注）"""

    EXPECTED_CATEGORIES: list[RuleCategory] = [
        "CRITICAL", "SEMANTIC", "HEURISTIC", "REDUNDANT", "LEGACY"
    ]

    def test_every_category_has_at_least_one_rule(self):
        for cat in self.EXPECTED_CATEGORIES:
            rules = get_rules_by_category(cat)
            assert len(rules) >= 1, f"类别 {cat} 未覆盖任何铁律"

    def test_category_totals_sanity(self):
        total = len(RULE_CATALOG)
        summed = sum(len(get_rules_by_category(c))
                     for c in self.EXPECTED_CATEGORIES)
        assert total == summed, (
            f"分类总数不一致：总 {total} vs 分类和 {summed}（存在未分类条目？）"
        )


class TestSafetyLocks:
    """安全锁：候选删除条目永远不得命中 CRITICAL 类"""

    def test_critical_rules_never_removable(self):
        for r in get_rules_by_category("CRITICAL"):
            assert r.removal_candidate is False, (
                f"CRITICAL 规则 {r.rule_id} 被误标为候选删除 —— "
                f"合规/契约铁律永不可删"
            )

    def test_semantic_rules_not_removable_by_default(self):
        """SEMANTIC 类默认不可删（保护正确性语义）"""
        for r in get_rules_by_category("SEMANTIC"):
            assert r.removal_candidate is False, (
                f"SEMANTIC 规则 {r.rule_id} 被标候选删除 —— "
                f"若确需删除请提升为 REDUNDANT/LEGACY"
            )


class TestRemovalCandidates:
    """P2.2-B 候选删除：清单非空 + 仅来自 REDUNDANT / LEGACY"""

    def test_removable_list_nonempty(self):
        rem = get_removable_rules()
        assert len(rem) >= 1, "候选删除列表为空 —— P2.2-B 就没活干了"

    def test_removable_only_from_redundant_or_legacy(self):
        allowed = {"REDUNDANT", "LEGACY"}
        for r in get_removable_rules():
            assert r.category in allowed, (
                f"{r.rule_id} 分类 {r.category} 不允许候选删除"
            )


class TestFormatSummary:
    """format_catalog_summary 生成可读报告（smoke test）"""

    def test_summary_contains_all_categories(self):
        out = format_catalog_summary()
        for cat in TestCategoryCoverage.EXPECTED_CATEGORIES:
            rules = get_rules_by_category(cat)
            if rules:
                assert cat in out, f"摘要缺失分类 {cat}"

    def test_summary_marks_removable(self):
        out = format_catalog_summary()
        if get_removable_rules():
            assert "🗑️" in out, "候选删除条目未标记"
