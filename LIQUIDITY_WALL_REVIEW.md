# 流动性墙引擎 · 评审与落地方案

> 文档定位：基于 LIQ 仓库现状盘点 + Coinglass 27 个 endpoint 真实数据 probe，
> 对 GPT 设计方案与整合方案做精华/糟粕评估，给出**取其精华、去其糟粕**的修订版落地路线。
>
> 起草：2026-04-28；维护：每个 Phase 完成更新一次。
>
> 评审基准：
> - 仓库 HEAD：`main` 分支，已合入 M4 P1 修复 + M4-3/M4-6/P2-8（commit `186ca99`）
> - probe 实测样本：`backend/scripts/coinglass_probe_samples/2026-04-28T*Z/`（27 endpoint × BTC 全 OK）
> - Phase 0 已完成：_safe_age 兼容 / data_quality stale / 死配置清理（**1811 测试全通过**）

---

## 0. TL;DR（一段话结论）

> **GPT 方案的方向 90% 正确，但 60% 的工作量被高估**——LIQ 已经接入了 GPT 列出的 P0 八大接口中的 7 个，**真正的工作不是"补数据源"，而是"复用现有数据 + 升级算法 + 桥接 KL"**。
>
> probe 实测发现 **3 个高价值惊喜**：(a) `large_orders_history` 实际 18 字段（LIQ 仅用 6），含 `executed_usd_value/trade_count` 直接可作"墙被吃 vs 撤单"判定；(b) `oi_exchange_list` 已带 5m/15m/30m/1h/4h/24h delta，无需本地计算；(c) `liquidation_max_pain` 直接给 `long/short_max_pain_liq_price` 作 `next_magnet_price`。这让 GPT M2/M3 大部分工作变得**几乎零成本**。
>
> **建议路线：保留 OP 模块命名（兼容外部消费者），内部能力 5 阶段递进升级，全程零新增 Coinglass poll**。

---

## 1. 现状盘点要点（先看清家底再设计）

### 1.1 LIQ 已接入的 Coinglass 接口（与 GPT P0/P1 清单对照）

| GPT 标签 | endpoint | LIQ 现状 | 接入方式 |
|---|---|---|---|
| P0 | `orderbook/history` | ✅ 已用 | `poll_orderbook_pressure`（5m × limit=2，**待升 limit=12**） |
| P0 | `orderbook/large-limit-order` | ✅ 已用 | `poll_large_orders` |
| P0 | `orderbook/large-limit-order-history` | ✅ 已用 | `poll_large_orders` |
| P0 | `aggregated-taker-buy-sell-volume/history` | ✅ 已用 | `poll_taker_volume` |
| P0 | `aggregated-cvd/history` | ✅ 已用 | `poll_cvd` |
| P0 | `open-interest/aggregated-history` | ✅ 已用 | `poll_oi`（5m + 1h） |
| P0 | `liquidation/aggregated-map` | ✅ 已用 | `poll_liquidation_map` |
| P0 | `liquidation/aggregated-heatmap/model1` | ✅ 已用 | `poll_liq_heatmap` |
| P0 | `liquidation/max-pain` | ✅ 已用 | `poll_liq_max_pain` |
| P1 | `funding-rate/oi-weight-history` | ✅ 已用 | `poll_funding_history_8h` |
| P1 | `global/top-long-short-*-ratio/history` | ✅ 已用 | `poll_ls_ratio` |
| P1 | `v2/net-position/history` | ✅ 已用（**比 GPT 推荐的 v1 更好**） | `poll_net_position` |
| P1 | `volume/footprint-history`（期货+现货） | ✅ 已用 | `poll_footprint` |
| P3 | `hyperliquid/whale-alert` / `whale-position` | ✅ 已用 | `poll_whale_data` |
| P3 | `option/max-pain` / `option/info` | ✅ 已用 | `poll_options` |
| P2 | `spot/aggregated-taker-buy-sell-volume/history` | ✅ 已用 | `poll_taker_volume` |
| P2 | `spot/aggregated-cvd/history` | ✅ 已用 | `poll_cvd` |

### 1.2 GPT 提了但 LIQ 还没接的（实际很少）

| endpoint | LIQ 状态 | 评估 |
|---|---|---|
| `orderbook/aggregated-ask-bids-history` | ✅ 已 poll，但 OP 没用 | **复用即可**，不必新增 |
| `liquidation/aggregated-heatmap/model2` / `model3` | ❌ 仅 model1 接入 | probe 验证 m1/m2 数据点数 17K/31K，**model1 已够用** |
| `spot/orderbook/history` | ❌ 完全没接 | 期现共振价值有限，留 M5 候选 |
| `spot/orderbook/large-limit-order` | ❌ 完全没接 | 同上 |
| 单所版 `taker-buy-sell-volume` / `cvd/history` | ❌ fetcher 有，未调用 | 聚合版已用，**单所版不必接** |

### 1.3 现有 CoinState 共享数据总线（墙引擎可直接复用）

```
state.orderbook_depth_snapshot  # 5m L2 深度（OP 路径 A）
state.large_orders_history      # 大单 lifecycle 列表（OP 路径 B）
state.taker_flow                # 主动买卖（M2 行为评估）
state.cvd_contract              # CVD（M2 吸收识别）
state.oi                        # OI（M3 拥挤度）
state.oi_exchange_rank          # 各所 OI + 5m/15m/30m/1h/4h/24h delta（M3 OI 趋势）
state.multi_funding             # Funding 现价（M3 拥挤度）
state.funding_history_8h        # Funding 历史（百分位）
state.ls_ratio + top_account/position  # LS 系列（M3 多空拥挤）
state.liq_maps[1d/7d/30d]       # 清算簇（M3 sweep_target）
state.liq_max_pain              # 最痛点（M3 next_magnet_price）
state.liq_heatmaps              # 清算热力（杠杆密度）
state.footprint_contract/spot   # 足迹（吸收识别）
state.whale_data                # Hyperliquid 鲸鱼（M5 候选）
```

