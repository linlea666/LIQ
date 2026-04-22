/**
 * 滚仓模块 Zustand store
 *
 * 职责：
 *   - 服务端资源的本地缓存（positions/templates/settings/enums）
 *   - 引擎 WS 推送的 signals 按 position_id 存取
 *   - 加载/错误状态统一管理，UI 组件消费 selector
 *
 * 策略：
 *   - 所有 async action 都先拉后 setState，失败时写入 error 字段
 *   - Signals 以 position_id 为 key，便于卡片懒读
 */

import { create } from "zustand";

import {
  createPosition as apiCreatePosition,
  deletePosition as apiDeletePosition,
  deleteTemplate as apiDeleteTemplate,
  deriveTemplate as apiDeriveTemplate,
  executeEvent as apiExecuteEvent,
  getEnums as apiGetEnums,
  getLatestSignal as apiGetLatestSignal,
  getPosition as apiGetPosition,
  getReplay as apiGetReplay,
  getSettings as apiGetSettings,
  listPositionEvents as apiListPositionEvents,
  listPositions as apiListPositions,
  listTemplates as apiListTemplates,
  overrideAdd as apiOverrideAdd,
  updateSettings as apiUpdateSettings,
  updateTemplate as apiUpdateTemplate,
} from "@/lib/rollApi";
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

interface RollState {
  // 集合数据
  positions: UserPosition[];
  plansById: Record<string, RollPlan>;
  templates: RollTemplate[];
  settings: RollGlobalSettings | null;
  enums: RollEnumsPayload | null;

  // 实时数据
  signalsByPosition: Record<string, RollSignal>;
  eventsByPosition: Record<string, RollEvent[]>;

  // 复盘缓存（按 position_id）
  replaysByPosition: Record<string, ReplayResp>;

  // 已平仓列表（独立缓存，避免和 active 列表互相覆盖）
  closedPositions: UserPosition[];

  // 状态
  positionsLoading: boolean;
  closedPositionsLoading: boolean;
  templatesLoading: boolean;
  settingsLoading: boolean;
  enumsLoading: boolean;
  replayLoading: Record<string, boolean>;
  error: string | null;

  // 初始化
  loadEnums: () => Promise<void>;
  loadSettings: () => Promise<void>;
  loadTemplates: () => Promise<void>;
  loadPositions: (status?: "active" | "closed" | "all") => Promise<void>;
  loadClosedPositions: () => Promise<void>;
  refreshPosition: (id: string) => Promise<void>;
  refreshPositionEvents: (id: string, limit?: number) => Promise<void>;
  refreshPositionSignal: (id: string) => Promise<void>;
  refreshAll: () => Promise<void>;

  // 复盘
  loadReplay: (id: string, force?: boolean) => Promise<ReplayResp>;

  // 写操作
  createPosition: (req: CreatePositionReq) => Promise<{
    position: UserPosition;
    plan: RollPlan;
  }>;
  executeEvent: (id: string, req: ExecuteEventReq) => Promise<UserPosition>;
  overrideAdd: (id: string, req: OverrideAddReq) => Promise<UserPosition>;
  deletePosition: (id: string) => Promise<void>;

  // 模板
  deriveTemplate: (req: DeriveTemplateReq) => Promise<RollTemplate>;
  updateTemplate: (id: string, req: UpdateTemplateReq) => Promise<RollTemplate>;
  deleteTemplate: (id: string) => Promise<void>;

  // 设置
  updateSettings: (patch: Partial<RollGlobalSettings>) => Promise<RollGlobalSettings>;

  // WS 推送接收（由 useRollWebSocket 调用）
  applySignal: (signal: RollSignal) => void;
  applyEvent: (positionId: string, event: RollEvent) => void;

  // 错误
  setError: (msg: string | null) => void;
}

function absorb(items: PositionWithPlanResp[]): {
  positions: UserPosition[];
  plansById: Record<string, RollPlan>;
  signalsByPosition: Record<string, RollSignal>;
} {
  const positions: UserPosition[] = [];
  const plansById: Record<string, RollPlan> = {};
  const signalsByPosition: Record<string, RollSignal> = {};
  for (const item of items) {
    if (!item.position) continue;
    positions.push(item.position);
    if (item.plan) plansById[item.plan.id] = item.plan;
    if (item.latest_signal) {
      signalsByPosition[item.position.id] = item.latest_signal;
    }
  }
  return { positions, plansById, signalsByPosition };
}

