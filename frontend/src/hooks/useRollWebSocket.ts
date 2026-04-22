"use client";

/**
 * 滚仓模块 WebSocket 订阅 hook
 *
 * 设计：
 *   - 独立 socket 实例（不复用主行情 socket），避免跨页面的状态泄漏
 *   - 订阅所有 active positions 的 roll:{id} 频道
 *   - positions 列表变化时自动订阅/退订差集
 *   - 服务端推送的 roll_signal 写入 rollStore.signalsByPosition
 *
 * 使用：
 *   在 /roll 根布局 useEffect 初始化一次即可；卸载时自动断开。
 */

import { useEffect, useRef } from "react";
import { io, Socket } from "socket.io-client";

import { WS_URL } from "@/lib/constants";
import { useRollStore } from "@/stores/rollStore";
import type { RollEvent, RollSignal } from "@/lib/rollTypes";

export function useRollWebSocket() {
  const socketRef = useRef<Socket | null>(null);
  const subscribedRef = useRef<Set<string>>(new Set());

  const positions = useRollStore((s) => s.positions);
  const applySignal = useRollStore((s) => s.applySignal);
  const applyEvent = useRollStore((s) => s.applyEvent);

  // 建立连接（只一次）
  useEffect(() => {
    const socket = io(WS_URL, {
      transports: ["websocket"],
      reconnection: true,
      reconnectionDelay: 2000,
      reconnectionDelayMax: 10000,
    });
    socketRef.current = socket;

    socket.on("connect", () => {
      console.log("[Roll-WS] connected");
      // 连接恢复后重订已记录的所有 positions
      subscribedRef.current.forEach((pid) => {
        socket.emit("subscribe_roll", { position_id: pid });
      });
    });

    socket.on("roll_signal", (data: RollSignal) => {
      if (!data?.position_id) return;
      applySignal(data);
    });

    socket.on(
      "roll_event",
      (data: { position_id: string; event: RollEvent }) => {
        if (!data?.position_id || !data?.event) return;
        applyEvent(data.position_id, data.event);
      },
    );

    socket.on("disconnect", () => {
      console.log("[Roll-WS] disconnected");
    });

    return () => {
      subscribedRef.current.forEach((pid) => {
        socket.emit("unsubscribe_roll", { position_id: pid });
      });
      subscribedRef.current.clear();
      socket.disconnect();
      socketRef.current = null;
    };
  }, [applySignal, applyEvent]);

  // positions 变化时同步订阅集合
  useEffect(() => {
    const socket = socketRef.current;
    if (!socket) return;

    const currentIds = new Set(
      positions.filter((p) => p.status === "active").map((p) => p.id),
    );
    const prevIds = subscribedRef.current;

    // 新增订阅
    currentIds.forEach((id) => {
      if (!prevIds.has(id)) {
        socket.emit("subscribe_roll", { position_id: id });
      }
    });
    // 取消不再需要的订阅
    prevIds.forEach((id) => {
      if (!currentIds.has(id)) {
        socket.emit("unsubscribe_roll", { position_id: id });
      }
    });

    subscribedRef.current = currentIds;
  }, [positions]);
}
