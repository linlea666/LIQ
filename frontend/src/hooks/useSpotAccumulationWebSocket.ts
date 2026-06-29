"use client";

import { useEffect, useRef } from "react";
import { io, Socket } from "socket.io-client";

import { WS_URL } from "@/lib/constants";

export function useSpotAccumulationWebSocket(
  coin: string,
  onUpdate: () => void,
) {
  const updateRef = useRef(onUpdate);
  useEffect(() => {
    updateRef.current = onUpdate;
  }, [onUpdate]);

  useEffect(() => {
    const socket: Socket = io(WS_URL, {
      transports: ["websocket"],
      reconnection: true,
      reconnectionDelay: 2_000,
      reconnectionDelayMax: 10_000,
    });
    socket.on("connect", () => socket.emit("subscribe", { coin }));
    socket.on("spot_accumulation_update", () => updateRef.current());
    return () => {
      socket.disconnect();
    };
  }, [coin]);
}
