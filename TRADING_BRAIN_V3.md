# 交易大脑大屏 v3 · 实施方案与算法审查文档

> 本文档面向第三方 AI / 资深工程师审查使用。聚焦「数据→模型→算法→接口→前端」全链路，
> 以及落地过程中所有关键决策的取舍依据。所有引用文件均给出绝对路径与行号锚点。
>
> 版本：v3（2026-04-29 完成）
> 入口：`/brain/{coin}`，REST：`GET /api/trading-brain/{coin}`
> 上游引擎：KeyLevelV2 / OrderbookPressure / LiquidationMap（均为既有权威输出，本系统**只读消费、不重新打分**）

---

## 0. 设计哲学（铁律）

| # | 铁律 | 落地约束 |
|---|---|---|
| 1 | **只读聚合，不重新打分** | 不动 `KeyLevelV2.final_score / strength_tier / cascade_risk`；不动 `WallZone` 任何评分字段 |
| 2 | **不输出交易指令** | 后端字段 long/short/neutral 仅描述结构方向；前端 UI 必须转译为「做多观察 / 做空观察 / 等待」 |
| 3 | **支撑/阻力 ≠ 清算磁铁** | 现货墙 / Coinbase 共振 / 关键位 → 防守位；清算簇 / max_pain → 磁吸目标位；二者**绝不混用** |
| 4 | **「打穿风险评分」不当概率** | `break_through_risk` 是 0–1 的相对评分（已校准的 wall_consumed / removal_risk 综合），UI 文案严禁出现 "概率"/"%" |
| 5 | **数据未就绪显式可见** | `data_quality.is_partial_ready` + ready_count/total_count 暖机期前端必须 banner |
| 6 | **观察区有清晰的失效结构** | 每个 setup 必须含 `soft_invalidation` / `hard_stop` / `cancel_conditions`；缺一不予输出 |
| 7 | **PriceZone 是唯一展示单元** | 同价位的现货墙 / 关键位 / 清算簇必须**先聚合到一个 zone 再上屏**，禁止三层独立列表 |

---

## 1. 系统总体架构

```
┌────────────────────────── 上游既有引擎 (只读) ──────────────────────────┐
│  KeyLevelV2 Engine        OrderbookPressure Engine     LiquidationMap   │
│   ↓ KeyLevelSnapshotV2     ↓ OrderbookPressureSnapshot   ↓ LiquidationMap│
│   - levels[]               - walls_above[] / walls_below[]              │
│   - magnet_levels[]        - wall_zones[] / wall_events[]               │
│   - regime / cvd / ...     - dual_source / coinbase_spot_*              │
└────────────────────────────────────────────────────────────────────────┘
                                   │
                ┌──────────────────┴──────────────────┐
                ▼                                     ▼
   ┌──────────────────────────┐         ┌────────────────────────────┐
   │ trading_brain_builder.py │  ───▶   │  TradingBrainSnapshot      │
   │   1. 收集 RawPiece        │         │    - zones[]                │
   │   2. 距离容差合并 cluster │         │    - rankings              │
   │   3. zone 角色分类        │         │    - opportunities[]       │
   │   4. ranking 分桶         │         │    - spot_book / fut_book  │
   │   5. spot/fut book 视图   │         │    - events[]               │
   │   6. opportunity 派生     │         │    - data_quality          │
   │   7. 状态机推进           │         │    - context (chips)       │
   └──────────────────────────┘         └────────────────────────────┘
                                                     │
                            REST GET /api/trading-brain/{coin}
                                                     ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Frontend /brain/[coin]  (Next.js 14 + Tailwind + Zustand)      │
   │  ─ TopStrip            (CVD 背离灯 / context chips / 数据质量)  │
   │  ─ PriceAxisMap        (SVG 价格轴 + zone 块；点击/hover 联动)   │
   │  ─ ZoneDetailCard      (选中 zone 的证据链 / 情景说明)            │
   │  ─ OpportunityBoard    (Kanban：等待触发/等待/做多观察/做空观察)  │
   │  ─ SpotOrderBookPanel  (近/中/远三段折叠；现货+合约厚度拼接)     │
   │  ─ FuturesHeatmap      (合约侧热力柱 + 磁铁 ◆ 叠加)              │
   │  ─ EventTimeline       (近 30min wall_events 时间轴)             │
   └─────────────────────────────────────────────────────────────────┘
```

