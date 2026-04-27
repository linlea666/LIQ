# NOFX 外部 AI 决策接口契约 (Schema v1.0.1)

> **v1.0.1 变更说明（向后兼容）**
> - `candles / cvd.last_point.ts / whale.hl_alerts_recent[].ts / whale.top_transfers[].ts / oi.history_30pts[].ts` 统一归一化为 **unix 秒**（此前个别源头会混入毫秒）
> - `options.expiries[].put_call_ratio` 与 `options.put_call_oi_ratio` 在源头为 0 时，按 `put_oi / call_oi` 自动补算
> - `whale.top_transfers` 会过滤掉上游返回的"空壳" transfer（`ts=0 && amount_usd=0`）；同时新增 `whale.transfers_count_raw` 字段用于观测过滤前数量
> - 纯增量、无字段删除/改名/改类型

> 给 NOFX 项目（3 分钟决策周期）提供**完整原始行情快照**的专用接口。
> 所有"已下结论"的加工字段（动能衰竭 / 市场结构 / 决策 / 评分等）**已剔除**，
> 仅返回原始/统计类数据，供 NOFX 的 AI 独立判断。

---

## 1. 基本信息

| 项 | 值 |
|---|---|
| Base URL | `http://<backend-host>:8000/api/nofx` |
| Content-Type | `application/json; charset=utf-8` |
| 认证 | 当前无（可在 `config.yaml::nofx` 启用） |
| 推荐调用频率 | **3 分钟 / 次**（硬上限 60/min/IP） |
| 单次响应大小 | 50 ~ 150 KB |
| 单次响应耗时 | < 50ms（缓存命中 < 5ms） |
| Schema 版本 | 顶层 `schema_version` 字段 + Response Header `X-NOFX-Schema-Version` |

### 1.1 字段命名约定

- 全部 `snake_case`
- 时间戳统一 **unix 秒**（不是毫秒）
- 金额统一 **USD**
- 缺失字段：`null` / `[]` / `0`（**永远不会抛 500**）

### 1.2 Schema 稳定性承诺

- ✅ 可以新增字段（NOFX 端应用 `map[string]interface{}` 反序列化，忽略未知字段）
- ❌ 不改名、不删字段、不改类型
- ❌ 破坏性变更必升 major（`2.0.0`）并提前通知

---

## 2. 端点清单

### 2.1 `GET /api/nofx/coins`

支持的币种列表。NOFX 端启动时调一次即可。

**响应示例：**
```json
{
  "schema_version": "1.0.0",
  "coins": [
    {"coin": "BTC", "symbol": "BTCUSDT", "exchange_primary": "Binance"},
    {"coin": "ETH", "symbol": "ETHUSDT", "exchange_primary": "Binance"},
    {"coin": "SOL", "symbol": "SOLUSDT", "exchange_primary": "Binance"}
  ],
  "default_coin": "BTC"
}
```

### 2.2 `GET /api/nofx/health`

接口 + 上游数据源健康自检。

**响应示例：**
```json
{
  "schema_version": "1.0.0",
  "status": "running",
  "ts": 1745300000,
  "coins_ready": {"BTC": true, "ETH": true, "SOL": false},
  "sources": [
    {"name": "coinglass", "status": "connected", "latency_ms": 234, "error_count": 0},
    {"name": "binance",   "status": "connected", "latency_ms": 45,  "error_count": 0},
    {"name": "bbx",       "status": "connected", "latency_ms": 180, "error_count": 0}
  ]
}
```

### 2.3 `GET /api/nofx/snapshot/{coin}` ⭐ 主接口

NOFX 3 分钟决策前调用。请求哪个币种就返回哪个币种。

**URL 参数：**
- `coin`: `BTC` / `ETH` / `SOL`（大小写不敏感）

**响应结构：**

