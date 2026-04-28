# 流动性墙 + 大单行为 + 持仓拥挤度监测引擎 — 审核快照

> **用途**：外部 AI / 审计方的当前状态快照（非过程文档，过程见 `LIQUIDITY_WALL_REVIEW.md`）  
> **最后更新**：2026-04-28（Phase A：双源融合 + 主动攻击因子 + 按币分层）  
> **当前阶段**：M1 + M2 + M2.5 + **Phase A**（基于 spot probe 实测发现升级）  
> **回归基线**：后端 **1881 passed** · 前端 tsc 0 错 · 本次改动 ESLint 0 错 0 警  
> **代码量**：引擎 ~1530 行 / 测试 1130 行（**70 用例**）/ 前端卡片 ~620 行 / 模型 ~410 行

---

## 一、模块定位与设计哲学

**定位**：合约 5m 订单簿热力图 + 多源大单行为 + 全局拥挤度 + 清算磁铁的综合监测引擎。

**铁律（模块独立性）**：
- 引擎只**读** `CoinState`，仅**写** `CoinState.orderbook_pressure_snapshot`
- **不直接修改** `KeyLevelV2` 字段（`final_score` / `strength_tier` / `cascade_risk`）
- 输出供 KL Tracker 作 "tier-based 共振判定" 的辅助参考，而不是评分输入
- KL 隔离测试：开关 M1+M2 引擎，KL 关键字段哈希不变（测试中已断言）

**问题域（用户的 6 大诉求）**：

| # | 诉求 | 落地方式 |
|---|---|---|
| 1 | 上方哪里有卖墙 | `walls_above[]` |
| 2 | 下方哪里有买墙 | `walls_below[]` |
| 3 | 多厚 / 多久 / 多源确认 | `current_usd` / `visible_minutes` / `has_spot_confluence` + `exchange_count` |
| 4 | 增强 / 减弱 / 撤掉 / 被吃 / 重挂 | `wall_events[]` 时间线 + 每 zone `trend` / `status` |
| 5 | 附近 OI / 清算 / Funding / 多空拥挤 | `crowding_global` 全局 chips + 每 zone `crowding_context` |
| 6 | 打穿后下一磁铁 / 风险区 | `sweep_target.magnet_price` + `vacuum_gap_pct` + `break_through_risk` |

附加洞察：**现货墙 = 真买卖家（真金白银），合约墙 = 流动性 + 清算磁铁（高杠杆挂单常是被扫目标）**——用 `trust_score` 阶梯 + `dual_source` 标记区分。

**Phase A 重大升级（基于 `/api/spot/orderbook/history` probe 实测）**：
- ✅ 接入**现货 5m 深度热力图**（与合约同结构，bin 间距 100 USD，1132 bids / 498 asks）
- ✅ **双源融合 zone**：合约 5m + 现货 5m 同价区共振 → `dual_source=True` → `source="spot+depth"` → trust_score +0.30（最强单一证据）
- ✅ **现货独立 zone**：未被合约 zone 覆盖的现货厚度 → `source="spot_only"`
- ✅ **主动攻击因子**：`active_attack_score`（taker 同向 + cvd_spot 同向 trend）接入 `break_through_risk`，回应 GPT P1-3
- ✅ **按币分层 seed_min_usd**：BTC 5M / ETH 2M / SOL 800K（避免 BTC 假厚度 / SOL 一墙皆无）
- ✅ **配额优化**：`spot_large_orders` 120 → 240s，`large_orders` 120 → 180s，`spot_orderbook_pressure` 120s（净 -0.5 calls/min）

---

## 二、Coinglass API 端点清单

所有端点经 `backend/scripts/probe_coinglass.py` 实测验证（27 个端点 schema + 真实样本 dump）。

### 2.1 本模块新增 / 强化使用的端点

