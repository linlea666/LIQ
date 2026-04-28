# 关键位 V3 路线图（M1 → M5）

> 目的：把"关键位智能 OS"的演进路径写清楚，方便后续按图施工。
> 起草：2026-04-28；维护：每完成一阶段更新本文件。

---

## 0. 阶段总览

| 阶段     | 名称                         | 状态     | 提交 / 备注                                     |
| -------- | ---------------------------- | -------- | ----------------------------------------------- |
| **M1**   | 行为评估层（纯观测）         | ✅ 已完成 | `ac61517` `feat(key-level-v3): M4-M1 …`         |
| **M2.5** | 双轨并行（V1 + V2 影子）     | ✅ 已完成 | `6db5d95` `feat(key-level): M2.5 …`             |
| **M3**   | V1/V2 回测对比系统           | ✅ 已完成 | `655387e` `feat(key-level): M3 …`               |
| **M3.1** | 统计严谨化 + 元信息 + 健康监控 | ✅ 已完成 | `90cbc8d` `feat(key-level): M3.1 …`             |
| **M4**   | **数据驱动 V1→V2 渐进切换**  | ⏳ 规划中 | 见 §3                                            |
| **M5**   | **行为评估接入决策路径**     | ⏳ 规划中 | 见 §4                                            |
| M6（备选） | 质量画像 + V2 思想推广       | 💭 候选   | 见 §5                                            |

---

## 1. 共识与铁律（M4/M5 都必须遵守）

这些是从 M1 起就立下的"不能动"的红线，进 M4/M5 也不打折：

1. **不删旧逻辑**：V1 函数（`_assess_bounce_quality` / `_assess_breakout_stage` /
   `_fake_break_reclaim` / `_is_broken`）原地保留，永远可回滚。
2. **零信号污染**：核心字段（`lv.state` / `lv.final_score` / `lv.strength_tier` /
   `lv.cascade_risk`）仍由 V3 主路径决定；M4/M5 改的是"用什么算这些"的算子选择，
   不是引入新决定者。
3. **配置化切换**：每个维度（bounce / breakout_stage / fake_break / break_depth）
   是独立开关，可单独切换、单独回滚。
4. **可观测优先**：每次切换前 / 后都要有 health 数据 + 信号链路 KPI 对比。
5. **失败即降级**：V2 任何阶段抛错或质量不足时，自动 fallback V1，不允许"V2 报错→
   信号链路崩溃"。
6. **AI prompt 不破坏**：§9g 主表始终有效；M5 时让 §9g 吸收 behavior_state 也只是
   "在已有列后追加可信度"，不重构。
7. **回测 ≠ 信号**：M3.1 的回测引擎是离线决策工具，永远不参与实盘 signal_builder。

---

## 2. M3.1 留下的基础设施（M4/M5 直接复用）

进 M4 时这些已经就绪，不需重新搭建：

- **回测引擎**：`backend/processors/behavior_backtest_engine.py`
  （McNemar / Wilson CI / 校准桶 / 多条件决策）
- **健康监控**：`_HEALTH_STATS` + `GET /api/key-levels/behavior-eval-health`
- **元信息字段**：`BehaviorEval.behavior_eval_available` / `input_quality` /
  `evaluator_error`（区分"未评估"vs"评估为 0"）
- **决策门槛**：`MIN_SAMPLES_TRUSTED=100` + 多条件联合判定
- **互斥校准**：selloff vs capitulation 已在 evaluator 内自动调和
- **前端对比页**：`/levels/[coin]/v1v2-compare`（每天可看到当前差距）

---

# 3. M4 · 数据驱动 V1→V2 渐进切换

## 3.1 阶段目标

把"双轨并行 + 离线对比"升级到"数据驱动的渐进切换"：

> 当某维度的 V2 算子在统计上显著优于 V1，且历史样本充分稳定时，
> 让生产链路从该维度切换到 V2，并保留一键回滚能力。

**核心承诺**：切换是"按维度、可灰度、可回滚"的；不是"一刀切替换 V1"。

---

## 3.2 进入门槛（任意维度满足才允许 M4 切换该维度）

必须**全部**满足，缺一不可：

