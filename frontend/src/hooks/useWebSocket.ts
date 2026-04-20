"use client";

import { useEffect, useRef } from "react";
import { io, Socket } from "socket.io-client";
import { WS_URL } from "@/lib/constants";
import { useMarketStore } from "@/stores/marketStore";
import type {
  AIAnalysisResult,
  MarketUpdate,
  TEAIInterpretation,
} from "@/lib/types";

export function useWebSocket() {
  const socketRef = useRef<Socket | null>(null);
  const coinRef = useRef<string>("BTC");
  const coin = useMarketStore((s) => s.coin);
  const updateMarketData = useMarketStore((s) => s.updateMarketData);
  const setAIResult = useMarketStore((s) => s.setAIResult);
  const setAIError = useMarketStore((s) => s.setAIError);
  const setTEAIResult = useMarketStore((s) => s.setTEAIResult);
  const setTEAIError = useMarketStore((s) => s.setTEAIError);

  coinRef.current = coin;

  useEffect(() => {
    const socket = io(WS_URL, {
      transports: ["websocket"],
      reconnection: true,
      reconnectionDelay: 2000,
      reconnectionDelayMax: 10000,
    });

    socketRef.current = socket;

    socket.on("connect", () => {
      console.log("[WS] connected");
      socket.emit("subscribe", { coin: coinRef.current });
    });

    socket.on("market_update", (data: MarketUpdate) => {
      updateMarketData(data);
    });

    socket.on("ai_result", (data: AIAnalysisResult) => {
      console.log("[WS] ai_result received | coin=%s", data.coin);
      setAIResult(data);
    });

    socket.on("ai_error", (data: { coin: string; message: string }) => {
      console.log("[WS] ai_error received | coin=%s", data.coin);
      setAIError(data.message);
    });

    // TE · AI 深度解读推送（对齐主 AI fire-and-forget 架构）
    socket.on("te_ai_result", (data: TEAIInterpretation) => {
      console.log(
        "[WS] te_ai_result received | coin=%s align=%s conf=%s",
        data.coin, data.alignment_with_rules, data.confidence,
      );
      setTEAIResult(data);
    });

    socket.on(
      "te_ai_error",
      (data: { coin: string; message: string; signal_fingerprint?: string }) => {
        console.log("[WS] te_ai_error received | coin=%s", data.coin);
        setTEAIError(data.coin, data.message);
      },
    );

    socket.on("disconnect", () => {
      console.log("[WS] disconnected");
    });

    return () => {
      socket.disconnect();
    };
  }, []);

  useEffect(() => {
    if (socketRef.current?.connected) {
      socketRef.current.emit("subscribe", { coin });
    }
  }, [coin]);
}
