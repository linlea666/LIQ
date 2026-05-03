"""策略注册中心 · SignalEngine 启动时构造单例

设计：
  - 与现有 polls / processors 的注册风格一致（手动 register，避免 magic auto-discover）
  - 提供 enabled_for(config, regime, horizon) 快速筛选可用策略
  - 不持有策略状态（策略本身应是无状态的纯函数式 detect）
"""

from __future__ import annotations

import logging
from typing import Optional

from models.scalp_signal import ScalpConfig, StrategyName

from processors.scalp_signal.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """策略注册表（多实例无意义，但不强制单例避免单测受限）"""

    def __init__(self) -> None:
        self._strategies: dict[StrategyName, BaseStrategy] = {}

    def register(self, strategy: BaseStrategy) -> None:
        """注册策略 · 同 name 重复 → 抛错（避免被覆盖式静默 bug）"""
        if strategy.name in self._strategies:
            raise ValueError(
                f"strategy already registered: {strategy.name.value}"
            )
        self._strategies[strategy.name] = strategy
        logger.info(
            "scalp strategy registered | name=%s display=%s regimes=%s horizons=%s",
            strategy.name.value, strategy.display_name,
            sorted(strategy.suitable_regimes), sorted(strategy.suitable_horizons),
        )

    def get(self, name: StrategyName) -> Optional[BaseStrategy]:
        return self._strategies.get(name)

    def all(self) -> list[BaseStrategy]:
        """返回所有已注册策略（顺序与注册顺序一致）"""
        return list(self._strategies.values())

    def names(self) -> list[StrategyName]:
        return list(self._strategies.keys())

    def enabled_for(
        self,
        config: ScalpConfig,
        regime: str,
        horizon: int,
    ) -> list[BaseStrategy]:
        """筛选当前 (regime, horizon) 下，配置开启 + 适用的策略

        过滤顺序：
            1) config.strategies[name].enabled = True
            2) strategy.is_applicable(regime, horizon)
            3) 未被 calibrator auto_disabled（auto_disabled 由 calibrator 持久化到 stats，
               此处不直接消费 stats，由调用方在更上层做 merge）
        """
        result: list[BaseStrategy] = []
        for name, strategy in self._strategies.items():
            sc = config.strategies.get(name)
            if sc is None or not sc.enabled:
                continue
            if not strategy.is_applicable(regime, horizon):
                continue
            result.append(strategy)
        return result

    def __len__(self) -> int:
        return len(self._strategies)

    def __contains__(self, name: StrategyName) -> bool:
        return name in self._strategies