数据全链路在浏览器侧每 **45 秒** 主动 poll 一次（`frontend/src/app/brain/[coin]/page.tsx:28-32`），无 WebSocket 推送以避免与既有 `market_update` 频道冲突。

---

## 2. REST 接口契约

### 2.1 `GET /api/trading-brain/{coin}`

**位置**：`backend/api/routes.py:750-806`

**入参**：
| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `coin` | path | – | BTC / ETH / SOL（受 `supported_coins` 配置约束） |
| `max_zones` | query int | 24 | 最多输出 zone 数（1–64） |

**响应**：`TradingBrainSnapshot` JSON，结构详见 §3。

**错误码**：
- `400`：`Unsupported coin`
- `503`：引擎未启动 / 无 ticker（仅当无任何价格数据时）
- `200`：即使所有数据源缺失也返回，依靠 `data_quality.is_partial_ready` 与 `notes` 表达缺口（避免暖机期前端硬错）

**幂等性 / 缓存**：纯读取 `engine._states[coin]`，无副作用；调用方可自行 60s TTL 缓存。

---

## 3. 数据模型（Pydantic）

**位置**：`backend/models/trading_brain.py`（前端镜像 `frontend/src/lib/types.ts:2191-2400`）

### 3.1 `BrainPriceZone`（核心展示单元）

```python
zone_id: str                # f"{coin}_{sha1(price_bounds|idx)[:12]}" 跨帧稳定
price_low / price_high / price_mid / distance_pct
roles: BrainZoneRoles       # 5 个布尔标记（多角色可同时为真）
dominant_label: str         # 人类可读 ("多源争夺区" / "清算磁铁")
dominant_role: DominantRole # 6 选 1 单值（前端按此上色 + 排行分桶）
wall_zone_ids: list[str]    # 关联的 WallZone.wall_zone_id
key_level_prices: list[float]
support_trust, resistance_trust, sweep_attractiveness, break_through_risk: float [0..1]
data_confidence: float [0..1]
evidence: list[str]         # 中文证据链（前端逐条展示）
scenario: BrainScenario     # if_hold / if_break / invalidates_if 三段中文
layer_notes: list[str]
```

**`DominantRole` 6 值语义**（互斥）：

| role | 触发条件 | UI 颜色 | 含义 |
|---|---|---|---|
| `spot_defense` | spot 墙 ∨ Coinbase ∨ (关键位 ∧ trust ≥ 0.55)，**且** 无 futures+liq 共存 | emerald | 防守位（限价试错可观察） |
| `contested` | 上述防守 **同时** 有 futures_wall ∨ liq_magnet | fuchsia | 争夺区（双向博弈） |
| `futures_target` | futures_wall ∧ liq_magnet（无现货防守） | rose | 合约目标位（易扫单） |
| `liquidation_magnet` | 仅 liq_magnet | amber | 纯磁吸目标 |
| `key_level_only` | 仅有关键位但无墙/磁铁 | sky | 关键位单独点 |
| `other` | 其他 | slate | 弱聚合 |

### 3.2 `TradeSetupCandidate`（机会雷达单元）

```python
setup_id, coin, zone_id, setup_type
direction: long | short | neutral   # 后端语义；UI 必须转译
entry_styles: [SetupEntryStyle]      # 通常 1 激进 + 1 保守
risk_plan: SetupRiskPlan             # soft_invalidation + hard_stop + structural_invalidation
targets: [SetupTarget]               # T1/T2/T3 + 各自 rr
asymmetry_score, opportunity_score, data_confidence: float [0..1]
state: SetupState                    # 9 态状态机
cancel_conditions: list[str]
evidence, notes: list[str]
```

**4 种 setup_type**（MVP）：

| type | 方向语义 | 触发条件（核心） |
|---|---|---|
| `support_limit_probe` | long | spot_defense ∨ contested + support_trust ≥ 0.70 |
| `resistance_limit_probe` | short | spot_defense ∨ contested + resistance_trust ≥ 0.70 |
| `fake_break_reclaim_long` | neutral（等待） | 同 support 但要求扫破后收回 |
| `fake_break_reclaim_short` | neutral（等待） | 同 resistance 但要求扫破后回落 |

### 3.3 `BrainSpotBook` / `BrainFutBook`（Phase B/C 新增）

