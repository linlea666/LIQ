# LIQ 防猎杀数据大屏 — 开发规范

## 项目定位
面向专业加密资产交易者的"防猎杀"实时数据大屏。聚合清算热力图、CVD 资金流向、OI 持仓量、深度订单簿、爆仓数据等多维底层订单流数据，通过 AI 大语言模型分析，为交易者输出关键支撑位、阻力位、止损安全区、最佳入场区间等决策参考信息。

## 核心原则：一次到位，拒绝返工

"一次到位"指的是设计和质量一次到位，不是代码必须一次写完。

---

## 1. 分步实施，整体完整

当单个功能涉及多个文件或代码量较大时，可以分步骤实施：
- 每一步必须是一个完整的、可独立运行的单元（不留半成品）
- 分步前先列出完整的步骤清单和每步的交付物
- 每步完成后做该步的完成度检查，再进入下一步
- 最后一步完成后做整体联调检查

分步示例（正确）：
```
Step 1: 定义所有数据模型（models/）→ 检查字段完整性 + 多币种兼容
Step 2: 实现数据源接入层（sources/）→ 检查各API连通 + 错误降级 + 日志
Step 3: 实现数据处理引擎（processors/）→ 检查CVD/清算/价位计算 + 单元测试
Step 4: 实现 WebSocket 推送层 → 检查实时性 + 多币种频道隔离
Step 5: 实现前端组件 → 检查渲染 + 币种切换 + 响应式
Step 6: 实现 AI 分析模块 → 检查数据快照组装 + Prompt + 输出格式化
Step 7: 整体联调 → 检查全链路：数据源→处理→推送→渲染→AI分析
```

禁止的分步方式：
- ❌ "先写个能跑的，错误处理下次补"
- ❌ "先硬编码，配置化以后再改"
- ❌ "日志先不加，出问题再补"
- ❌ "先只做BTC，多币种以后再加"

---

## 2. 设计先行，完整交付

每个功能必须完整设计后再动手编码。
"完整"意味着：正常流程 + 异常处理 + 日志 + 配置化 + 多币种支持，一步到位。
禁止"先跑通再补错误处理"、"先硬编码再改配置"等分步交付方式。
不确定的设计先讨论多个方案的利弊，确认后一次性实现。

---

## 3. 多币种架构（从第一行代码开始）

从设计阶段起按多币种架构实现，默认 BTC，可切换 ETH、SOL。

### 3.1 统一币种配置
```yaml
# config.yaml
coins:
  BTC:
    symbol_okx_swap: "BTC-USDT-SWAP"
    symbol_okx_spot: "BTC-USDT"
    symbol_binance: "BTCUSDT"
    symbol_bbx: "btcswapusdt:binance"
    ccy: "BTC"
    default: true
  ETH:
    symbol_okx_swap: "ETH-USDT-SWAP"
    symbol_okx_spot: "ETH-USDT"
    symbol_binance: "ETHUSDT"
    symbol_bbx: "ethswapusdt:binance"
    ccy: "ETH"
  SOL:
    symbol_okx_swap: "SOL-USDT-SWAP"
    symbol_okx_spot: "SOL-USDT"
    symbol_binance: "SOLUSDT"
    symbol_bbx: "solswapusdt:binance"
    ccy: "SOL"
```

### 3.2 多币种设计要求
- 所有数据模型必须包含 `coin` 字段标识币种
- 所有 API 调用参数必须通过币种配置动态获取，禁止硬编码 "BTCUSDT"
- 数据处理引擎按币种独立运行实例，互不干扰
- 前端通过币种选择器切换，切换时订阅对应币种的 WebSocket 频道
- Redis 缓存 key 必须包含币种前缀：`liq:{coin}:cvd`, `liq:{coin}:oi`
- AI 分析时传入当前选择的币种数据快照

### 3.3 禁止事项
- ❌ 在代码中写死 `"BTC-USDT-SWAP"` 或 `"BTCUSDT"`
- ❌ 用 if/else 分支处理不同币种的 API 差异（应统一由配置驱动）
- ❌ 全局变量存储"当前币种"（前端用组件状态，后端按币种隔离实例）