export const useRollStore = create<RollState>((set, get) => ({
  positions: [],
  plansById: {},
  templates: [],
  settings: null,
  enums: null,

  signalsByPosition: {},
  eventsByPosition: {},
  replaysByPosition: {},
  closedPositions: [],

  positionsLoading: false,
  closedPositionsLoading: false,
  templatesLoading: false,
  settingsLoading: false,
  enumsLoading: false,
  replayLoading: {},
  error: null,

  // ── Loaders ────────────────────────────────────────

  async loadEnums() {
    if (get().enumsLoading) return;
    set({ enumsLoading: true });
    try {
      const enums = await apiGetEnums();
      set({ enums, enumsLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, enumsLoading: false });
    }
  },

  async loadSettings() {
    if (get().settingsLoading) return;
    set({ settingsLoading: true });
    try {
      const settings = await apiGetSettings();
      set({ settings, settingsLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, settingsLoading: false });
    }
  },

  async loadTemplates() {
    if (get().templatesLoading) return;
    set({ templatesLoading: true });
    try {
      const { items } = await apiListTemplates();
      set({ templates: items, templatesLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, templatesLoading: false });
    }
  },

  async loadPositions(status = "active") {
    if (get().positionsLoading) return;
    set({ positionsLoading: true });
    try {
      const { items } = await apiListPositions(status);
      const bucket = absorb(items);
      set((prev) => ({
        positions: bucket.positions,
        plansById: { ...prev.plansById, ...bucket.plansById },
        signalsByPosition: { ...prev.signalsByPosition, ...bucket.signalsByPosition },
        positionsLoading: false,
      }));
    } catch (e) {
      set({ error: (e as Error).message, positionsLoading: false });
    }
  },

  async loadClosedPositions() {
    if (get().closedPositionsLoading) return;
    set({ closedPositionsLoading: true });
    try {
      const { items } = await apiListPositions("closed");
      const positions = items
        .map((it) => it.position)
        .filter((p): p is UserPosition => !!p)
        .sort((a, b) => (b.closed_at ?? 0) - (a.closed_at ?? 0));
      const plansById: Record<string, RollPlan> = {};
      for (const it of items) if (it.plan) plansById[it.plan.id] = it.plan;
      set((prev) => ({
        closedPositions: positions,
        plansById: { ...prev.plansById, ...plansById },
        closedPositionsLoading: false,
      }));
    } catch (e) {
      set({ error: (e as Error).message, closedPositionsLoading: false });
    }
  },

  async loadReplay(id, force = false) {
    const cached = get().replaysByPosition[id];
    if (cached && !force) return cached;
    set((prev) => ({
      replayLoading: { ...prev.replayLoading, [id]: true },
    }));
    try {
      const replay = await apiGetReplay(id);
      set((prev) => ({
        replaysByPosition: { ...prev.replaysByPosition, [id]: replay },
        replayLoading: { ...prev.replayLoading, [id]: false },
        // 同步把 plan 缓存进来，方便详情页直接读
        plansById: replay.plan
          ? { ...prev.plansById, [replay.plan.id]: replay.plan }
          : prev.plansById,
      }));
      return replay;
    } catch (e) {
      set((prev) => ({
        error: (e as Error).message,
        replayLoading: { ...prev.replayLoading, [id]: false },
      }));
      throw e;
    }
  },

  async refreshPosition(id) {
    try {
      const res = await apiGetPosition(id);
      const patch = absorb([res]);
      set((prev) => {
        const others = prev.positions.filter((p) => p.id !== id);
        return {
          positions: [...patch.positions, ...others],
          plansById: { ...prev.plansById, ...patch.plansById },
          signalsByPosition: { ...prev.signalsByPosition, ...patch.signalsByPosition },
        };
      });
    } catch (e) {
      set({ error: (e as Error).message });
    }
  },

  async refreshPositionEvents(id, limit = 500) {
    try {
      const { events } = await apiListPositionEvents(id, limit);
      set((prev) => ({
        eventsByPosition: { ...prev.eventsByPosition, [id]: events },
      }));
    } catch (e) {
      set({ error: (e as Error).message });
    }
  },

  async refreshPositionSignal(id) {
    try {
      const signal = await apiGetLatestSignal(id);
      set((prev) => ({
        signalsByPosition: { ...prev.signalsByPosition, [id]: signal },
      }));
    } catch {
      // 404 表示还没评估过，silent
    }
  },

  async refreshAll() {
    await Promise.all([
      get().loadEnums(),
      get().loadSettings(),
      get().loadTemplates(),
      get().loadPositions("active"),
    ]);
  },

  // ── Writers ────────────────────────────────────────

  async createPosition(req) {
    try {
      const res = await apiCreatePosition(req);
      set((prev) => ({
        positions: [res.position, ...prev.positions.filter((p) => p.id !== res.position.id)],
        plansById: { ...prev.plansById, [res.plan.id]: res.plan },
      }));
      return res;
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  async executeEvent(id, req) {
    try {
      const { position, plan } = await apiExecuteEvent(id, req);
      set((prev) => {
        const nextPositions = prev.positions.map((p) => (p.id === id ? position : p));
        const nextPlans = plan ? { ...prev.plansById, [plan.id]: plan } : prev.plansById;
        // close 事件：从 active 列表删除（仍可通过 loadPositions('closed') 查）
        const filtered = position.status === "closed"
          ? nextPositions.filter((p) => p.id !== id)
          : nextPositions;
        return {
          positions: filtered,
          plansById: nextPlans,
        };
      });
      await get().refreshPositionEvents(id);
      return position;
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  async overrideAdd(id, req) {
    try {
      const { position } = await apiOverrideAdd(id, req);
      set((prev) => ({
        positions: prev.positions.map((p) => (p.id === id ? position : p)),
      }));
      await get().refreshPositionEvents(id);
      return position;
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  async deletePosition(id) {
    try {
      await apiDeletePosition(id);
      set((prev) => {
        const { [id]: _sig, ...restSignals } = prev.signalsByPosition;
        const { [id]: _evs, ...restEvents } = prev.eventsByPosition;
        void _sig;
        void _evs;
        return {
          positions: prev.positions.filter((p) => p.id !== id),
          signalsByPosition: restSignals,
          eventsByPosition: restEvents,
        };
      });
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  async deriveTemplate(req) {
    try {
      const tpl = await apiDeriveTemplate(req);
      set((prev) => ({ templates: [...prev.templates, tpl] }));
      return tpl;
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  async updateTemplate(id, req) {
    try {
      const tpl = await apiUpdateTemplate(id, req);
      set((prev) => ({
        templates: prev.templates.map((t) => (t.id === id ? tpl : t)),
      }));
      return tpl;
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  async deleteTemplate(id) {
    try {
      await apiDeleteTemplate(id);
      set((prev) => ({
        templates: prev.templates.filter((t) => t.id !== id),
      }));
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  async updateSettings(patch) {
    try {
      const merged = await apiUpdateSettings(patch);
      set({ settings: merged });
      return merged;
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  // ── WS 推送接收 ────────────────────────────────────

  applySignal(signal) {
    set((prev) => ({
      signalsByPosition: {
        ...prev.signalsByPosition,
        [signal.position_id]: signal,
      },
    }));
  },

  applyEvent(positionId, event) {
    set((prev) => {
      const existing = prev.eventsByPosition[positionId] || [];
      return {
        eventsByPosition: {
          ...prev.eventsByPosition,
          [positionId]: [...existing, event],
        },
      };
    });
  },

  setError(msg) {
    set({ error: msg });
  },
}));

// ── 选择器（selector helpers） ───────────────────────

export const selectActivePositions = (s: RollState): UserPosition[] =>
  s.positions.filter((p) => p.status === "active");

export const selectPlanForPosition =
  (pos: UserPosition) =>
  (s: RollState): RollPlan | undefined =>
    s.plansById[pos.plan_id];

export const selectSignalForPosition =
  (positionId: string) =>
  (s: RollState): RollSignal | undefined =>
    s.signalsByPosition[positionId];