| 端点 | 用途 | 频率 | cache_ttl | 函数 |
|---|---|---|---|---|
| `/api/futures/orderbook/history` | 合约 5m 深度热力图（**12 帧滚动** = 1h history）| 90s | 60s | `fetch_orderbook_heatmap(exchange, symbol, interval, limit=12)` |
| `/api/spot/orderbook/history` ⭐ **Phase A 新增** | 现货 5m 深度热力图（双源融合 zone 关键源）| 120s | 60s | `fetch_spot_orderbook_heatmap` |
| `/api/futures/orderbook/large-limit-order` | 合约大单 holding 快照 | 180s | 60s | `fetch_large_orders` |
| `/api/futures/orderbook/large-limit-order-history` | 合约大单 ended lifecycle | 180s | 默认 | `fetch_large_orders_history` |
| `/api/spot/orderbook/large-limit-order` (M2.5) | 现货大单 holding（区分真买家/卖家）| 240s | 60s | `fetch_spot_large_orders` |
| `/api/spot/orderbook/large-limit-order-history` (M2.5) | 现货大单 ended lifecycle | 240s | 默认 | `fetch_spot_large_orders_history` |
| `/api/futures/openInterest/exchange-list` | OI 多周期 delta + 币本位/U本位拆分（已强化解析）| 120s | 默认 | `fetch_oi_exchange_list` |
| `/api/futures/liquidation/aggregated/max-pain` | 清算磁铁价（每 zone 的 sweep_target 源）| 300s | 默认 | `fetch_liquidation_max_pain` |

### 2.2 复用的现有数据（零新增 poll）

| 数据源 | CoinState 字段 | 用途 |
|---|---|---|
| Funding 多所聚合 | `multi_funding` / `funding_history_8h` | 拥挤度（`funding_now_pct` / 历史趋势）|
| LS Ratio | `ls_ratio` / `top_position_ratio` | 拥挤度（`top_position_ls_ratio`）|
| Taker Volume / CVD | `taker_flow` | wall_consumed_confidence 加权 + **Phase A active_attack_score** |
| 现货 CVD | `cvd_spot.trend_1h` | **Phase A active_attack_score**（同向趋势 +0.50） |
| Footprint 5m | `footprint_contract` / `footprint_spot` | absorption_zone 共振判定 |
| Liquidation Heatmap | `liq_summary` | sweep_target 备选 |
| Candles 4h | `candles_4h` | ATR(14) → merge_pct 自适应 |

### 2.3 Quota 评估（Phase A 修正后）

| 维度 | 实际值 | 来源 |
|---|---|---|
| **真正瓶颈** | `rate_limit_per_min: 10`（FixedIntervalLimiter 7s 间隔）→ **实际峰值 ~8.6/min** | `backend/sources/coinglass.py:24-32` |
| 日上限 | 50,000/day（实测 daily_usage 12-381，0-0.8%）| Coinglass 协议 |
| 活跃币 | **3（BTC / ETH / SOL）**——`allow_coins` 决定 | `backend/config/config.yaml: allow_coins` |
| 全引擎需求估算 | **~35 次/min**（Phase A 调优后较优化前 -1.5/min）| `backend/engine.py:802-920` |
| 实际表现 | 限速器排队抹平 → 实际等效 8-10/min | `coinglass_daily_usage` |

**Phase A 配额调整账（净 -0.5 calls/min，反向释放预算）**：

| 项 | 调整 | 净配额变化 |
|---|---|---|
| `large_orders` 120 → 180s | 3 币 × (60/180 - 60/120) | -0.5 calls/min |
| `spot_large_orders` 120 → 240s | 3 币 × (60/240 - 60/120) | -1.5 calls/min |
| ⭐ **新增 `spot_orderbook_pressure` 120s** | 3 币 × 60/120 | +1.5 calls/min |
| **合计** | | **-0.5 calls/min** |

**关键判断**：Phase A 通过"配额优化释放 ≥ 新接入消耗"——既接入了现货 5m 热力图（双源融合关键源），又**反而释放**了 0.5 calls/min。

**结论**：
1. ❌ **不是 daily quota 触底**（远未达 50K/day）
2. ✅ **是 10/min 限速器排队挤压**（真实瓶颈）
3. ✅ **Phase A 净 -0.5 calls/min**，对系统压力**降低**