---

## 4. 架构分层与模块边界

### 4.1 后端分层
```
backend/
├── config/          # 配置管理
│   ├── settings.py  # 加载 YAML + 环境变量
│   └── coins.py     # 币种配置解析
│
├── models/          # 数据模型（模块间契约）
│   ├── market.py    # MarketState, CandleData, OrderBook
│   ├── liquidation.py  # LiquidationMap, LiqCluster, VacuumZone
│   ├── flow.py      # CVDData, OIData, FundingRate
│   ├── levels.py    # SupportLevel, ResistanceLevel, StopLossZone, EntryZone
│   └── snapshot.py  # AISnapshot（发给AI的结构化数据）
│
├── sources/         # 数据源接入（只负责拉取+标准化，不做业务计算）
│   ├── base.py      # DataSource 基类（定义接口契约）
│   ├── bbx.py       # bbx.com 清算地图
│   ├── okx_rest.py  # OKX REST API（费率/OI/taker-volume/K线）
│   ├── okx_ws.py    # OKX WebSocket（深度/成交/爆仓）
│   ├── binance_rest.py  # Binance REST API
│   ├── binance_ws.py    # Binance WebSocket
│   └── macro.py     # [v2.0预留] 宏观数据
│
├── processors/      # 数据处理引擎（业务计算逻辑）
│   ├── cvd.py       # CVD 计算
│   ├── liquidation.py  # 清算密集区/真空区识别
│   ├── levels.py    # 支撑/阻力/止损/入场价位计算
│   ├── volume_profile.py  # 成交量分布 + POC + VWAP
│   ├── market_temp.py  # 市场温度计 + 插针风险评分
│   ├── percentile.py   # 各指标历史百分位计算
│   ├── waterfall.py    # 多空归因瀑布图因子计算
│   └── orderbook.py    # 大单检测 + 假单识别
│
├── ai/              # AI 分析模块
│   ├── snapshot.py  # 各数据源 → 结构化 AISnapshot 组装
│   ├── prompts.py   # Prompt 模板管理
│   └── analyzer.py  # LLM 调用 + 输出解析 + 格式化
│
├── api/             # 对外接口
│   ├── rest.py      # REST 接口（AI分析触发、历史数据查询）
│   ├── ws.py        # WebSocket 服务（实时数据推送）
│   └── routes.py    # 路由注册
│
├── engine.py        # 主引擎：调度数据源轮询 + 处理 + 推送
├── main.py          # 应用入口
└── requirements.txt
```

### 4.2 前端分层
```
frontend/
├── app/
│   ├── layout.tsx       # 根布局（暗色主题）
│   └── page.tsx         # 主页面（大屏布局容器）
│
├── components/
│   ├── TopBar/          # 顶部状态栏
│   │   ├── PriceBar.tsx
│   │   ├── StatusBadges.tsx
│   │   ├── CoinSelector.tsx   # 币种选择器（BTC/ETH/SOL）
│   │   └── AIButton.tsx
│   │
│   ├── FactorCards/     # 因子卡片横排
│   │   ├── FactorCard.tsx     # 通用卡片组件（含百分位条）
│   │   ├── CoreFactors.tsx    # D1-D8 核心因子行
│   │   └── MacroFactors.tsx   # [v2.0预留] M1-M6 宏观因子
│   │
│   ├── MainView/        # Tab 切换的主视图
│   │   ├── TabContainer.tsx
│   │   ├── LiquidationMap.tsx
│   │   ├── OrderBookDepth.tsx
│   │   ├── CVDOIChart.tsx
│   │   ├── VolumeProfile.tsx
│   │   └── WaterfallChart.tsx
│   │
│   ├── SidePanel/       # 右侧面板
│   │   ├── AIAnalysis.tsx
│   │   └── LiveFeed.tsx
│   │
│   └── common/          # 通用组件
│       ├── PercentileBar.tsx
│       ├── Tooltip.tsx
│       ├── StatusFooter.tsx
│       └── LoadingState.tsx
│
├── hooks/
│   ├── useWebSocket.ts      # WebSocket 连接管理
│   ├── useCoin.ts           # 当前币种状态
│   └── useMarketData.ts     # 市场数据订阅
│
├── stores/
│   └── marketStore.ts       # Zustand 状态管理（按币种隔离）
│
├── lib/
│   ├── types.ts             # TypeScript 类型定义
│   ├── format.ts            # 数据格式化工具
│   └── constants.ts         # 常量定义
│
└── styles/
    └── globals.css          # 全局样式（Tailwind 暗色主题）
```

