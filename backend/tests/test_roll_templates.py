"""滚仓策略模板管理 (processors/roll_templates.py) 单元测试

覆盖要点：
  1. 4 套预置模板合法性（通过 validate_template）
  2. validate_thresholds 递减 + 范围
  3. validate_gates 范围
  4. derive_template：正常派生 / 非法 id / 重复 id
  5. update_template：builtin 拒绝 / 越界拒绝 / 正常更新
  6. delete_template：builtin 拒绝 / 不存在拒绝 / 正常删除
  7. plan_from_template：字段完整映射 + overrides 生效
  8. save / load 往返一致 + 容错：文件缺失 / 损坏 / 预置丢失自动补齐
  9. bootstrap_templates：首次运行写盘 + 二次启动幂等
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from models.roll_position import (
    ConfidenceThresholds,
    RollTemplate,
    SafetyGates,
)
from processors.roll_templates import (
    BUILTIN_TEMPLATE_IDS,
    TemplateValidationError,
    bootstrap_templates,
    builtin_templates,
    delete_template,
    derive_template,
    find_template,
    load_templates,
    plan_from_template,
    save_templates,
    update_template,
    validate_gates,
    validate_template,
    validate_thresholds,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 预置模板
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBuiltinTemplates:
    def test_four_builtins(self):
        templates = builtin_templates()
        ids = {t.id for t in templates}
        assert ids == BUILTIN_TEMPLATE_IDS
        assert ids == {"fatzhai", "li_fashi", "pyramid", "conservative"}

    def test_all_builtins_valid(self):
        """4 套预置必须通过完整校验。"""
        for t in builtin_templates():
            validate_template(t)
            assert t.builtin is True

    def test_builtins_have_chinese_name(self):
        """UI 显示友好。"""
        for t in builtin_templates():
            assert t.name
            assert t.description
            assert len(t.description) > 10


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 阈值 / 闸门校验
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestValidateThresholds:
    def test_default_pass(self):
        validate_thresholds(ConfidenceThresholds())

    def test_full_add_too_high(self):
        with pytest.raises(TemplateValidationError, match="full_add"):
            validate_thresholds(ConfidenceThresholds(full_add=90))

    def test_full_add_too_low(self):
        with pytest.raises(TemplateValidationError, match="full_add"):
            validate_thresholds(ConfidenceThresholds(full_add=50))

    def test_not_strictly_decreasing(self):
        # full_add 最低值 65，half_add 最高值 65 —— 构造 full==half 且都在范围内
        with pytest.raises(TemplateValidationError, match="严格递减"):
            validate_thresholds(ConfidenceThresholds(
                full_add=65, half_add=65, small_add=40,
                full_reduce=60, half_reduce=40,
            ))

    def test_reduce_not_decreasing(self):
        # full_reduce 最低 50，half_reduce 最高 55 —— 构造 half > full 且都在范围内
        with pytest.raises(TemplateValidationError, match="严格递减"):
            validate_thresholds(ConfidenceThresholds(
                full_add=80, half_add=55, small_add=40,
                full_reduce=50, half_reduce=55,
            ))


class TestValidateGates:
    def test_default_pass(self):
        validate_gates(SafetyGates())

    def test_avg_distance_too_low(self):
        with pytest.raises(TemplateValidationError, match="min_avg_distance_pct"):
            validate_gates(SafetyGates(min_avg_distance_pct=0.5))

    def test_liq_distance_too_high(self):
        with pytest.raises(TemplateValidationError, match="min_liq_distance_pct"):
            validate_gates(SafetyGates(min_liq_distance_pct=40.0))

    def test_leverage_too_high(self):
        with pytest.raises(TemplateValidationError, match="max_eff_leverage"):
            validate_gates(SafetyGates(max_eff_leverage=50.0))


class TestValidateTemplate:
    def test_pyramid_decay_ratio_oob(self):
        tpl = builtin_templates()[2]  # pyramid
        tpl = tpl.model_copy(update={"pyramid_decay_ratio": 1.5})
        with pytest.raises(TemplateValidationError, match="pyramid_decay_ratio"):
            validate_template(tpl)

    def test_layered_pct_oob(self):
        tpl = builtin_templates()[1]  # li_fashi
        tpl = tpl.model_copy(update={"layered_pct_of_account": 0.50})
        with pytest.raises(TemplateValidationError, match="layered_pct_of_account"):
            validate_template(tpl)

    def test_margin_pct_oob(self):
        tpl = builtin_templates()[0]
        tpl = tpl.model_copy(update={"max_margin_pct_of_account": 0.80})
        with pytest.raises(TemplateValidationError, match="max_margin_pct_of_account"):
            validate_template(tpl)

    def test_trail_after_add_exceeds_max_add(self):
        tpl = builtin_templates()[3]  # conservative max_add_times=2
        tpl = tpl.model_copy(update={"trail_sl_after_add_n": 5})
        with pytest.raises(TemplateValidationError, match="trail_sl_after_add_n"):
            validate_template(tpl)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CRUD：derive / update / delete
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDeriveTemplate:
    def test_success(self):
        templates = builtin_templates()
        new = derive_template(templates, "fatzhai", "custom:my_fatzhai", "我的肥仔派")
        assert new.id == "custom:my_fatzhai"
        assert new.builtin is False
        assert new.name == "我的肥仔派"
        assert "派生自" in new.description
        # 配置字段继承
        assert new.add_mode == "passive_deleveraging"
        assert new.target_leverage == 10.0

    def test_missing_custom_prefix(self):
        templates = builtin_templates()
        with pytest.raises(TemplateValidationError, match="custom:"):
            derive_template(templates, "fatzhai", "my_one", "xx")

    def test_source_not_found(self):
        templates = builtin_templates()
        with pytest.raises(TemplateValidationError, match="源模板不存在"):
            derive_template(templates, "nope", "custom:x", "x")

    def test_duplicate_id(self):
        templates = builtin_templates()
        first = derive_template(templates, "fatzhai", "custom:x", "X1")
        templates.append(first)
        with pytest.raises(TemplateValidationError, match="已存在"):
            derive_template(templates, "fatzhai", "custom:x", "X2")


class TestUpdateTemplate:
    def test_builtin_rejected(self):
        templates = builtin_templates()
        with pytest.raises(TemplateValidationError, match="只读"):
            update_template(templates, "fatzhai", {"max_add_times": 5})

    def test_not_found(self):
        templates = builtin_templates()
        with pytest.raises(TemplateValidationError, match="模板不存在"):
            update_template(templates, "custom:none", {"name": "x"})

    def test_success(self):
        templates = builtin_templates()
        templates.append(derive_template(templates, "fatzhai", "custom:x", "X"))
        updated = update_template(templates, "custom:x", {"max_add_times": 4})
        assert updated.max_add_times == 4
        # 列表同步更新
        assert find_template(templates, "custom:x").max_add_times == 4

    def test_invalid_update_rejected(self):
        templates = builtin_templates()
        templates.append(derive_template(templates, "fatzhai", "custom:x", "X"))
        with pytest.raises(TemplateValidationError):
            update_template(templates, "custom:x", {
                "thresholds": ConfidenceThresholds(full_add=95)  # 越界
            })


class TestDeleteTemplate:
    def test_builtin_rejected(self):
        templates = builtin_templates()
        with pytest.raises(TemplateValidationError, match="不可删除"):
            delete_template(templates, "fatzhai")

    def test_not_found(self):
        templates = builtin_templates()
        with pytest.raises(TemplateValidationError, match="模板不存在"):
            delete_template(templates, "custom:none")

    def test_success(self):
        templates = builtin_templates()
        templates.append(derive_template(templates, "fatzhai", "custom:x", "X"))
        assert find_template(templates, "custom:x") is not None
        delete_template(templates, "custom:x")
        assert find_template(templates, "custom:x") is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# plan_from_template
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPlanFromTemplate:
    def test_fields_mapped(self):
        tpl = builtin_templates()[0]  # fatzhai
        plan = plan_from_template(tpl, "plan-1", "pos-1", name="BTC 大趋势")
        assert plan.id == "plan-1"
        assert plan.position_id == "pos-1"
        assert plan.template_id == "fatzhai"
        assert plan.name == "BTC 大趋势"
        assert plan.add_mode == "passive_deleveraging"
        assert plan.target_leverage == 10.0
        assert plan.max_add_times == tpl.max_add_times
        assert plan.add_triggers == tpl.default_add_triggers
        assert plan.reduce_signals == tpl.default_reduce_signals

    def test_overrides_applied(self):
        tpl = builtin_templates()[0]
        plan = plan_from_template(tpl, "plan-1", "pos-1", overrides={
            "max_add_times": 5,
            "trail_sl_atr_mult": 2.5,
        })
        assert plan.max_add_times == 5
        assert plan.trail_sl_atr_mult == 2.5

    def test_default_name_fallback(self):
        tpl = builtin_templates()[0]
        plan = plan_from_template(tpl, "plan-1", "pos-1")
        assert plan.name == tpl.name

    def test_deep_copy_of_mutables(self):
        """派生的 plan 修改不应影响 template 本身。"""
        tpl = builtin_templates()[0]
        original_triggers = list(tpl.default_add_triggers)
        plan = plan_from_template(tpl, "plan-1", "pos-1")
        plan.add_triggers.clear()
        assert tpl.default_add_triggers == original_triggers


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 持久化：save / load / bootstrap
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path):
        templates = builtin_templates()
        templates.append(derive_template(templates, "fatzhai", "custom:x", "自定义 X"))

        save_templates(str(tmp_path), templates)
        loaded = load_templates(str(tmp_path))

        assert len(loaded) == 5  # 4 builtin + 1 custom
        loaded_ids = {t.id for t in loaded}
        assert loaded_ids == {"fatzhai", "li_fashi", "pyramid", "conservative", "custom:x"}

    def test_load_missing_file_returns_builtins(self, tmp_path: Path):
        loaded = load_templates(str(tmp_path))
        assert len(loaded) == 4
        assert {t.id for t in loaded} == BUILTIN_TEMPLATE_IDS

    def test_load_corrupted_file_returns_builtins(self, tmp_path: Path):
        roll_dir = tmp_path / "roll"
        roll_dir.mkdir(parents=True)
        (roll_dir / "templates.json").write_text("{invalid json", encoding="utf-8")

        loaded = load_templates(str(tmp_path))
        assert len(loaded) == 4

    def test_load_refills_missing_builtin(self, tmp_path: Path):
        """磁盘上只存了 1 个 custom 模板（用户手动误删过）→ 自动补齐 4 个 builtin。"""
        roll_dir = tmp_path / "roll"
        roll_dir.mkdir(parents=True)
        custom = derive_template(builtin_templates(), "fatzhai", "custom:only", "X")
        payload = {
            "version": 1,
            "templates": [custom.model_dump()],
        }
        (roll_dir / "templates.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        loaded = load_templates(str(tmp_path))
        loaded_ids = {t.id for t in loaded}
        # 4 个 builtin 全部补回 + 保留 custom
        assert BUILTIN_TEMPLATE_IDS.issubset(loaded_ids)
        assert "custom:only" in loaded_ids

    def test_load_skips_corrupt_entry(self, tmp_path: Path):
        """单条记录反序列化失败不应影响其他条目。"""
        roll_dir = tmp_path / "roll"
        roll_dir.mkdir(parents=True)
        payload = {
            "version": 1,
            "templates": [
                {"id": "broken"},  # 缺必填字段
                builtin_templates()[0].model_dump(),
            ],
        }
        (roll_dir / "templates.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        loaded = load_templates(str(tmp_path))
        # broken 被跳过，fatzhai 保留，其他 3 个 builtin 补齐
        assert "broken" not in {t.id for t in loaded}
        assert "fatzhai" in {t.id for t in loaded}

    def test_bootstrap_first_run_writes_file(self, tmp_path: Path):
        path = tmp_path / "roll" / "templates.json"
        assert not path.exists()

        templates = bootstrap_templates(str(tmp_path))
        assert path.exists()
        assert len(templates) == 4

        # 再次调用 —— 幂等（不应报错且内容不变）
        templates2 = bootstrap_templates(str(tmp_path))
        assert len(templates2) == 4

    def test_save_atomic_tmp_file_cleanup(self, tmp_path: Path):
        """保存完毕后不应遗留 .tmp 文件。"""
        save_templates(str(tmp_path), builtin_templates())
        files = list((tmp_path / "roll").iterdir())
        assert any(f.name == "templates.json" for f in files)
        assert not any(f.name.endswith(".tmp") for f in files)
