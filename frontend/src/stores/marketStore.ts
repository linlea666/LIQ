import { create } from "zustand";
import type {
  AIAnalysisResult,
  MAAEvalSummary,
  MarketActionReport,
  MarketUpdate,
  OrderbookPressureSignal,
  SourceHealth,
  TEAIInterpretation,
} from "@/lib/types";
import { API_BASE } from "@/lib/constants";
import type { CoinType } from "@/lib/constants";

interface MarketStore {
  coin: CoinType;
  setCoin: (coin: CoinType) => void;

  data: Record<string, MarketUpdate>;
  updateMarketData: (update: MarketUpdate) => void;

  aiResult: AIAnalysisResult | null;
  aiLoading: boolean;
  aiError: string | null;
  aiHistory: AIAnalysisResult[];
  aiAvailable: boolean;
  setAIResult: (result: AIAnalysisResult) => void;
  setAILoading: (loading: boolean) => void;
  setAIError: (error: string | null) => void;
  setAIAvailable: (available: boolean) => void;
  loadAIHistory: (coin: string) => Promise<void>;

  sourceHealth: SourceHealth[];
  setSourceHealth: (health: SourceHealth[]) => void;

  displayMode: "beginner" | "pro";
  setDisplayMode: (mode: "beginner" | "pro") => void;

  aiPanelOpen: boolean;
  setAIPanelOpen: (open: boolean) => void;

  activeTab: string;
  setActiveTab: (tab: string) => void;

  // TE · AI 深度解读（按 coin 分存；WS 推送驱动）
  teAiByCoin: Record<string, TEAIInterpretation>;
  teAiLoadingByCoin: Record<string, boolean>;
  teAiErrorByCoin: Record<string, string | null>;
  setTEAIResult: (result: TEAIInterpretation) => void;
  setTEAILoading: (coin: string, loading: boolean) => void;
  setTEAIError: (coin: string, err: string | null) => void;

  // Market Action Analyzer（按 coin 分存；WS market_action_report 驱动 + REST 首屏补拉）
  maaByCoin: Record<string, MarketActionReport>;
  maaHistoryByCoin: Record<string, MarketActionReport[]>;
  maaLoadingByCoin: Record<string, boolean>;
  maaErrorByCoin: Record<string, string | null>;
  setMAAReport: (report: MarketActionReport) => void;
  setMAALoading: (coin: string, loading: boolean) => void;
  setMAAError: (coin: string, err: string | null) => void;
  loadMAAReport: (coin: string) => Promise<void>;
  loadMAAHistory: (coin: string, limit?: number) => Promise<void>;
  triggerMAARun: (coin: string) => Promise<void>;

  // MAA 事后评估（Phase 5）· REST 按需拉取，缓存在 store 中
  maaEvalByCoin: Record<string, MAAEvalSummary>;
  maaEvalLoadingByCoin: Record<string, boolean>;
  loadMAAEval: (coin: string, opts?: { refresh?: boolean; window_days?: number }) => Promise<void>;

  // 挂单压力监测器 · 独立 snipe 信号（WS 推送 orderbook_pressure_signal 驱动）
  // 按 coin 分存最近 N 条，方便前端展示历史触发列表
  obPressureSignalsByCoin: Record<string, OrderbookPressureSignal[]>;
  pushOrderbookPressureSignal: (signal: OrderbookPressureSignal) => void;
}