### 4.3 模块间通信契约

各模块通过明确的数据结构通信，禁止直接访问其他模块内部状态：

```
sources → processors:  标准化的原始数据（CandleData, RawOrderBook, RawTrades...）
processors → api/ws:   计算后的业务数据（CVDData, LiquidationMap, SupportLevel...）
processors → ai:       AISnapshot（所有维度数据的结构化快照）
ai → api/rest:         AIAnalysisResult（格式化的分析结果）
api/ws → frontend:     JSON 格式的实时推送数据
api/rest → frontend:   JSON 格式的请求响应数据
```

数据模型在 `models/` 中统一定义，作为模块间的契约。任何模块不得绕过模型直接传递原始 dict。

---

## 5. 数据源管理

### 5.1 数据源基类
所有数据源必须继承统一基类，实现标准接口：

```python
class DataSource(ABC):
    """数据源基类，所有数据源必须实现"""

    @abstractmethod
    async def fetch(self, coin: CoinConfig) -> Any:
        """拉取数据，coin 参数决定拉哪个币种"""
        ...

    @abstractmethod
    def get_poll_interval(self) -> int:
        """返回轮询间隔（秒）"""
        ...

    @abstractmethod
    def health_check(self) -> SourceHealth:
        """返回数据源健康状态"""
        ...
```

### 5.2 数据源降级策略
- BBX 清算地图不可用 → 显示"清算数据暂不可用"，其余模块正常运行
- OKX REST 失败 → 重试 3 次，间隔 2/4/8 秒，仍失败则标记为降级
- OKX WebSocket 断开 → 自动重连（指数退避），重连期间用 REST 轮询补数据
- Binance 不可用（地域限制）→ 仅使用 OKX 数据，因子卡片标注"单交易所"
- 任何数据源降级时，前端底部状态栏实时反映连接状态

### 5.3 数据源健康监控
每个数据源维护以下状态：
- `status`: connected / degraded / disconnected
- `last_success_ts`: 最后一次成功拉取时间
- `error_count`: 连续错误计数
- `latency_ms`: 最近一次请求耗时

通过底部状态栏推送给前端：
```
🟢 bbx清算(1.2s)  🟢 OKX行情(0.3s)  🟡 Binance(降级)  🟢 OKX-WS(实时)
```

---

## 6. 实时数据推送策略

### 6.1 REST 轮询（低频数据）
| 数据 | 轮询间隔 | 说明 |
|------|---------|------|
| 清算地图 | 30s | BBX API 响应较慢 |
| CVD (taker-volume) | 60s | OKX 5分钟颗粒度 |
| OI 快照 | 10s | 实时性要求中等 |
| 资金费率 | 60s | 每8小时结算，变化慢 |
| K线数据 | 5s | 计算 ATR/VWAP/VP 用 |

### 6.2 WebSocket 推流（高频数据）
| 频道 | 来源 | 说明 |
|------|------|------|
| trades | OKX WS | 逐笔成交 → 大单检测 |
| books5 | OKX WS | 5档深度 → 订单簿可视化 |
| liquidation-orders | OKX WS | 实时爆仓 |
| tickers | OKX WS | 最新价/24h统计 |

