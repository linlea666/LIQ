"""滚仓策略模板管理 —— 4 套预置 + 用户自定义

职责：
  - 定义 4 套预置模板（fatzhai / li_fashi / pyramid / conservative）
  - 加载 / 保存 / 派生 / 删除 / 校验模板
  - 模板派生新计划（作为 RollPlan 的配置原型）

持久化：
  backend/data/roll/templates.json
    结构：{"version": 1, "templates": [RollTemplate, ...]}
  首次启动时若文件不存在 → 自动写入 4 个预置模板
  预置模板（builtin=True）只读，用户不可删不可改；修改时引导"派生副本"

校验规则（超出范围直接拒绝，不静默夹紧）：
  - full_add ∈ [65, 85]
  - half_add ∈ [45, 65]
  - small_add ∈ [25, 45]
  - 必须严格递减：small_add < half_add < full_add
  - full_reduce ∈ [50, 75]
  - half_reduce ∈ [30, 55]
  - half_reduce < full_reduce
  - gates.min_avg_distance_pct ∈ [1.0, 8.0]
  - gates.min_liq_distance_pct ∈ [5.0, 30.0]
  - gates.max_eff_leverage ∈ [2.0, 30.0]
  - max_margin_pct_of_account ∈ [0.05, 0.50]
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from models.roll_position import (
    AddTrigger,
    ConfidenceThresholds,
    ReduceSignal,
    RollPlan,
    RollTemplate,
    SafetyGates,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 模板校验 —— 防止用户自定义走极端
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TemplateValidationError(ValueError):
    """策略模板参数越界 / 非法时抛出，由调用方显式捕获并提示用户。"""
    pass


_THRESHOLD_RANGES = {
    "full_add":    (65.0, 85.0),
    "half_add":    (45.0, 65.0),
    "small_add":   (25.0, 45.0),
    "full_reduce": (50.0, 75.0),
    "half_reduce": (30.0, 55.0),
}

_GATE_RANGES = {
    "min_avg_distance_pct": (1.0, 8.0),
    "min_liq_distance_pct": (5.0, 30.0),
    "max_eff_leverage":     (2.0, 30.0),
    "min_add_margin_usd":   (1.0, 1000.0),
}

_MARGIN_PCT_RANGE = (0.05, 0.50)


def validate_thresholds(t: ConfidenceThresholds) -> None:
    """校验置信度阈值是否在允许范围且严格递减。"""
    for key, (lo, hi) in _THRESHOLD_RANGES.items():
        val = getattr(t, key)
        if not (lo <= val <= hi):
            raise TemplateValidationError(
                f"{key}={val} 超出允许范围 [{lo}, {hi}]"
            )
    if not (t.small_add < t.half_add < t.full_add):
        raise TemplateValidationError(
            f"加仓阈值必须严格递减: small({t.small_add}) < half({t.half_add}) < full({t.full_add})"
        )
    if not (t.half_reduce < t.full_reduce):
        raise TemplateValidationError(
            f"减仓阈值必须严格递减: half({t.half_reduce}) < full({t.full_reduce})"
        )


def validate_gates(g: SafetyGates) -> None:
    """校验三道闸门参数是否在允许范围。"""
    for key, (lo, hi) in _GATE_RANGES.items():
        val = getattr(g, key)
        if not (lo <= val <= hi):
            raise TemplateValidationError(
                f"gates.{key}={val} 超出允许范围 [{lo}, {hi}]"
            )


def validate_template(tpl: RollTemplate) -> None:
    """完整校验策略模板（新建/更新前调用）。"""
    validate_thresholds(tpl.thresholds)
    validate_gates(tpl.gates)

    lo, hi = _MARGIN_PCT_RANGE
    if not (lo <= tpl.max_margin_pct_of_account <= hi):
        raise TemplateValidationError(
            f"max_margin_pct_of_account={tpl.max_margin_pct_of_account} 超出 [{lo}, {hi}]"
        )

    # 加仓模式的特定参数合理性
    if tpl.add_mode == "pyramid_decay":
        if not (0.1 <= tpl.pyramid_decay_ratio <= 0.95):
            raise TemplateValidationError(
                f"pyramid_decay_ratio={tpl.pyramid_decay_ratio} 应在 [0.1, 0.95]"
            )
    if tpl.add_mode == "layered_independent":
        if not (0.01 <= tpl.layered_pct_of_account <= 0.30):
            raise TemplateValidationError(
                f"layered_pct_of_account={tpl.layered_pct_of_account} 应在 [0.01, 0.30]"
            )
    if tpl.add_mode == "fixed_ratio":
        if not (0.05 <= tpl.fixed_ratio_of_position <= 0.50):
            raise TemplateValidationError(
                f"fixed_ratio_of_position={tpl.fixed_ratio_of_position} 应在 [0.05, 0.50]"
            )
    if tpl.add_mode == "passive_deleveraging":
        if not (1.1 <= tpl.target_leverage <= 30.0):
            raise TemplateValidationError(
                f"target_leverage={tpl.target_leverage} 应在 [1.1, 30.0]"
            )

    if not (1 <= tpl.max_add_times <= 10):
        raise TemplateValidationError(f"max_add_times={tpl.max_add_times} 应在 [1, 10]")

    if not (1 <= tpl.trail_sl_after_add_n <= tpl.max_add_times):
        raise TemplateValidationError(
            f"trail_sl_after_add_n={tpl.trail_sl_after_add_n} 应在 [1, max_add_times]"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4 套预置模板
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 共用减仓信号集（各模板可选择性覆盖）
_DEFAULT_REDUCE_SIGNALS: list[ReduceSignal] = [
    "long_upper_wick",
    "long_lower_wick",
    "cvd_bear_div",
    "cvd_bull_div",
    "sweep_fail_to_hold",
    "exhaustion_warn",
    "fake_break",
    "structure_choch_against",
    "reversal_pattern",
]


def _fatzhai_template() -> RollTemplate:
    """肥仔派 —— 浮盈再投入 · 复利滚趋势

    定位：趋势期的进攻型滚仓，让浮盈继续滚大。
    核心：用已实现的浮盈作为新加仓的保证金，维持名义杠杆。
    """
    return RollTemplate(
        id="fatzhai",
        name="肥仔派 · 浮盈再投入",
        description=(
            "趋势行情中，用浮盈作为新加仓的保证金，维持原名义杠杆。"
            "加仓量随浮盈自然递减，均价压缩可控。适合资金中大 + 趋势明确。"
        ),
        builtin=True,
        add_mode="passive_deleveraging",
        target_leverage=10.0,
        default_add_triggers=[
            "structure_breakout_retest",
            "key_level_bounce",
            "ema_pullback_reclaim",
        ],
        min_profit_pct_to_add=5.0,
        max_add_times=3,
        default_reduce_signals=_DEFAULT_REDUCE_SIGNALS,
        reduce_step_size_pct=0.30,
        trail_sl_after_add_n=1,
        trail_sl_atr_mult=1.5,
        gates=SafetyGates(
            min_avg_distance_pct=2.5,
            min_liq_distance_pct=10.0,
            max_eff_leverage=12.0,
            min_add_margin_usd=10.0,
            min_add_bar_distance_atr=1.0,
        ),
        thresholds=ConfidenceThresholds(
            full_add=80, half_add=55, small_add=40,
            full_reduce=60, half_reduce=40,
        ),
        max_margin_pct_of_account=0.30,
        recommended_margin_mode="cross",
        created_at=0,
    )


def _li_fashi_template() -> RollTemplate:
    """李法师派 —— 分层独立 · 小资金逐仓激进

    定位：小资金起家，用逐仓隔离风险，每次新开"独立仓"。
    核心：每次加仓金额固定为账户总额的 10%（用户可调），不合并均价视角。
          每仓独立 2% 止损（紧），错了就走，对了继续分层加。
    """
    return RollTemplate(
        id="li_fashi",
        name="李法师派 · 分层独立",
        description=(
            "逐仓模式，每次加仓用账户总额的固定比例（默认10%），"
            "各仓独立视角、各自 2% 止损。适合小资金积极进攻。"
        ),
        builtin=True,
        add_mode="layered_independent",
        layered_pct_of_account=0.10,
        default_add_triggers=[
            "float_profit_pct",
            "key_level_bounce",
            "structure_breakout_retest",
        ],
        min_profit_pct_to_add=15.0,         # 需要更高浮盈才触发（每仓 2% 止损 + 10x = 20% 保证金）
        max_add_times=5,
        default_reduce_signals=[
            # 李法师派不主动减仓，只靠每仓独立止损
            # 保留最关键的结构反向信号作为离场兜底
            "structure_choch_against",
            "exhaustion_warn",
        ],
        reduce_step_size_pct=0.20,
        trail_sl_after_add_n=2,              # 李法师派容忍度高
        trail_sl_atr_mult=2.0,
        gates=SafetyGates(
            min_avg_distance_pct=2.0,        # 独立仓视角下均价约束相对松
            min_liq_distance_pct=12.0,       # 但爆仓距离要严
            max_eff_leverage=10.0,
            min_add_margin_usd=10.0,
            min_add_bar_distance_atr=0.8,
        ),
        thresholds=ConfidenceThresholds(
            full_add=75, half_add=55, small_add=40,
            full_reduce=65, half_reduce=45,
        ),
        max_margin_pct_of_account=0.50,     # 独立仓总额上限放宽
        recommended_margin_mode="isolated",
        created_at=0,
    )


def _pyramid_template() -> RollTemplate:
    """通用金字塔 —— 越加越少 · 系统化保守

    定位：不认任何门派，只做数学上的"越加越少"，任何行情都偏保守。
    核心：每次加仓 = 上次 × 0.6，即使连加 3 次总加仓也不会比初始仓大。
    """
    return RollTemplate(
        id="pyramid",
        name="金字塔 · 越加越少",
        description=(
            "每次加仓金额按衰减比例（默认0.6）递减，天然防止均价被过度压缩。"
            "通用、系统化、保守。适合没有明确派系偏好的交易者。"
        ),
        builtin=True,
        add_mode="pyramid_decay",
        pyramid_decay_ratio=0.6,
        default_add_triggers=[
            "structure_breakout_retest",
            "key_level_bounce",
            "squeeze_release",
        ],
        min_profit_pct_to_add=5.0,
        max_add_times=3,
        default_reduce_signals=_DEFAULT_REDUCE_SIGNALS,
        reduce_step_size_pct=0.30,
        trail_sl_after_add_n=1,
        trail_sl_atr_mult=1.5,
        gates=SafetyGates(
            min_avg_distance_pct=3.0,
            min_liq_distance_pct=15.0,
            max_eff_leverage=8.0,
            min_add_margin_usd=10.0,
            min_add_bar_distance_atr=1.5,
        ),
        thresholds=ConfidenceThresholds(
            full_add=80, half_add=60, small_add=45,
            full_reduce=55, half_reduce=40,
        ),
        max_margin_pct_of_account=0.25,
        recommended_margin_mode="isolated",
        created_at=0,
    )


def _conservative_template() -> RollTemplate:
    """保守派 —— 极致防御 · 小资金试水

    定位：专门给滚仓新手/小资金/谨慎派设计的入门模板。
    核心：最严闸门 + 最低阈值 + 单次加仓 + 快速保本。
    """
    return RollTemplate(
        id="conservative",
        name="保守派 · 极致防御",
        description=(
            "最严格的闸门 + 最低的资金占用 + 单次加仓上限。适合滚仓新手试水。"
            "宁可错过机会也不让均价被压缩。"
        ),
        builtin=True,
        add_mode="pyramid_decay",
        pyramid_decay_ratio=0.5,
        default_add_triggers=[
            "structure_breakout_retest",
            "key_level_bounce",
        ],
        min_profit_pct_to_add=8.0,
        max_add_times=2,                     # 最多只加 2 次
        default_reduce_signals=_DEFAULT_REDUCE_SIGNALS + ["volume_stall_at_extreme"],
        reduce_step_size_pct=0.40,          # 减仓更激进
        trail_sl_after_add_n=1,             # 第一次加仓立刻挪保本
        trail_sl_atr_mult=1.2,
        gates=SafetyGates(
            min_avg_distance_pct=4.0,
            min_liq_distance_pct=18.0,
            max_eff_leverage=6.0,
            min_add_margin_usd=10.0,
            min_add_bar_distance_atr=2.0,
        ),
        thresholds=ConfidenceThresholds(
            full_add=82, half_add=62, small_add=45,
            full_reduce=55, half_reduce=38,
        ),
        max_margin_pct_of_account=0.15,
        recommended_margin_mode="isolated",
        created_at=0,
    )


def builtin_templates() -> list[RollTemplate]:
    """返回所有预置模板（每次调用生成新实例，避免共享可变状态）。"""
    return [
        _fatzhai_template(),
        _li_fashi_template(),
        _pyramid_template(),
        _conservative_template(),
    ]


BUILTIN_TEMPLATE_IDS = {"fatzhai", "li_fashi", "pyramid", "conservative"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 持久化：加载 / 保存
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STORAGE_VERSION = 1


def get_storage_path(data_dir: str) -> Path:
    return Path(data_dir) / "roll" / "templates.json"


def load_templates(data_dir: str) -> list[RollTemplate]:
    """从磁盘加载模板。首次运行 / 文件不存在 / 损坏 → 返回 builtin_templates。

    对 JSON 做容错：单条模板反序列化失败时跳过记录，不中断整体加载。
    """
    path = get_storage_path(data_dir)
    if not path.exists():
        return builtin_templates()

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return builtin_templates()

    if not isinstance(raw, dict) or "templates" not in raw:
        return builtin_templates()

    result: list[RollTemplate] = []
    seen_ids: set[str] = set()
    for item in raw.get("templates", []):
        try:
            tpl = RollTemplate(**item)
        except Exception:
            continue
        if tpl.id in seen_ids:
            continue
        seen_ids.add(tpl.id)
        result.append(tpl)

    # 补齐缺失的 builtin（用户可能删过旧文件，应该永远能拿到 4 个预置）
    for builtin in builtin_templates():
        if builtin.id not in seen_ids:
            result.append(builtin)

    return result


def save_templates(data_dir: str, templates: list[RollTemplate]) -> None:
    """原子写入模板列表（临时文件 rename 保证不会损坏）。"""
    path = get_storage_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": STORAGE_VERSION,
        "updated_at": int(time.time()),
        "templates": [t.model_dump() for t in templates],
    }

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CRUD 操作（业务层）—— 不直接操作磁盘，由 api/roll_position.py 调用
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def find_template(templates: list[RollTemplate], template_id: str) -> Optional[RollTemplate]:
    for t in templates:
        if t.id == template_id:
            return t
    return None


def derive_template(
    templates: list[RollTemplate],
    source_id: str,
    new_id: str,
    new_name: str,
) -> RollTemplate:
    """从现有模板派生一份新模板（用户自定义的起点）。

    约束：
      - source_id 必须存在
      - new_id 必须不存在 且 不能与 builtin 重名
      - new_id 必须以 "custom:" 前缀（防止和 builtin 混淆）

    返回派生后的新 RollTemplate（builtin=False），调用方负责 append + save。
    """
    source = find_template(templates, source_id)
    if source is None:
        raise TemplateValidationError(f"源模板不存在: {source_id}")

    if not new_id.startswith("custom:"):
        raise TemplateValidationError('自定义模板 id 必须以 "custom:" 前缀')
    if new_id in BUILTIN_TEMPLATE_IDS:
        raise TemplateValidationError(f"id 与预置模板冲突: {new_id}")
    if find_template(templates, new_id) is not None:
        raise TemplateValidationError(f"id 已存在: {new_id}")

    # 深拷贝配置，重置元信息
    new_tpl = source.model_copy(update={
        "id": new_id,
        "name": new_name,
        "description": f"派生自 {source.name}",
        "builtin": False,
        "created_at": int(time.time()),
    })
    return new_tpl


def update_template(
    templates: list[RollTemplate],
    template_id: str,
    patch: dict,
) -> RollTemplate:
    """更新自定义模板（builtin 拒绝更新）。

    patch 只包含要修改的字段，其他字段保留原值。
    更新后会走完整校验，不通过抛 TemplateValidationError。
    """
    idx = _index_of(templates, template_id)
    existing = templates[idx]

    if existing.builtin:
        raise TemplateValidationError(f"预置模板 {template_id} 只读，请先派生副本")

    updated = existing.model_copy(update=patch)
    validate_template(updated)
    templates[idx] = updated
    return updated


def delete_template(
    templates: list[RollTemplate],
    template_id: str,
) -> None:
    """删除自定义模板（builtin 拒绝删除）。就地修改 list。"""
    idx = _index_of(templates, template_id)
    if templates[idx].builtin:
        raise TemplateValidationError(f"预置模板 {template_id} 不可删除")
    templates.pop(idx)


def _index_of(templates: list[RollTemplate], template_id: str) -> int:
    for i, t in enumerate(templates):
        if t.id == template_id:
            return i
    raise TemplateValidationError(f"模板不存在: {template_id}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 从模板派生 RollPlan（建仓时使用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def plan_from_template(
    template: RollTemplate,
    plan_id: str,
    position_id: str,
    name: str = "",
    overrides: Optional[dict] = None,
) -> RollPlan:
    """用模板配置派生一个新的 RollPlan。

    overrides 用于用户在新建页面微调默认参数（不改模板本体）。
    """
    base_kwargs = dict(
        id=plan_id,
        position_id=position_id,
        name=name or template.name,
        template_id=template.id,
        add_mode=template.add_mode,
        target_leverage=template.target_leverage,
        pyramid_decay_ratio=template.pyramid_decay_ratio,
        layered_pct_of_account=template.layered_pct_of_account,
        fixed_ratio_of_position=template.fixed_ratio_of_position,
        add_triggers=list(template.default_add_triggers),
        min_profit_pct_to_add=template.min_profit_pct_to_add,
        max_add_times=template.max_add_times,
        reduce_signals=list(template.default_reduce_signals),
        reduce_step_size_pct=template.reduce_step_size_pct,
        trail_sl_after_add_n=template.trail_sl_after_add_n,
        trail_sl_atr_mult=template.trail_sl_atr_mult,
        gates=template.gates.model_copy(),
        thresholds=template.thresholds.model_copy(),
        max_margin_pct_of_account=template.max_margin_pct_of_account,
        active=True,
        created_at=int(time.time()),
    )
    if overrides:
        base_kwargs.update(overrides)

    return RollPlan(**base_kwargs)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 初始化辅助：启动时确保 templates.json 存在
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def bootstrap_templates(data_dir: str) -> list[RollTemplate]:
    """启动时调用：确保 templates.json 存在且包含 4 个预置模板。

    返回加载后的完整模板列表。
    """
    path = get_storage_path(data_dir)
    if not path.exists():
        templates = builtin_templates()
        save_templates(data_dir, templates)
        return templates

    templates = load_templates(data_dir)
    # 如果 load 后补齐了预置（说明磁盘上有缺失），重新落盘
    existing_ids = {t.id for t in templates}
    builtin_ids = {t.id for t in builtin_templates()}
    if not builtin_ids.issubset(existing_ids):
        # load_templates 已经补齐过了，这里只是保证落盘一致
        save_templates(data_dir, templates)

    return templates
