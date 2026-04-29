"""W4-T1 阶段 4 · 止损扫单观察台（Sweep Watch）。

模块定位：
  把现有 PriceZone / wall_event lifecycle / KeyLevel retest / CVD divergence /
  active_attack_score 等数据**编排**成一个"双向博弈过程态"视图。

设计原则（用户原则 5/6）：
  - 不引入新数据源，全部从现有字段派生
  - 不修改 PriceZone / SetupCandidate schema
  - 与 OpportunityBoard 的关系是「过程态视角」vs「决策态视角」（不重复）

5 态机（waiting / approaching / in_sweep / swept_reclaiming / swept_continuing）
全部由 wall_event lifecycle + KeyLevel retest + active_attack_score 现有字段判定。

3 派生分（扫单吸引 / 反转潜力 / 延续风险）全部派生公式，不引入新存储字段。

可观测性：
  每次 build 产出完整 trace_log（步骤名 / inputs / output / 命中规则），
  供前端 SweepWatchTracePanel 实时展示 + archives/sweep_watch jsonl 落盘后验。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


SweepPhase = Literal[
    "waiting",            # ⚪ 距离 > 1.5%
    "approaching",        # 🟡 距离 ≤ 1.5%，未触发扫单
    "in_sweep",           # 🔴 最近 5min 内有 wall_consumed / 价格穿破
    "swept_reclaiming",   # 🟢 已扫 + 价格收回 + active_attack 下降
    "swept_continuing",   # 🟠 已扫 + 未收回 + CVD 同向继续
]
"""扫单阶段 5 态机（全部由现有字段派生）。"""


SweepSide = Literal["below", "above"]
"""下方多头止损带 / 上方空头止损带。"""


class SweepWatchTraceEntry(BaseModel):
    """单条算法运行轨迹（每个关键决策步骤都产出一条）。

    三个用途：
      1. 前端 SweepWatchTracePanel 展示运行过程
      2. archives/sweep_watch jsonl 落盘供后验"判定准确率"打分
      3. 调试时定位"为什么是这个 phase / 这个分数"

    不可变：每次 build_sweep_watch 调用都重新产出，不持久化在内存中。
    """

    ts_iso: str
    """触发时间戳（ISO 8601，便于人类阅读和后验脚本解析）。"""

    side: SweepSide
    """记录归属于上方（空头止损带）还是下方（多头止损带）。"""

    step: str
    """步骤名（如 select_representative / phase_decision / reversal_potential）。"""

    inputs: dict[str, Any] = Field(default_factory=dict)
    """输入字段名 → 值；可读的 JSON-safe 类型（float/str/bool/list/dict）。"""

    output: Any = None
    """步骤输出（标量 / 字符串 / 嵌套结构都允许）。"""

    rule_hit: Optional[str] = None
    """命中的判定规则名（如 wall_consumed_within_5min / closest_strong_role）。"""

    notes: str = ""
    """人类可读的简短说明，1 句话即可。"""


class SweepWatchSide(BaseModel):
    """单侧（下方多头止损带 / 上方空头止损带）的观察对象。"""

    direction: SweepSide
    """方向（below = 下方多头止损 / above = 上方空头止损）。"""

    label: str
    """展示用大标题（如「下方多头止损带」）。"""

    representative_zone_id: str
    """代表 zone（用于 hover 联动 PriceAxisMap / ZoneDetailCard）。"""

    representative_zone_label: str = ""
    """代表 zone 的人类可读标签（dominant_label，前端直接展示）。"""

    price_band: tuple[float, float]
    """价格区间 [low, high]，对应代表 zone 的 price_low/high。"""

    distance_pct: float
    """区间最近边到现价的距离百分比（带符号；下方为负、上方为正）。"""

    sweep_phase: SweepPhase
    """5 态机当前阶段。"""

    sweep_attractiveness: float = 0.0
    """扫单吸引（0-1）：直接复用 zone.sweep_attractiveness（不重打分）。"""

    reversal_potential: float = 0.0
    """反转潜力（0-1）：派生分（below 用 support_*；above 用 resistance_*）。
    公式 = 0.40 × strength + 0.20 × (1 - fragility) + 0.20 × data_confidence
         + 0.20 × cvd_against_score
    其中 cvd_against_score：below 侧 CVD 越上涨越反向 → 接近 1。"""

    continuation_risk: float = 0.0
    """延续风险（0-1）：派生分（below 用 support_*；above 用 resistance_*）。
    公式 = 0.35 × break_through_risk + 0.25 × sweep_attractiveness
         + 0.20 × cvd_alignment_score + 0.10 × (1 - data_confidence) + 0.10 × fragility
    其中 cvd_alignment_score：below 侧 CVD 越下跌越同向 → 接近 1。"""

    triggers: list[str] = Field(default_factory=list)
    """触发观察点（≤ 3 条，人类可读）。"""

    invalidations: list[str] = Field(default_factory=list)
    """失效条件（≤ 2 条，人类可读）。"""


class BrainSweepWatch(BaseModel):
    """阶段 4 主聚合对象：双向止损扫单观察。"""

    coin: str
    last_price: float
    ts_iso: str

    below: Optional[SweepWatchSide] = None
    """下方多头止损带 / 潜在低点观察（无强 zone 时 None，前端隐藏该栏）。"""

    above: Optional[SweepWatchSide] = None
    """上方空头止损带 / 潜在高点观察。"""

    trace_log: list[SweepWatchTraceEntry] = Field(default_factory=list)
    """完整运行轨迹（约 8-12 条 / 每次 build）。前端 trace 抽屉展示 + jsonl 落盘后验。"""
