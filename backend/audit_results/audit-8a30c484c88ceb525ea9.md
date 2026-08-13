# BTC 底部模型 · 全维度数学审计报告

审计 ID：`audit-8a30c484c88ceb525ea9`

## 1. Executive Audit Conclusion

**INSUFFICIENT EVIDENCE。** bottom-v4 是启发式证据评分，不是已校准概率。所有 legacy 回放均为 PIT_APPROX，没有严格 PIT OOS；主 180 天标签的 Walk-Forward 仅 6 个成熟点且没有正类，PR-AUC、MCC、Recall 均不可评分。样本内结果不能证明优于简单估值/等权基准。最大风险是 legacy 数据缺少真实 vintage，且生产旧快照已确认混入未来日与未收盘周线。Confirmation 和 60/40 组合没有显示稳定增量。经济显著性与概率校准均不可评分。当前只能保留为研究型状态指标。

## 2. Data Integrity Audit

dataset_id=`64cf5d0a473b1d15fe459fca28692696aaf12d58cd7aef7db056ae902de50383`；policy=pit-final-v2；历史状态=PIT_APPROX。来源=production_server_frozen_sqlite_copy；冻结时间=2026-08-13 (Asia/Shanghai; exact second not recorded)；SQLite quick_check=ok。生产遗留快照污染证据：2026-08-12 snapshot referenced 2026-08-13 BTC daily close/volume, aggregate OI, CME OI, liquidations, funding and fear-greed; it also had access to the still-open weekly bar starting 2026-08-10.

未来日、未收盘周线和失败多输出已在新生产链路 fail-closed；旧历史仍不得冒充严格 PIT。逐指标缺失率、最大时间戳间隔、更新滞后、角色与来源见 JSON `indicator_audit`。第二数据源一致性：**UNSCORABLE**。

## 3. Target / Label Audit

生成 Label A/B/C 共 34 个组合，完整 N/正例率见 JSON `labels`。主审计标签 `C_180_r20_mae20` 同时约束 180 天终点收益与 MAE。所有标签均排除未走完前向窗口；不同时间尺度不混合。

## 4. Sample Size Audit

Daily raw N=5695；weekly replay N=661；组合分数 N=196；主标签成熟 N=170；事件 N=4；180 天非重叠 N=7；N_eff=11.836165455412358。独立统计功效严重不足。

## 5. Accuracy Dashboard

| 尺度 | Label | 状态 | OOS N | Precision | Recall | F1 | MCC | Bal.Acc | ROC-AUC | PR-AUC |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 4W | B_30_r10 | RESEARCH_ONLY_PIT_APPROX | 79 | 0.103 | 0.364 | 0.160 | -0.105 | 0.424 | 0.520 | 0.199 |
| 12W | B_90_r20 | UNSCORABLE_INSUFFICIENT_OOS_CLASS_SUPPORT | 19 | UNSCORABLE | 0.000 | 0.000 | UNSCORABLE | 0.500 | 0.222 | 0.067 |
| 26W | C_180_r20_mae20 | UNSCORABLE_INSUFFICIENT_OOS_CLASS_SUPPORT | 6 | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE |
| 52W | C_365_r30_mae25 | UNSCORABLE | 0 | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE |

## 6. Probability Calibration

产品 probability=null。主标签 OOS 状态=UNSCORABLE_INSUFFICIENT_OOS_CLASS_SUPPORT；Brier=0.871，Log Loss=2.708，ECE=0.933。

Reliability 分桶已写入 JSON `calibration.curve`；由于 OOS 只有单一类别，不能发布概率。

## 7. Economic Performance

Score Bin 结果是 PIT_APPROX 样本内描述，不是 OOS 交易证据。180 天摘要：
| Score Bin | N | Mean Return | Median Return | Win Rate | Median MAE | Median MFE |
|---|---:|---:|---:|---:|---:|---:|
| 0-20 | 0 | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE |
| 20-40 | 10 | 0.105 | 0.080 | 0.500 | -0.142 | 0.294 |
| 40-60 | 48 | 0.344 | 0.339 | 0.729 | -0.122 | 0.452 |
| 60-80 | 96 | 0.228 | 0.207 | 0.688 | -0.159 | 0.335 |
| 80-100 | 16 | 0.290 | 0.247 | 0.875 | -0.115 | 0.329 |