| 门槛                  | 阈值                                | 数据来源                    |
| --------------------- | ----------------------------------- | --------------------------- |
| 样本量                | n ≥ 100                             | `ComparisonStats.sample_size` |
| McNemar p             | < 0.05                              | `mcnemar_p_value`           |
| Δprecision            | ≥ 0.05                              | `delta_precision`           |
| V2 recall 不崩塌      | V2 recall ≥ V1 recall × 0.85       | `confusion_v1/v2.recall`    |
| 校准曲线              | 弱单调（高分桶 hit_rate ≥ 低分桶） | `calibration_monotonic`     |
| 健康度                | error_rate < 0.5%，avg_latency 稳定 | `/behavior-eval-health`      |
| **观察期持续时间**    | ≥ 7 天，每天满足上述全部条件        | 新增持久化窗口指标           |
| **跨 regime 鲁棒**    | trend_up / trend_down / range 三种 regime 下 V2 不显著退化 | 用 `regime_filter` 三次回测 |

> 这些门槛在 M3.1 引擎里已经能跑出，但**持续时间和 regime 鲁棒检查**需要在 M4 单独实现。

---

## 3.3 子任务清单

### M4-1. 切换配置层（核心）

- 在 `key_level_tracker_v2.py` 引入 4 个开关：
  ```python
  _BEHAVIOR_SWITCH = {
      "bounce_quality":    "v1",  # "v1" / "v2"
      "breakout_stage":    "v1",
      "fake_break_reclaim": "v1",
      "break_depth_pct":   "v1",
  }
  ```
- 配置由环境变量 / `.env` 加载，不写死代码（方便运维即时回滚）。
- 切换"路由器"：调用方仍是 `_assess_bounce_quality(...)`，
  但函数内部根据开关分发到 V1 实现或 V2 影子函数。
- V2 异常时**自动 fallback V1**（带告警日志 + health 计数）。

**改动范围**：仅 `key_level_tracker_v2.py` 与 `key_level_behavior_eval.py`，
**不动** `models/key_level.py`、不动 signal_builder。

### M4-2. 切换审计与 KPI 对比

- 新增 `processors/behavior_switch_audit.py`：
  - 记录每次切换：维度、from→to、操作时间、生效配置、切换原因（哪些指标过门槛）。
  - 持久化到 `kl_history.json` 同级的 `kl_switch_audit.json`。
- 信号链路 KPI 对比：
  - 切换前 7 天 vs 切换后 7 天的：
    - signal 触发次数 / 类型分布
    - signal P&L 分布（依赖现有 `signal_pnl_tracker`）
    - false breakout 率 / true breakout 率（依赖回测真相判定）
  - 退化阈值：任一 KPI 退化 > 10% 自动告警；> 25% 自动回滚。

### M4-3. 持续监测：Rolling Stats

- 引擎新增 `compute_rolling_comparison(history, window_days=7, step_days=1)`：
  - 滑窗 7 天回测，输出 7 个时间点的 ComparisonStats 序列。
  - 用于"持续 7 天满足门槛"的判定。
- 新建 API `GET /api/key-levels/v1v2-rolling/{coin}?dim=bounce_quality&days=14`
  返回滑窗序列。
- 前端在 `v1v2-compare` 页加"近 14 天滑窗"折线图（McNemar p / Δprecision）。

### M4-4. 跨 regime 鲁棒检查

- `run_full_comparison` 已支持 `regime_filter`（M3.1 留下）。
- 切换决策器（M4-5）调用方法：分别跑 `regime_filter=["trend_up"]` /
  `["trend_down"]` / `["range"]` 三次，三组结果都不能"显著退化"。

### M4-5. 切换决策器与一键操作

- 新增 `scripts/behavior_switch.py`：
  - `--check`：跑全套门槛 + rolling + regime，输出每个维度的"可切换 / 待观察 /
    不可切"判断。
  - `--apply DIMENSION`：写入 `_BEHAVIOR_SWITCH`（更新环境配置 + reload）。
  - `--rollback DIMENSION`：一键回滚到 V1。
  - `--audit`：打印近 30 天切换审计。
- 不要做"自动切换"。所有 apply 必须人工执行（合规 + 安全）。

