# Coinglass Probe Summary · 2026-04-28T09:32:19+00:00

> 本报告由 `backend/scripts/probe_coinglass.py` 自动生成。
> 用途：流动性墙引擎设计调研，不进生产链路。

## 总体
- 探测条数：10
- 成功：10
- 失败：0

## P0-拥挤(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `oi_aggregated_history_5m` | ✅ | 12977.3ms | list × 50 | OI 聚合历史（5m 颗粒，用于 oi_delta_5m） |

## P0-磁铁(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `liquidation_max_pain` | ✅ | 10802.7ms | list × 563 | 清算 max-pain · 全市场 list，无需 symbol |

## P0-行为(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `aggregated_cvd` | ✅ | 12941.3ms | list × 100 | 聚合 CVD 时间序列 |

## P1-拥挤(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `top_ls_position_ratio` | ✅ | 13481.0ms | list × 24 | 大户持仓多空比（需要 exchange+pair） |
| BTC | `net_position_v2` | ✅ | 12830.6ms | list × 25 | 净持仓 v2（需要 exchange+pair） |

## P1-磁铁(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `liquidation_aggregated_heatmap_m1` | ✅ | 13818.5ms | dict (keys: y_axis, liquidation_leverage_data, price_candlesticks, update_time) | 清算热力图 model 1 · 实测 range 必须为 24h/7d/30d |

## P3-鲸鱼(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `hyperliquid_whale_alert` | ✅ | 13073.3ms | list × 50 | Hyperliquid 鲸鱼成交告警 |
| BTC | `hyperliquid_whale_position` | ✅ | 13351.9ms | list × 861 | Hyperliquid 鲸鱼持仓快照 |

## 未接(GPT 提及)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `liquidation_aggregated_heatmap_m2` | ✅ | 26777.8ms | **空** | model 2 数据差异调研 |
| BTC | `liquidation_aggregated_heatmap_m3` | ✅ | 2265.9ms | dict (keys: y_axis, liquidation_leverage_data, price_candlesticks, update_time) | model 3 数据差异调研 |