### 6.3 前端推送频率
后端聚合后按以下频率推送给前端（非原始频率）：
| 数据类型 | 推送频率 | 说明 |
|---------|---------|------|
| 价格+Ticker | 1s | 顶栏价格 |
| 因子卡片 | 5s | 8张卡片数据 |
| 清算地图 | 30s | 重新计算后推送 |
| CVD+OI曲线 | 10s | 图表数据 |
| 爆仓/大单 | 实时 | 事件驱动推送 |
| 订单簿深度 | 2s | 聚合后的深度 |

---

## 7. AI 分析模块规范

### 7.1 触发方式
仅手动触发（用户点击 [🤖 AI分析] 按钮），不自动触发。

### 7.2 数据快照组装
点击按钮后，后端将当前所有维度的数据组装为结构化 `AISnapshot`：
- 当前币种的清算地图（密集区 + 真空区）
- CVD 当前值 + 1h趋势
- OI 当前值 + 变化率
- 资金费率（多交易所）
- 期现溢价
- 订单簿大单（前5买墙/卖墙）
- 近30分钟爆仓统计
- Volume Profile POC + Value Area
- ATR + VWAP
- 价格 + 24h 高低
- 市场温度 + 插针风险等级

快照必须是确定性的 JSON 结构（models/snapshot.py 定义），不允许随意塞字段。

### 7.3 Prompt 规范
- Prompt 模板统一在 `ai/prompts.py` 管理，不散落在代码各处
- 系统 Prompt 定义 AI 角色和输出格式约束
- 用户 Prompt 传入结构化数据快照
- 输出必须严格遵循固定的 section 结构（格局总览/关键价位/止损区/入场区/风险/场景推演）
- AI 输出中禁止包含"胜率"、"建议做多/做空"等交易指令性内容
- AI 输出定位为"信息分析和价位计算参考"

### 7.4 错误处理
- LLM API 调用超时（>15秒）→ 提示用户"分析超时，请重试"
- LLM 返回格式异常 → 降级显示原始文本 + 记录错误日志
- LLM API 不可用 → 显示"AI服务暂不可用"，不影响大屏其他功能

### 7.5 扩展预留
- `AISnapshot` 中预留 `macro_context: Optional[MacroSnapshot]` 字段（v2.0 启用）
- Prompt 模板中预留宏观分析 section（默认注释，v2.0 取消注释）

---

## 8. 日志体系

使用 Python 标准 logging 模块，统一格式：
```
[%(asctime)s] [%(levelname)s] [%(name)s] [%(coin)s] %(message)s
```
时间格式：`%Y-%m-%d %H:%M:%S`

### 8.1 日志分级
- **INFO**：关键业务节点
  - 数据源连接成功/断开
  - 清算地图更新（新密集区/真空区变化）
  - AI 分析触发及完成
  - 关键价位变化（支撑阻力位发生显著移动）
- **WARNING**：可恢复的异常
  - API 限频/超时 → 自动重试
  - 数据源降级（单源不可用但整体可运行）
  - AI 响应格式异常 → 降级显示
  - WebSocket 断开 → 自动重连
- **ERROR**：不可恢复的失败
  - 数据源连续失败超过熔断阈值
  - AI API 认证失败
  - 关键数据处理异常（CVD 计算溢出等）
  - WebSocket 重连持续失败
- **DEBUG**：调试细节
  - 原始 API 响应体
  - AI 完整 prompt + response
  - 数据处理中间结果
  - WebSocket 消息原文

### 8.2 日志要求
- 所有日志必须包含 `coin` 字段（BTC/ETH/SOL），方便按币种过滤
- 数据源相关日志包含：source_name, endpoint, latency_ms, status_code
- AI 分析日志包含：snapshot 摘要（不含完整数据）、LLM 响应时间、token 用量
- 错误日志包含完整 traceback，不只是 `str(e)`

---

## 9. 错误处理（完整，不留"以后再补"）

### 9.1 外部调用规范
所有外部调用（BBX API、OKX API、Binance API、LLM API）必须：
- `try/except` 包裹并记录完整错误信息
- 有明确的超时设置（REST: 10s, WebSocket心跳: 30s, AI: 15s）
- 有明确的重试策略（指数退避：2s → 4s → 8s，最多3次）
- 有降级方案（见 5.2 节）

