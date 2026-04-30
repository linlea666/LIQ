import { create } from "zustand";
import type {
  MAAEvalSummary,
  MarketActionReport,
  MarketUpdate,
  SourceHealth,
  StrategicReport,
  TEAIInterpretation,
  TradingBrainSnapshot,
} from "@/lib/types";
import { API_BASE } from "@/lib/constants";
import type { CoinType } from "@/lib/constants";

interface MarketStore {
  coin: CoinType;
  setCoin: (coin: CoinType) => void;

  data: Record<string, MarketUpdate>;
  updateMarketData: (update: MarketUpdate) => void;

  strategicReport: StrategicReport | null;
  strategicLoading: boolean;
  strategicError: string | null;
  strategicHistory: StrategicReport[];
  strategicAvailable: boolean;
  setStrategicReport: (result: StrategicReport) => void;
  setStrategicLoading: (loading: boolean) => void;
  setStrategicError: (error: string | null) => void;
  setStrategicAvailable: (available: boolean) => void;
  loadStrategicHistory: (coin: string) => Promise<void>;
  loadStrategicReport: (coin: string) => Promise<void>;

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

  tradingBrainByCoin: Record<string, TradingBrainSnapshot>;
  tradingBrainLoadingByCoin: Record<string, boolean>;
  tradingBrainErrorByCoin: Record<string, string | null>;
  setTradingBrain: (snap: TradingBrainSnapshot) => void;
  setTradingBrainLoading: (coin: string, loading: boolean) => void;
  setTradingBrainError: (coin: string, err: string | null) => void;
  loadTradingBrain: (coin: string, maxZones?: number) => Promise<void>;
}

export const useMarketStore = create<MarketStore>((set, get) => ({
  coin: "BTC",
  setCoin: (coin) =>
    set({
      coin,
      strategicReport: null,
      strategicError: null,
      strategicLoading: false,
      strategicHistory: [],
    }),

  data: {},
  updateMarketData: (update) =>
    set((state) => ({
      data: {
        ...state.data,
        [update.coin]: { ...state.data[update.coin], ...update },
      },
    })),

  strategicReport: null,
  strategicLoading: false,
  strategicError: null,
  strategicHistory: [],
  strategicAvailable: false,
  setStrategicReport: (result) =>
    set((state) => {
      // 严格按当前 coin 过滤——避免 WS 串频或多 tab 共享 store 时把别的币
      // 的报告塞到当前 history 里污染 UI
      if (
        !result?.coin ||
        result.coin.toUpperCase() !== state.coin.toUpperCase()
      ) {
        return {};
      }
      const ts = result.timestamp;
      const exists = state.strategicHistory.some((h) => h.timestamp === ts);
      const history = exists
        ? state.strategicHistory
        : [result, ...state.strategicHistory].slice(0, 5);
      return {
        strategicReport: result,
        strategicLoading: false,
        strategicError: null,
        strategicHistory: history,
      };
    }),
  setStrategicLoading: (loading) =>
    set({ strategicLoading: loading, strategicError: null }),
  setStrategicError: (error) =>
    set({ strategicError: error, strategicLoading: false }),
  setStrategicAvailable: (available) => set({ strategicAvailable: available }),
  loadStrategicReport: async (coin) => {
    const c = coin.toUpperCase();
    try {
      const resp = await fetch(
        `${API_BASE}/api/strategic/report?coin=${encodeURIComponent(c)}&slim=1`,
      );
      if (!resp.ok) return;
      const data = (await resp.json()) as StrategicReport;
      if (data?.coin && data.timestamp) {
        get().setStrategicReport(data);
      }
    } catch {
      // silently ignore
    }
  },
  loadStrategicHistory: async (coin) => {
    try {
      const resp = await fetch(
        `${API_BASE}/api/strategic/report/history?coin=${encodeURIComponent(
          coin,
        )}&limit=5&slim=1`,
      );
      if (!resp.ok) return;
      const data = await resp.json();
      const items: StrategicReport[] = Array.isArray(data.items) ? data.items : [];
      if (items.length > 0) {
        set((state) => {
          const merged = [...items];
          for (const existing of state.strategicHistory) {
            if (!merged.some((m) => m.timestamp === existing.timestamp)) {
              merged.push(existing);
            }
          }
          merged.sort((a, b) => b.timestamp - a.timestamp);
          return { strategicHistory: merged.slice(0, 5) };
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

  tradingBrainByCoin: {},
  tradingBrainLoadingByCoin: {},
  tradingBrainErrorByCoin: {},
  setTradingBrain: (snap) =>
    set((state) => {
      const c = (snap.coin || "").toUpperCase();
      if (!c) return {};
      return {
        tradingBrainByCoin: { ...state.tradingBrainByCoin, [c]: snap },
        tradingBrainLoadingByCoin: { ...state.tradingBrainLoadingByCoin, [c]: false },
        tradingBrainErrorByCoin: { ...state.tradingBrainErrorByCoin, [c]: null },
      };
    }),
  setTradingBrainLoading: (coin, loading) =>
    set((state) => {
      const c = coin.toUpperCase();
      return {
        tradingBrainLoadingByCoin: { ...state.tradingBrainLoadingByCoin, [c]: loading },
        tradingBrainErrorByCoin: loading
          ? { ...state.tradingBrainErrorByCoin, [c]: null }
          : state.tradingBrainErrorByCoin,
      };
    }),
  setTradingBrainError: (coin, err) =>
    set((state) => {
      const c = coin.toUpperCase();
      return {
        tradingBrainErrorByCoin: { ...state.tradingBrainErrorByCoin, [c]: err },
        tradingBrainLoadingByCoin: { ...state.tradingBrainLoadingByCoin, [c]: false },
      };
    }),
  loadTradingBrain: async (coin, maxZones = 24) => {
    const c = coin.toUpperCase();
    get().setTradingBrainLoading(c, true);
    try {
      const q = new URLSearchParams({ max_zones: String(maxZones) });
      const resp = await fetch(
        `${API_BASE}/api/trading-brain/${encodeURIComponent(c)}?${q}`,
      );
      if (!resp.ok) {
        get().setTradingBrainError(c, `HTTP ${resp.status}`);
        return;
      }
      const data = (await resp.json()) as TradingBrainSnapshot;
      if (data?.coin) {
        get().setTradingBrain(data);
      }
    } catch (e) {
      get().setTradingBrainError(c, e instanceof Error ? e.message : String(e));
    } finally {
      get().setTradingBrainLoading(c, false);
    }
  },

}));