---

## 三、核心算法分层

> **行号说明**：下文 `line XXX` 行号在 Phase A 后有约 ±100 行偏移，请以 grep 函数名为准。

### 3.1 M1 — Wall Observation Layer

**目标**：把 1247 个 bin（合约 5m 热力图）压成 ≤ 5 个有意义的 zone。

**关键决策（避免初版 "全 bin 灌水成一片墙" bug）**：

```python
# 1. 种子 bin 双闸（backend/processors/liquidity_wall_engine.py:362）
seed_min_usd = 1_000_000   # ≥ 1M USD 才算"显著厚度"
top_seed_count = 30        # 每侧最多 top 30 个种子参与合并

# 2. 自适应 merge_pct（line 145）
merge_pct = clamp(0.15 × ATR / last_price, 0.05%, 0.30%)

# 3. zone 边界由"相邻种子合并"决定（line 179）
#    相邻种子价差 ≤ merge_pct 才合并

# 4. zone 量化指标（line 263）
#    current_usd / max_usd_1h / avg_usd_1h = 价区内全 bin USD（流动性视图）
#    seen_count / visible_minutes = 该价区是否有 ≥ seed_min 种子（墙真实存在）
#    两层语义分离：max/avg 不失真 + visible 严格判定
```

**实测产出（同份真实 BTC 12 帧 history）**：

| 指标 | 修复前（全 bin 合并）| 修复后（种子机制）|
|---|---|---|
| ask 最大 zone 跨度 | $76,740–85,700（**8,960 USD**）| $76,960–77,000（**40 USD**）|
| ask 最大 zone USD | **12.5 亿**（无意义）| **2,737 万**（合理墙厚）|
| zone 数 | 2 个（一片）| 5 个（散点）|

**视图字段**：
- `walls_above` / `walls_below`：每侧最多 5 个 `WallZone`
- `WallZone` 共 24 字段（含 M2/M2.5 增量）

### 3.2 M2 — Behavior + Crowding + Magnet

**6 种事件类型**（`backend/processors/liquidity_wall_engine.py:1030`）：

| 事件 | 触发条件 | 后端字段 | 前端 chip |
|---|---|---|---|
| `wall_appeared` | 上一帧无、本帧有 | size_after_usd | ✨ 出现 |
| `wall_strengthened` | current ≥ avg ×1.2 + 持续上升 | size_before/after | ↑ 增厚 |
| `wall_weakened` | current ≤ avg ×0.85 + 持续下降 | size_before/after | ↓ 减薄 |
| `wall_removed` | large_order ended 且 executed_usd_value < 30% | size_before | ✗ 撤掉 |
| `wall_consumed` | large_order ended 且 executed_usd_value ≥ 30% | executed_usd_value | 🔥 被吃 |
| `wall_reloaded` | 撤后 < 10min 同价位重挂 | size_after | ↻ 重挂 |

**`wall_consumed_confidence` GPT 加权公式（line 940）**：
```python
confidence = (
    0.50 × large_order_executed_score   # 大单 executed_usd_value 占比
  + 0.25 × taker_pressure_score          # CVD/Taker volume 同向爆量
  + 0.25 × price_through_score           # 价格穿透深度
)
```

**`wall_removal_risk` 软分（line 998）**：避免硬贴"假单"标签——综合大单 ended 状态、撤离速度、价格反向，输出 0-1 概率。

**`PositionCrowdingSnapshot`（line 664）**：
- `oi_delta_1h_pct` / `oi_delta_24h_pct`（来自 oi_exchange_list "All" 行）
- `oi_margin_split`：`coin_dominant` / `stable_dominant` / `balanced`（GPT 提议的"币本位 vs U本位"区分）
- `funding_now_pct` / `funding_avg_8h_pct`
- `top_position_ls_ratio`（大户多空比，强信号）
- `inferred_position_state`：5 态（`long_opening` / `short_opening` / `long_closing_or_liquidation` / `short_covering_or_liquidation` / `liquidation_flush` / `mixed`）
- `long_crowding_risk` / `short_crowding_risk` ∈ [0,1]