两者共享 `bracket` 三档：
- `near` ≤ 0.5%（短线即时关注）
- `mid` 0.5–2%（中短期仓位）
- `far` 2–5%（战略观察）
- > 5% **截断不展示**

**SpotBook** 每条：`total_usd / spot_usd / futures_usd / is_dual_source / has_coinbase / trust_score / strength_tier / dominant_role`，前端横向柱条 = 现货绿 + 合约蓝拼接。

**FutBook** 每条：`futures_usd / sweep_attractiveness / break_through_risk / persistence_score / is_attached_magnet`，叠加 `magnets[]`（来自 `LiquidationMap.clusters_*` + `KeyLevel.magnet_levels`）。

### 3.4 `BrainContextChips`

| chip | 来源 |
|---|---|
| regime / regime_description | `KeyLevelSnapshotV2.regime` |
| oi_delta_1h_pct | `state.oi.change_1h_pct` |
| funding_interpretation | `state.funding.interpretation` |
| cvd_contract_trend / cvd_spot_trend | `state.cvd_contract.trend_1h` / `state.cvd_spot.trend_1h` |
| nearest_magnet_above / below | `_iter_sweep_targets(op)` 聚合 |

### 3.5 `BrainDataQuality`

```python
liquidity_wall_quality: ok|partial|stale|warming|missing
usd_usdt_basis_pct: Optional[float]     # 美元/USDT 基差
overall_freshness_score: 0..1
stale_sources, missing_sources: list[str]
is_partial_ready: bool                   # ready_count < 3
ready_count: int
total_count: int = 3                     # KL / Orderbook / Liq
```

---

## 4. 核心算法

### 4.1 Zone 聚合 · 距离容差合并

**位置**：`backend/processors/trading_brain_builder.py:67-184`

**容差公式**（`merge_tolerance`，Q2=A 决议）：
```
merge_tol = max(0.5 × ATR, 0.3% × last_price)    # ATR 缺失时仅用 0.3%
```

**步骤**：
1. **`_collect_pieces`**（L114）：将 4 类原子证据装入 `_RawPiece`
   - WallZone：`anchor=price_mid`，区间 = `[price_low, price_high]`
   - KeyLevelV2：`anchor=price`，区间 = `[price ± merge_tol/2]`
   - LiqCluster：`anchor=price_center`，区间 = `[price_from, price_to]`
   - LiqMagnet：`anchor=price`，区间 = `[price ± merge_tol×0.35]`
2. **`_cluster_pieces`**（L170）：按 anchor 排序，相邻 piece 若 `|anchor_i − cluster_mid| ≤ tol` 则合并
3. **`_build_zone_from_cluster`**（L196）：
   - `support_trust = max` 所有 bid wall 与 support KL 的归一化分
   - `resistance_trust = max` 所有 ask wall 与 resistance KL 的归一化分
   - `sweep_attractiveness = max(WallZone.SA, _liq_sweep_score(LiqCluster))`
   - `break_through_risk = max(WallZone.btr, KL.cascade_risk)`
   - `data_confidence = mean(各 piece 单独可信度)`
   - 不重新计算任何一个上游已有的评分

**清算簇 → 扫单吸引力 proxy**（L187-193）：
```python
raw = total_usd / (200_000_000 + total_usd)      # 软上限至 1.0
if exchange_count >= 3: raw += 0.08              # 多所共振加成
return min(1.0, raw)
```

### 4.2 dominant_role 分类

**位置**：`trading_brain_builder.py:346-382`

```python
has_spot   = roles.spot_supply_wall ∨ roles.coinbase_confluence
strong_kl  = roles.key_level ∧ max(support_trust, resistance_trust) ≥ 0.55
has_target = roles.futures_liquidity_wall ∧ roles.liquidation_magnet

if (has_spot ∨ strong_kl) ∧ (futures_wall ∨ liq_magnet):
    return "contested"
if has_spot ∨ strong_kl:        return "spot_defense"
if has_target:                  return "futures_target"
if liq_magnet:                  return "liquidation_magnet"
if key_level:                   return "key_level_only"
return "other"
```

**保守原则**：spot_defense 必须有"硬证据"（现货墙 / Coinbase / 高 trust 关键位），避免把弱关键位当防守位喂给观察区生成器。

### 4.3 OpportunityEngine · 不对称机会评分

**位置**：`backend/processors/opportunity_engine.py`