### M4-6. 前端切换状态可视化

- 在 `/levels/[coin]/v1v2-compare` 顶部加 4 个 chip 显示**当前生效版本**：
  - `反弹质量: V1` / `突破阶段: V1` / `假破回收: V1` / `破位阈值: V1`（绿色 = V1，紫色 = V2）
- 每个 chip 鼠标 hover 显示"上次切换时间 / 切换原因 / 健康指标"。
- chip 不可点击切换（保持"前端不能改生产配置"）。

### M4-7. 测试与回归

- `test_behavior_switch_audit.py`：审计读写 / 滚动窗口正确性。
- `test_behavior_switch_router.py`：开关路由 + 异常 fallback / health 计数。
- `test_behavior_rolling.py`：rolling comparison 输出形状与单点回测一致。
- 端到端冒烟：`--check` → `--apply bounce_quality` → 跑一轮 tracker_v2 →
  `--rollback bounce_quality` → 再跑一轮 → KPI 不变。

---

## 3.4 改动范围一览

| 文件                                         | 改动类型 | 说明                          |
| -------------------------------------------- | -------- | ----------------------------- |
| `key_level_tracker_v2.py`                    | 修改     | 加 4 个开关 + 路由 + fallback |
| `key_level_behavior_eval.py`                 | 修改     | 路由调用 V2 函数（已有）      |
| `behavior_backtest_engine.py`                | 修改     | 加 `compute_rolling_comparison` |
| `processors/behavior_switch_audit.py`        | 新增     | 切换审计模块                  |
| `scripts/behavior_switch.py`                 | 新增     | CLI 切换工具                  |
| `api/routes.py`                              | 修改     | 加 rolling / 当前开关 API     |
| `frontend/src/app/levels/[coin]/v1v2-compare/page.tsx` | 修改 | 加滑窗折线 + 当前开关 chip    |
| `tests/test_behavior_switch_*.py`            | 新增     | 3 个测试文件                  |

---

## 3.5 风险与回滚预案

| 风险                              | 概率 | 影响 | 应对                                                |
| --------------------------------- | ---- | ---- | --------------------------------------------------- |
| V2 抛异常导致信号断流             | 低   | 高   | 路由器自动 fallback；health 计数即时告警            |
| V2 在小样本边缘 case 表现不一致   | 中   | 中   | rolling + regime 双重门槛已经过滤                   |
| 切换后 P&L 退化但单维度指标看似 OK | 中   | 高   | KPI 退化 > 10% 即告警，> 25% 自动回滚                |
| 切换审计文件丢失                  | 低   | 低   | 与 `kl_history.json` 一同每日备份                   |
| 多维度同时切换互相耦合            | 中   | 中   | 强制按 chip 顺序"一次切一个，观察 7 天再切下一个"   |

---

## 3.6 验收标准

至少完成 1 个维度（建议从 `bounce_quality` 开始）的真实切换：
1. ✅ M4-1 ~ M4-7 全部子任务完成 + 测试通过。
2. ✅ 该维度通过全部门槛连续 7 天。
3. ✅ `--apply` 后 7 天，信号链路 KPI 退化 < 10%。
4. ✅ `--rollback` 演练通过，KPI 立即恢复。
5. ✅ 全过程审计可追溯。

> 完成 1 个维度的完整切换循环 = M4 阶段交付。其它 3 个维度按需逐步推进。

---

# 4. M5 · 行为评估接入决策路径

## 4.1 阶段目标

把"已经被验证可信的"行为评估字段（state_confidence / behavior_state / 6 大行为分）
**轻度接入**信号生成与 AI 决策，让关键位辅助层从"观察"升级到"参与"。

> **关键限制**：M5 仍然不改变信号 schema、不抢 V3 state machine 的决定权；
> 行为评估只作为"加分项 / 扣分项 / 提示项"，最终决策仍由现有 V3 主路径出。

---

## 4.2 进入门槛

- M4 至少完成 1 个维度的稳定切换 ≥ 30 天。
- `behavior-eval-health.error_rate < 0.1%`。
- `behavior_eval_version` 升级稳定 ≥ 14 天（无 hotfix）。
- 离线评估：`state_confidence` 与"未来 4h 关键位有效性"的相关系数 > 0.4
  （新增一次性回测脚本验证）。

