"use client";

/**
 * useRollAlerts —— 把 RollSignal 转成桌面 Notification（可选声音）
 *
 * 触发条件（都必须同时满足）：
 *   1. settings.notification_enabled = true
 *   2. 位于静默时段外；或静默时段内但为 urgent 且 quiet_allow_urgent = true
 *   3. urgency = urgent 或 attention （action != hold）
 *   4. (position_id, action, ts) 尚未通知过
 *
 * 行为：
 *   - 首次挂载请求 Notification.permission（已授权则无操作）
 *   - 点击通知 → 聚焦窗口 + 路由到 /roll/{position_id}
 *   - urgent 信号 + notification_sound_for_urgent=true → 发声（短蜂鸣）
 *   - 前瞻窗口（action=hold 且 forward_windows.length>0）不在此 hook 内触发通知，
 *     避免噪声；前瞻信息由详情页/总览页可视化展示。
 *
 * 去抖：useEffect 基于 signalsByPosition 引用变化触发，比较 `notifiedKey`
 *        （position_id#action#ts）避免重复。
 */

import { useEffect, useRef } from "react";

import { useRollStore } from "@/stores/rollStore";
import type { RollGlobalSettings, RollSignal } from "@/lib/rollTypes";

function isInQuietHours(now: Date, s: RollGlobalSettings): boolean {
  if (!s.quiet_hours_enabled) return false;
  const hourUtc = now.getUTCHours();
  const { quiet_start_utc: a, quiet_end_utc: b } = s;
  if (a === b) return false;
  if (a < b) return hourUtc >= a && hourUtc < b;
  // 跨日：start > end（如 23 → 7）
  return hourUtc >= a || hourUtc < b;
}

function shouldNotify(signal: RollSignal, settings: RollGlobalSettings): boolean {
  if (!settings.notification_enabled) return false;
  if (signal.action === "hold") return false;
  if (signal.urgency !== "urgent" && signal.urgency !== "attention") return false;

  if (isInQuietHours(new Date(), settings)) {
    if (signal.urgency === "urgent" && settings.quiet_allow_urgent) return true;
    return false;
  }
  return true;
}

const ACTION_CN: Record<string, string> = {
  add: "建议加仓",
  reduce: "建议减仓",
  close: "建议离场",
  move_sl: "建议移止损",
  hold: "持有",
};

function makeTitle(signal: RollSignal): string {
  const urg = signal.urgency === "urgent" ? "🔴 紧急" : "🟡 关注";
  const act = ACTION_CN[signal.action] ?? signal.action;
  return `${urg} · ${signal.coin} · ${act}`;
}

function makeBody(signal: RollSignal): string {
  const parts: string[] = [];
  if (signal.action === "add") {
    parts.push(`烈度 ${signal.add_intensity}`);
    if (signal.add_preview) {
      parts.push(`建议加仓 ${signal.add_preview.final_margin_usd.toFixed(0)} USD`);
    }
  } else if (signal.action === "reduce" && signal.reduce_pct != null) {
    parts.push(`建议减 ${(signal.reduce_pct * 100).toFixed(0)}%`);
  } else if (signal.action === "move_sl" && signal.suggested_new_sl != null) {
    parts.push(`→ 止损 ${signal.suggested_new_sl.toLocaleString()}`);
  }
  parts.push(`现价 ${signal.current_price.toLocaleString()}`);
  parts.push(`conf ${signal.confidence_score.toFixed(0)}`);
  const first = parts.join(" · ");
  return signal.headline_cn ? `${first}\n${signal.headline_cn}` : first;
}

/** 播放一声短蜂鸣（Web Audio，不依赖任何外部资源）。 */
function playBeep(): void {
  try {
    const AudioCtx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.36);
    setTimeout(() => ctx.close().catch(() => undefined), 500);
  } catch {
    /* silent */
  }
}

export function useRollAlerts() {
  const signalsByPosition = useRollStore((s) => s.signalsByPosition);
  const settings = useRollStore((s) => s.settings);
  const notifiedKeysRef = useRef<Set<string>>(new Set());
  const permissionAskedRef = useRef(false);

  // 首次挂载请求权限
  useEffect(() => {
    if (permissionAskedRef.current) return;
    permissionAskedRef.current = true;
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (Notification.permission === "default") {
      Notification.requestPermission().catch(() => undefined);
    }
  }, []);

  useEffect(() => {
    if (!settings) return;
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (Notification.permission !== "granted") return;

    const seen = notifiedKeysRef.current;

    for (const [positionId, signal] of Object.entries(signalsByPosition)) {
      const key = `${positionId}#${signal.action}#${signal.ts}`;
      if (seen.has(key)) continue;
      seen.add(key);

      if (!shouldNotify(signal, settings)) continue;

      try {
        const n = new Notification(makeTitle(signal), {
          body: makeBody(signal),
          tag: `roll:${positionId}`, // 同 position 自动替换
          requireInteraction: signal.urgency === "urgent",
          icon: "/favicon.ico",
        });
        n.onclick = () => {
          try {
            window.focus();
            window.location.href = `/roll/${positionId}`;
          } catch {
            /* silent */
          } finally {
            n.close();
          }
        };
        if (signal.urgency === "urgent" && settings.notification_sound_for_urgent) {
          playBeep();
        }
      } catch {
        /* silent */
      }
    }

    // 控制 seen 尺寸（防止长时间运行后无限膨胀）
    if (seen.size > 1000) {
      const arr = Array.from(seen);
      notifiedKeysRef.current = new Set(arr.slice(-500));
    }
  }, [signalsByPosition, settings]);
}