export const useMarketStore = create<MarketStore>((set, get) => ({
  coin: "BTC",
  setCoin: (coin) => set({ coin, aiResult: null, aiError: null, aiLoading: false }),

  data: {},
  updateMarketData: (update) =>
    set((state) => ({
      data: {
        ...state.data,
        [update.coin]: { ...state.data[update.coin], ...update },
      },
    })),

  aiResult: null,
  aiLoading: false,
  aiError: null,
  aiHistory: [],
  aiAvailable: false,
  setAIResult: (result) =>
    set((state) => {
      const exists = state.aiHistory.some((h) => h.ts === result.ts);
      const history = exists
        ? state.aiHistory
        : [result, ...state.aiHistory].slice(0, 5);
      return { aiResult: result, aiLoading: false, aiError: null, aiHistory: history };
    }),
  setAILoading: (loading) => set({ aiLoading: loading, aiError: null }),
  setAIError: (error) => set({ aiError: error, aiLoading: false }),
  setAIAvailable: (available) => set({ aiAvailable: available }),
  loadAIHistory: async (coin) => {
    try {
      const resp = await fetch(`${API_BASE}/api/ai/history/${coin}?limit=5`);
      if (!resp.ok) return;
      const data = await resp.json();
      const analyses: AIAnalysisResult[] = data.analyses ?? [];
      if (analyses.length > 0) {
        set((state) => {
          const merged = [...analyses];
          for (const existing of state.aiHistory) {
            if (!merged.some((m) => m.ts === existing.ts)) {
              merged.push(existing);
            }
          }
          merged.sort((a, b) => b.ts - a.ts);
          return { aiHistory: merged.slice(0, 5) };
        });
      }
    } catch {
      // silently ignore
    }
  },

  sourceHealth: [],
  setSourceHealth: (health) => set({ sourceHealth: health }),

  displayMode: "pro",
  setDisplayMode: (mode) => set({ displayMode: mode }),

  aiPanelOpen: false,
  setAIPanelOpen: (open) => set({ aiPanelOpen: open }),

  activeTab: "liquidation",
  setActiveTab: (tab) => set({ activeTab: tab }),

  teAiByCoin: {},
  teAiLoadingByCoin: {},
  teAiErrorByCoin: {},
  setTEAIResult: (result) =>
    set((state) => {
      const coin = (result.coin || "").toUpperCase();
      if (!coin) return {};
      return {
        teAiByCoin: { ...state.teAiByCoin, [coin]: result },
        teAiLoadingByCoin: { ...state.teAiLoadingByCoin, [coin]: false },
        teAiErrorByCoin: {
          ...state.teAiErrorByCoin,
          [coin]: result.error ?? null,
        },
      };
    }),
  setTEAILoading: (coin, loading) =>
    set((state) => {
      const c = coin.toUpperCase();
      return {
        teAiLoadingByCoin: { ...state.teAiLoadingByCoin, [c]: loading },
        teAiErrorByCoin: loading
          ? { ...state.teAiErrorByCoin, [c]: null }
          : state.teAiErrorByCoin,
      };
    }),
  setTEAIError: (coin, err) =>
    set((state) => {
      const c = coin.toUpperCase();
      return {
        teAiErrorByCoin: { ...state.teAiErrorByCoin, [c]: err },
        teAiLoadingByCoin: { ...state.teAiLoadingByCoin, [c]: false },
      };
    }),

  maaByCoin: {},
  maaHistoryByCoin: {},
  maaLoadingByCoin: {},
  maaErrorByCoin: {},
  setMAAReport: (report) =>
    set((state) => {
      const c = (report.coin || "").toUpperCase();
      if (!c) return {};
      const existing = state.maaHistoryByCoin[c] ?? [];
      const merged = existing.some((r) => r.timestamp === report.timestamp)
        ? existing
        : [report, ...existing].slice(0, 20);
      return {
        maaByCoin: { ...state.maaByCoin, [c]: report },
        maaHistoryByCoin: { ...state.maaHistoryByCoin, [c]: merged },
        maaLoadingByCoin: { ...state.maaLoadingByCoin, [c]: false },
        maaErrorByCoin: { ...state.maaErrorByCoin, [c]: null },
      };
    }),
  setMAALoading: (coin, loading) =>
    set((state) => {
      const c = coin.toUpperCase();
      return {
        maaLoadingByCoin: { ...state.maaLoadingByCoin, [c]: loading },
        maaErrorByCoin: loading
          ? { ...state.maaErrorByCoin, [c]: null }
          : state.maaErrorByCoin,
      };
    }),
  setMAAError: (coin, err) =>
    set((state) => {
      const c = coin.toUpperCase();
      return {
        maaErrorByCoin: { ...state.maaErrorByCoin, [c]: err },
        maaLoadingByCoin: { ...state.maaLoadingByCoin, [c]: false },
      };
    }),
  loadMAAReport: async (coin) => {
    const c = coin.toUpperCase();
    try {
      const resp = await fetch(
        `${API_BASE}/api/market-action/report?coin=${encodeURIComponent(c)}&slim=0`,
      );
      if (!resp.ok) {
        const msg = resp.status === 404
          ? "尚未生成首份 MAA 报告"
          : `HTTP ${resp.status}`;
        get().setMAAError(c, msg);
        return;
      }
      const data = (await resp.json()) as MarketActionReport;
      if (data && data.coin) {
        get().setMAAReport(data);
      }
    } catch (e) {
      get().setMAAError(c, e instanceof Error ? e.message : String(e));
    }
  },
  loadMAAHistory: async (coin, limit = 20) => {
    const c = coin.toUpperCase();
    try {
      const resp = await fetch(
        `${API_BASE}/api/market-action/report/history?coin=${encodeURIComponent(
          c,
        )}&limit=${limit}&slim=1`,
      );
      if (!resp.ok) return;
      const data = await resp.json();
      const items: MarketActionReport[] = Array.isArray(data.items) ? data.items : [];
      if (items.length > 0) {
        set((state) => ({
          maaHistoryByCoin: { ...state.maaHistoryByCoin, [c]: items },
        }));
      }
    } catch {
      // silently ignore
    }
  },
  triggerMAARun: async (coin) => {
    const c = coin.toUpperCase();
    get().setMAALoading(c, true);
    try {
      const resp = await fetch(
        `${API_BASE}/api/market-action/run?coin=${encodeURIComponent(c)}`,
        { method: "POST" },
      );
      if (!resp.ok && resp.status !== 409) {
        get().setMAAError(c, `HTTP ${resp.status}`);
      }
    } catch (e) {
      get().setMAAError(c, e instanceof Error ? e.message : String(e));
    }
  },

  maaEvalByCoin: {},
  maaEvalLoadingByCoin: {},
  loadMAAEval: async (coin, opts) => {
    const c = coin.toUpperCase();
    set((state) => ({
      maaEvalLoadingByCoin: { ...state.maaEvalLoadingByCoin, [c]: true },
    }));
    try {
      const params = new URLSearchParams({ coin: c });
      if (opts?.refresh) params.set("refresh", "1");
      if (opts?.window_days) params.set("window_days", String(opts.window_days));
      const resp = await fetch(`${API_BASE}/api/market-action/eval?${params}`);
      if (!resp.ok) return;
      const data = await resp.json();
      if (data?.ready && data.summary) {
        set((state) => ({
          maaEvalByCoin: { ...state.maaEvalByCoin, [c]: data.summary },
        }));
      }
    } catch {
      // silently ignore
    } finally {
      set((state) => ({
        maaEvalLoadingByCoin: { ...state.maaEvalLoadingByCoin, [c]: false },
      }));
    }
  },

  obPressureSignalsByCoin: {},
  pushOrderbookPressureSignal: (signal) =>
    set((state) => {
      const c = (signal.coin || "").toUpperCase();
      if (!c) return {};
      const existing = state.obPressureSignalsByCoin[c] ?? [];
      // 去重：dedup_key 重复（理论上后端已过滤，前端再防一次）
      if (existing.some((s) => s.dedup_key === signal.dedup_key
                              && Math.abs(s.ts_sec - signal.ts_sec) < 60)) {
        return {};
      }
      const merged = [signal, ...existing].slice(0, 20);
      return {
        obPressureSignalsByCoin: { ...state.obPressureSignalsByCoin, [c]: merged },
      };
    }),
}));