---

## 4.3 子任务清单

### M5-1. signal_synthesizer 引入 confidence 加权

`backend/processors/signal_synthesizer.py` 增加可选权重：
- 当 `lv.behavior.state_confidence` ≥ 0.65 → 该 level 触发的信号 strength + 5%。
- 当 `lv.behavior.state_confidence` < 0.30 → 该 level 触发的信号 strength - 5%。
- 当 `lv.behavior.contradiction_with_state` 非空 → 信号 explain 列表追加 ⚠ 提示，
  但**不直接扣分**（避免被 noisy 矛盾信号牵着走）。
- 加权由 cfg 开关控制，默认关闭；先放灰度，对比 7 天 P&L。

### M5-2. AI prompt §9g 主表融入 confidence

- 当前 §9g 主表是核心决策表，§9g.1 是第二意见。
- M5 升级：§9g 表每行末尾追加一列 `信心度`（取自 `behavior.state_confidence`），
  仅展示，不改 prompt 决策结构。
- §9g.1 移到附录区，作为"二次验证"文档；§9g 已有的列保持不变。
- 改动范围：`backend/ai/prompts.py` 的 §9g 渲染逻辑 + 现有快照测试。

### M5-3. AI 决策语义化提示

`backend/ai/prompts.py` 在系统 prompt 增补：

> 当某关键位的 `behavior_state` 为 `failed_breakout` / `wait_for_second_test` /
> `capitulation_flush`，或 `contradiction_with_state` 非空时：
> - 不要在该 level 触发即刻入场建议。
> - 必须在 reasoning 中说明"行为评估指出 XX 风险"。
> - 仍以 §9g 主表的状态决定方向，但保守程度需上调一档。

### M5-4. 前端关键位详情：行为级"操作建议"

`LevelDetailRow` 在折叠区底部加"操作提示"小条：
- `confirmed_flip` → "可视为新支撑/阻力，回踩可低吸"。
- `pending_breakout` → "等收盘确认，禁止追"。
- `failed_breakout` → "假破已成立，反向操作慎入"。
- `capitulation_flush` → "等二次确认才考虑做多，单根 K 不可信"。
- `wait_for_second_test` → "等二次回测同向不破才入场"。

> 所有提示都是**展示型**，不参与策略下单。

### M5-5. 关键位质量画像（轻量版）

- 新增 `processors/key_level_quality_profile.py`：
  - 输入：`kl_history.json` 近 30 天数据。
  - 对每个曾出现的 level（按 `level_id` 聚合），统计：
    - 命中次数 / 假突破次数 / 翻转成功次数
    - 平均 state_confidence
    - 历史 behavior_state 分布
  - 输出：`level_quality_profile.json`（每周自动重算）。
- 前端 `LevelDetailRow` 在折叠区底部加迷你画像（"过去 30 天表现：3 次真破 / 1 次假破"）。
- 不参与信号决策，纯展示。

### M5-6. 测试与回归

- `test_signal_confidence_weight.py`：confidence 加减分边界 / 默认关闭。
- `test_prompt_9g_confidence_column.py`：§9g 加列后渲染一致性。
- `test_quality_profile.py`：聚合 + 边界 case（无历史 / 单次 level）。
- 端到端：开 confidence 加权 → 跑 1 周回放 → P&L 对比基线（KPI 退化 < 5%）。

---

## 4.4 改动范围一览

| 文件                                            | 改动类型 | 说明                              |
| ----------------------------------------------- | -------- | --------------------------------- |
| `processors/signal_synthesizer.py`              | 修改     | 加 confidence 加权（cfg 开关）    |
| `ai/prompts.py`                                 | 修改     | §9g 加信心度列；系统 prompt 升级  |
| `processors/key_level_quality_profile.py`       | 新增     | 关键位质量画像                    |
| `frontend/src/components/Levels/LevelDetailRow.tsx` | 修改     | 加操作提示 + 画像迷你区           |
| `frontend/src/lib/types.ts`                     | 修改     | 加 QualityProfile 类型            |
| `api/routes.py`                                 | 修改     | 暴露 quality_profile API          |
| `tests/test_*.py`                               | 新增     | 3 个测试文件                      |