**严筛门槛**（L46-50，常量化、不暴露配置）：
```python
_MIN_SUPPORT_TRUST    = 0.70
_MIN_RESISTANCE_TRUST = 0.70
_MIN_RR_T1            = 2.0
_MIN_DATA_CONFIDENCE  = 0.75
_MAX_DISTANCE_PCT     = 1.5
```

**失效结构**（按 ATR 退避）：
```python
_SOFT_BUFFER_ATR = 0.30   # 软失效：可快速收回
_HARD_BUFFER_ATR = 0.80   # 硬止损：结构失败
# fake_break 类多退 0.4 ATR（_HARD_BUFFER_ATR + 0.4）
```

**`asymmetry_score`**（L163-201）：
```
asymmetry = rr_score × invalidation_clarity × proximity ×
            target_quality × liquidity_path × max(0.4, data_confidence)

  rr_score             = min(1.0, top_target_rr / 6.0)          # 6R 给满
  invalidation_clarity = clamp(risk / (price × 0.5%), [0.3, 1]) # 风险点位清不清晰
  proximity            = max(0.2, 1 − |dist_pct| / 1.5%)
  target_quality       = mean(per-target {spot_wall=0.9 / liq=0.8 / kl=0.75 / other=0.6})
  liquidity_path       = 1 − min(break_through_risk × 0.5, 0.5)
```

**`opportunity_score`**（L204-218）：
```
opp = 0.30·asymmetry + 0.20·confirmation + 0.15·kl_quality
    + 0.15·liquidity_path + 0.10·regime_fit + 0.10·data_confidence
    − execution_risk_penalty
clamp [0, 1]
```

`execution_risk_penalty = 0.10 if data stale/partial else 0.0`，配合 `cancel_conditions[0] = "[已触发] 数据未就绪/部分源 stale"` 让前端立即可见。

**Targets 选择**（L98-159）：从 `all_zones` 找 long 方向"上方所有 zone"按价升序、short 方向"下方所有 zone"按价降序，取前 3 个 RR>0 的作为 T1/T2/T3。**RR 严格用 `(target − entry) / risk_to_hard_stop`**，与限价单口径一致。

### 4.4 OpportunityStateMachine · 9 态生命周期

**位置**：`backend/processors/opportunity_state_machine.py`

```
forming
  │ (price 接近入场区 ∨ regime 适配)
  ▼
waiting_for_trigger
  │ (price 进入入场区)
  ▼
triggered ──→ (墙重挂/增厚)──→ confirmation_pending ──→ (持续证据) ──→ confirmed
  │                                                                        │
  └─→ (墙撤出+消耗) ─→ cancelled                                          │
                                                                          │
硬止损被穿 ────────────────────────────────────────────→ invalidated     │
30min 未触发 + 远离 ─────────────────────────────────→ missed             │
invalidated/cancelled/missed ──30min──→ cooldown ────────────────────────┘
```

**事件源**：仅 `OrderbookPressureSnapshot.wall_events`（`wall_appeared / strengthened / weakened / removed / consumed / reloaded / consumed_and_removed`）+ 实时 last_price + ctx.regime。**不消费 KL state**（避免与 KL state machine 重复推进）。

**优先级**（`advance_setup_state` L152-247）：
1. 硬止损穿透 → `invalidated`（最高优先；short 方向是 `last ≥ hard_stop`）
2. regime 反转（long 遇 trend_down / short 遇 trend_up）→ `cancelled`
3. forming → waiting → triggered → confirmation_pending → confirmed（沿主线推进）
4. 终态 30min 内 → `cooldown`，30min 后保持终态

**历史记录**：`SetupState.history` 滚动保留最近 5 条 `{ts, from, to, reason}`，前端时间轴可回放。

### 4.5 SpotBook / FutBook 分桶

**位置**：`trading_brain_builder.py:548-712`

**bracket 阈值**（与产品层确认）：
```python
_BRACKET_NEAR = 0.5   # |dist| ≤ 0.5%
_BRACKET_MID  = 2.0   # 0.5% < |dist| ≤ 2.0%
_BRACKET_FAR  = 5.0   # 2.0% < |dist| ≤ 5.0%
_BRACKET_CAP  = {"near": 8, "mid": 8, "far": 6}
```

**SpotBook 现货 vs 合约厚度拆分**：
```python
spot_usd    = WallZone.spot_current_usd + WallZone.coinbase_spot_usd
total_usd   = WallZone.current_usd
futures_usd = max(total_usd − spot_usd, 0)
```
> 边界处理：纯合约墙（无 spot_confluence）`spot_usd=0`，全额计入合约侧。