**结论：墙引擎所需的数据 95% 已在 CoinState，零新增 poll 即可推进 Phase 0–4**。

---

## 2. probe 实测发现的关键数据点

完整样本：`backend/scripts/coinglass_probe_samples/2026-04-28T*Z/BTC/{endpoint}.{json,schema.json}`

### 2.1 ⭐ 惊喜 1：`large_orders_history` 实际 18 字段（LIQ 仅用 6）

```json
{
  "id": 8284778063,
  "exchange_name": "Binance",
  "symbol": "BTCUSDT",
  "limit_price": 76743.3,
  "start_time": 1777368223000,
  "start_quantity": 14.434,
  "start_usd_value": 1107712.79,
  "current_quantity": 14.434,
  "current_usd_value": 1107712.79,
  "current_time": 1777368223000,
  "executed_volume": 3.159,            // ⭐ 已成交数量
  "executed_usd_value": 242433.23,     // ⭐ 已成交 USD（"墙被吃多少"的精确证据）
  "trade_count": 118,                   // ⭐ 触发成交次数
  "order_side": 2,                      // 1/2 = bid/ask
  "order_state": 2,                     // 1/2 = holding/ended
  "order_end_time": 1777368234000
}
```

**LIQ 当前 `LargeOrderLifecycle` 只用了**：`id / side / limit_price / start_time / end_time / state`。

**意义**：
1. **GPT 把"墙被吃 vs 撤单"列为 M2 难点**，要靠 taker_flow + CVD 反推。
2. **实际上 Coinglass 直接给了 `executed_usd_value` 和 `trade_count`** — 单条 large_order 自带"被吃多少 USD"的精确数据。
3. → 判定逻辑可极简：
   - `state==2` 且 `executed_usd_value > 0` → **wall_consumed**（被吃）
   - `state==2` 且 `executed_usd_value == 0` → **wall_removed**（撤单）
   - `state==1` 且 `current_quantity > start_quantity` → **wall_strengthened**（加厚）
   - `state==1` 且 `current_quantity < start_quantity` → **wall_weakened**（减薄）
4. **完全不需要做 GPT M2 的"反向推断"**，准确度还更高。

### 2.2 ⭐ 惊喜 2：`oi_exchange_list` 自带 5m/15m/30m/1h/4h/24h delta

```json
{
  "exchange": "All",
  "symbol": "BTC",
  "open_interest_usd": 55543131754.49,
  "open_interest_quantity": 723719.88,
  "open_interest_by_coin_margin": 5412461964.63,
  "open_interest_by_stable_coin_margin": 50130669789.86,
  "open_interest_change_percent_5m": -0.1,
  "open_interest_change_percent_15m": -0.05,
  "open_interest_change_percent_30m": 0.03,
  "open_interest_change_percent_1h": 0.13,
  "open_interest_change_percent_4h": -0.19,
  "open_interest_change_percent_24h": -2.52
}
```

**意义**：
1. **GPT M3 提的 `oi_delta_5m / oi_delta_1h / oi_percentile_30d`**，**5m/1h delta 直接可读**，无需本地计算。
2. 同一接口还分了**币本位 vs U 本位**（margin_mode），可识别"币本位激增 = 老用户加杠杆"vs"U 本位激增 = 新资金入场"。
3. 25 项含 `All` 聚合行 + 24 个交易所明细，**多维 OI 视角全免费**。

### 2.3 ⭐ 惊喜 3：`liquidation_max_pain` 直接给 next_magnet 价位

```json
[
  {
    "symbol": "BTC",
    "price": 76729.6,                          // 当前价
    "long_max_pain_liq_level": 40574087.23,    // 多头最痛清算金额（USD）
    "long_max_pain_liq_price": 76311.34,       // ⭐ 多头被打爆最痛价位（下方磁铁）
    "short_max_pain_liq_level": 64345225.77,   // 空头最痛清算金额
    "short_max_pain_liq_price": 78618.97       // ⭐ 空头被打爆最痛价位（上方磁铁）
  },
  ...   // 全市场 563 个币
]
```

**意义**：
1. **GPT M3 的 `next_magnet_price`** 直接来自这里，无需自己组装清算簇。
2. 例：BTC 现价 76729.6，下方多头磁铁 76311.34（-0.5%），上方空头磁铁 78618.97（+2.5%）→ **支撑位附近若被打穿，多头扫单概率高**。
3. 全市场 list × 563 项，**一次拉取覆盖所有币**。

### 2.4 ⭐ 惊喜 4：`hyperliquid/whale-position` 14 字段含 `entry_price/liq_price`

```json
{
  "user": "0x0ddf9bae2af4b874b96d287a5ad42eb47138a902",
  "symbol": "BTC",
  "position_size": -1000.0,             // 负数 = 空头 1000 BTC
  "entry_price": 67992.1,               // ⭐ 进场价
  "mark_price": 76692.0,
  "liq_price": 101019.77,               // ⭐ 清算价
  "leverage": 3,
  "margin_balance": 25564000.0,
  "position_value_usd": 76692000.0,
  "unrealized_pnl": -8699868.76,
  "funding_fee": 32085.88,
  "margin_mode": "cross",
  "create_time": 1774977506000,
  "update_time": 1777368731000
}
```