```jsonc
{
  "schema_version": "1.0.0",
  "ready": true,                 // false 时只有 coin/ts/reason，NOFX 应跳过
  "ts": 1745300000,              // 服务端生成快照的 unix 秒
  "coin": "BTC",
  "symbol": "BTCUSDT",
  "price": 76234.5,              // 最新成交价
  "high_24h": 77100.0,
  "low_24h":  75200.0,
  "vol_24h":  12450000000.0,
  "change_pct_24h": -0.82,

  "snapshot": {
    "candles":            { /* 多时间框 K 线 */ },
    "cvd":                { /* 合约 + 现货 CVD */ },
    "oi":                 { /* 持仓量 */ },
    "funding":            { /* 资金费率 */ },
    "long_short_ratio":   { /* 多空比（3 维度） */ },
    "liquidation_map":    { /* 清算地图（4 周期） */ },
    "liquidation_heatmap":{ /* 清算热力图密度峰值 */ },
    "liquidation_max_pain":{ /* 24h 清算最大痛点（多/空） */ },
    "liquidation_stats":  { /* 24h 爆仓统计 */ },
    "recent_sweeps_1h":   [ /* 近 1h 流动性扫取事件 */ ],
    "orderbook":          { /* 订单簿墙 + 总量 */ },
    "large_orders":       { /* 大单追踪 */ },
    "whale":              { /* 巨鲸：HL / 链上转账 */ },
    "etf":                { /* ETF 净流入 */ },
    "on_chain_cycle":     { /* 链上周期数值 */ },
    "options":            { /* 期权 Max Pain / OI / IV */ },
    "taker_volume":       { /* Taker 买卖量 */ },
    "volume_profile":     { /* POC / VAH / VAL / VWAP */ },
    "atr_14":             825.4,
    "net_position_td":    { /* 净持仓 + 合约流 + TD 序列 */ },
    "macro":              { /* DXY / 纳指 / 黄金 / 美债 / 恐贪 ... */ },
    "news":               { /* 简报结论 + 地缘等级 + 叙事标签 */ }
  },

  "data_age_sec": {             // 每维度最后更新到现在的秒数（null = 该维度缺失）
    "ticker": 12,
    "candles_1h": 50,
    "oi": 45,
    "funding": 30,
    "liquidation_map_24h": 55,
    "etf": 1820,
    "on_chain_cycle": 3500,
    "news_brief": 2100
    // ...
  },

  "source_health": [ /* 同 /health 端点的 sources */ ]
}
```

> **重点**：`ready=false` 时只保证顶层 `coin/ts/symbol/reason` 存在，`snapshot` 可能缺失。NOFX 应检测后跳过该次决策。

---

## 3. `snapshot` 字段详细规格

### 3.1 `candles` · 多时间框 K 线

每根 K 线压缩为数组 `[ts, open, high, low, close, vol]`，节省体积（相比对象形式省 ~60%）。

| 周期 | 默认根数 | 覆盖时长 |
|---|---|---|
| `15m` | 96 | 最近 24h |
| `1h` | 168 | 最近 7d |
| `4h` | 120 | 最近 20d |
| `1d` | 90 | 最近 3 个月 |
| `1w` | 60 | 最近 ~1.2 年 |

```json
{
  "candles": {
    "1h": [
      [1745296400, 76100.0, 76350.0, 76050.0, 76234.5, 12450.3],
      [1745300000, 76234.5, 76300.0, 76180.0, 76250.0,  8420.1]
    ]
  }
}
```

### 3.2 `cvd` · 累计买卖净差

仅保留**原始数值**，剔除 trend 文字解读（`cvd_contract_trend` / `cvd_divergence` 等）。

```json
{
  "cvd": {
    "contract": {
      "delta_1h": -1250.3,            // 合约最近 1h 买卖净差（USD 单位，源自 Coinglass）
      "has_divergence": true,
      "cumulative": 3241500.0,        // series 最后一条 cvd 累计值
      "last_point": {"ts": 1745300000, "buy_vol": 1820, "sell_vol": 2100, "delta": -280},
      "series_len": 288               // 内部保留的 5m 序列长度
    },
    "spot":     { /* 同上结构 */ }
  }
}
```

### 3.3 `oi` · 持仓量