**FutBook 磁铁叠加**（`_collect_fut_magnets`）：
- 数据来源：`LiquidationMap.clusters_above + clusters_below + KeyLevel.magnet_levels`
- 5% 内截断；价格分桶去重（±0.05% 内视为同一磁铁）
- 与 bin 同价区共振检测：`|bin.distance_pct − magnet.distance_pct| ≤ 0.10%` → `is_attached_magnet=True`

### 4.6 CVD 背离检测（前端）

**位置**：`frontend/src/components/Brain/TopStrip.tsx:34-55`

```typescript
if (spot === "rising" && futures === "declining")
    → "CVD 背离 · 现强合弱" (绿色 pulse) // 现货吸筹 + 合约退潮：底部强信号
if (spot === "declining" && futures === "rising")
    → "CVD 背离 · 合强现弱" (红色 pulse) // 现货抛压 + 合约追涨：顶部虚弱
otherwise → 不显示（同向不报警；任一为 flat 不报警）
```

### 4.7 MAA JSON 解析容错（A0 顺手修）

**位置**：`backend/ai/market_action_prompts.py:839-866` + `backend/ai/te_interpreter.py:955-984`

**根因**：deepseek 偶发在 `analyst_reasoning` 长字符串中夹带未转义控制字符（0x00–0x1f），触发 `json.loads` 默认 `strict=True` 抛 `Invalid control character at: line N column M`，整轮报告归零。

**修复**：两条 LLM JSON 解析入口统一改为 `json.loads(block, strict=False)`，仅放宽字符串内字面控制字符；JSON 结构、isinstance(dict) 校验不变。

---

## 5. 前端组件结构

**根页面**：`frontend/src/app/brain/[coin]/page.tsx`

```
<div className="brain-page flex min-h-screen flex-col">          // body:has(.brain-page) 解锁滚动
  <header />                                                       // 标题 + 角色色板图例
  <banner if is_partial_ready />                                  // 暖机期警示
  <topStrip />                                                     // CVD 背离灯 + 8 个 chip + summary
  <main flex-[3] min-h-[420px]>                                   // 第一排
    <PriceAxisMap   w=[260|300|320] />                            // SVG 价格轴 + zone 块
    <ZoneDetailCard flex-1 />                                     // 选中 zone 的证据链
    <OpportunityBoard w=[320|360|400] />                          // Kanban 4 列
  </main>
  <section flex-[2] min-h-[360px] lg:flex-row>                    // 第二排（<lg 自动堆叠）
    <SpotOrderBookPanel flex-1 />                                 // 现货订单簿（near/mid/far 三段折叠）
    <FuturesHeatmap     flex-1 />                                 // 合约堆积 + 磁铁 ◆ 叠加
  </section>
  <footer h=[100|120]>
    <EventTimeline />                                             // 近 30min wall_events
  </footer>
</div>
```

**响应式 breakpoint**（Tailwind 默认）：
- `lg` 1024px / `xl` 1280px / `2xl` 1536px
- 价格轴：260 / 300 / 320 px
- 机会雷达：320 / 360 / 400 px
- 第二排在 `<1024px` 自动堆叠为上下布局

**联动机制**：
- 全局 `selectedId` / `hoverZoneId` 双 state 管理
- `PriceAxisMap` 点击 → `setSelectedId` → `ZoneDetailCard` 切换内容
- `EventTimeline` hover → `setHoverZoneId` → `PriceAxisMap` ring 高亮
- `OpportunityBoard / SpotOrderBookPanel / FuturesHeatmap` 点击 wall_zone_id → 通过 zone.wall_zone_ids 反查对应 zone → setSelectedId

---

## 6. UI 词汇规范（语义合规）

| 后端字段值 | 前端显示文案 | 严禁文案 |
|---|---|---|
| `direction: long` | 做多观察 | 买入 / 开多 / 入场 |
| `direction: short` | 做空观察 | 卖出 / 开空 |
| `direction: neutral` | 等待 | 观望（无差异，但需统一口径） |
| `break_through_risk: 0.45` | 打穿风险评分 0.45 | 打穿概率 45% |
| `data_quality.is_partial_ready` | 数据未就绪：N/3 项核心源已接入 | 服务异常 / 错误 |
| `setup_state.name: confirmed` | 已确认（结构成立） | 入场信号 / 触发买入 |