**`SweepTarget`（line 857）**：
- `magnet_price`：来自 `liq_max_pain.24h` 或 `4h`
- `vacuum_gap_pct`：墙到磁铁之间最大相邻 bin 价差（≥ 0.5% 标"真空大"）
- `direction`：above / below
- `magnet_amount_usd`：清算金额

**`break_through_risk`（line 1000，Phase A 升级）**：
```python
# 静态因素（≤ 0.95）+ Phase A 主动攻击因子（最多 +0.20）
risk = clamp(
    0.30 × (1 if current/max_usd_1h < 0.5)             # 厚度衰减
  + 0.20 × (1 if persistence_score < 0.3)              # 持续性差
  + 0.20 × (1 if magnet_distance_pct < 0.5)            # 磁铁很近
  + 0.15 × (1 if vacuum_gap_pct >= 0.5)                # 真空跨度大
  + 0.10 × (1 if crowding_risk >= 0.6)                 # 同向拥挤（Phase A 0.15→0.10）
  + 0.20 × active_attack_score                         # Phase A 新增
, 0, 1)
```

`active_attack_score` 公式（line 920）：
```python
score = 0
# 1. taker 同向占比（线性映射 0.5→0, 0.6→1.0）
if same_side_ratio > 0.5:
    score += 0.50 × clamp((same_side_ratio - 0.5) / 0.10, 0, 1)
# 2. cvd_spot 同向 trend
if cvd_spot.trend_1h in same_direction_trends:
    score += 0.50
return clamp(score, 0, 1)
```

### 3.3 M2.5 — 现货 vs 合约 区分

**核心洞察（用户提出）**：
- 现货墙 = 真买卖家 → **真支撑/真阻力候选**
- 合约墙 = 流动性 + **清算磁铁**（被扫目标）
- 双源共振 = **最强单一证据**

**M2.5 算法（line 595，spot 大单 lifecycle augment）**：

```python
def _augment_zones_with_spot_large_orders(zones, spot_large_orders, cfg):
    for z in zones:
        tol = max(z.peak_price × 0.001, 5.0)   # 0.1% 或 5 USD
        for lo in spot_holding:
            if z.price_low - tol ≤ lo.limit_price ≤ z.price_high + tol:
                z.has_spot_confluence = True
                z.spot_large_order_ids.append(lo.id)
```

### 3.4 Phase A — 现货 5m 热力图双源融合（基于 spot probe 实测落地）

**为何能做（probe 实测发现）**：`/api/spot/orderbook/history` 与合约 `/api/futures/orderbook/history` **同结构**，bin 间距 100 USD（合约 5-10 USD），1132 bids + 498 asks。**实测 BTC**：$80,000 现货卖墙 1.56亿 + 合约同价位 2,051万 → **同价位 1.76 亿双源压顶**——这才是"真支撑/阻力"该出现的硬证据。

**算法分层（`liquidity_wall_engine.py`）**：

```python
# 1. _augment_zones_with_spot_depth（line 535）
#    在已有合约 zone 价区上累加现货厚度
def _augment_zones_with_spot_depth(zones, spot_history, cfg):
    for z in zones:
        cur_usd = sum(b.usd_value for b in latest_spot_bins
                       if z.price_low ≤ b.price ≤ z.price_high)
        max_usd = max(per_frame_totals)
        z.spot_current_usd = cur_usd
        z.spot_max_usd_1h = max_usd
        if cur_usd ≥ wall_min_usd and max_usd ≥ wall_min_usd:
            z.dual_source = True
            z.source = "spot+depth"

# 2. _build_spot_only_zones（line 580）
#    在现货 history 上独立跑 _build_zones_for_side，过滤掉与合约 zone 重合的部分
def _build_spot_only_zones(spot_history, last_price, side, atr, cfg, excluded_price_ranges):
    zones = _build_zones_for_side(spot_history, last_price, side, atr, cfg)
    return [z for z in zones if not in_any(z.peak_price, excluded_price_ranges)]
    # 输出 source="spot_only"
```