```json
{
  "oi": {
    "current_usd":   12300000000.0,
    "change_5m_pct": 0.12,
    "change_1h_pct": -0.42,
    "change_24h_pct": 1.83,
    "history_30pts": [{"ts":..., "oi":..., "oi_usd":...}],   // 最近 30 个时间点
    "by_exchange":   [{"exchange":"Binance","oi_usd":...}]
  }
}
```

### 3.4 `funding` · 资金费率

```json
{
  "funding": {
    "okx": 0.0089,
    "binance": 0.0091,
    "avg_current": 0.0089,
    "avg_7d":  0.0072,
    "oi_weighted": 0.0087,
    "next_funding_ts": 1745308800,
    "by_exchange": [
      {"exchange": "OKX",     "current": 0.0089, "avg_3d": 0.0081, "avg_7d": 0.0072, "avg_30d": 0.0065},
      {"exchange": "Binance", "current": 0.0091, "avg_3d": 0.0079, "avg_7d": 0.0070, "avg_30d": 0.0062}
    ]
  }
}
```

### 3.5 `long_short_ratio` · 多空比

三维度：`global`（全网总账户）/ `top_account`（大V账户）/ `top_position`（大V持仓）。

```json
{
  "long_short_ratio": {
    "global":       {"cycle": "1h", "dimension": "global",       "avg_ratio": 1.23, "by_exchange": [...]},
    "top_account":  {"cycle": "1h", "dimension": "top_account",  "avg_ratio": 1.05, "by_exchange": [...]},
    "top_position": {"cycle": "1h", "dimension": "top_position", "avg_ratio": 0.92, "by_exchange": [...]},

    "global_long_pct": 55.2, "global_short_pct": 44.8, "global_change_24h": -0.04,
    "top_account_long_pct": 51.2, "top_account_short_pct": 48.8, "top_account_change_24h": 0.02
  }
}
```

### 3.6 `liquidation_map` · 清算地图（3 个周期）

`24h` / `7d` / `30d`，每个周期结构一致（数据源仅采集这三个周期；旧版 `3d`
key 已下线，请勿再读取）：

```json
{
  "liquidation_map": {
    "24h": {
      "ts": 1745299940,
      "cycle": "1d",
      "exchange": "",
      "imbalance_ratio": 1.18,
      "clusters_above": [
        {"price_center": 78500, "price_from": 78300, "price_to": 78700, "total_usd": 320000000, "side": "short", "dominant_leverage": "50", "distance_pct": 2.97}
      ],
      "clusters_below": [ /* 同结构 side=long */ ],
      "vacuum_zones":   [{"price_from": 80100, "price_to": 81200, "midpoint": 80650, "note": ""}],
      "leverage_groups":[
        {"leverage":"25", "short_bands":[...], "long_bands":[...], "short_total_usd":..., "long_total_usd":...}
      ]
    },
    "7d":  { /* 或 null */ },
    "30d": { /* 或 null */ }
  }
}
```

### 3.7 `liquidation_heatmap` · 价格-时间密度峰值

按清算量 USD 排序的 Top 10 价格点：

```json
{
  "liquidation_heatmap": {
    "range": "24h",
    "model": 1,
    "exchange": "",
    "hotspots": [
      {"price": 78900, "total_usd": 420000000, "pct_from_price": 3.49, "ts": 1745297000}
    ],
    "points_total": 300
  }
}
```

### 3.7b `liquidation_max_pain` · 24h 清算最大痛点

Coinglass `liquidation/max-pain` 计算的"若价格触及该位则会引发最大规模清算"的关键
价位与对应金额。多/空分别给出，与 `liquidation_heatmap.hotspots` 互为印证。

```json
{
  "liquidation_max_pain": {
    "range": "24h",
    "current_price": 77903.2,
    "long_pain_price": 76963.86,
    "long_pain_usd": 86909802.27,
    "long_pain_pct_from_price": -1.20,
    "short_pain_price": 78536.6,
    "short_pain_usd": 86909802.27,
    "short_pain_pct_from_price": 0.81,
    "ts": 1745297000
  }
}
```