**意义**：
1. **GPT 把"识别已开仓大单位置"列为 6.8/10 难点**，因为大多数 CEX 不公开。
2. **Hyperliquid 鲸鱼数据精确给了 `entry_price`（开仓价）+ `liq_price`（清算价）+ `position_size`** — 这就是"可见鲸鱼仓位"的最强证据。
3. 单次拉取 861 个鲸鱼仓位（覆盖多币），按价位分桶可立即得到"哪些价位有鲸鱼仓位密集 / 哪些价位是清算引爆点"。
4. **限制**：Hyperliquid 不代表全市场（GPT 已说），仅作"可见鲸鱼证据"用。

### 2.5 重要结构观察

| endpoint | 实测 | 意义 |
|---|---|---|
| `orderbook/history` 5m × limit=12 | 单帧 1247 个 bin，价位横跨 38480→78000+（-50%~0%） | **当前 OP 用 limit=2 信息利用率仅 16.7%**；改 limit=12 后 1h 滚动可立即得到 persistence。但 bin 范围广，**必须按距离过滤** |
| `orderbook/aggregated-ask-bids-history` | 5 字段 dict 时序：`aggregated_bids_usd / asks_usd / *_quantity / time` | **总额时序**，**不是分价位墙**。可作"总深度趋势 + bid/ask 失衡比"，但不能替代 `orderbook/history` |
| `aggregated_cvd` | 4 字段：`time, agg_taker_buy_vol, agg_taker_sell_vol, cum_vol_delta` | **包含累计 delta**，单接口能同时支持"taker 流向"和"CVD 累积"两个视角，**M2 吸收识别足够用** |
| `liquidation_aggregated_heatmap_m1` | dict（y_axis 127 价位、liquidation_leverage_data 17,457 点、price_candlesticks 288 K线） | 数据密集，已可支持"杠杆密度热力" |
| `liquidation_aggregated_heatmap_m2` | y_axis 119、leverage_data 31,788（密度 1.8x） | 比 m1 更密但**信号噪声同样上升**；**model1 够用** |
| `liquidation/aggregated-map` | 长前缀 dict（已 raw_response），含 `liq_buy/sell` 簇分布 + `total_liquidation` | 这是 LIQ 现有 `liq_maps[1d/7d/30d]` 来源，**复用即可** |
| `large_orders_current` | 17 字段（少 `order_end_time`）/ 434 条 | **当前活跃** + holding 状态；按 `state==1` 筛即得"实时持仓中的大单" |
| `large_orders_history` | 18 字段 / **1000 条**（API 上限） | 含已结束的；start_time 跨度可达 **9 天前**（4-19 ~ 4-28），足以做 7 天 persistence 计算 |
| `oi_exchange_list` | 25 项含 `All` 聚合 + 24 所明细 | **币本位 / U 本位分流 + 6 周期 delta 全自带** |
| `liquidation_max_pain` | 全市场 list × 563 币 | **不需要 symbol 参数**，一次拉取覆盖所有币 |
| `hyperliquid/whale-position` | 14 字段 / 861 条 | 真实鲸鱼仓位含 `entry_price`、`liq_price`、`unrealized_pnl`、`funding_fee` |

### 2.6 实测限流反馈

- `FixedIntervalLimiter rate_per_min=10` 在 keystore 代理层仍偶发 429
- probe 实测 5/min（12s 间隔）才完全无 429
- **生产环境 7s 间隔 + ≥300s 缓存目前能扛住**（盘点显示绝大多数 poll TTL ≥ 间隔）
- 墙引擎升级**只读复用，不加 poll**，对配额零冲击

---

## 3. GPT 方案精华/糟粕评估

### 3.1 设计方案评估表

