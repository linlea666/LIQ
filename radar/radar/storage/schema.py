"""数据库结构定义。

设计原则：
  1. As-of 不可变——快照/警报/里程碑/决策事件写入后不再修改，
     禁止用"后来才知道的信息"覆盖历史记录（防未来数据泄漏）。
  2. NULL 就是 UNKNOWN——缺字段一律存 NULL，绝不填 0。
     "没拿到 Dev 持仓"和"Dev 持仓 0%"是完全不同的事实。
  3. 高频查询字段独立成列，不常查询的明细才进 JSON。
  4. 拒绝与 Near-Miss 单独建表/打标，因为"我们当时为什么没报警"
     在几个月后往往比"我们报了什么"更有研究价值。
"""

from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- ═══════════════════════════════════════════════════════════════════════
-- 元信息
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ═══════════════════════════════════════════════════════════════════════
-- 代币主表：一枚币只要被看见过就永久建档
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS token_master (
    token_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id             TEXT    NOT NULL,
    contract_address     TEXT    NOT NULL,
    symbol               TEXT,
    name                 TEXT,
    decimals             INTEGER,
    launch_time_ms       INTEGER,          -- 代币创建时间（用于计算 token age）
    creator_address      TEXT,
    launch_platform      TEXT,

    -- 供应量及其可信度：max_supply 为 NULL 表示未知，不可解释为 0
    circulating_supply   REAL,
    total_supply         REAL,
    max_supply           REAL,
    supply_source        TEXT,
    supply_updated_at    INTEGER,
    supply_confidence    REAL,

    -- 首次发现现场（不可变，回答"我们在多少市值时发现它"）
    first_seen_ms            INTEGER NOT NULL,
    first_seen_market_cap    REAL,
    first_seen_price         REAL,
    first_seen_holders       INTEGER,
    first_seen_source        TEXT,

    -- 机器状态
    state                TEXT    NOT NULL DEFAULT 'DISCOVERED',
    state_since_ms       INTEGER,
    last_observed_ms     INTEGER,
    last_snapshot_ms     INTEGER,

    -- 研究采样与留存
    is_reject_sample     INTEGER NOT NULL DEFAULT 0,
    retention_class      TEXT    NOT NULL DEFAULT 'normal',  -- normal | important
    reject_reason        TEXT,

    UNIQUE(chain_id, contract_address)
);
CREATE INDEX IF NOT EXISTS idx_token_state     ON token_master(state, last_observed_ms DESC);
CREATE INDEX IF NOT EXISTS idx_token_first_seen ON token_master(first_seen_ms DESC);
CREATE INDEX IF NOT EXISTS idx_token_chain     ON token_master(chain_id, state);

-- ═══════════════════════════════════════════════════════════════════════
-- 池子与迁移：同一 token 可能经历 bonding curve → DEX，且存在多个池
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS pool_master (
    pool_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id       INTEGER NOT NULL,
    pool_address   TEXT,
    quote_asset    TEXT,
    protocol       TEXT,
    is_primary     INTEGER NOT NULL DEFAULT 1,
    first_seen_ms  INTEGER,
    last_seen_ms   INTEGER,
    UNIQUE(token_id, pool_address)
);
CREATE INDEX IF NOT EXISTS idx_pool_token ON pool_master(token_id);

CREATE TABLE IF NOT EXISTS migration_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id      INTEGER NOT NULL,
    from_stage    TEXT,
    to_stage      TEXT,
    occurred_at   INTEGER,
    detected_at   INTEGER NOT NULL,
    payload_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_migration_token ON migration_events(token_id, detected_at DESC);