策略只采用首次跨阈值后的下一可交易日、现货、1x、0/10/25bps；收益分位、block-bootstrap CI、MAE/MFE、路径最大回撤、资金占用与机会成本见 JSON `strategy`。经济显著性=UNSCORABLE。

## 8. Benchmark Comparison

以下为共同样本上的描述性排序，不是 OOS 升级证据：
| Benchmark | PR-AUC | MCC | Recall |
|---|---:|---:|---:|
| never_bottom | 0.512 | UNSCORABLE | 0.000 |
| random_prevalence | 0.522 | 0.081 | 0.563 |
| 200w | 0.596 | 0.284 | 0.253 |
| ath_drawdown | 0.562 | 0.231 | 0.103 |
| single_valuation | 0.717 | 0.309 | 0.609 |
| equal_factor | 0.732 | 0.234 | 0.805 |
| trend_confirmation | 0.580 | -0.178 | 0.678 |
| champion_stress | 0.741 | 0.279 | 0.851 |
| combined | 0.600 | -0.135 | 0.793 |

## 9. Indicator Audit Table

逐指标统计为 PIT_APPROX 描述；Incremental Value/逐指标消融因旧回放未冻结原始特征而 UNSCORABLE。
| 指标 | 角色 | 来源 | 覆盖 | N | Lag(d) | PIT | ROC-AUC | PR-AUC | IC180 | Max abs rho | 建议 |
|---|---|---|---|---:|---:|---|---:|---:|---:|---:|---|
| btc_close_1d | model_input | coinglass | 2021-02-20..2026-08-12 | 2000 | 0 | PIT_APPROX | 0.134 | 0.342 | -0.682 | 1.000 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| btc_close_1w | model_input | coinglass | 2019-09-09..2026-08-03 | 361 | 9 | PIT_APPROX | 0.134 | 0.342 | -0.675 | 0.998 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| btc_high_1w | model_input | coinglass | 2019-09-09..2026-08-03 | 361 | 9 | PIT_APPROX | 0.132 | 0.341 | -0.678 | 0.998 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| btc_low_1w | model_input | coinglass | 2019-09-09..2026-08-03 | 361 | 9 | PIT_APPROX | 0.140 | 0.343 | -0.677 | 0.996 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| btc_price_onchain | model_input | coinglass | 2010-07-13..2026-08-12 | 5875 | 0 | PIT_APPROX | 0.135 | 0.342 | -0.682 | 1.000 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| btc_vol_1d | unused | coinglass | 2021-02-20..2026-08-12 | 2000 | 0 | PIT_APPROX | 0.277 | 0.395 | -0.311 | 0.608 | DEPRECATE |
| cme_close_1w | display_only | yahoo_cme | 2017-12-18..2026-08-03 | 451 | 9 | PIT_APPROX | 0.134 | 0.342 | -0.674 | 0.998 | DISPLAY_ONLY |
| cme_oi_usd | model_input | coinglass | 2021-02-20..2026-08-12 | 2000 | 0 | PIT_APPROX | 0.165 | 0.348 | -0.606 | 0.968 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| cme_vol_1w | model_input | yahoo_cme | 2017-12-18..2026-08-03 | 451 | 9 | PIT_APPROX | 0.421 | 0.458 | -0.124 | 0.608 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| coinbase_premium_rate | model_input | coinglass | None..None | 0 | None | PIT_UNAVAILABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | BLOCKED_MISSING_DATA |
| etf_flow_usd | model_input | coinglass | 2024-01-11..2026-08-12 | 665 | 0 | PIT_APPROX | 0.366 | 0.261 | -0.133 | 0.671 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| exchange_balance_btc | model_input | coinglass | 2024-08-23..2026-08-12 | 673 | 0 | PIT_APPROX | 0.904 | 0.805 | 0.825 | 0.986 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| fear_greed | model_input | coinglass | 2018-02-01..2026-08-12 | 3093 | 0 | PIT_APPROX | 0.366 | 0.411 | -0.093 | 0.832 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| funding_oiw | model_input | coinglass | 2023-11-17..2026-08-12 | 1000 | 0 | PIT_APPROX | 0.455 | 0.360 | -0.002 | 0.630 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| global_m2_yoy | model_input | coinglass | 2013-05-20..2026-06-29 | 685 | 44 | PIT_APPROX | 0.352 | 0.406 | -0.476 | 0.878 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| liq_long_usd | model_input | coinglass | 2023-11-17..2026-08-12 | 1000 | 0 | PIT_APPROX | 0.360 | 0.326 | -0.165 | 0.612 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| liq_short_usd | model_input | coinglass | 2023-11-17..2026-08-12 | 1000 | 0 | PIT_APPROX | 0.426 | 0.357 | -0.079 | 0.504 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| lth_realized_loss | model_input | bgeometrics | 2022-08-12..2026-08-11 | 1441 | 1 | PIT_APPROX | 0.318 | 0.453 | -0.102 | 0.775 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| lth_realized_price | display_only | coinglass | 2010-08-17..2026-08-11 | 5836 | 1 | PIT_APPROX | 0.251 | 0.373 | -0.682 | 0.948 | DISPLAY_ONLY |
| lth_sopr | model_input | coinglass | 2010-07-15..2026-08-11 | 5872 | 1 | PIT_APPROX | 0.321 | 0.411 | -0.055 | 0.752 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| ma_200w | model_input | coinglass | 2010-07-13..2026-08-12 | 5875 | 0 | PIT_APPROX | 0.190 | 0.355 | -0.648 | 0.988 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| mvrv_zscore | model_input | bgeometrics | 2022-08-12..2026-08-11 | 1461 | 1 | PIT_APPROX | 0.181 | 0.354 | -0.494 | 0.976 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| nupl | model_input | coinglass | 2009-01-09..2026-08-11 | 6424 | 1 | PIT_APPROX | 0.203 | 0.360 | -0.402 | 0.910 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| oi_agg_usd | model_input | coinglass | 2021-02-20..2026-08-12 | 2000 | 0 | PIT_APPROX | 0.148 | 0.345 | -0.691 | 0.986 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| puell_multiple | model_input | coinglass | 2010-08-17..2026-08-11 | 5836 | 1 | PIT_APPROX | 0.395 | 0.447 | -0.160 | 0.610 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| realized_loss | model_input | bgeometrics | 2022-08-12..2026-08-11 | 1461 | 1 | PIT_APPROX | 0.640 | 0.671 | 0.252 | 0.612 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| realized_profit | model_input | bgeometrics | 2022-08-12..2026-08-11 | 1461 | 1 | PIT_APPROX | 0.188 | 0.358 | -0.312 | 0.764 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| reserve_risk | model_input | coinglass | 2010-08-17..2026-08-11 | 5836 | 1 | PIT_APPROX | 0.141 | 0.343 | -0.598 | 0.976 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| sopr | model_input | bgeometrics | 2022-08-12..2026-08-11 | 1461 | 1 | PIT_APPROX | 0.335 | 0.406 | -0.169 | 0.764 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| spot_net_taker_usd | model_input | coinglass | None..None | 0 | None | PIT_UNAVAILABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | UNSCORABLE | BLOCKED_MISSING_DATA |
| stablecoin_total_mcap | model_input | coinglass | 2017-12-14..2026-08-12 | 3164 | 0 | PIT_APPROX | 0.148 | 0.344 | -0.694 | 0.988 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| sth_mvrv | model_input | bgeometrics | 2022-08-12..2026-08-11 | 1461 | 1 | PIT_APPROX | 0.359 | 0.419 | -0.177 | 0.832 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| sth_realized_price | model_input | coinglass | 2010-08-17..2026-08-11 | 5836 | 1 | PIT_APPROX | 0.191 | 0.355 | -0.647 | 0.971 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| sth_sopr | model_input | coinglass | 2010-07-13..2026-08-11 | 5874 | 1 | PIT_APPROX | 0.418 | 0.451 | -0.071 | 0.776 | HOLD_PENDING_INCREMENTAL_OOS_TEST |
| sth_supply | model_input | coinglass | 2011-08-20..2026-08-11 | 5468 | 1 | PIT_APPROX | 0.172 | 0.356 | -0.504 | 0.689 | HOLD_PENDING_INCREMENTAL_OOS_TEST |

