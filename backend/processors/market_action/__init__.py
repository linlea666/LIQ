"""Market Action Analyzer · 市场动作分析器

模块职责：
- facts_collector: state → MarketActionFacts（AI 输入）
- footprint_analyzer: 原始 footprint buckets → FootprintBarStats
- price_context: swing / VP / 区间位置派生
- liq_cluster_analyzer: 清算图上下簇对比
- derived_labels: oi_price_coherence / funding_trend / spot_contract_coherence
- ai_arbiter: AI 编排（后续阶段）
"""