**位置**：`frontend/src/components/Brain/types.ts::translateDirection / translateSetupState`

---

## 7. body 滚动白名单（CSS 注意点）

**位置**：`frontend/src/app/globals.css:8-20`

```css
body { overflow: hidden; }    /* 仪表盘主页 h-screen 默认策略 */

body:has(.ai-detail-page),
body:has(.levels-detail-page),
body:has(.range-detail-page),
body:has(.news-brief-page),
body:has(.roll-page),
body:has(.brain-page) {        /* ← brain 页面注册到白名单 */
  overflow: auto;
}
```

**根因**：仪表盘主页 `h-screen` 锁视口的设计副作用 — 任何使用 `min-h-screen` 期望整页滚动的页面都必须把自身 className 加到这个白名单，否则 body 会吞掉滚轮事件。

---

## 8. 文件清单 / commit 历史

### 8.1 后端

| 文件 | 用途 | LOC |
|---|---|---|
| `backend/models/trading_brain.py` | 全部 Pydantic 模型 | ~370 |
| `backend/processors/trading_brain_builder.py` | builder 主入口 + spot/fut book 抽取 | ~960 |
| `backend/processors/opportunity_engine.py` | 4 种 setup 派生 + 评分 | ~615 |
| `backend/processors/opportunity_state_machine.py` | 9 态状态机 | ~258 |
| `backend/api/routes.py:750` | `GET /api/trading-brain/{coin}` | – |
| `backend/ai/market_action_prompts.py:839` | `extract_json_payload(strict=False)` | – |
| `backend/ai/te_interpreter.py:955` | `_extract_json(strict=False)` | – |

### 8.2 测试

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `backend/tests/test_trading_brain_builder.py` | 17 | 合并/角色/排行/spot_book/fut_book/暖机 |
| `backend/tests/test_opportunity_engine.py` | 17 | 严筛/RR/risk_plan/cancel/dq |
| `backend/tests/test_opportunity_state_machine.py` | 11 | 9 态转移 + 历史 + cooldown |
| `backend/tests/test_extract_json_payload.py` | 5 | 控制字符/字面 \n / 非 dict 拒绝 |

后端全量回归 **2253 passed**（v3 起步基线 2243 + 新增 10 条）。

### 8.3 前端

| 文件 | 用途 |
|---|---|
| `frontend/src/app/brain/[coin]/page.tsx` | 根页面 + 三排响应式布局 |
| `frontend/src/components/Brain/TopStrip.tsx` | CVD 背离灯 + chip |
| `frontend/src/components/Brain/PriceAxisMap.tsx` | SVG 价格轴（300px viewBox） |
| `frontend/src/components/Brain/ZoneDetailCard.tsx` | zone 详情 |
| `frontend/src/components/Brain/SetupCard.tsx` | 单个 setup 展示 |
| `frontend/src/components/Brain/OpportunityBoard.tsx` | Kanban |
| `frontend/src/components/Brain/EventTimeline.tsx` | 事件时间轴 |
| `frontend/src/components/Brain/SpotOrderBookPanel.tsx` | Phase B 现货订单簿 |
| `frontend/src/components/Brain/FuturesHeatmap.tsx` | Phase C 合约堆积 |
| `frontend/src/components/Brain/types.ts` | ROLE_COLORS + 翻译函数 |
| `frontend/src/lib/types.ts` | TS 镜像 |
| `frontend/src/stores/marketStore.ts` | tradingBrainByCoin + loadTradingBrain |

`tsc --noEmit` 0 error；ESLint 0 warning。

### 8.4 commit 时间线（最新→最旧）

| Hash | 主题 |
|---|---|
| `e7693d8` | fix(brain): allow page-level scroll on trading brain dashboard |
| `228a1a5` | style(brain): responsive layout for trading brain dashboard |
| `816dcfb` | feat(brain): futures liquidity heatmap with magnet overlay (Phase C) |
| `dda93f0` | feat(brain): spot orderbook panel with near/mid/far brackets (Phase B) |
| `f9e6487` | feat(brain): CVD divergence chip + widen price axis to 300px (Phase A) |
| `2d8ec07` | fix(ai): tolerate unescaped control chars in LLM JSON output (Phase A0) |
| `f7abd3a` | feat(brain): 事件驱动状态机推进 setup 生命周期（Phase 4） |
| `a6aa9a5` | feat(brain): 机会雷达 + 事件 timeline 接入（Phase 3b） |