**Phase A trust_score 阶梯（line 670）**：

```python
score = 0.50                                  # base
if zone.dual_source:
    score += 0.30   # ⭐ 双源共振（最强单一证据）
if zone.has_spot_confluence:
    score += 0.15   # 现货大单 lifecycle 共振
if zone.exchange_count >= 2:
    score += 0.10   # 多家共振
if zone.persistence_score >= 0.70:
    score += 0.10   # 持久
return clamp(score, 0, 1)
```

**前端来源徽章（5 档互斥）**：

| 条件 | 标签 | 颜色 |
|---|---|---|
| `dual_source=True` | `💎 双源卖墙/买墙` | 琥珀（最强）|
| `source="spot_only"` | `💰 仅现货墙` | 青色 |
| `has_spot_confluence` | `💰 现货共振 ×N` | 绿 |
| `trust_score ≥ 0.65` | `⚡ 较可信` | 蓝 |
| `trust_score < 0.55` 且仅合约大单 | `⚡ 仅合约` | 橙（警示）|

**A6 按币分层 seed_min_usd**：

| 币种 | seed_min_usd | 理由 |
|---|---|---|
| BTC | 5,000,000 | 高市值，单 bin 1-15M USD；500K 阈值会出现"假厚度" |
| ETH | 2,000,000 | 中市值 |
| SOL | 800,000 | 低市值，单 bin 50-500K USD；阈值过高会一墙皆无 |
| 其他 | 1,000,000 | ENGINE_DEFAULTS 默认 |

存储位置：`config.processors.orderbook_pressure.seed_min_usd_by_coin`，在 `build_liquidity_wall_outputs` 入口按 `state.coin` 动态覆盖 cfg。

---

## 四、关键代码索引

### 4.1 后端
| 文件 | 用途 | 关键函数 |
|---|---|---|
| `backend/sources/coinglass.py:474–525` | 端点封装 | `fetch_orderbook_heatmap` / `fetch_large_orders[_history]` / `fetch_spot_large_orders[_history]` |
| `backend/polls/orderbook_pressure.py` | 5m 深度 history 写入 deque | `poll_orderbook_pressure`（limit=12，按 ts_sec 去重）|
| `backend/polls/orderflow.py:278–386` | 大单 lifecycle 双轨合并 | `poll_large_orders` / `poll_spot_large_orders` |
| `backend/polls/derivatives.py` | OI 全聚合 + 多周期 delta | `poll_oi_exchange_rank`（写入 `state.oi_exchange_rank.all_aggregated`）|
| `backend/engine.py:160–170, 836–840, 1290–1296` | CoinState 字段 + poll loop 注册 | `orderbook_depth_history` deque(12) / `large_orders_history` / `spot_large_orders_history` |
| `backend/models/orderbook_pressure.py:49, 207, 244, 271, 295` | 5 个核心模型 | `LargeOrderLifecycle`(18 字段) / `WallZone` / `WallEvent` / `PositionCrowdingSnapshot` / `SweepTarget` |
| `backend/processors/liquidity_wall_engine.py:1187` | 主入口 | `build_liquidity_wall_outputs` |
| `backend/processors/orderbook_pressure.py` | 旧主入口（兼容路径）| `compute_pressure_snapshot`（末尾调 build_liquidity_wall_outputs）|
| `backend/processors/key_level_freshness.py` | 含修复后的 `_safe_age` | 兼容 ts/ts_sec/timestamp 三种时间字段 |

### 4.2 前端
| 文件 | 用途 |
|---|---|
| `frontend/src/lib/types.ts:1086–1300` | TS 类型镜像 (WallZone / WallEvent / PositionCrowdingSnapshot / SweepTarget) |
| `frontend/src/components/MainView/LiquidityWallCard.tsx` | M1+M2+M2.5 主卡（5 子组件：CrowdingChips / WallSideCard / ZoneRow / WallEventsTimeline / BreakThroughCard）|
| `frontend/src/components/MainView/OrderbookPressureView.tsx` | Tab 容器（hasWallZones \|\| isWarming → 新视图，否则 fallback 旧 StrongPressureCard）|