## 10. Redundancy Audit

因子 Pearson/Spearman/MI、VIF、|ρ|≥0.70 聚类见 JSON `factor_diagnostics`。聚类=[['capitulation', 'demand', 'valuation'], ['leverage'], ['macro'], ['structure']]。同簇信号不能当独立证据；原始指标级最大相关性见上表。

## 11. Ablation Results

{'stress_only': {'n': 170, 'roc_auc': 0.7181830771361307, 'pr_auc': 0.7413433954280476}, 'confirmation_only': {'n': 170, 'roc_auc': 0.5002077274615704, 'pr_auc': 0.5802971111535044}, 'combined': {'n': 170, 'roc_auc': 0.5359368508516826, 'pr_auc': 0.6002701137190404}, 'equal_factor_candidate': {'n': 170, 'roc_auc': 0.7010109403129761, 'pr_auc': 0.7320139698178111}, 'with_fake_bottom_filter': {'n': 170, 'roc_auc': 0.5359368508516826, 'pr_auc': 0.6002701137190404}, 'without_fake_bottom_filter': {'n': 170, 'roc_auc': 0.5252735078244011, 'pr_auc': 0.5969267681958338}, 'factor_group_leave_one_out_equal_weight_proxy': {'without_capitulation': {'n': 170, 'roc_auc': 0.6707519734108849, 'pr_auc': 0.7069105017655349}, 'without_demand': {'n': 170, 'roc_auc': 0.7121589807505886, 'pr_auc': 0.7175079293611951}, 'without_leverage': {'n': 170, 'roc_auc': 0.7352859714720953, 'pr_auc': 0.7794785751036781}, 'without_macro': {'n': 170, 'roc_auc': 0.6868854729261875, 'pr_auc': 0.7131037827512627}, 'without_structure': {'n': 170, 'roc_auc': 0.6766375848220468, 'pr_auc': 0.7069997579233329}, 'without_valuation': {'n': 170, 'roc_auc': 0.6737294003600609, 'pr_auc': 0.7212895669690054}}, 'with_vs_without_eq': 'UNSCORABLE_NO_PRE_EQ_REPLAY_FEATURES'}

