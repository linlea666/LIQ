"""数据质量评估。

这个模块存在的理由，是防住比"程序崩溃"危险得多的一种失效：
**程序一直正常运行，但某个字段解析错了 / 某个接口静默过期，
于是接下来几小时发出一堆错误的 S1。**

因此它做三件事：
  1. 按字段组判定新鲜度——聪明钱接口挂了 20 分钟，只降低聪明钱相关可信度，
     而不是一刀切把整个代币标成不可信。
  2. 跨源交叉校验——用 price × circulating_supply 反算市值，
     与接口上报的市值比对，偏离过大即视为冲突。
  3. 把结论变成**硬闸门**：质量不达标直接禁止晋升 S1/S2。
     只在前端标个黄点是不够的，必须真实影响决策。
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import FieldGroup, QualityReport, TokenView

# 核心字段：缺失即代表这枚币基本不可评估
_CORE_FIELDS: tuple[str, ...] = ("price", "market_cap", "liquidity")


class QualityEvaluator:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self._freshness: dict[str, dict[str, float]] = {
            name: {
                "fresh_sec": float(cfg.get("fresh_sec", 600)),
                "stale_sec": float(cfg.get("stale_sec", 1800)),
            }
            for name, cfg in (config.get("freshness", {}) or {}).items()
        }
        self._missing_penalty: dict[str, float] = {
            k: float(v) for k, v in (config.get("missing_penalty", {}) or {}).items()
        }
        self._stale_penalty: dict[str, float] = {
            k: float(v) for k, v in (config.get("stale_penalty", {}) or {}).items()
        }
        self._mc_warn = float(config.get("mc_deviation_warn", 0.15))
        self._mc_conflict = float(config.get("mc_deviation_conflict", 0.35))
        self._mc_penalty = float(config.get("mc_conflict_penalty", 15))
        self._min_for_s1 = float(config.get("min_for_s1", 55))
        self._min_for_s2 = float(config.get("min_for_s2", 70))

    def evaluate(self, view: TokenView, now_ms: int) -> QualityReport:
        penalties: dict[str, float] = {}
        stale_groups: list[str] = []
        missing_groups: list[str] = []
        conflicts: list[str] = []

        for group in FieldGroup:
            name = group.value
            age = view.group_age_sec(group, now_ms)
            if age is None:
                # 该组从未有过任何数据
                penalty = self._missing_penalty.get(name, 5.0)
                if penalty > 0:
                    penalties[f"missing_{name}"] = penalty
                missing_groups.append(name)
                continue

            thresholds = self._freshness.get(name)
            if not thresholds:
                continue
            fresh_sec = thresholds["fresh_sec"]
            stale_sec = max(thresholds["stale_sec"], fresh_sec + 1)
            if age <= fresh_sec:
                continue

            # 在 fresh → stale 之间线性升高罚分，超过 stale 后取满罚
            max_penalty = self._stale_penalty.get(name, 5.0)
            ratio = min(1.0, (age - fresh_sec) / (stale_sec - fresh_sec))
            penalty = max_penalty * ratio
            if penalty > 0.5:
                penalties[f"stale_{name}"] = penalty
            if age > stale_sec:
                stale_groups.append(name)

        # 核心字段缺失单独重罚：market 组"有数据"但 price 为 None 也要罚
        missing_core = [f for f in _CORE_FIELDS if view.get(f) is None]
        if missing_core:
            penalties["missing_core_fields"] = 12.0 * len(missing_core)
            conflicts.extend(f"核心字段缺失: {f}" for f in missing_core)

        computed_mc, deviation = self._cross_check_market_cap(view)
        if deviation is not None:
            if deviation >= self._mc_conflict:
                penalties["mc_conflict"] = self._mc_penalty
                conflicts.append(
                    f"市值交叉校验冲突: 上报与反算偏离 {deviation:.1%}"
                )
            elif deviation >= self._mc_warn:
                penalties["mc_deviation"] = self._mc_penalty * 0.4
                conflicts.append(f"市值偏离 {deviation:.1%}")

        # 观测次数过少时数据本身不足以支撑判断（与 Confidence 的区别见下）
        if view.observation_count < 2:
            penalties["insufficient_observations"] = 10.0

        score = max(0.0, min(100.0, 100.0 - sum(penalties.values())))

        return QualityReport(
            score=score,
            stale_groups=tuple(stale_groups),
            missing_groups=tuple(missing_groups),
            conflicts=tuple(conflicts[:8]),
            penalties=penalties,
            mc_deviation_ratio=deviation,
            computed_market_cap=computed_mc,
            block_s1=score < self._min_for_s1,
            block_s2=score < self._min_for_s2,
        )

    @staticmethod
    def _cross_check_market_cap(view: TokenView) -> tuple[float | None, float | None]:
        """用 price × circulating_supply 反算市值并与上报值比对。

        流通量缺失时回退到总量：对绝大多数 Meme 币两者相同
        （一次性铸造、无锁仓），但这是回退而非等价，因此偏离阈值
        本身留了较宽的容忍度。
        """
        price = view.getf("price")
        supply = view.getf("circulating_supply") or view.getf("total_supply")
        reported = view.getf("market_cap")
        if price is None or supply is None or supply <= 0:
            return None, None
        computed = price * supply
        if reported is None or reported <= 0:
            return computed, None
        deviation = abs(computed - reported) / max(reported, 1e-9)
        return computed, deviation


def best_market_cap(view: TokenView, quality: QualityReport) -> tuple[float | None, str]:
    """选择用于里程碑与 Outcome 的市值，并返回口径来源。

    优先用接口上报值（币安口径与前端展示一致，便于人工核对）；
    只有在上报值缺失时才用反算值。**绝不静默混用**——
    里程碑记录里必须写清当时用的是哪个口径，否则
    "$100K 里程碑"在不同代币间根本不可比。
    """
    reported = view.getf("market_cap")
    if reported is not None:
        return reported, "reported"
    if quality.computed_market_cap is not None:
        return quality.computed_market_cap, "computed"
    return None, "unknown"