### 4.3 配置 / 文档
| 文件 | 用途 |
|---|---|
| `backend/scripts/probe_coinglass.py` | 27 端点 probe 工具（dev 用，dump schema + 样本，FixedIntervalLimiter 友好）|
| `backend/scripts/coinglass_probe_samples/` | 各端点 schema/sample 留存（.gitignore 过滤大 .json，仅留 schema）|
| `LIQUIDITY_WALL_REVIEW.md` | 过程文档（M1+M2 完成历史 + GPT 反馈消化记录，607 行）|

---

## 五、前端可视化（用户最终看到什么）

```
┌─────────────────────────────────────────────────────────────┐
│ 📊 拥挤度 OI(1h) -0.10% · OI(24h) -3.95% · Funding -0.003%  │  ← 全局 chips
│        U本位主导(新资金加杠杆)                                │
├──────────────────────────────┬──────────────────────────────┤
│ 🟥 上方卖墙 5个墙区           │ 🟩 下方买墙 5个墙区           │
│                              │                              │
│ $76,500 — $76,500  +0.32% A │ $75,960 — $76,000  -0.36% S │
│ ━━━━━━━━━━━━━━━━━            │ ━━━━━━━━━━━━━━━━━━━━━━        │
│ 当前 9百万 1h峰值 1千万        │ 当前 4千万 1h峰值 4千万        │
│ 持续 55min 峰值 $76,500 (1)  │ 持续 55min 峰值 $76,000 (3)  │
│ [增厚 ↑] [💎 真阻力]          │ [稳定 ─] [💰 现货共振 ×2]    │  ← M2.5 标签
│ ▶ 如果打穿 → 磁铁 $77,811     │ ▶ 如果打穿 → 磁铁 $76,009    │  ← 折叠预览
│   (+2.04%) · 风险 0%         │   (-0.32%) · 风险 20%        │
│                              │                              │
│ ... 4 more zones ...         │ ... 4 more zones ...         │
└──────────────────────────────┴──────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│ 📜 行为事件流  最近 4 条 · 倒序                               │
│ 20:43 $77,185 卖墙 ↑增厚 +6百万 墙增厚 34%                60%│
│ 20:43 $76,955 卖墙 ↑增厚 +5百万 墙增厚 42%                60%│
│ 20:43 $76,500 卖墙 ↑增厚 +3百万 墙增厚 57%                60%│
│ 20:43 $73,815 买墙 ↑增厚 +2千万 墙增厚 63%                60%│
└─────────────────────────────────────────────────────────────┘
▼ 展开完整明细（所有 wall · 高阶视图）   ← 旧 StrongPressureCard fallback
```

---

## 六、模块独立性证据

```python
# backend/tests/test_liquidity_wall_engine.py - TestKLIsolation
def test_kl_isolation_with_engine_enabled():
    """开关 M1+M2 引擎，KL final_score / strength_tier / cascade_risk 不变。"""
    state_before = build_state_without_engine()
    state_after = build_state_with_engine()
    
    for kl in state_after.key_levels:
        before = state_before.find_kl(kl.price)
        assert kl.final_score == before.final_score
        assert kl.strength_tier == before.strength_tier
        assert kl.cascade_risk == before.cascade_risk
```

引擎对 KL 的"附加值"通过外部观察：KL Tracker 读 `state.orderbook_pressure_snapshot` 做 tier-based 共振判定，但此判定**不写回 KL 字段**，仅作前端一层 chip 显示。

---

## 七、测试覆盖