### 9.2 熔断机制
- 单个数据源连续失败 5 次 → 标记为 disconnected，60秒后自动尝试恢复
- AI API 连续失败 3 次 → 暂停 AI 功能 5 分钟，前端灰显按钮
- WebSocket 连续断开 10 次 → 切换为纯 REST 轮询模式

### 9.3 前端错误展示
- 数据源异常 → 对应模块显示"数据暂不可用"占位符，不显示过时数据
- 网络断开 → 全屏蒙版提示"网络连接中断，正在重连..."
- AI 分析失败 → 分析面板内显示具体错误，不影响其他模块

---

## 10. 配置外置

所有可变参数放配置文件（YAML），禁止硬编码。

### 10.1 config.yaml 结构
```yaml
# 币种配置
coins:
  BTC: { ... }
  ETH: { ... }
  SOL: { ... }

# 数据源配置
sources:
  bbx:
    base_url: "https://bbx.com/api/data"
    poll_interval_sec: 30
    timeout_sec: 10
  okx:
    rest_base_url: "https://www.okx.com/api/v5"
    ws_url: "wss://ws.okx.com:8443/ws/v5/public"
    poll_intervals:
      oi: 10
      funding_rate: 60
      taker_volume: 60
      candles: 5
    timeout_sec: 10
  binance:
    rest_base_url: "https://fapi.binance.com"
    ws_url: "wss://fstream.binance.com/ws"
    enabled: true  # 可关闭

# 数据处理配置
processors:
  cvd:
    source: "okx_taker_volume"  # 或 "websocket_trades"
    period: "5m"
  percentile:
    lookback_hours: 168  # 7天
  market_temp:
    weights:
      funding_rate: 0.25
      oi_change: 0.25
      cvd_trend: 0.20
      basis: 0.15
      liquidation_ratio: 0.15
  levels:
    min_liq_cluster_usd: 10000000
    atr_multiplier_sl: 1.5
    volume_profile_bins: 50

# AI 配置
ai:
  provider: "openai"  # 或 "anthropic"
  model: "gpt-4o"
  api_base: ""  # 可选代理地址
  timeout_sec: 15
  max_retries: 2

# 推送配置
push:
  ticker_interval_ms: 1000
  factor_cards_interval_ms: 5000
  liq_map_interval_ms: 30000
  orderbook_interval_ms: 2000

# 服务配置
server:
  host: "0.0.0.0"
  port: 8000
  cors_origins: ["http://localhost:3000"]
```

### 10.2 敏感信息
敏感信息用环境变量或 `.env`，不入 Git：
```env
AI_API_KEY=sk-xxx
OKX_API_KEY=xxx        # 如需私有接口
OKX_SECRET_KEY=xxx
OKX_PASSPHRASE=xxx
```

### 10.3 禁止事项
- ❌ 代码中出现 `"https://bbx.com/..."` 等硬编码 URL
- ❌ 代码中出现 `poll_interval = 30` 等硬编码时间
- ❌ 代码中出现 `if coin == "BTC"` 等硬编码币种逻辑
- ❌ `.env` 文件或含密钥的文件入 Git

---

## 11. 前端规范

### 11.1 技术栈
- 框架：Next.js 14+ (App Router)
- 样式：Tailwind CSS（暗色主题为主）
- UI 组件：shadcn/ui
- 图表：Lightweight Charts（K线）、D3.js（热力图/自定义图）
- 状态管理：Zustand
- 实时通信：Socket.IO Client
- 语言：TypeScript（严格模式）

### 11.2 组件设计原则
- 所有数据展示组件接收 `coin: string` prop，不从全局读取币种
- 通用组件（FactorCard, PercentileBar, Tooltip）必须可复用，不含业务逻辑
- 图表组件在数据未加载时显示骨架屏（Skeleton），不显示空白
- 数据源断开时对应区域显示降级 UI，不显示过时数据
- 币种切换时清空旧数据 → 显示骨架屏 → 加载新数据，不闪烁旧数据

