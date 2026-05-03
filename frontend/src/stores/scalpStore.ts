/**
 * 短线信号 zustand store
 *
 * 职责：
 *   - 服务端资源本地缓存（active 信号 / 历史 / 配置 / 统计 / calibration）
 *   - WS 推送的 created/settled/cancelled 事件实时更新
 *   - REST 写操作（patch config / cancel signal）
 *
 * 历史策略：
 *   - active 列表全量刷新（数据量小，<10 条）
 *   - history 倒序分页（首屏 200 条 + WS 增量插入头部）
 */

import { create } from "zustand";

import {
  cancelSignal as apiCancelSignal,
  getCalibration as apiGetCalibration,
  getConfig as apiGetConfig,
  getStats as apiGetStats,
  listActiveSignals as apiListActive,
  listHistorySignals as apiListHistory,
  patchConfig as apiPatchConfig,
} from "@/lib/scalpApi";
import type {
  CalibrationCurve,
  GlobalStats,
  HistoryFilter,
  ScalpConfig,
  ScalpConfigPatch,
  ScalpSignal,
} from "@/lib/scalpTypes";

const HISTORY_CAP = 500;

interface ScalpState {
  // 集合数据
  active: ScalpSignal[];
  history: ScalpSignal[];
  config: ScalpConfig | null;
  stats: GlobalStats | null;
  calibration: CalibrationCurve | null;

  // 加载/错误状态
  activeLoading: boolean;
  historyLoading: boolean;
  configLoading: boolean;
  statsLoading: boolean;
  calibrationLoading: boolean;
  error: string | null;

  // WS 连接状态（hook 写入）
  wsConnected: boolean;

  // ── Loaders ──
  loadActive: () => Promise<void>;
  loadHistory: (filter?: HistoryFilter) => Promise<void>;
  loadConfig: () => Promise<void>;
  loadStats: (recompute?: boolean) => Promise<void>;
  loadCalibration: (recompute?: boolean) => Promise<void>;
  refreshAll: () => Promise<void>;

  // ── Writers ──
  patchConfig: (patch: ScalpConfigPatch) => Promise<ScalpConfig>;
  cancelSignal: (signalId: string, reason?: string) => Promise<void>;

  // ── WS apply ──
  applyCreated: (signal: ScalpSignal) => void;
  applySettled: (signal: ScalpSignal) => void;
  applyCancelled: (signal: ScalpSignal) => void;
  setWsConnected: (connected: boolean) => void;

  // ── Errors ──
  setError: (msg: string | null) => void;
}

export const useScalpStore = create<ScalpState>((set, get) => ({
  active: [],
  history: [],
  config: null,
  stats: null,
  calibration: null,

  activeLoading: false,
  historyLoading: false,
  configLoading: false,
  statsLoading: false,
  calibrationLoading: false,
  error: null,
  wsConnected: false,

  // ───────── Loaders ─────────

  async loadActive() {
    if (get().activeLoading) return;
    set({ activeLoading: true });
    try {
      const resp = await apiListActive();
      set({ active: resp.signals, activeLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, activeLoading: false });
    }
  },

  async loadHistory(filter = { limit: 200 }) {
    if (get().historyLoading) return;
    set({ historyLoading: true });
    try {
      const resp = await apiListHistory(filter);
      set({ history: resp.signals.slice(0, HISTORY_CAP), historyLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, historyLoading: false });
    }
  },

  async loadConfig() {
    if (get().configLoading) return;
    set({ configLoading: true });
    try {
      const cfg = await apiGetConfig();
      set({ config: cfg, configLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, configLoading: false });
    }
  },

  async loadStats(recompute = false) {
    if (get().statsLoading) return;
    set({ statsLoading: true });
    try {
      const stats = await apiGetStats(recompute);
      set({ stats, statsLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, statsLoading: false });
    }
  },

  async loadCalibration(recompute = false) {
    if (get().calibrationLoading) return;
    set({ calibrationLoading: true });
    try {
      const curve = await apiGetCalibration(recompute);
      set({ calibration: curve, calibrationLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, calibrationLoading: false });
    }
  },

  async refreshAll() {
    await Promise.all([
      get().loadActive(),
      get().loadHistory({ limit: 200 }),
      get().loadConfig(),
      get().loadStats(),
      get().loadCalibration(),
    ]);
  },

  // ───────── Writers ─────────

  async patchConfig(patch) {
    try {
      const merged = await apiPatchConfig(patch);
      set({ config: merged });
      return merged;
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  async cancelSignal(signalId, reason = "manual cancel") {
    try {
      const resp = await apiCancelSignal(signalId, reason);
      // 从 active 移除 + 加入 history 头部
      set((prev) => ({
        active: prev.active.filter((s) => s.signal_id !== signalId),
        history: dedupePrepend(prev.history, resp.signal),
      }));
    } catch (e) {
      set({ error: (e as Error).message });
      throw e;
    }
  },

  // ───────── WS apply ─────────

  applyCreated(signal) {
    set((prev) => {
      // 已在 active 中（重复推送）→ 替换
      const filtered = prev.active.filter((s) => s.signal_id !== signal.signal_id);
      // 按 created_at 倒序插入（最新在头部）
      const next = [signal, ...filtered].sort((a, b) => b.created_at - a.created_at);
      return { active: next };
    });
  },

  applySettled(signal) {
    set((prev) => ({
      // 从 active 移除
      active: prev.active.filter((s) => s.signal_id !== signal.signal_id),
      // 插入 history 头部（去重）
      history: dedupePrepend(prev.history, signal),
    }));
    // 结算后让 stats / calibration 自动失效（下次拉取重算）
    // 不主动重拉以避免过度请求；用户可手动点击 refresh
  },

  applyCancelled(signal) {
    set((prev) => ({
      active: prev.active.filter((s) => s.signal_id !== signal.signal_id),
      history: dedupePrepend(prev.history, signal),
    }));
  },

  setWsConnected(connected) {
    set({ wsConnected: connected });
  },

  setError(msg) {
    set({ error: msg });
  },
}));

function dedupePrepend(history: ScalpSignal[], signal: ScalpSignal): ScalpSignal[] {
  const filtered = history.filter((s) => s.signal_id !== signal.signal_id);
  return [signal, ...filtered].slice(0, HISTORY_CAP);
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Selectors
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const selectActiveByStrategy =
  (strategyName: string) =>
  (s: ScalpState): ScalpSignal[] =>
    s.active.filter((sig) => sig.strategy === strategyName);

export const selectHistoryByOutcome =
  (outcome: "won" | "lost" | "push") =>
  (s: ScalpState): ScalpSignal[] =>
    s.history.filter((sig) => sig.outcome === outcome);

export const selectEnabledStrategyCount = (s: ScalpState): number => {
  if (!s.config) return 0;
  return Object.values(s.config.strategies).filter((sc) => sc.enabled).length;
};