逐因子 1,000 次 permutation 及 CI 见 `permutation_importance`。Stress-only 的样本内排序高于 Confirmation/60:40 组合，但没有可评分 OOS CI，不能据此直接改生产权重。

## 12. Regime Stability

价格趋势/波动 Regime：{'status': 'DESCRIPTIVE_CAUSAL_PRICE_REGIMES_PIT_APPROX_NOT_OOS', 'price_regimes': {'bear_below_200d': {'n': 46, 'positive_n': 31, 'tp': 3, 'tn': 15, 'fp': 0, 'fn': 28, 'precision': 1.0, 'recall': 0.0967741935483871, 'specificity': 1.0, 'f1': 0.17647058823529413, 'mcc': 0.18373469898171788, 'balanced_accuracy': 0.5483870967741935, 'roc_auc': 0.6129032258064516, 'pr_auc': 0.7706650817687533, 'brier': 'UNSCORABLE', 'log_loss': 'UNSCORABLE', 'ece': 'UNSCORABLE'}, 'high_vol_ge_60pct': {'n': 12, 'positive_n': 5, 'tp': 1, 'tn': 5, 'fp': 2, 'fn': 4, 'precision': 0.3333333333333333, 'recall': 0.2, 'specificity': 0.7142857142857143, 'f1': 0.25, 'mcc': -0.09759000729485331, 'balanced_accuracy': 0.4571428571428572, 'roc_auc': 0.2, 'pr_auc': 0.46050505050505053, 'brier': 'UNSCORABLE', 'log_loss': 'UNSCORABLE', 'ece': 'UNSCORABLE'}, 'low_vol_lt_60pct': {'n': 158, 'positive_n': 82, 'tp': 40, 'tn': 49, 'fp': 27, 'fn': 42, 'precision': 0.5970149253731343, 'recall': 0.4878048780487805, 'specificity': 0.6447368421052632, 'f1': 0.5369127516778524, 'mcc': 0.13400105804445955, 'balanced_accuracy': 0.5662708600770219, 'roc_auc': 0.5557605905006419, 'pr_auc': 0.6156357980977292, 'brier': 'UNSCORABLE', 'log_loss': 'UNSCORABLE', 'ece': 'UNSCORABLE'}, 'bull_above_200d': {'n': 124, 'positive_n': 56, 'tp': 38, 'tn': 39, 'fp': 29, 'fn': 18, 'precision': 0.5671641791044776, 'recall': 0.6785714285714286, 'specificity': 0.5735294117647058, 'f1': 0.6178861788617886, 'mcc': 0.2517375110620139, 'balanced_accuracy': 0.6260504201680672, 'roc_auc': 0.6721376050420168, 'pr_auc': 0.6427846434430355, 'brier': 'UNSCORABLE', 'log_loss': 'UNSCORABLE', 'ece': 'UNSCORABLE'}}, 'liquidity_macro_crisis_halving_regimes': 'UNSCORABLE: versioned regime series and sufficient cycle support are missing'}