-- ═══════════════════════════════════════════════════════════════════════
-- 特征快照：研究数据库的主体
--   三个时间戳缺一不可，否则日后无法区分
--   "我们领先了 47 分钟" 和 "币安接口自己缓存晚了 20 分钟"
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id       INTEGER NOT NULL,

    source_at      INTEGER,          -- 数据本身对应的时刻（币安返回）
    observed_at    INTEGER NOT NULL, -- 雷达真正看到的时刻
    stored_at      INTEGER NOT NULL, -- 写入数据库的时刻

    endpoint       TEXT,
    latency_ms     INTEGER,
    response_hash  TEXT,
    parser_version TEXT,

    -- cohort 键：为 V2 的"同链同龄同市值段百分位评分"预先积累
    cohort_chain      TEXT,
    cohort_age_bucket TEXT,
    cohort_mc_bucket  TEXT,
    cohort_stage      TEXT,

    -- 市场
    price                 REAL,
    market_cap            REAL,
    reported_market_cap   REAL,
    computed_market_cap   REAL,     -- price × circulating_supply，用于交叉校验
    mc_deviation_ratio    REAL,
    fdv                   REAL,
    liquidity             REAL,

    -- 从 trending 的 1 分钟序列提取的区间极值，回填低频轮询间隙（防 ATH 漏采）
    interval_high    REAL,
    interval_low     REAL,
    interval_volume  REAL,
    -- detail 的 24h 极值；对上线不足 24h 的币即历史极值
    price_high_24h   REAL,
    price_low_24h    REAL,

    -- 生命周期
    bonding_progress REAL,
    migrate_status   INTEGER,
    binance_score    REAL,

    -- 持有人
    holders      INTEGER,
    kyc_holders  INTEGER,

    -- 筹码分布
    top10_percent        REAL,
    dev_percent          REAL,
    sniper_percent       REAL,
    insider_percent      REAL,
    bundler_percent      REAL,
    new_wallet_percent   REAL,
    smart_money_percent  REAL,
    kol_percent          REAL,
    pro_percent          REAL,
    sniper_count         INTEGER,
    dev_sell_percent     REAL,

    -- 成交
    volume_5m        REAL,
    volume_1h        REAL,
    volume_4h        REAL,
    volume_24h       REAL,
    volume_1h_buy    REAL,
    volume_1h_sell   REAL,
    count_5m         INTEGER,
    count_1h         INTEGER,
    count_1h_buy     INTEGER,
    count_1h_sell    INTEGER,
    unique_trader_5m   INTEGER,
    unique_trader_1h   INTEGER,
    unique_trader_24h  INTEGER,
    pct_change_5m    REAL,
    pct_change_1h    REAL,
    pct_change_4h    REAL,
    pct_change_24h   REAL,
    -- meme_rush / meme_rank 的无时间窗聚合量（对新币等于自上线累计）
    volume_agg       REAL,
    count_agg        INTEGER,
    count_agg_buy    INTEGER,
    count_agg_sell   INTEGER,
    pct_change_agg   REAL,

    -- 聪明钱与资金流
    smart_money_count   INTEGER,
    smart_money_traders INTEGER,
    exit_rate           REAL,
    max_gain            REAL,
    alert_market_cap    REAL,
    net_inflow          REAL,
    signal_direction    TEXT,
    signal_type         TEXT,
    signal_status       TEXT,

    -- 社交
    social_hype       REAL,
    social_hype_cn    REAL,
    social_hype_en    REAL,
    kol_count         INTEGER,
    search_count_24h  INTEGER,
    sentiment         TEXT,
    twitter_followers INTEGER,

    -- 审计
    audit_available   INTEGER,
    audit_risk_level  INTEGER,
    buy_tax_pct       REAL,
    sell_tax_pct      REAL,
    contract_verified INTEGER,

    -- 派生特征与评分（存下来才能回答"当时算出的是什么"）
    features_json  TEXT,
    opportunity    REAL,
    confidence     REAL,
    data_quality   REAL,
    rug_risk       REAL,
    distribution   REAL,
    dq_json        TEXT,
    risk_flags_json TEXT,
    risk_parser_version TEXT,

    token_age_sec  INTEGER,
    state          TEXT,
    -- 降采样标记：清理任务据此抽稀，重要快照（警报现场）永不抽稀
    keep_forever   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snap_token_time ON snapshots(token_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_snap_observed   ON snapshots(observed_at);
CREATE INDEX IF NOT EXISTS idx_snap_cohort     ON snapshots(cohort_chain, cohort_age_bucket, cohort_mc_bucket);

-- ═══════════════════════════════════════════════════════════════════════
-- 市值里程碑：只记首次上穿（带滞回），不覆盖
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS milestones (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id          INTEGER NOT NULL,
    milestone_usd     REAL    NOT NULL,
    direction         TEXT    NOT NULL DEFAULT 'up',   -- up | down | reclaim
    sequence          INTEGER NOT NULL DEFAULT 1,
    is_first_upcross  INTEGER NOT NULL DEFAULT 1,
    occurred_at       INTEGER NOT NULL,
    token_age_sec     INTEGER,
    market_cap        REAL,
    price             REAL,
    liquidity         REAL,
    holders           INTEGER,
    data_quality      REAL,
    mc_source         TEXT,
    snapshot_id       INTEGER,
    state             TEXT,
    UNIQUE(token_id, milestone_usd, direction, sequence)
);
CREATE INDEX IF NOT EXISTS idx_milestone_token ON milestones(token_id, milestone_usd);
CREATE INDEX IF NOT EXISTS idx_milestone_time  ON milestones(occurred_at DESC);

-- ═══════════════════════════════════════════════════════════════════════
-- 警报：不可变。review_state 是唯一可变列（用户工作流，与机器状态严格分离）
--   is_near_miss=1 表示"差一点就报警"，落库不发信，供阈值反事实研究
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS alerts (
    alert_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id       INTEGER NOT NULL,
    alert_kind     TEXT    NOT NULL,   -- S0 | S1 | S2 | DISTRIBUTION
    is_near_miss   INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    correlation_id TEXT,
    snapshot_id    INTEGER,

    market_cap     REAL,
    price          REAL,
    liquidity      REAL,
    holders        INTEGER,
    token_age_sec  INTEGER,

    opportunity    REAL,
    confidence     REAL,
    data_quality   REAL,
    rug_risk       REAL,
    distribution   REAL,

    factors_json   TEXT,   -- 各因子得分明细（正向/负向）
    trigger_json   TEXT,   -- 触发规则、实际值、阈值
    prev_scores_json TEXT, -- 上一周期评分，用于"为什么突然报警"

    strategy_version TEXT,
    feature_version  TEXT,
    parser_version   TEXT,
    config_hash      TEXT,
    code_commit      TEXT,

    review_state   TEXT    NOT NULL DEFAULT 'NEW',  -- NEW|REVIEWED|TRACKING|CLOSED
    reviewed_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alert_token   ON alerts(token_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_created ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_kind    ON alerts(alert_kind, is_near_miss, created_at DESC);

-- ═══════════════════════════════════════════════════════════════════════
-- 拒绝记录：单独建表，因为"错杀了哪些未来赢家"是核心研究问题
--   必须保存 规则/实际值/阈值/当时年龄市值，而不是只存 reason 字符串
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS rejections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id       INTEGER NOT NULL,
    occurred_at    INTEGER NOT NULL,
    gate           TEXT    NOT NULL,  -- execution_blocker | research_gate
    rule           TEXT    NOT NULL,  -- top10_max | dev_max | honeypot | ...
    actual_value   REAL,
    threshold_value REAL,
    actual_text    TEXT,
    token_age_sec  INTEGER,
    market_cap     REAL,
    holders        INTEGER,
    liquidity      REAL,
    data_quality   REAL,
    strategy_version TEXT,
    config_hash    TEXT,
    snapshot_id    INTEGER,
    correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_reject_token ON rejections(token_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_reject_rule  ON rejections(rule, occurred_at DESC);

-- ═══════════════════════════════════════════════════════════════════════
-- Outcome：整个数据库最值钱的一张表
--   多窗口 MFE/MAE 由追踪器增量维护，不依赖事后从降采样快照重算
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS outcomes (
    alert_id            INTEGER PRIMARY KEY,
    token_id            INTEGER NOT NULL,
    signal_at           INTEGER NOT NULL,
    signal_price        REAL,
    signal_market_cap   REAL,
    signal_liquidity    REAL,

    -- 延迟入场：报警时价格不等于真实可成交价
    entry_15s   REAL,
    entry_30s   REAL,
    entry_60s   REAL,
    entry_120s  REAL,

    -- 三种 ATH：屏幕最高 / 可持续 / 流动性调整估计
    raw_ath_price        REAL,
    raw_ath_mc           REAL,
    raw_ath_at           INTEGER,
    sustained_ath_price  REAL,
    sustained_ath_mc     REAL,
    sustained_ath_at     INTEGER,
    liq_adjusted_multiple REAL,

    min_price     REAL,
    min_price_at  INTEGER,

    horizons_json TEXT,   -- {"1h":{"mfe_pct":..,"mae_pct":..,"matured":true}, ...}

    time_to_2x_sec   INTEGER,
    time_to_5x_sec   INTEGER,
    time_to_10x_sec  INTEGER,

    peak_multiple    REAL,
    current_multiple REAL,
    mfe_pct          REAL,
    mae_pct          REAL,
    outcome_label    TEXT,

    trending_seen_at INTEGER,
    lead_time_sec    INTEGER,

    last_updated  INTEGER,
    is_final      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outcome_token ON outcomes(token_id);
CREATE INDEX IF NOT EXISTS idx_outcome_label ON outcomes(outcome_label);

-- ═══════════════════════════════════════════════════════════════════════
-- 纸面交易（Shadow）：不自动下单，但按真实规则模拟三档仓位
--   否则只知道"报警后涨了多少"，不知道"按我们的规则到底赚多少"
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS paper_positions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id               INTEGER NOT NULL,
    token_id               INTEGER NOT NULL,
    size_usd               REAL    NOT NULL,
    opened_at              INTEGER NOT NULL,
    entry_price            REAL,
    entry_price_source     TEXT,
    est_slippage_pct       REAL,
    effective_entry_price  REAL,
    peak_value_usd         REAL,
    current_value_usd      REAL,
    closed_at              INTEGER,
    exit_price             REAL,
    realized_multiple      REAL,
    status                 TEXT    NOT NULL DEFAULT 'open',
    last_updated           INTEGER,
    UNIQUE(alert_id, size_usd)
);
CREATE INDEX IF NOT EXISTS idx_paper_alert ON paper_positions(alert_id);

-- ═══════════════════════════════════════════════════════════════════════
-- 事件与决策审计
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS radar_events (
    event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at      INTEGER NOT NULL,
    event_type       TEXT    NOT NULL,
    category         TEXT    NOT NULL,
    severity         TEXT    NOT NULL,
    importance       TEXT    NOT NULL,
    module           TEXT    NOT NULL,
    chain_id         TEXT,
    token_id         INTEGER,
    contract_address TEXT,
    symbol           TEXT,
    correlation_id   TEXT,
    old_state        TEXT,
    new_state        TEXT,
    snapshot_id      INTEGER,
    alert_id         INTEGER,
    duration_ms      INTEGER,
    strategy_version TEXT,
    feature_version  TEXT,
    config_hash      TEXT,
    summary          TEXT,
    payload_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_time  ON radar_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_token ON radar_events(token_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_type  ON radar_events(event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_corr  ON radar_events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_event_sev   ON radar_events(severity, occurred_at DESC);

-- ═══════════════════════════════════════════════════════════════════════
-- 原始响应归档：列表类短留存，详情类对重要币长期保留
--   三个月后发现某字段很重要时，还能重新解析历史
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS raw_archive (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at      INTEGER NOT NULL,
    endpoint        TEXT    NOT NULL,
    chain_id        TEXT,
    token_id        INTEGER,
    kind            TEXT    NOT NULL,   -- list | detail | audit | signal | social
    http_status     INTEGER,
    latency_ms      INTEGER,
    response_hash   TEXT,
    item_count      INTEGER,
    payload_gz      BLOB,
    retention_class TEXT    NOT NULL DEFAULT 'short',
    expires_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_raw_expires ON raw_archive(expires_at);
CREATE INDEX IF NOT EXISTS idx_raw_token   ON raw_archive(token_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_time    ON raw_archive(fetched_at DESC);

-- ═══════════════════════════════════════════════════════════════════════
-- 邮件 outbox：幂等键防重启后重复发信
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS email_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT    NOT NULL UNIQUE,
    kind            TEXT    NOT NULL,
    subject         TEXT    NOT NULL,
    html            TEXT    NOT NULL,
    token_id        INTEGER,
    alert_id        INTEGER,
    status          TEXT    NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    next_retry_at   INTEGER,
    created_at      INTEGER NOT NULL,
    sent_at         INTEGER,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON email_outbox(status, next_retry_at);

-- ═══════════════════════════════════════════════════════════════════════
-- 市场环境：V1 只记录不参与评分，为 V2 的动态阈值校准积累数据
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS market_regime (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at           INTEGER NOT NULL,
    chain_id              TEXT,
    new_token_count       INTEGER,
    total_volume_usd      REAL,
    total_net_inflow_usd  REAL,
    median_pct_change_1h  REAL,
    trending_activity     REAL,
    regime_label          TEXT,
    payload_json          TEXT
);
CREATE INDEX IF NOT EXISTS idx_regime_time ON market_regime(recorded_at DESC);

-- ═══════════════════════════════════════════════════════════════════════
-- 配置审计：旧策略永不覆盖，否则日后无法复盘"当时为什么给 83 分"
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS config_audit (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at      INTEGER NOT NULL,
    config_hash      TEXT    NOT NULL,
    prev_config_hash TEXT,
    strategy_version TEXT,
    feature_version  TEXT,
    parser_version   TEXT,
    code_commit      TEXT,
    changes_json     TEXT,
    operator         TEXT,
    config_snapshot  TEXT
);
CREATE INDEX IF NOT EXISTS idx_config_audit_time ON config_audit(recorded_at DESC);

-- ═══════════════════════════════════════════════════════════════════════
-- 每日 KPI：按成熟队列统计，避免把未到期样本算成失败（右删失）
-- ═══════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS kpi_daily (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    stat_date        TEXT    NOT NULL,
    strategy_version TEXT    NOT NULL,
    alert_kind       TEXT    NOT NULL,
    horizon          TEXT    NOT NULL,
    matured_count    INTEGER NOT NULL DEFAULT 0,
    payload_json     TEXT,
    created_at       INTEGER NOT NULL,
    UNIQUE(stat_date, strategy_version, alert_kind, horizon)
);
CREATE INDEX IF NOT EXISTS idx_kpi_date ON kpi_daily(stat_date DESC);
"""


PRAGMA_SQL = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    # 单进程内存受限（512MB），page cache 不宜过大
    "PRAGMA cache_size=-8000",
    "PRAGMA foreign_keys=OFF",
)