---

## 4.5 风险与回滚预案

| 风险                                | 概率 | 应对                                              |
| ----------------------------------- | ---- | ------------------------------------------------- |
| confidence 加权放大噪声             | 中   | cfg 默认关闭；按维度灰度；KPI 退化即关             |
| §9g 加列让 AI 输出格式漂移          | 低   | 用现有 prompt 测试套件 + 真实 4 个 LLM 对比验证     |
| 质量画像基于历史数据"事后聪明"      | 中   | 仅展示，不参与决策；明确标注"历史 ≠ 未来"          |
| AI 把 capitulation_flush 误读成做多 | 低   | M3.1 已加显式禁忌；M5 prompt 二次强化               |

---

## 4.6 验收标准

1. ✅ confidence 加权开启 7 天，P&L 不退化（最好 +2% ~ +5%）。
2. ✅ AI 在含 contradiction 的关键位 reasoning 中正确引用风险。
3. ✅ 前端"操作提示"经测试用户反馈"信息密度合适、不打扰主决策"。
4. ✅ 质量画像每周稳定生成，前端展示不卡顿。

---

# 5. M6（候选）· 质量画像深化 + V2 思想推广

> 这是开放性方向，**不必现在就敲定**；列在这里防止思路丢失。

候选方向（择 1-2 个推进）：

1. **关键位质量画像 V2**：从"事后统计"升级为"前瞻预测"。
   用 LightGBM / 简单回归把"前 30 天质量画像 + 当前 6 大行为分 + regime"映射到
   "未来 4h 关键位有效性"概率。仅作展示，不替代主路径。

2. **V2 算子推广到 cascade_risk**：现有 cascade_risk 是 M2 的 4 子分加权；
   可以参考 M2.5 双轨思路，给每个子分搞 V2 增强版（例如 `count_score_v2` 用
   percentile 自适应代替死阈值）。先双轨观测 6 周再回测决策。

3. **V2 算子推广到 magnet 流动性**：`key_level_magnets.py` 现在也是死阈值；
   同样套路上 V2 影子 + 回测。

4. **AI 主路径融合**：M5 的 §9g 加列只是渲染。M6 可以让 AI 在 reasoning 阶段
   显式"基于 confidence 加权关键位重要性"，但需要 prompt 工程 + 大量 A/B 验证。

5. **回测引擎扩展**：当前回测只对单维度做混淆矩阵；M6 可加"组合事件"评估，
   比如"breakout + retest + bounce 连续序列的真伪"。

---

# 6. 跨阶段共享的工程纪律

无论 M4 还是 M5，每次提交都遵守：

- **小步快跑**：单 PR ≤ 12 个文件，覆盖 1 个 sub-task。
- **每步可独立运行**：M4-1 不能依赖 M4-3；M5-2 不能强依赖 M5-1。
- **零 schema 破坏**：现有快照、prompt、API、前端 props 都向后兼容。
- **测试先行**：每个新模块的 happy path + 边界 case + 异常 fallback 都要有单测。
- **回滚演练**：每次切换 / 接入开 PR 时，PR 描述里列"如何回滚"。
- **审计日志**：所有"自动 fallback"、"配置切换"、"V2 异常"都进 logger.warning
  级别以上。

---

# 7. 当前下一步建议

按优先级：

1. **第一步（建议先做）**：M4-3（rolling stats）+ M4-6（前端开关 chip）。
   原因：这两步**纯只读**、零风险，可以让你每天打开 v1v2-compare 页就看到"哪些维度
   接近门槛了"，为后续切换决策提供"数据语感"。
2. **第二步**：M4-1 + M4-2（切换路由 + 审计）。开关默认全 V1，先把基础设施铺好。
3. **第三步**：等积累到 100+ 样本 + 7 天稳定后，做 `bounce_quality` 维度的真实切换。
4. **再之后**：M5-1（confidence 加权）。

> M4 用 4-6 周；M5 用 3-4 周；总周期 8-10 周（按真实历史样本积累节奏）。