LOCO：[{'cycle': '2013-2016', 'n': 0, 'positive_n': 0, 'train_n': 170, 'threshold_from_other_cycles': 68.0, 'pr_auc': None, 'roc_auc': None, 'mcc': None, 'recall': None}, {'cycle': '2017-2020', 'n': 0, 'positive_n': 0, 'train_n': 170, 'threshold_from_other_cycles': 68.0, 'pr_auc': None, 'roc_auc': None, 'mcc': None, 'recall': None}, {'cycle': '2021-2024', 'n': 112, 'positive_n': 78, 'train_n': 58, 'threshold_from_other_cycles': 36.0, 'pr_auc': 0.7206378753703614, 'roc_auc': 0.4967948717948718, 'mcc': None, 'recall': 1.0}, {'cycle': '2025-2029', 'n': 58, 'positive_n': 9, 'train_n': 112, 'threshold_from_other_cycles': 62.0, 'pr_auc': 0.10622779140424722, 'roc_auc': 0.19954648526077098, 'mcc': -0.31874456788670646, 'recall': 0.1111111111111111}]

宏观、流动性、减半和危机 Regime 因点时标签/周期不足为 UNSCORABLE。

## 13. Overfitting Audit

IS={'n': 170, 'positive_n': 87, 'tp': 30, 'tn': 67, 'fp': 16, 'fn': 57, 'precision': 0.6521739130434783, 'recall': 0.3448275862068966, 'specificity': 0.8072289156626506, 'f1': 0.45112781954887216, 'mcc': 0.17108577805430789, 'balanced_accuracy': 0.5760282509347736, 'roc_auc': 0.5359368508516826, 'pr_auc': 0.6002701137190404, 'brier': 'UNSCORABLE', 'log_loss': 'UNSCORABLE', 'ece': 'UNSCORABLE', 'threshold': 68.0}

OOS={'n': 6, 'positive_n': 0, 'tp': 0, 'tn': 6, 'fp': 0, 'fn': 0, 'precision': None, 'recall': None, 'specificity': 1.0, 'f1': None, 'mcc': None, 'balanced_accuracy': None, 'roc_auc': None, 'pr_auc': None, 'brier': 0.8711111111111113, 'log_loss': 2.7080502011022105, 'ece': 0.9333333333333335, 'validation_status': 'UNSCORABLE_INSUFFICIENT_OOS_CLASS_SUPPORT'}