| GPT 提议 | 评估 | 修订意见 |
|---|---|---|
| 模块改名为"流动性墙 + 大单 + 持仓拥挤度引擎" | ⚠️ 半采纳 | 语义升级正确，但**改文件名/类名/state 字段会破坏 9 个文件 + payload + 前端**；保留 `orderbook_pressure` 命名，**内部能力升级**（V3 双轨同样策略） |
| 8 个核心分（wall_strength / persistence / consumption / spoof / absorption / break_through / sweep / bid_ask wall） | ✅ 大部分采纳 | spoof_risk 改为 `wall_removal_risk`（软分，不下"假单"绝对结论） |
| WallEvent 事件流（appeared/strengthened/weakened/removed/consumed/reloaded/absorbed/broken） | ✅ 采纳 | 现有 `LargeOrderLifecycle.state` 已有基础，扩展为 event log |
| `walls_above` / `walls_below` + `wall_events` | ✅ 采纳 | 新增字段，旧 `walls`（含 side）保留兼容 |
| **WallZone 区间聚合**（相邻 bin 合并为墙区） | ✅ **强烈采纳，最高优先级** | 截图问题的**直接根因**之一；现 `merge_tol_pct=0.05%` 对 BTC 78000 ≈ 39 美元过紧 |
| **滚动历史保留 1h/6h/24h** | ✅ **强烈采纳，第二高优先级** | 现 `limit=2` 是另一根因；改 `limit=12` 一次拉满 + 内存 deque |
| distance_weight 连续衰减（exp / ATR 化） | ✅ 采纳 | 替代当前 `≤4% / 4-12%` 离散分桶 |
| 历史分位阈值 + 绝对额双闸（按币分层） | ✅ 采纳 | BTC/ETH/小币不应共用 500k 静态阈值 |
| 多交易所聚合（Binance + OKX + Bybit + Bitget + Gate） | ⚠️ 改造采纳 | LIQ 已有 `orderbook/aggregated-ask-bids-history`（多所聚合 USD 总额），**不需要本地拼接**；`large_orders_history` 数据**已含 `exchange_name`**，可分组 |
| PositionCrowdingSnapshot（OI delta / Funding percentile / LS / 净持仓） | ✅ 采纳但**不放在 OP 内部** | 这些数据在 LIQ 是**全局共享的**（state.oi 已含 5m/1h/24h delta、state.multi_funding 等）；OP 输出 `crowding_context_ref` 指向现有字段 |
| SweepTarget / next_magnet_price / vacuum_gap_pct | ✅ 采纳 | LIQ 已有 `liq_maps + liq_max_pain`，组合即得；probe 验证 max_pain 直接给 long/short_max_pain_liq_price |
| Hyperliquid 鲸鱼仓位作"可见鲸鱼证据" | ✅ 采纳但放 M5 | 已接 alert/position；价位绑定（按 entry_price 分桶）需新映射函数 |
| 现货 orderbook 期现共振 | ⚠️ 改造采纳 | spot CVD/taker 已接 → 实现 80% 期现背离价值；spot orderbook L2 留 M5 候选 |
| WallEvent 写入 KL `lifecycle_events` | ❌ **不采纳** | 违反"模块独立"原则；改为：墙引擎自己有 `wall_event_log`，KL 只在需要时**只读引用** |
| 5m 内秒级 spoof 真假判定 | ❌ **不采纳，GPT 自己也承认** | 数据精度天花板限制；改用 persistence + `wall_removal_risk` 软分 |
| 直接修改 KeyLevel.final_score（M3 ±5~±8） | ❌ **不采纳** | 违反 V3「零信号污染」铁律（`KEY_LEVEL_V3_ROADMAP.md` §1.2）；只进 `confirmations` / `explain_chips` / `contradiction_reasons`，不动 score |

### 3.2 整合方案 M0-M5 评估

| GPT M0-M5 | 评估 | 修订 |
|---|---|---|
| **M0 修 _safe_age + data_quality stale + 死配置** | ✅ **完全采纳** | **本次已完成**（见 §6） |
| M1 滚动历史 + WallZone + persistence + 历史分位 | ✅ 采纳 | 算法核心，回答"挂单少金额小"截图问题 |
| M2 接入 taker / CVD 判断墙被攻击 | ✅ 采纳但**简化** | **直接读 `executed_usd_value`** 替代 taker 反推；CVD 仅作次要佐证 |
| M3 接入 OI / Funding / 清算磁铁 | ✅ 采纳，**全部已有数据可复用** | OI delta 已带、Funding 已有、清算地图/max-pain 已接 |
| M4 和关键位 V3 融合 | ✅ 采纳但**收紧融合方式** | 仅追加 `confirmations` + `explain_chips`；不改 final_score / strength_tier / cascade_risk |
| M5 spot orderbook + Hyperliquid 仓位深度 | 💭 候选可后置 | 投入产出比低于 M1-M4 |

---

## 4. 数据接口缺口清单（基于 probe 验证）

### 4.1 完全无需新增（**95% 数据已就位**）

所有 GPT P0 + 大部分 P1 已经在 LIQ 跑了，墙引擎只需**在 processor 层从 CoinState 读**：

```python
# 墙引擎读取契约（不新增 poll）
class LiquidityWallEngine:
    def __init__(self, state):
        self.depth_snapshot = state.orderbook_depth_snapshot      # 5m L2
        self.depth_history = state.orderbook_depth_history        # 新增 deque（M1）
        self.large_orders = state.large_orders_history            # 含 18 字段
        self.taker_flow = state.taker_flow                        # CVD/taker
        self.cvd = state.cvd_contract
        self.oi = state.oi
        self.oi_rank = state.oi_exchange_rank                     # 含 5m/1h delta
        self.funding = state.multi_funding
        self.funding_history = state.funding_history_8h
        self.ls = state.ls_ratio
        self.liq_maps = state.liq_maps                            # 1d/7d/30d 簇
        self.liq_max_pain = state.liq_max_pain                    # long/short magnet
        self.liq_heatmaps = state.liq_heatmaps
        self.footprint = state.footprint_contract
        self.whale = state.whale_data                             # M5 候选
```

### 4.2 唯一需要新增的"内部状态"

```python
state.orderbook_depth_history: deque[OrderbookDepthSnapshot] = deque(maxlen=12)  # M1
state.liquidity_wall_snapshot: LiquidityWallSnapshot                              # M1 输出
```

### 4.3 选配（可在 M5 评估）

| 候选 | 评估 |
|---|---|
| `spot/orderbook/history` | M5 期现深度共振；现阶段用 spot CVD 已能判 80% |
| `spot/orderbook/large-limit-order` | 同上 |
| `hyperliquid/position`（单币种） | 已可用 `whale-position` 全量过滤 |

### 4.4 Coinglass 配额预算

