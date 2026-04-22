/**
 * 滚仓模块 REST 客户端 —— 对 /api/roll/* 的轻量封装
 *
 * 设计：
 *   - 每个方法返回 Promise<T>，失败时 throw Error（带后端错误消息）
 *   - 不做缓存，由 Zustand store 负责
 *   - 所有 URL 都通过 API_BASE 常量拼接，便于生产部署切换 host
 */

import { API_BASE } from "@/lib/constants";
import type {
  CreatePositionReq,
  DeriveTemplateReq,
  ExecuteEventReq,
  OverrideAddReq,
  PositionWithPlanResp,
  ReplayResp,
  RollEnumsPayload,
  RollEvent,
  RollGlobalSettings,
  RollPlan,
  RollSignal,
  RollTemplate,
  UpdateTemplateReq,
  UserPosition,
} from "@/lib/rollTypes";

async function httpRequest<T>(path: string, init?: RequestInit): Promise<T> {
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
// 持仓
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface ListPositionsResp {
  items: PositionWithPlanResp[];
  count: number;
}

export function listPositions(
  status: "active" | "closed" | "all" = "active",
): Promise<ListPositionsResp> {
  return httpRequest<ListPositionsResp>(`/api/roll/positions?status=${status}`);
}

export function createPosition(req: CreatePositionReq): Promise<{
  position: UserPosition;
  plan: RollPlan;
}> {
  return httpRequest("/api/roll/positions", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function getPosition(id: string): Promise<PositionWithPlanResp> {
  return httpRequest<PositionWithPlanResp>(
    `/api/roll/positions/${encodeURIComponent(id)}`,
  );
}

export function deletePosition(id: string): Promise<{ ok: boolean; deleted: string }> {
  return httpRequest(`/api/roll/positions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function listPositionEvents(
  id: string,
  limit = 500,
): Promise<{ position_id: string; count: number; events: RollEvent[] }> {
  return httpRequest(
    `/api/roll/positions/${encodeURIComponent(id)}/events?limit=${limit}`,
  );
}

export function getLatestSignal(id: string): Promise<RollSignal> {
  return httpRequest<RollSignal>(
    `/api/roll/positions/${encodeURIComponent(id)}/signal`,
  );
}

export function executeEvent(
  id: string,
  req: ExecuteEventReq,
): Promise<{ position: UserPosition; plan: RollPlan | null }> {
  return httpRequest(
    `/api/roll/positions/${encodeURIComponent(id)}/execute`,
    { method: "POST", body: JSON.stringify(req) },
  );
}

export function getReplay(id: string): Promise<ReplayResp> {
  return httpRequest<ReplayResp>(
    `/api/roll/positions/${encodeURIComponent(id)}/replay`,
  );
}

export function overrideAdd(
  id: string,
  req: OverrideAddReq,
): Promise<{ position: UserPosition }> {
  return httpRequest(
    `/api/roll/positions/${encodeURIComponent(id)}/override`,
    { method: "POST", body: JSON.stringify(req) },
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 模板
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function listTemplates(): Promise<{ items: RollTemplate[]; count: number }> {
  return httpRequest("/api/roll/templates");
}

export function deriveTemplate(req: DeriveTemplateReq): Promise<RollTemplate> {
  return httpRequest("/api/roll/templates", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function updateTemplate(
  id: string,
  req: UpdateTemplateReq,
): Promise<RollTemplate> {
  return httpRequest(`/api/roll/templates/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(req),
  });
}

export function deleteTemplate(id: string): Promise<{ ok: boolean; deleted: string }> {
  return httpRequest(`/api/roll/templates/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 设置 + 枚举
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function getSettings(): Promise<RollGlobalSettings> {
  return httpRequest("/api/roll/settings");
}

export function updateSettings(
  patch: Partial<RollGlobalSettings>,
): Promise<RollGlobalSettings> {
  return httpRequest("/api/roll/settings", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

export function getEnums(): Promise<RollEnumsPayload> {
  return httpRequest("/api/roll/enums");
}