字段说明：
- `long_pain_price/usd`：多头痛点（价格**下行**触达该价位 → 多头集中爆仓的 USD 金额）。
  通常 `long_pain_price < current_price`（位于当前价下方），`*_pct_from_price < 0`。
- `short_pain_price/usd`：空头痛点（价格**上行**触达该价位 → 空头集中爆仓的 USD 金额）。
  通常 `short_pain_price > current_price`（位于当前价上方），`*_pct_from_price > 0`。
- `*_pct_from_price`：与当前价的偏离百分比（正=上方，负=下方），可能为 null。

### 3.8 `recent_sweeps_1h` · 流动性扫取事件（原始事件）

过去 1h 内市场扫过清算簇的原始事件：

```json
{
  "recent_sweeps_1h": [
    {"ts": 1745298500, "side": "above", "usd": 12400000, "price": 78520, "cluster_price": 78500, "cluster_distance_pct": 0.03},
    {"ts": 1745299800, "side": "below", "usd":  8200000, "price": 75180, "cluster_price": 75200, "cluster_distance_pct": -0.03}
  ]
}
```

### 3.9 `orderbook`

```json
{
  "orderbook": {
    "bid_walls":      [{"price": 75100, "size": 0, "size_usd": 8500000, "order_count": 0}],
    "ask_walls":      [...],
    "bid_total_usd":  240000000,
    "ask_total_usd":  195000000,
    "spread_pct":     0.012
  }
}
```

> 注：`bid_walls/ask_walls` 数据源是 Coinglass 大单追踪（非订单簿档位聚合），`size` 字段为占位。

### 3.10 `large_orders`

```json
{
  "large_orders": {
    "buy_count": 12,
    "sell_count": 9,
    "net_usd": 4200000,
    "total_bid_usd": 18000000,
    "total_ask_usd": 13800000,
    "recent": [
      {"ts": 1745299800, "exchange": "Binance", "symbol": "BTCUSDT", "side": "bid", "price": 76200, "size_usd": 2500000, "status": "active"}
    ]
  }
}
```

### 3.11 `whale` · 巨鲸（Hyperliquid + 链上转账）

```json
{
  "whale": {
    "hl_alerts_count": 4,
    "hl_positions_count": 12,
    "transfers_count": 11,
    "transfer_inflow_usd":  3200000,   // 转入交易所（通常看空信号原料）
    "transfer_outflow_usd": 8500000,   // 转出交易所（通常看多信号原料）
    "transfer_net_usd":    -5300000,
    "hl_alerts_recent": [
      {"ts": ..., "symbol": "BTC", "side": "long", "action": "open", "size_usd": ..., "entry_price": ...}
    ],
    "hl_positions": [
      {"address": "0x...", "symbol": "BTC", "side": "long", "size_usd": ..., "entry_price": ..., "unrealized_pnl": ..., "leverage": 20}
    ],
    "top_transfers": [
      {"ts": ..., "symbol": "BTC", "amount": ..., "amount_usd": ..., "from_label": "unknown", "to_label": "binance", "blockchain": "bitcoin"}
    ]
  }
}
```

### 3.12 `etf`

```json
{
  "etf": {
    "asset": "BTC",
    "net_3d": -125300000,
    "recent_days": [
      {"date": "2026-04-22", "total_net": -12000000, "detail": {}},
      {"date": "2026-04-21", "total_net":  -8500000, "detail": {}}
    ]
  }
}
```

### 3.13 `on_chain_cycle` · 链上周期原始数值

**全部保留原始数值**，剔除了我们自己打的 `cps_label` / `price_vs_sth_label` 标签（这些是加工结论）。

