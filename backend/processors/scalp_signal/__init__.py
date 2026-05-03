"""短线预测合约信号引擎 · 完全独立的二元方向预测闭环

模块职责：
  - 监听 state（只读），按周期生成"涨/跌"方向预测信号
  - 每条信号在 horizon_min 分钟后自动结算（命中 / 未中 / 平）
  - 全程 test_mode=True，零实盘下单
  - 为币安事件合约（赔率 0.8:1，临界胜率 55.56%）优化

模块边界（铁律）：
  - 只读 state，绝不修改任何上游字段
  - 不重新计算 KL / Wall / MAA / TradingBrain 评分
  - 信号 reference_price 仅做"参考价"，不构成下单建议

子模块：
  - base_strategy / strategy_registry：策略抽象层
  - regime_gate：根据 regime 决定是否允许出信号
  - mtf_bias：多周期偏置打分（消费 1h MAA + market_structure）
  - confidence_scorer：5 因子加权置信度
  - veto_gate：黑天鹅 / 数据陈旧 / 反向偏置等否决
  - signal_engine：主异步任务（生成 + 状态机 + 结算）
  - calibrator：命中率统计 + 自动停用
"""

from __future__ import annotations