| 项 | 数值 | 说明 |
|---|---|---|
| 全局限流 | 10 req/min（实际 7s 间隔） | 经盘点验证可承受当前负载 |
| 默认缓存 TTL | 非 FAST 路径 ≥ 300s | 多数 poll 比 TTL 快 → 缓存命中、不消耗令牌 |
| 墙引擎升级带来的额外配额 | **0** | 全程从 CoinState 读，零新增 poll |
| Probe 调研用量（一次性） | ~30 req | 已完成，不影响生产 |

---

## 5. 模块独立性设计（按用户偏好）

```
                    ┌────────────────────────────┐
                    │  CoinState (共享数据总线)  │
                    │  - oi / cvd / taker /      │
                    │    liq_maps / footprint    │
                    │  - large_orders /          │
                    │    orderbook_depth_snap    │
                    │  - orderbook_depth_history │ ← 新增（M1）
                    └────────────┬───────────────┘
                                 │ (只读)
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
   ┌──────▼──────┐       ┌───────▼────────────┐    ┌────▼──────────────┐
   │  KL V3      │       │ LiquidityWall      │    │  MAA / 清算地图   │
   │  Engine     │       │ Engine (新)        │    │  独立模块         │
   │             │       │                    │    │                   │
   │ 只读引用     │ ←──── │ 输出 snapshot      │ ──→│  只读消费 snapshot │
   │ wall ctx    │       │ + event log        │    │  (API payload)    │
   │ (注入式)    │       │                    │    │                   │
   └─────────────┘       └────────────────────┘    └───────────────────┘
                                 │
                                 ▼
                          API + 前端 Tab
```

**契约边界**：

1. **墙引擎只读 CoinState 共享数据**，不重复轮询 OI / Funding / CVD / Liq
2. **墙引擎对外只写 `state.liquidity_wall_snapshot`**（实现层名仍叫 `orderbook_pressure_snapshot` 兼容旧消费者），**不直接写到 `lv.behavior` / `lv.final_score` / `kl_history`**
3. **KL V3 想用墙数据**：通过现有 `pressure_snapshot=...` 注入式只读引用（已有模式），无返回写
4. **MAA / AI** 想用墙数据：**走 API payload 的 `orderbook_pressure` key**（向后兼容），新增字段（`wall_zones / wall_events / crowding_context`）不破坏旧 schema
5. **前端**继续用 `data?.orderbook_pressure`，新字段渐进展示

→ 实现"互相调用、互不耦合"。

---

## 6. Phase 0 已完成（本次落地内容）

### 6.1 实测调研

- 新建 `backend/scripts/probe_coinglass.py`（独立工具，**不进生产链路**）
- 27 个 endpoint × BTC 全部成功拉取，样本 + schema dump 在 `backend/scripts/coinglass_probe_samples/`
- 关键发现：18 字段 large_orders / OI delta 自带 / max_pain 直接给磁铁价 / Hyperliquid 含 entry_price

### 6.2 Bug 修复（上轮评审遗留）

| Bug | 修复 | 位置 |
|---|---|---|
| **Bug-1** `_safe_age` 仅读 `.ts`，与 `OrderbookPressureSnapshot.ts_sec` 不匹配 → freshness 永远 missing | 兼容 `ts / ts_sec / timestamp` 三种字段名 + dict 兜底 | `backend/processors/key_level_freshness.py:97-128` |
| **Bug-2** `data_quality="stale"` 字面量定义但**计算路径从未赋值** | 加 `stale_age_sec`（默认 180s）+ depth 主源陈旧时赋 stale | `backend/processors/orderbook_pressure.py:78-81, 605-612` |
| **Bug-3** `spoofing_ttl_sec: 3` 死配置（仅 yaml，0 Python 引用） | 移除 + 注释说明替代方案（`wall_removal_risk` 软分） | `backend/config/config.yaml:144-146` |

### 6.3 测试

- 新增 6 个测试（3 个 _safe_age 字段兼容 + 3 个 stale 赋值场景）
- **全量回归 1811 passed**（前次基线 1805 + 新增 6）
- 0 lint 错

### 6.4 验收

- `freshness` 不再永远把 `orderbook_pressure` 算 missing
- `data_quality="stale"` 真实可被赋值，前端可展示"depth 主源已陈旧"提示
- yaml 不再含误导性的 `spoofing_ttl_sec`

---

## 7. 修订版 Phase 1-5 路线图（接下来要做的）

### **Phase 1：墙观测引擎升级（2-3 天）**
**目标**：让用户看到的墙变厚、变区域、有持续时间，回答"挂单少金额小"截图问题

| 子任务 | 改动 |
|---|---|
| 1-1 数据模型扩展 | `OrderbookPressureSnapshot` 新增 `walls_above / walls_below / wall_events / wall_zones`（旧 `walls` 保留兼容） |
| 1-2 滚动历史 | `state.orderbook_depth_history: deque(maxlen=12)`（1h），`poll_orderbook_pressure` 改 `limit=12` 一次拉满本地滚动 |
| 1-3 WallZone 聚合 | `merge_tol_pct = max(0.05%, 0.15 × ATR/price)`，相邻 bin 合并成墙区 |
| 1-4 persistence_score | `visible_count / total_snapshots × min(1, visible_minutes / target_minutes)` |
| 1-5 历史分位阈值 | `max(static_min, recent_p80)`，按币分层默认值 |
| 1-6 距离衰减 | `exp(-|distance_pct| / scale)` 或 ATR 化 |
| 1-7 WallEvent 流 | `appeared / strengthened / weakened / removed`（基于 large_orders 的 18 字段差分） |
| 1-8 前端 | 单点 → 墙区视图，加 `当前/峰值/均值/趋势` |