```
backend/tests/test_liquidity_wall_engine.py: 70 用例
├── TestModels                 (4)  4 模型序列化往返 + 18 字段
├── TestMergePct                (3)  ATR clamp 上下界 + 默认
├── TestMergeBins               (5)  相邻合并 + 远距分离 + min_usd 过滤 + 跨距过滤
├── TestPersistence             (6)  full_history / cold_start / strengthening / weakening
│                                   + visible_minutes 严格判定 + 部分历史
├── TestAugmentTol              (5)  容差匹配 + 5 USD 错位 + 远离 + 错侧 + 多家共振计数
├── TestSpotConfluenceAndTrust  (10) spot augment + trust 6 档（含 dual_source）+ clamp ⭐ M2.5+Phase A
├── TestWarming                 (4)  empty / short / fresh / sufficient
├── TestM2Confidence            (8)  GPT 加权公式 + 边界
├── TestM2Crowding              (7)  OI margin split + inferred 5 态 + crowding 边界
├── TestSweepAndBreakThrough    (5)  vacuum_gap + break_through_risk（含 active_attack 验证）
├── TestZoneEvents              (5)  6 事件触发条件
├── TestPhaseA                  (8)  ⭐ Phase A：dual_source augment + spot_only zones
│                                          + active_attack（同向/逆向）
│                                          + break_through_risk 含 attack 因子
│                                          + seed_min_usd_by_coin 动态覆盖
└── TestKLIsolation             (1)  KL 关键字段哈希不变 (铁律)

后端全量：1881 passed
前端 tsc：0 错
本次改动 ESLint：0 错 0 警
```

---

## 八、commit 时间线（设计演进）

| commit | 内容 | 测试基线 |
|---|---|---|
| `3731d24` | Phase 0：probe 27 端点 + 3 个长期 bug 修复 | 1811 |
| `a4b24dc` | M1+M2：流动性墙引擎主体（4 模型 + 引擎核心 + 前端 LiquidityWallCard）| 1856 |
| `334ed90` | 修复"zone 全 bin 灌水"算法 bug（种子机制 + top-N 双闸）| 1856 |
| `ea512fd` | 4 项诉求覆盖完善：visible_minutes bug + 大单容差 + 事件流 + 打穿预览 | 1863 |
| `9706153` | M2.5 现货 vs 合约（spot poll + trust_score + 三档互斥标签）| 1871 |
| **Phase A** | **现货 5m 热力图双源融合 + active_attack + seed_by_coin + 配额优化** | **1881** |

---

## 九、已知限制与盲点

| # | 限制 | 影响 | 备注 |
|---|---|---|---|
| L1 | Coinglass 大单 API 单家局限 | `exchange_count` 几乎永远 = 1，多所共振加分（+0.10）极难触发 | 解法：未来主动多家 poll（OKX/Bybit），quota 充足时启用 |
| L2 | 合约 5m 订单簿热力图来源单一（仅 Binance）| 多源指 large_orders + 现货热力图，不指合约 orderbook 多所 | 与 L1 同根 |
| ~~L3~~ | ~~spot 5m 订单簿热力图未启用~~ | ✅ **Phase A 已落地**：`spot_only` zone + `dual_source=True` 已支持 | — |
| L4 | wall_events 暖机期内不渲染 | 重启后 1h 内事件流为空 | 设计取舍：避免冷启动误导 |
| L5 | break_through_risk 模型权重未做参数搜索 | 0.30/0.20/0.20/0.15/0.10/0.20×attack 是经验值，未对历史数据回测调优 | 待 M3 桥接 KL 后用历史回测调参 |
| L6 | trust_score 阈值（0.85/0.65/0.55）未历史校准 | 高可信档可能过高/过低 | 落地 1-2 周后视实际频率调整 |
| L7 | wall_consumed_confidence 0.5/0.25/0.25 权重 | GPT 提议而非历史拟合 | 同 L5 |
| L8 | 信任标签互斥 vs 并存 | 当前 5 档互斥，但理论上多重证据可同时存在 | 设计取舍：避免标签泛滥；后续可加细节面板展开 |
| L9 | Hyperliquid whale positions 未接入 | GPT 提及但未实现 | M4 候选项 |
| ~~L10~~ | ~~spot CVD 未联动 break_through_risk~~ | ✅ **Phase A 已落地**：`active_attack_score` 同向加分 | — |
| L11 | OI 分流 (`coin_dominant` vs `stable_dominant`) 未参与 trust_score | 当前仅展示 chip，未参与 zone 评分 | 后续可加 |
| L12 | AI Snapshot 未集成 wall 数据 | AI Analyzer 看不到墙 / 事件 / 拥挤度的结构化输入 | M3 后跟进 |
| L13 | spot 热力图 bin 间距 100 USD（合约 5-10）| 现货 zone 边界比合约粗，目前用合约 zone 作主路径锚点已规避 | Phase A 设计取舍 |
| L14 | `dual_source` 判定阈值（≥ wall_min 500K 单帧 + max）| BTC 用 5M seed 触发更难，可能某些价位"现货厚但单帧不达标"被漏 | 落地观察后视情况降低 wall_min 或加滚动窗口 |

