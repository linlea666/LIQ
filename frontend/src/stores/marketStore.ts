import { create } from "zustand";
import type {
  AIAnalysisResult,
  MarketUpdate,
  SourceHealth,
} from "@/lib/types";
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
  setAIResult: (result: AIAnalysisResult) => void;
  setAILoading: (loading: boolean) => void;
  setAIError: (error: string | null) => void;

  sourceHealth: SourceHealth[];
  setSourceHealth: (health: SourceHealth[]) => void;

  displayMode: "beginner" | "advanced" | "pro";
  setDisplayMode: (mode: "beginner" | "advanced" | "pro") => void;

  aiPanelOpen: boolean;
  setAIPanelOpen: (open: boolean) => void;

  activeTab: string;
  setActiveTab: (tab: string) => void;
}

export const useMarketStore = create<MarketStore>((set, get) => ({
  coin: "BTC",
  setCoin: (coin) => set({ coin, aiResult: null, aiError: null }),

  data: {},
  updateMarketData: (update) =>
    set((state) => ({
      data: { ...state.data, [update.coin]: update },
    })),

  aiResult: null,
  aiLoading: false,
  aiError: null,
  aiHistory: [],
  setAIResult: (result) =>
    set((state) => ({
      aiResult: result,
      aiLoading: false,
      aiError: null,
      aiHistory: [result, ...state.aiHistory].slice(0, 5),
    })),
  setAILoading: (loading) => set({ aiLoading: loading, aiError: null }),
  setAIError: (error) => set({ aiError: error, aiLoading: false }),

  sourceHealth: [],
  setSourceHealth: (health) => set({ sourceHealth: health }),

  displayMode: "advanced",
  setDisplayMode: (mode) => set({ displayMode: mode }),

  aiPanelOpen: false,
  setAIPanelOpen: (open) => set({ aiPanelOpen: open }),

  activeTab: "liquidation",
  setActiveTab: (tab) => set({ activeTab: tab }),
}));