**验收**：BTC 墙金额从"千万级散点"升级到"几亿级墙区"；前端能看出"持续 31 分钟，正在增强"

---

### **Phase 2：墙行为评估（1-2 天，比 GPT 估的少）**
**目标**：判断墙在被谁攻击 — **数据全部从 CoinState 复用，且 large_orders 18 字段免去反推**

| 子任务 | 改动 |
|---|---|
| 2-1 直接读 `executed_usd_value / trade_count` | 判定 `wall_consumed`（执行量>0+state=2）vs `wall_removed`（state=2+executed=0） |
| 2-2 `wall_strengthened` / `wall_weakened` | 基于 `current_quantity` vs `start_quantity` 差分 |
| 2-3 `wall_reloaded` | 同价位 ±0.1% 短时间（< 60s）再现的 large_order |
| 2-4 absorption_score | CVD 下行 + 价格 hold + 长下影 + state.footprint 吸收带共振（数据已全有） |
| 2-5 `wall_removal_risk` 软分 | 不输出"假单"绝对结论，仅给百分比 |
| 2-6 前端状态徽标 | 🟢 稳定 / 🟡 新墙 / 🧊 吸收 / 💥 被吃 / 🔁 补单 / 🔴 撤单风险 |

**验收**：77500 买墙的 5 种场景（稳定/被吃/撤单/吸收/重挂）能在前端展示对应状态

---

### **Phase 3：拥挤度与扫单磁铁（1-2 天，全部复用现有数据）**

| 子任务 | 改动 |
|---|---|
| 3-1 PositionCrowdingSnapshot | 读 `state.oi_exchange_rank` 自带的 `change_percent_5m/15m/30m/1h/4h/24h`；读 `state.multi_funding`、`state.ls_ratio` |
| 3-2 inferred_position_state | 多空开仓/平仓/清算（按 §2.2 表格规则） |
| 3-3 SweepTarget | `next_magnet_price` 直接来自 `state.liq_max_pain.long_max_pain_liq_price` / `short_max_pain_liq_price` |
| 3-4 vacuum_gap_pct | `state.orderbook_depth_snapshot.bids/asks` 中下方第一个非空 bin 的距离 |
| 3-5 break_through_risk + sweep_target_score | wall_thinning + taker pressure + CVD + OI 拥挤 + magnet（GPT 公式） |
| 3-6 前端"如果打穿"预测卡片 | 展示 next_magnet / vacuum_gap / 多/空清算金额 |

**验收**：墙引擎能输出"77500 被吃穿后下一磁铁 76311.34（多头清算 4057 万 USD）"

---

### **Phase 4：和关键位 V3 桥接（1-2 天，铁律不破）**

> **铁律遵守**：不改 `lv.final_score` / `lv.strength_tier` / `lv.cascade_risk`

| 子任务 | 改动 |
|---|---|
| 4-1 `KeyLevelV2.behavior` 加 `wall_context: WallContextRef`（**只读引用**） | 含 `nearby_wall_strength / wall_persistence_min / wall_status / explain_chips` |
| 4-2 `_apply_pressure_alignment` 升级 | 从只追加 `ob_strong_bid/ask` 升级为更细 chip（"稳定买墙 31m / 卖墙撤单风险 / 上方清算磁铁"） |
| 4-3 `behavior_eval._detect_contradictions` | 加 3 条规则（强支撑+买墙撤单 / 突破+卖墙重挂 / 等） |
| 4-4 AI prompt | 主决策**仍不喂挂单**（保持 V3 现行策略）；MAA facts 可选追加 `wall_context_summary` chip 字符串 |

**验收**：v1v2-compare / 关键位详情能看到"附近 3.2 亿稳定买墙"提示，但 final_score 不变

---

### **Phase 5（候选）：spot orderbook + Hyperliquid 仓位深度（2-3 天）**
- 期现共振 / 鲸鱼可见仓位作"可见鲸鱼证据"
- **优先级低**，等 Phase 1-4 稳定且有真实需求再做

---

## 8. 命名策略：保留 OP 不改名

| 方案 | 优势 | 劣势 |
|---|---|---|
| 改名为 `liquidity_wall`（GPT 主张） | 语义贴切 | 破坏 OP 模块 9 个文件 + state 字段 + payload key + 前端 Tab + 测试；回归风险高 |
| **保留 `orderbook_pressure` 命名**，内部能力升级 ✅ **本方案** | 零破坏；外部 schema 兼容；可逐步扩字段；旧文件名/字段名保留 | 命名稍弱（接受） |

→ 用 V3 双轨并行的同样策略：
- 模块内部能力升级到"流动性墙引擎"
- 外部 API key、state 字段名、Python 模块名仍叫 `orderbook_pressure`
- 只是新增字段（`wall_zones`、`wall_events`、`crowding_context`、`sweep_targets`）

---

## 9. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| Phase 1 改 `limit=2 → 12` 后 payload 变大 | 中 | snapshot 内只保留聚合后的 wall_zones（数十个），不暴露 1247×12 个原始 bin |
| persistence_score 在冷启动时 sample 不足 | 低 | 头 30 分钟 score 标 `data_quality=warming`，前端展示"暖机中" |
| WallEvent 事件流过多 | 中 | 限频：同 wall 每分钟最多 1 个 event，event_log 滚动保留 100 条 |
| 历史分位阈值在新币上无样本 | 低 | fallback 到静态阈值 |
| Phase 4 `confirmations` 增多导致 KL 信号噪声 | 中 | chip 类型分级（high/medium/low），`final_score` 不影响是底线 |

