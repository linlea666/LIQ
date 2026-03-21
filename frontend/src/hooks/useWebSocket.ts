"use client";

import { useEffect, useRef } from "react";
import { io, Socket } from "socket.io-client";
import { WS_URL } from "@/lib/constants";
import { useMarketStore } from "@/stores/marketStore";
import type { MarketUpdate } from "@/lib/types";

export function useWebSocket() {
  const socketRef = useRef<Socket | null>(null);
  const coin = useMarketStore((s) => s.coin);
  const updateMarketData = useMarketStore((s) => s.updateMarketData);

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
      socket.emit("subscribe", { coin });
    });

    socket.on("market_update", (data: MarketUpdate) => {
      updateMarketData(data);
    });

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
