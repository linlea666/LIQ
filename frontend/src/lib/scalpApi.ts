/**
 * 短线信号 REST 客户端 · 对 /api/scalp/* 的轻量封装
 *
 * 设计：
 *   - 每个方法返回 Promise<T>，失败时 throw Error（带后端 detail）
 *   - 不做缓存，由 zustand store 负责
 */

import { API_BASE } from "@/lib/constants";
import type {
  CalibrationCurve,
  CancelSignalResp,
  GlobalStats,
  HistoryFilter,
  ListSignalsResp,
  ScalpConfig,
  ScalpConfigPatch,
  ScalpSignal,
} from "@/lib/scalpTypes";

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
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

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Signals
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function listActiveSignals(): Promise<ListSignalsResp> {
  return http<ListSignalsResp>("/api/scalp/signals/active");
}

export function listHistorySignals(filter: HistoryFilter = {}): Promise<ListSignalsResp> {
  const qs = new URLSearchParams();
  if (filter.limit !== undefined) qs.set("limit", String(filter.limit));
  if (filter.strategy) qs.set("strategy", filter.strategy);
  if (filter.coin) qs.set("coin", filter.coin);
  if (filter.horizon_min !== undefined) qs.set("horizon_min", String(filter.horizon_min));
  if (filter.since_ts !== undefined) qs.set("since_ts", String(filter.since_ts));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return http<ListSignalsResp>(`/api/scalp/signals/history${suffix}`);
}

export function getSignalById(signalId: string): Promise<ScalpSignal> {
  return http<ScalpSignal>(`/api/scalp/signals/${encodeURIComponent(signalId)}`);
}

export function cancelSignal(signalId: string, reason = "manual cancel"): Promise<CancelSignalResp> {
  return http<CancelSignalResp>(`/api/scalp/signals/${encodeURIComponent(signalId)}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Config
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function getConfig(): Promise<ScalpConfig> {
  return http<ScalpConfig>("/api/scalp/config");
}

export function patchConfig(patch: ScalpConfigPatch): Promise<ScalpConfig> {
  return http<ScalpConfig>("/api/scalp/config", {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Stats / Calibration
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function getStats(recompute = false): Promise<GlobalStats> {
  const suffix = recompute ? "?recompute=true" : "";
  return http<GlobalStats>(`/api/scalp/stats${suffix}`);
}

export function getCalibration(recompute = false): Promise<CalibrationCurve> {
  const suffix = recompute ? "?recompute=true" : "";
  return http<CalibrationCurve>(`/api/scalp/calibration${suffix}`);
}