---

## 10. 给评审 AI 的指引

如你要评审本方案：

1. **先看 §1.1 现状盘点**：所有 GPT P0 接口在 LIQ 是否真的已接入（含路径行号）。
2. **再看 §2 实测发现**：probe 样本是否真的支撑文中"惊喜"结论。
3. **再看 §3.1 / §3.2 评估表**：每条 GPT 提议的精华/糟粕评分是否合理。
4. **再看 §5 模块独立性**：是否真的做到"调用而不耦合"。
5. **重点质疑**：
   - large_orders 18 字段中 `executed_usd_value` 真的能替代 taker_flow 反推吗（边界 case）？
   - WallZone 合并系数 `max(0.05%, 0.15×ATR/price)` 是否在 ETH/小币上失效？
   - persistence_score 在 cold-start 下如何避免误报？
   - max_pain `long/short_max_pain_liq_price` 与 `liq_maps` 簇的语义差异是否清楚？
6. **比较项**：与 GPT 原方案的 9.3/10 评分体系对比，**修订版应在"工程可行性"维度更高**（接口已就位 + 铁律不破 + 0 新增 poll）。

---

## 附录 A：probe 数据样本索引

```
backend/scripts/coinglass_probe_samples/
  20260428T092239Z/         # 第一次冒烟
    BTC/
      orderbook_history.json + .schema.json
  20260428T092349Z/         # 全套首次（27 endpoint）
    BTC/{27 endpoints}.json + .schema.json
    summary.md
  20260428T092920Z/         # 补跑 LS ratio 签名修复
  20260428T093007Z/         # 补跑 8 个失败/空 endpoint
  20260428T093253Z/         # 补跑 m2（429 重试）
```

## 附录 B：本次改动文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `backend/scripts/probe_coinglass.py` | 新增 | Coinglass 接口探测工具（独立，0 生产影响）|
| `backend/scripts/coinglass_probe_samples/2026-04-28T*Z/` | 数据 | 27 endpoint 真实样本 + 字段 schema |
| `backend/processors/key_level_freshness.py` | 修改 | `_safe_age` 兼容 `ts_sec` / `timestamp` |
| `backend/processors/orderbook_pressure.py` | 修改 | `data_quality="stale"` 真实赋值 + `stale_age_sec` 配置 |
| `backend/config/config.yaml` | 修改 | 移除 `spoofing_ttl_sec` 死配置 |
| `backend/tests/test_key_level_freshness.py` | 修改 | +3 测试覆盖字段兼容 |
| `backend/tests/test_orderbook_pressure.py` | 修改 | +3 测试覆盖 stale 赋值 |
| `LIQUIDITY_WALL_REVIEW.md` | 新增 | 本评审文档 |

---

# M1+M2 落地完成记录（2026-04-28）

## 一、最终采纳的 GPT 反馈精华

| GPT 建议 | 落地 |
|---|---|
| **`executed_usd_value` 是硬证据但不能全替代 taker/CVD** | ✅ 采纳。新设计中 `wall_consumed_confidence = 0.50×lo + 0.25×taker + 0.25×price` 三项加权，taker/CVD 仍参与 |
| **GPT `wall_consumed_confidence` 加权公式** | ✅ 直接复刻在 `_compute_wall_consumed_confidence` |
| **`ts_sec` 去重写入 deque（90s poll 撞 5m 帧）** | ✅ `polls/orderbook_pressure.py` 用 `existing_ts` set 跳过重复 |
| **`merge_pct` 上下限 clamp 0.05%–0.30%** | ✅ `_resolve_merge_pct` 强制 clamp |
| **暖机期 < 30min 标 warming** | ✅ 但仅在 `1 ≤ history_size < 4` 或时间 < 30min 时才标；history==0 时回退旧路径，避免破坏旧测试夹具 |
| **U 本位 / 币本位 OI 分流解释** | ✅ 新增 `oi_margin_split: coin_dominant/stable_dominant/balanced/unknown`，前端 chip "U本位主导(新资金加杠杆)" |
| **不要把 wall_events 写进 KL lifecycle** | ✅ 引擎只写 `state.orderbook_pressure_snapshot`，KL tracker 通过 `pressure_snapshot=` 注入只读引用 |
| **AI prompt 只给摘要 chip** | ⏳ 留 M3 KL 桥接阶段约束 |
| **1247×12 个 bin 不暴露给前端** | ✅ snapshot 只输出聚合后的 ≤ 5 个/侧 `WallZone`，原始 history 留在 backend deque |

## 二、最终路线图

| 里程碑 | 状态 | 用户诉求覆盖 |
|---|---|---|
| **Phase 0** 调研 + 3 bug 修复 | ✅ commit 3731d24 | 数据前置 |
| **M1** 墙观测层（聚合 + 持续性 + 趋势） | ✅ 本次 | 1/2/3 |
| **M2** 行为事件 + 拥挤度 + 磁铁 | ✅ 本次 | 4/5/6 |
| **M3** KL 桥接（只读 chip + contradiction，铁律守护） | ⏳ 待启动 | 关键位详情页墙提示 |

## 三、本次 M1+M2 改动文件清单

