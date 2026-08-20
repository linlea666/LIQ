import { expect, test } from "@playwright/test";

const now = 1_787_243_900;

function evidence(index: number) {
  return {
    evidence_id: `evidence-${index}`,
    coin: "BTC",
    pillar: index % 2 ? "spot_demand" : "liquidation_risk",
    causal_root: index % 2 ? "spot_demand" : "liquidation_pressure",
    name: `证据 ${index}`,
    direction: index % 3 ? "up" : "down",
    role: "scoring",
    strength: 0.8,
    raw_strength: 1.2,
    confidence: 0.8,
    event_time: now - index,
    observed_at: now,
    decision_time: now,
    watermark: now - index,
    source_id: "fixture",
    config_version: "cfg",
    calibration_version: "cal",
    values: { amount_usd: 1_000_000 * index },
    explanation: "用于验证长时间线滚动和证据说明不会造成横向溢出。",
  };
}

const qualities = Object.fromEntries(Array.from({ length: 18 }, (_, index) => [
  `source_${index}_with_a_deliberately_long_identifier`,
  {
    source_id: `source_${index}_with_a_deliberately_long_identifier`,
    availability: "available",
    freshness: "fresh",
    completeness: 1,
    continuity: "continuous",
    validity: "valid",
    as_of: now,
    observed_at: now,
    watermark: now,
    decision_usable: true,
    reasons: [],
  },
]));

const intelligence = {
  product_name: "LIQ BTC 开仓决策情报室",
  coin: "BTC",
  mode: "shadow",
  decision_time: now,
  live_observation: { decision_time: now, direction: "up", quality_layer: "normal", spot_confirmed: true, independent_root_count: 2, causal_roots: ["spot_demand", "liquidation_pressure"], summary: "实时证据偏上行观察。" },
  confirmed_incident: { stage: "watch", direction: "up", confirmed_at: now, stage_since: now, frozen: false, frozen_since: 0, frozen_age_sec: 0, incident_id: "incident", episode_id: "episode" },
  decision_support: {
    stance: "observe_long", strength_band: "medium", summary: "多源证据偏多，但仍需等待入场触发。",
    supporting_evidence: ["现货主动成交偏多"], opposing_evidence: ["下方存在清算磁铁"],
    supporting_details: [], opposing_details: [], blockers: [],
    invalidation_conditions: ["现货主动成交反向并持续一个闭合窗口"], execution_eligible: false,
  },
  factors: Array.from({ length: 18 }, (_, index) => ({ factor_id: `factor-${index}`, label: `普通异常因子 ${index}`, direction: "up", status: "normal", strength_band: "weak", decision_role: "informational", source_ids: ["fixture"], as_of: now, decision_usable: false, plain_summary: "仅展示，不参与评分。", values: {} })),
  context: {
    market_overview: { trend_horizons: Object.fromEntries(["1m", "5m", "15m", "1h", "4h", "1d"].map((period) => [period, { availability: "available", change_pct: 1.2, as_of: now, closed: true, direction: "up" }])) },
    etf: { availability: "unavailable", reason: "官方源降级" }, options: { availability: "unavailable" }, native_btc_onchain: { availability: "unavailable" }, stablecoin: { availability: "unavailable" }, institutional_futures: { availability: "unavailable" }, exchange_flows: { availability: "unavailable" }, institutional_entities: { availability: "unavailable" },
  },
  incident: {
    product_name: "LIQ BTC 联合风险预警系统", coin: "BTC", event_time: now, observed_at: now, decision_time: now, watermark: now,
    stage: "watch", quality_layer: "normal", direction: "up", live_direction: "up", incident_id: "incident", episode_id: "episode", stage_since: now,
    mode: "shadow", shadow_mode: true, stage_frozen: false, frozen_since: 0, last_confirmed_at: now, valid_for_calibration: true, pit_violations: [], research_signals: [], causal_roots: ["spot_demand"], live_causal_roots: ["spot_demand"], independent_root_count: 1, spot_confirmed: true,
    pillars: {}, evidence: Array.from({ length: 80 }, (_, index) => evidence(index)), source_quality: qualities, context: {}, transition_reason: "", config_version: "cfg", calibration_version: "cal", calibration_admitted: false, notification_eligible: false,
  },
};

const readiness = {
  ready_for_mode: "shadow", current_mode: "shadow", pit_violations_24h: 0,
  valid_for_calibration_24h: 100, snapshot_count_24h: 100, core_coverage_24h: 1,
  governed_shadow_age_sec: 3600, clean_epoch_started_at: now - 3600,
  last_epoch_reset_at: 0, last_epoch_reset_reason: "", hard_violations_14d: 0,
  governance_identity: "identity", rss_observation_age_sec: 3600, rss_p95_gib: 1.1,
  rss_slope_mib_per_hour: 0, raw_queue_dropped: 20, raw_dropped_in_epoch: 0,
  raw_store: { queue_size: 0, queue_max: 20_000, oldest_queue_age_sec: 0, projected_files_per_day: 900 }, blockers: ["修复后 shadow 连续时长不足 14 天"],
};

test.beforeEach(async ({ page }) => {
  await page.route("http://localhost:8000/api/market-risk/BTC/intelligence", (route) => route.fulfill({ json: intelligence }));
  await page.route("http://localhost:8000/api/market-risk/ready", (route) => route.fulfill({ json: readiness }));
});

test("risk room remains vertically scrollable without horizontal overflow", async ({ page }) => {
  await page.goto("/market-risk/BTC");
  await expect(page.getByRole("heading", { name: "LIQ BTC 开仓决策情报室" })).toBeVisible();
  const dimensions = await page.evaluate(() => ({
    bodyOverflowY: getComputedStyle(document.body).overflowY,
    scrollHeight: document.documentElement.scrollHeight,
    clientHeight: document.documentElement.clientHeight,
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(dimensions.bodyOverflowY).not.toBe("hidden");
  expect(dimensions.scrollHeight).toBeGreaterThan(dimensions.clientHeight);
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth);
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
  const header = page.getByTestId("market-risk-header");
  await expect(header).toBeVisible();
  expect(Math.abs((await header.boundingBox())?.y ?? 100)).toBeLessThanOrEqual(1);
});
