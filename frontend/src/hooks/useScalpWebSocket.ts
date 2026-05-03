"use client";

/**
 * 短线信号 WS 订阅 hook
 *
 * 监听后端广播事件：
 *   - scalp_signal_created  → store.applyCreated
 *   - scalp_signal_settled  → store.applySettled
 *   - scalp_signal_cancelled → store.applyCancelled
 *
 * 设计：
 *   - 独立 socket 实例（与主行情 socket / roll socket 隔离）
 *   - 不需主动 emit subscribe（后端使用全局广播）
 *   - 浏览器通知由本 hook 同时处理（confidence ≥ 阈值 + 用户已授权）
 */

import { useEffect, useRef } from "react";
import { io, Socket } from "socket.io-client";

import { WS_URL } from "@/lib/constants";
import { useScalpStore } from "@/stores/scalpStore";
import { STRATEGY_META, type ScalpSignal } from "@/lib/scalpTypes";

export function useScalpWebSocket() {
  const socketRef = useRef<Socket | null>(null);

  const applyCreated = useScalpStore((s) => s.applyCreated);
  const applySettled = useScalpStore((s) => s.applySettled);
  const applyCancelled = useScalpStore((s) => s.applyCancelled);
  const setWsConnected = useScalpStore((s) => s.setWsConnected);

  useEffect(() => {
    const socket = io(WS_URL, {
      transports: ["websocket"],
      reconnection: true,
      reconnectionDelay: 2000,
      reconnectionDelayMax: 10000,
    });
    socketRef.current = socket;

    socket.on("connect", () => {
      setWsConnected(true);
    });

    socket.on("disconnect", () => {
      setWsConnected(false);
    });

    socket.on("scalp_signal_created", (data: ScalpSignal) => {
      if (!data?.signal_id) return;
      applyCreated(data);
      maybeNotifyBrowser(data);
    });

    socket.on("scalp_signal_settled", (data: ScalpSignal) => {
      if (!data?.signal_id) return;
      applySettled(data);
    });

    socket.on("scalp_signal_cancelled", (data: ScalpSignal) => {
      if (!data?.signal_id) return;
      applyCancelled(data);
    });

    return () => {
      socket.disconnect();
      socketRef.current = null;
      setWsConnected(false);
    };
  }, [applyCreated, applySettled, applyCancelled, setWsConnected]);
}

/** 浏览器通知触发（confidence 阈值由 store.config 决定） */
function maybeNotifyBrowser(signal: ScalpSignal): void {
  if (typeof window === "undefined" || !("Notification" in window)) return;
  if (Notification.permission !== "granted") return;

  const cfg = useScalpStore.getState().config;
  if (!cfg?.notification.browser_enabled) return;
  if (signal.confidence < cfg.notification.browser_min_confidence) return;

  const meta = STRATEGY_META[signal.strategy];
  const dirText = signal.direction === "up" ? "看涨 ↑" : "看跌 ↓";
  const title = `${cfg.test_mode ? "[测试] " : ""}${signal.coin} ${dirText} ${signal.horizon_min}min`;
  const body = `${meta?.shortCn ?? signal.strategy} · 参考价 $${signal.reference_price.toLocaleString()} · 置信 ${signal.confidence}`;

  try {
    const notification = new Notification(title, {
      body,
      icon: "/favicon.ico",
      tag: `scalp:${signal.signal_id}`,
    });
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
  } catch {
    // 静默失败（某些浏览器在用户未交互前会拒绝）
  }
}

/** 用户主动请求浏览器通知权限（在用户交互中调用） */
export async function requestNotificationPermission(): Promise<boolean> {
  if (typeof window === "undefined" || !("Notification" in window)) return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  const result = await Notification.requestPermission();
  return result === "granted";
}
