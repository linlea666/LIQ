# Coinglass Probe Summary · 2026-04-28T09:27:13+00:00

> 本报告由 `backend/scripts/probe_coinglass.py` 自动生成。
> 用途：流动性墙引擎设计调研，不进生产链路。

## 总体
- 探测条数：27
- 成功：23
- 失败：4

## P0-墙(已 poll 但 OP 未用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `orderbook_aggregated_ask_bids` | ✅ | 6527.6ms | list × 12 | 多交易所聚合的买卖盘 USD 总额时间序列 |

## P0-墙(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `orderbook_history` | ✅ | 7462.4ms | list × 12 | 5m 分价位深度热力图，OP 模块当前 limit=2，建议升级 limit=12 |
| BTC | `large_orders_current` | ✅ | 6981.1ms | list × 434 | 当前活跃的大额限价单 |
| BTC | `large_orders_history` | ✅ | 7098.2ms | list × 1000 | 大单生命周期历史（含 holding/ended 状态） |

## P0-拥挤(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `oi_aggregated_history_5m` | ✅ | 15781.2ms | **空** | OI 聚合历史（5m 颗粒，用于 oi_delta_5m） |
| BTC | `oi_aggregated_history_1h` | ✅ | 918.9ms | list × 24 | OI 聚合历史（1h 颗粒，用于 24h 累计 delta） |
| BTC | `oi_exchange_list` | ✅ | 6709.6ms | list × 25 | 各交易所 OI 排名（市场份额） |

## P0-磁铁(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `liquidation_aggregated_map_1d` | ✅ | 7091.1ms | dict (keys: code, data) | 1d 清算地图（簇） · 使用 raw_response=True |
| BTC | `liquidation_max_pain` | ❌ | 0.0ms | ? | TypeError: fetch_liquidation_max_pain() got an unexpected keyword argument 'symbol' |

## P0-行为(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `aggregated_taker` | ✅ | 6876.9ms | list × 24 | 聚合多所主动买卖成交量 |
| BTC | `aggregated_cvd` | ✅ | 22004.1ms | **空** | 聚合 CVD 时间序列 |

## P1-拥挤(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `funding_oi_weight_history` | ✅ | 6534.3ms | list × 90 | OI 加权 Funding 历史（用于百分位） |
| BTC | `global_ls_ratio` | ❌ | 0.0ms | ? | TypeError: fetch_global_ls_ratio_history() missing 1 required positional argument: 'exchange' |
| BTC | `top_ls_position_ratio` | ❌ | 0.0ms | ? | TypeError: fetch_top_ls_position_ratio_history() missing 1 required positional argument: 'exchange' |
| BTC | `net_position_v2` | ❌ | 0.0ms | ? | TypeError: fetch_net_position_v2_history() missing 1 required positional argument: 'exchange' |

## P1-磁铁(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `liquidation_aggregated_heatmap_m1` | ✅ | 6807.3ms | **空** | 清算热力图 model 1（杠杆密度） |

## P1-行为(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `footprint_history_5m` | ✅ | 474.1ms | list × 6 | 合约足迹图（吸收 / stacked imbalance 用） |

## P2-期现(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `spot_aggregated_taker` | ✅ | 6852.1ms | list × 24 | 现货聚合主动成交 |
| BTC | `spot_aggregated_cvd` | ✅ | 7082.8ms | list × 100 | 现货聚合 CVD |

## P3-鲸鱼(已用)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `hyperliquid_whale_alert` | ✅ | 21911.5ms | **空** | Hyperliquid 鲸鱼成交告警 |
| BTC | `hyperliquid_whale_position` | ✅ | 16415.1ms | **空** | Hyperliquid 鲸鱼持仓快照 |

## P3-鲸鱼(未接)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `hyperliquid_position_per_coin` | ✅ | 1303.5ms | dict (keys: total_pages, list, current_page) | Hyperliquid 单币种鲸鱼持仓详情 |

## 未接(GPT M4 候选)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `spot_orderbook_history` | ✅ | 6784.2ms | list × 6 | 现货 L2 深度（期现差异分析） |
| BTC | `spot_large_orders` | ✅ | 6964.6ms | list × 193 | 现货大单（看现货墙） |

## 未接(GPT 提及)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `liquidation_aggregated_heatmap_m2` | ✅ | 7005.5ms | **空** | model 2 数据差异调研 |
| BTC | `liquidation_aggregated_heatmap_m3` | ✅ | 22072.6ms | **空** | model 3 数据差异调研 |

## 未接(单所版)

| coin | endpoint | ok | latency | data | note |
|------|----------|----|---------|------|------|
| BTC | `orderbook_ask_bids_history_per_exchange` | ✅ | 6850.9ms | list × 12 | 单所版本，聚合版已可用，一般不需要 |