Bootstrap={'status': 'UNSCORABLE', 'reason': 'insufficient_oos_rows'}；Permutation={'iterations': 1000, 'observed_pr_auc': 0.6002701137190404, 'p_value': 0.03596403596403597}；参数敏感性见 `parameter_sensitivity`。features=32，N_eff/features=0.3698801704816362，风险=HIGH。Challenger=RESEARCH_ONLY，升级结论=REJECT_CHALLENGER_PROMOTION。

## 14. False Bottom Analysis

Top 10（样本内阈值案例，仅用于诊断）：
- 2023-02-28: score=83.4, stress=72.3, confirmation=100.0, factors={'valuation': 70.1, 'capitulation': 68.2, 'leverage': 85.9, 'demand': None, 'structure': 84.7, 'macro': 39.5}
- 2023-03-28: score=81.5, stress=69.1, confirmation=100.0, factors={'valuation': 60.8, 'capitulation': 59.5, 'leverage': 81.5, 'demand': None, 'structure': 80.0, 'macro': 64.4}
- 2023-04-25: score=80.4, stress=67.3, confirmation=100.0, factors={'valuation': 62.0, 'capitulation': 71.1, 'leverage': 74.4, 'demand': None, 'structure': 79.3, 'macro': 35.3}
- 2023-03-21: score=80.2, stress=67.0, confirmation=100.0, factors={'valuation': 58.1, 'capitulation': 55.4, 'leverage': 79.1, 'demand': None, 'structure': 79.2, 'macro': 65.3}
- 2023-02-21: score=80.0, stress=66.7, confirmation=100.0, factors={'valuation': 63.8, 'capitulation': 66.4, 'leverage': 68.9, 'demand': None, 'structure': 82.9, 'macro': 37.2}
- 2023-03-07: score=77.7, stress=74.4, confirmation=82.7, factors={'valuation': 70.3, 'capitulation': 65.4, 'leverage': 99.2, 'demand': None, 'structure': 86.4, 'macro': 39.8}
- 2023-04-04: score=75.9, stress=65.6, confirmation=91.4, factors={'valuation': 56.7, 'capitulation': 55.7, 'leverage': 75.2, 'demand': None, 'structure': 79.3, 'macro': 61.6}
- 2023-04-18: score=75.4, stress=59.0, confirmation=100.0, factors={'valuation': 53.7, 'capitulation': 58.2, 'leverage': 59.6, 'demand': None, 'structure': 77.2, 'macro': 34.2}
- 2023-03-14: score=73.6, stress=67.5, confirmation=82.7, factors={'valuation': 63.7, 'capitulation': 50.3, 'leverage': 95.8, 'demand': None, 'structure': 82.1, 'macro': 37.7}
- 2023-04-11: score=73.2, stress=61.0, confirmation=91.4, factors={'valuation': 54.1, 'capitulation': 51.5, 'leverage': 61.8, 'demand': None, 'structure': 77.3, 'macro': 59.8}

旧回放未冻结所有原始子信号，逐案因果归因仍为 UNSCORABLE。

## 15. Missed Bottom Analysis