```json
{
  "on_chain_cycle": {
    "ts": 1745296000,
    "cps": 6.3,                              // Coinglass 原始 0-10 周期分（非我们的判断）
    "mvrv_z_score": 2.1,    "mvrv_z_contribution": 0.8,
    "ahr999_value":  0.42,  "ahr999_contribution": 0.6,
    "price_vs_200w_ratio": 2.45, "price_vs_200w_contribution": 1.2,
    "price_vs_sth_contribution": 0.3,
    "pi_cycle_ratio": 0.87, "pi_cycle_contribution": 0.5,
    "rplr_proxy": 1.34,
    "btc_rsi_daily": 58.2,
    "sma_200w": 31200.0,
    "sth_cost_1d": 69800, "sth_cost_1w": 68900, "sth_cost_1m": 65200, "sth_cost_3m": 58100,
    "pi_350dma": 52400, "pi_111dma_x2": 62100, "cvdd": 28900
  }
}
```

### 3.14 `options`

```json
{
  "options": {
    "nearest_max_pain": 76500,
    "nearest_expiry":   "2026-04-25",
    "nearest_call_oi":  320000000,
    "nearest_put_oi":   245000000,
    "nearest_put_call_ratio": 0.77,
    "expiries": [/* 10 个到期日汇总 */],
    "total_oi_usd":      18500000000,
    "total_vol_24h_usd":  1820000000,
    "put_call_oi_ratio":  0.78,
    "put_call_vol_ratio": 0.65,
    "iv_atm": 58.4
  }
}
```

### 3.15 `taker_volume`

```json
{
  "taker_volume": {
    "buy_ratio": 0.512,
    "sell_ratio": 0.488,
    "spot_buy_vol":     1.2e9, "spot_sell_vol":     1.1e9,
    "contract_buy_vol": 3.8e9, "contract_sell_vol": 3.7e9,
    "spot_contract_divergence": false
  }
}
```

### 3.16 `volume_profile`

```json
{"volume_profile": {"poc": 76050, "value_area_high": 76800, "value_area_low": 75100, "vwap": 76200}}
```

### 3.17 `liquidation_stats`

```json
{
  "liquidation_stats": {
    "recent_24h": {"long_usd": ..., "short_usd": ..., "long_count": ..., "short_count": ..., "ratio": 1.24, "period_min": 1440},
    "global":     {"long_1h_usd": ..., "short_1h_usd": ..., "long_24h_usd": ..., "short_24h_usd": ..., "ratio_1h": ..., "ratio_24h": 1.82, "largest_single_usd": ...}
  }
}
```

### 3.18 `net_position_td`

```json
{
  "net_position_td": {
    "net_position_latest":     -12500.3,   // 净持仓（正=多头占优）
    "net_position_change_24h": -3200.1,
    "futures_coin_netflow_1h": -8500000,   // 合约资金净流（USD，1h）
    "td_sequential_count":     9           // TD 序列 1-9（9 常为反转信号原料）
  }
}
```

### 3.19 `macro` · 宏观 + 链上补充

```json
{
  "macro": {
    "dxy":    104.2, "dxy_change_pct":    -0.18,
    "nasdaq": 18450, "nasdaq_change_pct":  0.42,
    "sp500":   5230, "sp500_change_pct":   0.31,
    "gold":    2380, "gold_change_pct":    0.12,
    "us_10y_yield": 4.21, "fed_rate": 5.50,
    "fear_greed": 38, "fear_greed_prev": 42,
    "btc_dominance": 54.2,
    "stablecoin_dominance": 7.8,
    "stablecoin_total_mcap": 160000000000,
    "stablecoin_7d_change_pct": 0.4,
    "coinbase_premium_current": -0.012,
    "coinbase_btc_premium_mi":  -0.012,
    "usdt_otc_premium": 0.0,
    "usdt_market_cap":  110000000000,
    "btc_hashrate":     620.5,
    "btc_hist_vol":     0.47, "btc_implied_vol": 0.58, "btc_iv_skew_1m": -3.2,
    "btc_put_call_oi":  0.78,
    "btc_mvrv":         2.1,
    "ahr999":           0.42,
    "okx_ls_ratio_btc": 1.12, "binance_ls_ratio_btc": 1.08
  }
}
```

### 3.20 `news` · 新闻简报结论 + 地缘 + 叙事