### 11.3 颜色语言（全局统一）
```
多头/看涨/安全/支撑:  #22c55e (green-500)
空头/看跌/危险/阻力:  #ef4444 (red-500)
警告/当前价格/关注:   #eab308 (yellow-500)
AI 计算结果/信息:     #3b82f6 (blue-500)
中性/辅助文字:        #94a3b8 (slate-400)
背景:                #0f172a (slate-900) → #1e293b (slate-800) 渐变
卡片背景:            #1e293b (slate-800)
```

### 11.4 响应式
- 主要适配 1920x1080 及以上分辨率（大屏/显示器场景）
- 1440x900 可用但允许滚动
- 移动端暂不适配（v2.5 规划）

---

## 12. 三档显示模式

### 12.1 模式定义
| 模式 | 目标用户 | 信息密度 |
|------|---------|---------|
| 小白模式 | 新手交易者 | 只看颜色、方向、文字标签，隐藏具体数字 |
| 进阶模式（默认） | 有经验的交易者 | 颜色 + 关键数字 + 百分位条 |
| 专业模式 | 量化/专业交易者 | 全部原始数据 + 时间戳 + 数据源标注 |

### 12.2 实现方式
- 全局 `displayMode: 'beginner' | 'advanced' | 'pro'` 状态
- 每个组件根据 displayMode 条件渲染不同层级的信息
- 不创建三套组件，而是在同一组件内用条件渲染控制信息层级
- 模式切换保存到 localStorage，下次访问保持

---

## 13. 先说后做

涉及以下情况时，必须先完整说明方案，确认后再写代码：
- 新增模块或文件（先说清文件职责和对外接口）
- 修改数据模型（先说清字段变更和对下游的影响）
- 修改模块间通信方式
- 引入新的第三方依赖
- 架构层面的决策变更

单文件内的实现可直接进行，但需遵守其他规范。

---

## 14. 完成度检查

每个功能完成后必须全部通过：
- (a) 正常流程跑通且日志完整
- (b) 异常流程有兜底且不崩溃（数据源断开、API超时、AI失败）
- (c) 所有参数可配置化（无硬编码值）
- (d) 多币种场景下互不影响（BTC 数据不会污染 ETH）
- (e) 币种切换流畅无闪烁
- (f) 日志足够定位任何线上问题
- (g) 前端降级展示正确（数据缺失时不显示空白或旧数据）

---

## 15. Git 规范

### 15.1 分支策略
- `main`: 稳定版本
- `dev`: 开发主线
- `feature/*`: 功能分支（如 `feature/liquidation-map`, `feature/ai-analysis`）

### 15.2 Commit 信息格式
```
<type>(<scope>): <description>

type: feat / fix / refactor / docs / style / perf / test / chore
scope: sources / processors / ai / frontend / config / models
```

示例：
```
feat(sources): 接入 OKX taker-volume API 支持多币种 CVD 计算
fix(processors): 修复 CVD 累加在币种切换时未重置的问题
refactor(models): 统一 LiquidationMap 数据结构增加 coin 字段
```

### 15.3 .gitignore 必须包含
```
.env
*.pyc
__pycache__/
node_modules/
.next/
*.log
```

---

## 16. 版本路线图

```
v1.0 — MVP 数据大屏
├── 后端: 所有数据源接入 + 处理引擎 + WebSocket推送
├── 前端: 完整布局 + 8张因子卡片 + 5个Tab视图
├── AI: 手动分析按钮 + 结构化输出
├── 多币种: BTC / ETH / SOL 切换
└── 三档模式: 小白/进阶/专业

v1.5 — 增强版
├── Spoofing 假单检测
├── 历史数据回放
├── AI 分析增加场景推演
└── 数据导出

v2.0 — 宏观融合
├── M1-M6 宏观因子卡片
├── 经济日历事件预警
├── AI 分析接入宏观上下文
└── 新闻情绪分析

v2.5 — 移动端 + 个性化
├── 移动端适配
├── 自定义告警
├── 用户偏好设置
└── 更多山寨币支持
```