Top 10（样本内阈值案例，仅用于诊断）：
- 2024-09-10: score=36.2, stress=60.4, confirmation=0.0, factors={'valuation': 59.6, 'capitulation': 73.0, 'leverage': 93.5, 'demand': 41.9, 'structure': 41.5, 'macro': 53.1}
- 2025-04-08: score=36.8, stress=54.2, confirmation=10.8, factors={'valuation': 54.9, 'capitulation': 61.9, 'leverage': 95.9, 'demand': 40.6, 'structure': 22.4, 'macro': 59.3}
- 2025-03-11: score=37.3, stress=55.1, confirmation=10.5, factors={'valuation': 48.6, 'capitulation': 56.6, 'leverage': 96.8, 'demand': 40.4, 'structure': 35.6, 'macro': 63.5}
- 2025-03-04: score=37.4, stress=55.3, confirmation=10.5, factors={'valuation': 42.1, 'capitulation': 54.5, 'leverage': 97.0, 'demand': 43.2, 'structure': 42.2, 'macro': 65.5}
- 2025-02-25: score=40.0, stress=53.0, confirmation=20.4, factors={'valuation': 41.1, 'capitulation': 53.8, 'leverage': 85.7, 'demand': 44.3, 'structure': 43.4, 'macro': 58.4}
- 2024-08-06: score=41.5, stress=56.7, confirmation=18.6, factors={'valuation': 60.7, 'capitulation': 66.6, 'leverage': 94.2, 'demand': 38.6, 'structure': 24.3, 'macro': 64.2}
- 2024-07-09: score=42.5, stress=54.6, confirmation=24.4, factors={'valuation': 55.6, 'capitulation': 53.2, 'leverage': 92.4, 'demand': 43.2, 'structure': 30.6, 'macro': 63.9}
- 2024-06-25: score=44.2, stress=49.6, confirmation=36.0, factors={'valuation': 53.6, 'capitulation': 56.7, 'leverage': 74.9, 'demand': 28.5, 'structure': 41.3, 'macro': 38.2}
- 2025-04-15: score=44.7, stress=54.0, confirmation=30.8, factors={'valuation': 47.9, 'capitulation': 62.5, 'leverage': 92.7, 'demand': 39.2, 'structure': 35.2, 'macro': 51.0}
- 2025-04-01: score=45.2, stress=54.8, confirmation=30.7, factors={'valuation': 46.7, 'capitulation': 64.5, 'leverage': 91.3, 'demand': 53.5, 'structure': 38.5, 'macro': 31.8}

必须在未来严格 PIT 数据上验证共同原因。

## 16. Missing Indicator Audit

Critical：真实 available_at/vintage/revision、关键指标第二数据源。Important：期权 skew/期限结构、basis、订单簿流动性、DXY/实质利率/金融条件的点时序列。Optional：Hash Ribbon、Dormancy 等长历史链上项。任何新增项必须通过独立 OOS 增量测试；当前一律 `REQUIRES_PIT_OOS`。

## 17. Model Audit Score

{'predictive_power': 'UNSCORABLE', 'calibration': 'UNSCORABLE', 'robustness': 'UNSCORABLE', 'data_quality': 'UNSCORABLE_STRICT_PIT_MISSING', 'feature_quality': 'UNSCORABLE_INCREMENTAL_TEST_MISSING', 'overfitting_risk': 'HIGH', 'final_mas': 'UNSCORABLE'}

因严格 OOS、Calibration、Robustness 与数据点时完整性不可评分，禁止为 Dashboard 填造 0-100 数字，也禁止把 MAS 当准确率。

## 18. 最终模型评级

**INSUFFICIENT EVIDENCE — 研究型状态指标。** `prediction.kind=score`，`probability=null`。不连接自动交易，不称底部概率。

## 19. 最值得修改的 10 个问题

