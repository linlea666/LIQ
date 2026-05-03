"""信号冲突解决器（P0-6）

为什么需要 ConflictResolver？
  GPT 审查 #4 + 自审报告：单 tick 内多策略可能输出方向冲突或同向竞争的信号。
  如不显式解决，会出现：
    1) 同 coin × 同 horizon 同时进多空（必然 50% 输 → 净亏 stake×0.2）
    2) 多策略给出同方向信号但 confidence 差距巨大 → 弱信号污染统计
  
  这是币安事件合约结构性风险（赔率 0.8:1），必须在生成前拦下。

规则（按依赖顺序）：
  R1 反向冲突解决：
     - 单 tick 内同 (coin, horizon) 出现 up & down 候选
     - 取 confidence 最高者，弱方记 reject_reason="conflict_opposite_direction_lower"
     - 若 top-up 与 top-down 的 confidence 差距 < 5 → 全部拒绝
       reason="conflict_unresolved（双方信号强度接近）"
       
  R2 同向竞争解决：
     - 同方向多策略时，取 confidence 最高者；其余记 reject_reason="conflict_same_direction_lower"
     - 这样统计仍能反映真实独立性（一个 tick 一个信号 / 方向）

设计原则：
  - 纯函数 resolve(candidates) → resolved；零副作用
  - reject_reason 进入 evidence 链路（透明性）
  - dev-constraints #3：与现有任何"opportunity 冲突"逻辑无关，独立新写
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# 反向冲突全拒阈值：top-up 与 top-down confidence 差距 < 此值 → 都拒
OPPOSITE_TIE_THRESHOLD = 5


@dataclass
class CandidateBundle:
    """单策略当前 tick 通过 veto + threshold 的候选 + 评分"""
    strategy_name: str
    direction: str          # "up" / "down"
    confidence: int
    candidate: object       # StrategyCandidate
    scoring: object         # ScoringResult


@dataclass
class ResolvedCandidate:
    """冲突解决后单条候选的最终结果"""
    bundle: CandidateBundle
    accepted: bool
    reject_reason: Optional[str] = None       # accepted=False 时填


@dataclass
class ResolutionReport:
    """resolve 的整体报告 · 用于日志/单测"""
    accepted: list[ResolvedCandidate] = field(default_factory=list)
    rejected: list[ResolvedCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def resolve(
    candidates: list[CandidateBundle],
    *,
    opposite_tie_threshold: int = OPPOSITE_TIE_THRESHOLD,
) -> ResolutionReport:
    """解决候选间的冲突 · 输入同 (coin, horizon) 的全部候选

    Args:
        candidates: 单 tick 单 (coin, horizon) 所有通过 veto+threshold 的候选
        opposite_tie_threshold: 反向冲突时 top-up vs top-down confidence 平局阈值

    Returns:
        ResolutionReport
    """
    report = ResolutionReport()
    if not candidates:
        return report

    if len(candidates) == 1:
        report.accepted.append(ResolvedCandidate(bundle=candidates[0], accepted=True))
        return report

    # 1) 拆分 up / down
    ups = sorted(
        [c for c in candidates if c.direction == "up"],
        key=lambda c: -c.confidence,
    )
    downs = sorted(
        [c for c in candidates if c.direction == "down"],
        key=lambda c: -c.confidence,
    )

    # 2) R1: 反向冲突
    if ups and downs:
        top_up = ups[0]
        top_down = downs[0]
        diff = abs(top_up.confidence - top_down.confidence)
        if diff < opposite_tie_threshold:
            # 双方信号强度接近 → 全拒
            note = (
                f"R1 反向冲突全拒（top_up={top_up.strategy_name}@{top_up.confidence} vs "
                f"top_down={top_down.strategy_name}@{top_down.confidence}, diff={diff} < {opposite_tie_threshold}）"
            )
            report.notes.append(note)
            for c in ups + downs:
                report.rejected.append(
                    ResolvedCandidate(
                        bundle=c, accepted=False,
                        reject_reason="conflict_opposite_direction_unresolved",
                    )
                )
            return report
        # 否则保留强方向；弱方向全拒
        if top_up.confidence > top_down.confidence:
            losing_dir = "down"
            losing_list = downs
            ups_keep = ups
        else:
            losing_dir = "up"
            losing_list = ups
            ups_keep = downs  # 注意：这里 ups_keep 是名义"获胜方向"列表
        report.notes.append(
            f"R1 反向冲突解决：保留 {ups_keep[0].direction} 方向"
            f"（{ups_keep[0].strategy_name}@{ups_keep[0].confidence}），"
            f"舍弃 {losing_dir} 方向 {len(losing_list)} 条"
        )
        for c in losing_list:
            report.rejected.append(
                ResolvedCandidate(
                    bundle=c, accepted=False,
                    reject_reason="conflict_opposite_direction_lower",
                )
            )
        winning = ups_keep
    else:
        winning = ups or downs

    # 3) R2: 同向竞争
    if len(winning) == 1:
        report.accepted.append(ResolvedCandidate(bundle=winning[0], accepted=True))
        return report

    top = winning[0]
    report.accepted.append(ResolvedCandidate(bundle=top, accepted=True))
    for c in winning[1:]:
        report.rejected.append(
            ResolvedCandidate(
                bundle=c, accepted=False,
                reject_reason="conflict_same_direction_lower",
            )
        )
    report.notes.append(
        f"R2 同向竞争解决：保留 {top.strategy_name}@{top.confidence}，"
        f"舍弃同向较弱 {len(winning) - 1} 条"
    )
    return report
