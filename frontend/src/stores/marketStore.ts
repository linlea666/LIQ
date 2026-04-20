import { create } from "zustand";
import type {
  AIAnalysisResult,
  MarketUpdate,
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
}));