1. **历史数据没有真实 available_at/vintage/revision** — 证据：legacy rows are PIT_APPROX/PIT_UNAVAILABLE；修改：持续积累 append-only 观测版本并冻结每次审计数据集；预期：允许未来开展严格 PIT 回测；幅度当前不可量化；验证：用已知发布滞后和历史修订夹具验证 as-of 查询。
2. **严格 Walk-Forward 测试样本和类别支持不足** — 证据：only 6 OOS rows and zero positives in the sole scored fold；修改：延长严格 PIT 数据积累期，不降低门槛换取表面数字；预期：提高可评分性而非保证提升性能；验证：OOS N>=30 且正负类均有足够事件后才计算 PR-AUC/MCC。
3. **独立事件和有效样本极少** — 证据：event_n=4, n_eff=11.836165455412358；修改：所有结论绑定事件 N、非重叠 N 与 block-bootstrap；预期：降低虚假显著性；不会凭空增加预测力；验证：复制重叠周点不得改变事件点估计或置信区间。
4. **Confirmation 未证明增量价值** — 证据：sample-in Confirmation IC is negative and combined PR-AUC is below Stress；修改：保持为 Challenger 候选，拆分价格确认与需求确认后重新 OOS；预期：未知；可能应降权或删除；验证：paired OOS delta PR-AUC/Brier/Recall block-bootstrap。
5. **现有人工权重缺少统计来源** — 证据：HEURISTIC WEIGHT; no scorable multi-fold Challenger comparison；修改：只在训练折比较等权、Logistic、Ridge、Lasso、Elastic Net；预期：不可预设；验证：多个冻结测试窗均优于 Champion 才升级。
6. **原始指标未逐项进入版本化回放特征** — 证据：individual incremental value and ablation are UNSCORABLE；修改：冻结每个子信号/原始指标的 as-of 特征值与缺失原因；预期：使死指标与噪声指标可被证伪；验证：逐指标/因子组消融和 1000 次置换重要性。
7. **核心数据源缺少第二供应商交叉验证** — 证据：source consistency metrics are UNSCORABLE；修改：为价格、OI、资金费、ETF、稳定币建立只读第二源对照；预期：降低 Vendor Risk；预测增量未知；验证：相关性、MAD、最大偏差和缺失日期报告。
8. **假底过滤器仅有样本内微弱差异** — 证据：with/without filter difference has no OOS confidence interval；修改：冻结过滤器输入，单独评估 FPR、Recall、MAE 与净收益；预期：不可预设；验证：paired block-bootstrap net benefit and Recall non-inferiority。
9. **Regime 稳定性缺少宏观/流动性点时序列** — 证据：only causal price trend/volatility regimes are descriptively scorable；修改：版本化 DXY、实质利率、流动性与危机标签；预期：识别模型失效环境；不保证整体指标上升；验证：训练折定义 Regime，冻结测试折分别报告指标。
10. **经济结果仍受 PIT_APPROX 和少周期限制** — 证据：economic significance is UNSCORABLE；修改：仅用首次跨阈值、下一可交易日、成本和资金占用回测；预期：避免重叠信号夸大收益；验证：与 DCA/Buy-and-Hold/ATH/200W 同机会集配对比较。

## 20. 下一轮实验清单

- **1. PIT ingestion shadow test**：连续收集 observation_ts/period_end/available_at/revision；成功标准：所有 as-of 查询零未来值且修订可追溯；证伪条件：任一决策时点读到 available_at 之后的数据。
- **2. Confirmation incremental test**：Stress-only vs Confirmation-only vs 组合 vs 过滤器，purged Walk-Forward；成功标准：组合 ΔPR-AUC>0、ΔBrier<0 且 Recall 不恶化的 95% CI；证伪条件：区间跨 0 或 Recall/MAE 恶化。
- **3. Weight challenger**：训练折比较等权/Logistic/Ridge/Lasso/Elastic Net；成功标准：多个冻结窗稳定优于 Champion；证伪条件：仅样本内提升或单窗提升。
- **4. Individual feature ablation**：冻结原始特征后逐项删除和 1000 次 permutation；成功标准：增量方向稳定且 CI 不含 0；证伪条件：删除不降反升或置换无影响。
- **5. Leave-One-Cycle-Out**：每次完整留出一个 BTC 周期；成功标准：所有可评分周期方向一致；证伪条件：新周期 PR-AUC/MCC 反向。
- **6. Calibration gate**：嵌套 OOS 生成 reliability/Brier/ECE；成功标准：校准在冻结窗稳定且优于基准；证伪条件：过度自信或 Brier 无改善。
- **7. Source consistency**：关键指标双供应商逐日对齐；成功标准：偏差在预注册容忍内且缺失可解释；证伪条件：方向性分歧或 schema 漂移。
- **8. Score monotonicity**：按训练折边界分箱，测试折比较收益/MAE；成功标准：分数越高收益改善且 MAE 不恶化；证伪条件：高分箱无区分或更差。
- **9. Strategy opportunity-cost test**：首次跨阈值后次日入场，0/10/25bps，与 DCA/B&H 配对；成功标准：收益/MAE/资金占用的 CI 支持增量；证伪条件：机会成本为正或风险恶化。
- **10. Leakage canary**：持续运行纯随机/已知信号/故意未来特征合成集；成功标准：分别判无效/有效/DATA_LEAKAGE；证伪条件：泄漏集未被阻断或随机集被验证。
