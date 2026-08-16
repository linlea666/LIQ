/**
 * 潜力币雷达 · REST 客户端
 *
 * 与 rollApi/scalpApi 同构，但多一层考虑：雷达是**独立容器**，
 * 它可能在主后端健康时单独挂掉。因此这里不吞错——
 * 一个静默失败的雷达页面会显示上一次的数据，
 * 让人以为"最近没有新币"，而实际上是采集早就停了。
 */

import { RADAR_API_BASE } from "./constants";
import type {
  AdminConfigResponse,
  AdminSaveResponse,
  RadarAlert,
  RadarDiagnostics,
  RadarEvent,
  RadarHealth,
  RadarKpi,
  RadarKpiSummary,
  RadarOutcome,
  RadarRejection,
  RadarTokenDetail,
  RadarTokenListResponse,
} from "./radarTypes";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${RADAR_API_BASE}/api/radar${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail: string;
    try {
      const body = await res.json();
      detail = typeof body?.detail === "string" ? body.detail : JSON.stringify(body);
    } catch {
      detail = `HTTP ${res.status}`;
    }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

type QueryValue = string | number | boolean | undefined | null;

function qs(params: Record<string, QueryValue> | object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params) as [string, QueryValue][]) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

// ── 健康与配置 ────────────────────────────────────────────────────────────

export const getHealth = () => request<RadarHealth>("/health");
export const getDiagnostics = () => request<RadarDiagnostics>("/diagnostics");
export const getConfig = () => request<Record<string, unknown>>("/config");
export const getSchedulerSnapshot = () => request<Record<string, unknown>>("/scheduler");
export const getDiagnosticsBundle = () =>
  request<Record<string, unknown>>("/diagnostics/bundle");

// ── 扫描器 ────────────────────────────────────────────────────────────────

export interface TokenQuery {
  state?: string;
  chain_id?: string;
  min_opportunity?: number;
  sort?: "opportunity" | "market_cap" | "age" | "holders";
  limit?: number;
}

export const listTokens = (query: TokenQuery = {}) =>
  request<RadarTokenListResponse>(`/tokens${qs(query)}`);

export const getTokenDetail = (chainId: string, address: string) =>
  request<RadarTokenDetail>(
    `/tokens/${encodeURIComponent(chainId)}/${encodeURIComponent(address)}`,
  );

// ── 警报 ──────────────────────────────────────────────────────────────────

export const listAlerts = (query: {
  kind?: string;
  include_near_miss?: boolean;
  since_hours?: number;
  limit?: number;
} = {}) => request<{ total: number; items: RadarAlert[] }>(`/alerts${qs(query)}`);

export const getAlertDetail = (alertId: number) =>
  request<{
    alert: RadarAlert;
    decision_snapshot: Record<string, unknown> | null;
    outcome: RadarOutcome | null;
    paper_positions: Array<Record<string, unknown>>;
  }>(`/alerts/${alertId}`);

export const reviewAlert = (alertId: number, state: string) =>
  request<{ alert_id: number; review_state: string }>(
    `/alerts/${alertId}/review${qs({ state })}`,
    { method: "POST" },
  );

// ── 研究 ──────────────────────────────────────────────────────────────────

export const listRejections = (query: {
  rule?: string;
  since_hours?: number;
  limit?: number;
} = {}) =>
  request<{
    total: number;
    items: RadarRejection[];
    by_rule: Array<{ rule: string; gate: string; n: number }>;
  }>(`/research/rejections${qs(query)}`);

export const listNearMiss = (query: { since_hours?: number; limit?: number } = {}) =>
  request<{ total: number; items: RadarAlert[] }>(`/research/near-miss${qs(query)}`);

export const listKpi = (query: { days?: number; horizon?: string } = {}) =>
  request<{ total: number; items: RadarKpi[] }>(`/research/kpi${qs(query)}`);

export const getKpiSummary = (days = 7) =>
  request<RadarKpiSummary>(`/research/kpi/summary${qs({ days })}`);

export const rebuildKpi = () =>
  request<{ groups: number }>("/research/kpi/rebuild", { method: "POST" });

// ── 运维 ──────────────────────────────────────────────────────────────────

export const listEvents = (query: {
  category?: string;
  severity?: string;
  since_hours?: number;
  limit?: number;
} = {}) => request<{ total: number; items: RadarEvent[] }>(`/events${qs(query)}`);

export const exportUrl = (dataset: string, sinceHours = 168) =>
  `${RADAR_API_BASE}/api/radar/export/${dataset}${qs({ since_hours: sinceHours })}`;

// ── 管理接口：运行时配置 ──────────────────────────────────────────────────
// 令牌来自 radar/.env 的 RADAR_ADMIN_TOKEN，由用户在配置页输入后存
// localStorage，随请求头发送。绝不能进 NEXT_PUBLIC 构建变量——
// 那会把令牌烧进对外公开的 JS 文件。

const adminHeaders = (token: string) => ({ "X-Radar-Admin-Token": token });

export const getAdminConfig = (token: string) =>
  request<AdminConfigResponse>("/admin/config", { headers: adminHeaders(token) });

export const saveAdminConfig = (
  token: string,
  changes: Record<string, unknown>,
  remove: string[],
) =>
  request<AdminSaveResponse>("/admin/config", {
    method: "PUT",
    headers: adminHeaders(token),
    body: JSON.stringify({ changes, remove }),
  });

export const requestAdminRestart = (token: string) =>
  request<{ restarting: boolean; expected_downtime_sec: number }>(
    "/admin/restart",
    { method: "POST", headers: adminHeaders(token) },
  );
