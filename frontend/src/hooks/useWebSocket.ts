"use client";

import { useEffect, useRef } from "react";
import { io, Socket } from "socket.io-client";
import { WS_URL } from "@/lib/constants";
import { useMarketStore } from "@/stores/marketStore";
import type {
  MarketActionReport,
  MarketUpdate,
  StrategicReport,
  TEAIInterpretation,
} from "@/lib/types";

export function useWebSocket() {
  const socketRef = useRef<Socket | null>(null);
  const coinRef = useRef<string>("BTC");
  const coin = useMarketStore((s) => s.coin);
  const updateMarketData = useMarketStore((s) => s.updateMarketData);
  const setStrategicReport = useMarketStore((s) => s.setStrategicReport);
  const setTEAIResult = useMarketStore((s) => s.setTEAIResult);
  const setTEAIError = useMarketStore((s) => s.setTEAIError);
  const setMAAReport = useMarketStore((s) => s.setMAAReport);
  const loadMAAReport = useMarketStore((s) => s.loadMAAReport);
  const loadStrategicReport = useMarketStore((s) => s.loadStrategicReport);
  const setStrategicError = useMarketStore((s) => s.setStrategicError);

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
      loadMAAReport(coinRef.current);
      loadStrategicReport(coinRef.current);
    });

    socket.on("market_update", (data: MarketUpdate) => {
      updateMarketData(data);
    });

    socket.on("strategic_report", (data: StrategicReport) => {
      console.log(
        "[WS] strategic_report | coin=%s decision=%s",
        data.coin,
        data.decision,
      );
      setStrategicReport(data);
    });

    socket.on(
      "strategic_error",
      (data: { coin: string; reason: string; ts?: number }) => {
        console.warn(
          "[WS] strategic_error | coin=%s reason=%s",
          data.coin,
          data.reason,
        );
        // 后端早 return 路径（arbiter/snapshot 不可用 / 任务异常）走这里，
        // 解锁 AIButton 的 strategicLoading；只在当前订阅币种生效避免串频
        if (data.coin && data.coin.toUpperCase() === coinRef.current.toUpperCase()) {
          setStrategicError(data.reason || "strategic_task_failed");
        }
      },
    );

    socket.on("te_ai_result", (data: TEAIInterpretation) => {
      console.log(
        "[WS] te_ai_result received | coin=%s align=%s conf=%s",
        data.coin,
        data.alignment_with_rules,
        data.confidence,
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

    socket.on("market_action_report", (data: MarketActionReport) => {
      console.log(
        "[WS] market_action_report | coin=%s scenario=%s conf=%s bias=%s",
        data.coin,
        data.scenario,
        data.confidence,
        data.trading_implications?.bias,
      );
      setMAAReport(data);
    });

    socket.on("disconnect", () => {
      console.log("[WS] disconnected");
    });

    return () => {
      socket.disconnect();
    };
  }, [loadMAAReport, loadStrategicReport, setMAAReport, setStrategicError, setStrategicReport, setTEAIError, setTEAIResult, updateMarketData]);

  useEffect(() => {
    if (socketRef.current?.connected) {
      socketRef.current.emit("subscribe", { coin });
      loadMAAReport(coin);
      loadStrategicReport(coin);
    }
  }, [coin, loadMAAReport, loadStrategicReport]);
}