---

## 十、下一步规划

### 10.1 短期（1-2 周内）

| 项 | 内容 | 预期改动 |
|---|---|---|
| **M3** KL 桥接 | zone 与最近 KL 关联（read-only），输出"墙在 KL ±0.5% 内"标志，前端 KL 卡片显示 chip | KL Tracker 加一层非破坏性观察；测试增强 |
| **观察期** | 让 trust_score / break_through_risk 在生产跑 1-2 周收集数据 | 不改代码 |
| **阈值校准** | 基于观察数据调 trust_score 阈值（0.85/0.55）+ break_through 权重 | 调 ENGINE_DEFAULTS |
| **M4-AI 集成** | AI Snapshot 注入 wall_zones / wall_events / crowding_global，让 AI Analyzer 用 | `ai/snapshot.py` 扩展 |

### 10.2 中期（1 个月）

| 项 | 内容 | 风险 |
|---|---|---|
| 多家交易所大单 poll | OKX / Bybit / Bitget 单独拉 large_orders（区分真多家共振）| Quota：+~3K/day（仍远低于 5w/day limit）|
| spot CVD 联动 | spot 净买卖压力 vs 现货墙位置，输出"现货资金行为标签" | 复用 spot_aggregated_cvd 已有 endpoint |
| Hyperliquid whale | 接入 hyperliquid_whale_position，做 Hyperliquid 大资金区位识别 | 已 probe 验证可用 |

### 10.3 长期（待评估）

| 项 | 内容 |
|---|---|
| 历史回测 | wall_consumed_confidence / break_through_risk 权重历史拟合 |
| 多空双向墙 | 同一价位同时是 R/S，分析"夹层结构" |
| 跨币种共振 | BTC 大墙 vs ETH/SOL 大墙的相关性 |

---

## 十一、外部审核重点关切（建议审计方关注）

1. **算法正确性**：种子 bin 双闸是否过度过滤？merge_pct clamp 上下界是否合理？请核对 `_build_zones_for_side`（line 362）
2. **trust_score 权重**：0.50/0.25/0.15/0.10 是否平衡？请审 `_compute_trust_score`（line 557）
3. **wall_consumed_confidence 公式**：0.5/0.25/0.25 是否过度倚重 large_orders？请审 line 940
4. **KL 隔离铁律**：是否有遗漏字段被引擎间接修改？请审所有 `state.orderbook_pressure_snapshot` 写入路径
5. **Quota 风险**：spot 大单接入后是否在所有币种 / 所有交易场景下安全？请审 30 币 × 4 calls/cycle 的实际峰值
6. **trust_score 阈值（0.85 / 0.55）**：是否反映实际 BTC / ETH / 小币市场？建议请审计方提供历史数据样本
7. **L1 / L2 限制**：单家 orderbook 局限是否需要立即 mitigate？
8. **暖机期处理**：1h 暖机期是否过长？是否可缩短到 30min？
9. **前端默认 fallback**：旧 StrongPressureCard 路径是否还有用？或可下线？

---

**审计联系点**：所有问题可对照 commit 时间线（第 8 节）+ 关键代码索引（第 4 节）追溯具体决策点。
