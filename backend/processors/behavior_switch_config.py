"""V3-M4 P0-6 · 行为评估 V1/V2 切换配置（仅状态层，路由器属 M4-1）。

═══════════════════════════════════════════════════════════════
设计目标
═══════════════════════════════════════════════════════════════
为 M4 阶段的"数据驱动 V1→V2 渐进切换"提供配置层。
当前阶段（M4 基础设施）：
    1. 仅记录每个维度的当前生效版本
    2. 默认全部 V1（生产链路 0 改动）
    3. 暴露给前端在 v1v2-compare 页顶部显示当前生效版本 chip
    4. 不实际路由信号（路由器留给 M4-1）

═══════════════════════════════════════════════════════════════
设计纪律
═══════════════════════════════════════════════════════════════
1. 进程内单例 dict（重启清零，重新读环境变量）；不读写文件
2. 由 M4-5 CLI（scripts/behavior_switch.py）通过环境变量 / 修改本文件
   触发 set_switch_state（运行时即时生效，无需重启）
3. 切换状态变化必须经过审计（M4-2）；本模块不直接审计，由调用方负责
4. 维度命名与 ComparisonStats.dimension 严格一致

═══════════════════════════════════════════════════════════════
支持的维度（与 ComparisonStats.dimension 对齐 + 1 个 break_depth）
═══════════════════════════════════════════════════════════════
- bounce_quality：反弹质量（V1=proactive/passive 死阈值；V2=z-score/percentile 0-1）
- breakout_stage：突破阶段（V1=固定时间窗；V2=按 timeframe 自适应）
- fake_break：    假破回收（V1=布尔事件；V2=0-1 强度连续）
- break_depth：   破位阈值（V1=cfg["break_depth_pct"] 0.3% 死阈值；V2=动态 ATR%）

每个维度独立切换；可同时切多个，可独立回退。
"""
from __future__ import annotations

import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

VersionTag = Literal["V1", "V2"]
DIMENSION_KEYS = ("bounce_quality", "breakout_stage", "fake_break", "break_depth")

# 进程内切换状态（默认全 V1；M4 数据稳定后由 CLI 切换）
_SWITCH_STATE: dict[str, str] = {dim: "V1" for dim in DIMENSION_KEYS}


def _read_env_overrides() -> None:
    """从环境变量读取覆盖（启动时一次；运行期由 set_switch_state 修改）。

    支持的环境变量（值为 "V1" 或 "V2"，大小写不敏感；其它值忽略）：
        BEHAVIOR_SWITCH_BOUNCE_QUALITY
        BEHAVIOR_SWITCH_BREAKOUT_STAGE
        BEHAVIOR_SWITCH_FAKE_BREAK
        BEHAVIOR_SWITCH_BREAK_DEPTH
    """
    for dim in DIMENSION_KEYS:
        env_key = f"BEHAVIOR_SWITCH_{dim.upper()}"
        val = (os.getenv(env_key) or "").strip().upper()
        if val in ("V1", "V2"):
            _SWITCH_STATE[dim] = val
            logger.info("[behavior_switch] %s = %s (from env)", dim, val)


# 模块加载时读一次（保持轻量；测试中可 reset_switch_state 重置）
_read_env_overrides()


def get_switch_state() -> dict[str, str]:
    """获取当前切换状态快照（不可变副本，避免外部误改）。"""
    return dict(_SWITCH_STATE)


def get_dimension_version(dimension: str) -> str:
    """获取单维度当前生效版本（默认 V1）。"""
    return _SWITCH_STATE.get(dimension, "V1")


def is_v2_active(dimension: str) -> bool:
    """快速判定该维度是否已切到 V2。"""
    return get_dimension_version(dimension) == "V2"


def set_switch_state(dimension: str, version: str) -> str:
    """设置单维度的切换版本（M4-5 CLI 使用）。

    Returns:
        变更前的版本（用于审计回退）
    Raises:
        ValueError: dimension 未知或 version 非 V1/V2
    """
    if dimension not in DIMENSION_KEYS:
        raise ValueError(f"未知维度：{dimension}（合法：{DIMENSION_KEYS}）")
    version_u = version.strip().upper()
    if version_u not in ("V1", "V2"):
        raise ValueError(f"version 必须是 V1 或 V2，实际：{version}")
    prev = _SWITCH_STATE[dimension]
    _SWITCH_STATE[dimension] = version_u
    if prev != version_u:
        logger.warning(
            "[behavior_switch] %s: %s → %s（运行期切换）",
            dimension, prev, version_u,
        )
    return prev


def reset_switch_state() -> None:
    """重置全部维度为 V1（仅供测试；生产不应调用）。"""
    for dim in DIMENSION_KEYS:
        _SWITCH_STATE[dim] = "V1"