| 文件 | 类型 | 关键改动 |
|---|---|---|
| `backend/models/orderbook_pressure.py` | 扩展 | LargeOrderLifecycle +`exchange_name`；新增 WallZone / WallEvent / PositionCrowdingSnapshot / SweepTarget 4 个模型；OrderbookPressureSnapshot +`walls_above` / `walls_below` / `wall_zones` / `wall_events` / `crowding_global` / `history_window_minutes` / `sample_count_depth_history`；`data_quality` literal 加 `warming` |
| `backend/engine.py` | 扩展 | CoinState 加 `orderbook_depth_history: deque(maxlen=12)` |
| `backend/polls/orderbook_pressure.py` | 修改 | `limit=2 → 12`；按 `ts_sec` 去重写入 deque |
| `backend/polls/orderflow.py` | 扩展 | `_build_lifecycles` 解析 `exchange_name`（多所共振依据） |
| `backend/polls/derivatives.py` | 扩展 | `oi_exchange_rank` 加 `all_aggregated` key（6 周期 delta + U本位/币本位金额）|
| `backend/processors/orderbook_pressure.py` | 修改 | 主入口 `compute_pressure_snapshot` 末尾调用引擎，整合新字段；旧 `walls` 路径完全保留 |
| `backend/processors/liquidity_wall_engine.py` | **新增** | 700 行核心算法：M1 聚合/持续性/趋势 + M2 事件/拥挤度/磁铁/置信度/击穿风险 |
| `backend/tests/test_liquidity_wall_engine.py` | **新增** | 45 个测试覆盖 M0-M2 + KL 隔离铁律 |
| `frontend/src/lib/types.ts` | 扩展 | 8 个新类型 + OrderbookPressureSnapshot 7 个新字段 |
| `frontend/src/components/MainView/LiquidityWallCard.tsx` | **新增** | 墙区视图 + 暖机横幅 + 全局拥挤度 chips + "如果打穿"展开卡 |
| `frontend/src/components/MainView/OrderbookPressureView.tsx` | 修改 | 主视图按 `wall_zones` 存在性切换：新视图 / 旧视图 fallback；Footer 加滚动历史 + 墙区 + 事件计数 |

## 四、验收结果

| 维度 | 结果 |
|---|---|
| 后端 pytest | ✅ 1856 passed（前次 1811 + 新增 45）零回归 |
| 后端 ReadLints | ✅ 0 errors |
| 前端 `tsc --noEmit` | ✅ 0 errors |
| 前端 ESLint | ✅ 0 errors / 0 warnings |
| KL 铁律自动守护 | ✅ `test_kl_iso_walls_field_unchanged` 通过：旧 `walls` 字段 + KL 消费路径不变 |

## 五、用户 6 大诉求 ↔ 实现位置最终映射

| # | 诉求 | 数据字段 | 前端组件 |
|---|---|---|---|
| 1 | 上方哪里有卖墙 | `walls_above[]` | `LiquidityWallCard > WallSideCard(title="上方卖墙")` |
| 2 | 下方哪里有买墙 | `walls_below[]` | `LiquidityWallCard > WallSideCard(title="下方买墙")` |
| 3 | 多厚 / 多久 / 多源 | `current_usd / max_usd_1h / persistence_minutes / exchange_count` | `ZoneRow` 数据条（4 列）+ "多所共振"徽标 |
| 4 | 增强 / 减弱 / 撤 / 吃 / 重挂 | `status / trend / wall_consumed_confidence / wall_removal_risk` | `ZoneRow` 状态徽标条（active/strengthening/weakening/removed/consumed/reloaded/absorbed）|
| 5 | OI / 清算 / Funding / 拥挤 | `crowding_global` + `crowding_context.explain_chips` | `LiquidityWallCard > CrowdingChips`（顶部）+ 暖机横幅 |
| 6 | 打穿后下一磁铁和风险区 | `sweep_target.{magnet_price, vacuum_gap_pct} + break_through_risk` | `ZoneRow > BreakThroughCard`（点击"如果打穿"展开）|

## 六、铁律守护

```
不写：state.kl_history.* / KL.final_score / KL.strength_tier / KL.cascade_risk
不喂：AI prompt 中 wall_zones / wall_events 原始数据
只读：state.{taker_flow, cvd_*, oi_exchange_rank, multi_funding, ls_ratio, top_position_ratio,
            liq_max_pain, liq_summary, footprint_*, large_orders_history, orderbook_depth_history}
只写：state.orderbook_pressure_snapshot（向后兼容字段名 + 7 个新字段）
```

测试 `test_kl_iso_walls_field_unchanged` 自动验证：M1+M2 引擎调用后，旧 `walls` 字段 / `top_resistance` / `top_support` 仍由旧 `PressureWall` 路径填充，**不被新引擎污染**。这是 KL tracker 实际消费路径，铁律自动守护。

## 七、下一步（M3 桥接 · 待批准启动）

1. `KeyLevelV2.behavior` 加 `wall_context_ref`（只读引用 nearby zone 的 strength/status/persistence）
2. `_apply_pressure_alignment` chip 升级：`ob_strong_bid → 稳定买墙 31m / 卖墙撤单风险 / 上方清算磁铁`
3. `behavior_eval._detect_contradictions` 加 3 条规则：
   - 强支撑 + 买墙 removed → contradiction(medium)
   - 突破阻力 + 卖墙 reloaded → contradiction(high)
   - 支撑 + 买墙 consumed + next_magnet 在下方 → contradiction(high)
4. AI prompt：只追加 wall 摘要 chip 到 MAA facts，不给原始 zones/events
5. KL `final_score / strength_tier / cascade_risk` 自动测试守护不变（铁律）

预估 1-2 天，等用户拍板。