**只给结论字段**（tldr_cn 一句话 + 版本元信息），不返回简报全文。

```json
{
  "news": {
    "brief": {
      "version": 142,
      "updated_at": 1745297000,
      "coverage_hours": 24.0,
      "tldr_cn": "美联储官员鹰派表态压制风险资产；ETF 连续流出；链上巨鲸转入交易所，短期偏空。",
      "update_trigger": "scheduled",
      "based_on_events_count": 28,
      "model_used": "deepseek-v4-flash"
    },
    "geo_risk": {
      "ts": 1745296800,
      "overall_level": 2,
      "overall_label": "WATCH",
      "overall_summary_cn": "中东-伊朗紧张，综合风险 2/5",
      "escalation_count_24h": 3,
      "de_escalation_count_24h": 1,
      "has_blackswan_24h": false
    },
    "active_narratives": [
      {
        "theme_id": "etf_outflow", "theme_name_cn": "ETF 资金流出", "category": "macro_flow",
        "latest_event_ts": 1745296200, "flip_flop_count_24h": 0,
        "current_intensity": 3, "current_direction_bias": "bearish",
        "avg_abs_reaction_pct": 1.2, "hit_rate": 0.67
      }
    ]
  }
}
```

---

## 4. 已剔除字段清单（**不会出现在响应里**）

为避免干扰 NOFX 的独立判断，以下本项目的加工层字段全部不返回：

| 类别 | 剔除字段 |
|---|---|
| 动能 / 结构信号 | `trend_exhaustion`, `market_structure`, `market_structure_1d`, `market_structure_1w` |
| 方向共识 | `direction_vote`, `regime_snapshot` |
| 市场温度 | `temperature`, `market_temperature`, `pin_risk_level` |
| 箱体信号 | `range_signal` |
| 关键位推断 | `levels.*`, `key_level_snapshot_v2`, `key_levels`, `rule_supports`, `rule_resistances`, `sniper_entries`, `ladder_plans` |
| 决策引擎 | `execution_plan`, `ai_trader_report`, `final_decision`, `waterfall` |
| 解读文本 | `cvd_*_trend`, `funding_interpretation`, `taker_dominant`, `oi_trend`, `cvd_divergence`（note 字段），`cps_label`, `price_vs_sth_label` |
| K 线形态 | `candlestick_pattern_*` |

---

## 5. 错误码

| HTTP | 场景 | 响应 |
|---|---|---|
| `200` | 正常 | `{ready:true, ...}` 或 `{ready:false, reason:"..."}`（服务正常但数据未就绪） |
| `400` | coin 不在 `allow_coins` | `{"detail":"Unsupported coin: XXX. Allowed: [...]"}` |
| `429` | IP 超限 | `{"detail":"Rate limit exceeded: 60/min"}` + `Retry-After` header |
| `500` | 内部异常（应罕见） | `{"detail":"snapshot build failed: ..."}` |
| `503` | 引擎/接口未启用 | `{"detail":"Engine not ready"}` / `"NOFX interface disabled"` |

---

## 6. 客户端推荐实现要点

1. **解析**：用 `map[string]interface{}` 反序列化；只读你关心的字段，忽略未知字段。
2. **ready 判断**：先看顶层 `ready`，false 时跳过本轮决策。
3. **时效判断**：用 `data_age_sec[<field>]`——例如若 `funding` age > 300s，视为资金费率这一维失效，prompt 里别太依赖。
4. **ETag / Cache-Control**：服务端返回 `Cache-Control: public, max-age=30` 与 `ETag`；NOFX 可带 `If-None-Match` 命中 304（后续版本可能加，v1 不保证）。
5. **失败重试**：429 看 `Retry-After`；5xx 退避重试 1 次后跳过。
6. **时区**：所有 `ts` / `updated_at` 都是 **unix 秒 UTC**。

---

## 7. 版本历史

| 版本 | 日期 | 变更 |
|---|---|---|
| `1.0.0` | 2026-04-22 | 首版发布 |