---

## 9. 已知限制 / 后续 Roadmap

| 限制 | 影响 | 后续计划 |
|---|---|---|
| ETH/SOL 数据不齐 | 部分 zone 缺 Coinbase 共振 | 待 Coinbase 现货 API 扩展到这两个币种 |
| 状态机只看 wall_events | 价格剧烈插针未必有 wall_event | M5：补充 1m/5m K 线刺破检测 |
| Opportunity 无历史回测 | 无法显示 setup 历史命中率 | P2：接入 plan_pnl_replay 同源样本，统计 cancel→invalidate→missed→confirmed 的分布 |
| AI 解读未接入 | 大屏纯结构化展示，无自然语言 | P3：复用 te_interpreter 模板生成"本帧大脑结论" |
| 单位仍为英文 B/M/k | 中文用户阅读节奏割裂 | **本轮待确认方案后替换为「亿/万」两档** |

---

## 10. 审查关注点（建议第三方 AI 重点检查）

1. **铁律 1 是否被破坏**：搜索 `final_score = ` / `strength_tier =` / `cascade_risk =` 在 `trading_brain_*.py` / `opportunity_*.py` 中应**只读不写**
2. **铁律 4 是否被破坏**：grep 文件内容中出现"概率/probability/%"是否与 `break_through_risk` 同上下文（应只在 ranking comment 中出现）
3. **opportunity_engine.py 的 `_MIN_*` 阈值**：是否过严导致 BTC 行情外多数币种永远输出 0 个 setup？
4. **状态机 advance_setup_state**：`waiting_for_trigger` 老化判定（30min）在 ETH/SOL 这种波动率不同的币上是否合理？建议改成 `_MISSED_AGE_SEC × max(1, 30 / atr_pct)`？
5. **dominant_role classification**：当 `key_level=True` 但 `final_score < 55` 时是否应进入 `key_level_only` 而非 `other`？目前逻辑已是 KL→key_level_only，OK
6. **spot_usd 计算**：`spot_current_usd + coinbase_spot_usd` 是否存在 double counting（Binance 现货深度可能与 Coinbase 现货深度同一笔）？— 经查 WallZone 字段定义文档 (`models/orderbook_pressure.py:380-410`)，二者数据源完全独立（前者 Binance 5m heatmap，后者 Coinbase 原生 API），**不存在 double counting**
7. **响应式断点**：`lg/xl/2xl` 阈值在 1366×768 / 1440×900 / 1920×1080 / 2560×1440 实测下是否各档都看得清晰？
8. **45s 轮询**：是否会因 API 慢响应导致 stale UI？建议加 cache-stale 时间戳显示

---

## 11. 复用决策对照表（dev-constraints 第 3 条审查）

| 新增功能 | 决策 | 理由 |
|---|---|---|
| spot_book 数据 | **直接复用** WallZone | walls_above/below 已有现货厚度字段，新建会重复 |
| fut_book 数据 | **直接复用** WallZone + LiquidationMap | 合约侧 = current − spot，磁铁来自 clusters，零新数据源 |
| 单位格式化 fmtUsd | **提取复用**（待执行） | 当前后端 `_fmt_usd_short` + 前端两处 `fmtUsd` 三处重复，应提到 `frontend/src/lib/format.ts` |
| body 滚动白名单 | **扩展复用** :has() 模式 | 与 ai/levels/range/news-brief/roll 完全同模式 |
| MAA JSON 解析容错 | **同步口径** | MAA 与 TE 两条独立链路同步改 strict=False，避免一条修一条漏 |

---

## 12. 测试运行

```bash
# 后端
cd backend && python3 -m pytest -q                          # 2253 passed
python3 -m pytest tests/test_trading_brain_builder.py -v    # 17 passed
python3 -m pytest tests/test_opportunity_engine.py -v       # 17 passed
python3 -m pytest tests/test_opportunity_state_machine.py -v# 11 passed
python3 -m pytest tests/test_extract_json_payload.py -v     # 5 passed

# 前端
cd frontend && npx tsc --noEmit                             # 0 error
```

---

> 本文档严格依据已合入 main 的代码状态描述。任何与代码不一致之处以代码为准。
> 反馈渠道：直接在仓库 issue 引用本文档第 N 节即可。
