"use client";

/**
 * TE · AI 解读历史列表
 *
 * 放在 TEAIInterpretBlock 下方；展示最近 N 条 AI 解读的精简信息，
 * 点击条目跳转到 /te-ai/[coin]/[ts]/page.tsx 详情页。
 *
 * 数据源：GET /api/te/ai_interpret/{coin}/history
 * 架构决策：与主 AI 对齐（主 AI 用 /api/ai/history + /ai/[coin]/[ts]/page.tsx）
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";
import { useMarketStore } from "@/stores/marketStore";
import type {
  TEAIHistoryItem,
  TEAIHistoryResponse,
  TEAIAlignment,
  TEAIScenario,
  TETradeDirection,
} from "@/lib/types";

const ALIGNMENT_SHORT: Record<TEAIAlignment, { label: string; color: string }> = {
  agree: { label: "AI 确认", color: "text-emerald-300" },
  partial_disagree: { label: "AI 补充", color: "text-amber-300" },
  strong_disagree: { label: "AI 推翻", color: "text-rose-300" },
  neutral: { label: "AI 独立", color: "text-cyan-300" },
  insufficient: { label: "证据不足", color: "text-slate-400" },
};

const SCENARIO_SHORT: Record<TEAIScenario, string> = {
  trend_continuation: "顺势续航",
  bear_rebound: "熊市反弹",
  bull_pullback: "牛市回调",
  reversal_early: "反转早期",
  reversal_confirmed: "反转确认",
  choppy_range: "震荡观望",
  unclear: "场景不清",
};

const BIAS_SHORT: Record<TETradeDirection, { label: string; color: string }> = {
  long: { label: "试多", color: "text-emerald-300" },
  short: { label: "试空", color: "text-rose-300" },
  neutral: { label: "中性", color: "text-slate-400" },
  avoid: { label: "回避", color: "text-purple-300" },
};

function formatTime(ts: number): string {
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) {
    return d.toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatPrice(v: number): string {
  if (!Number.isFinite(v) || v === 0) return "—";
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

export default function TEAIHistoryList({ coin }: { coin: string }) {
  const [items, setItems] = useState<TEAIHistoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [expanded, setExpanded] = useState(false);
  // 监听最新 AI 结果，触发列表刷新（新一条生成 → 历史 +1）
  const latestTs = useMarketStore(
    (s) => s.teAiByCoin[coin.toUpperCase()]?.ts ?? 0,
  );

  const fetchHistory = useCallback(async () => {
    if (!coin) return;
    setLoading(true);
    setErr("");
    try {
      const r = await fetch(
        `${API_BASE}/api/te/ai_interpret/${coin.toUpperCase()}/history?limit=20`,
        { cache: "no-store" },
      );
      if (!r.ok) {
        const txt = await r.text();
        throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`);
      }
      const j = (await r.json()) as TEAIHistoryResponse;
      setItems(j.items || []);
    } catch (e) {
      setErr((e as Error).message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, [coin]);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory, latestTs]);

  const visible = expanded ? items : items.slice(0, 5);

  return (
    <div className="rounded-xl border border-slate-700/60 bg-slate-900/40 p-3 mt-2">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-slate-200">
            📚 AI 解读历史
          </span>
          <span className="text-[10px] text-slate-500">
            仅你手动触发过的 AI 记录
          </span>
        </div>
        <div className="flex items-center gap-2">
          {items.length > 0 && (
            <span className="text-[10px] text-slate-500">
              {items.length} 条
            </span>
          )}
          <button
            onClick={fetchHistory}
            disabled={loading}
            className="text-[11px] text-slate-400 hover:text-slate-200 underline underline-offset-2 disabled:opacity-50"
          >
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>

      {err && (
        <div className="text-[11px] text-rose-300 bg-rose-900/20 border border-rose-800/40 rounded px-2 py-1">
          ⚠ {err}
        </div>
      )}

      {!err && items.length === 0 && !loading && (
        <div className="text-[11px] text-slate-500 py-2">
          暂无历史记录（手动触发 AI 解读后会在此显示）
        </div>
      )}

      {visible.length > 0 && (
        <ul className="space-y-1.5">
          {visible.map((item) => {
            const align = ALIGNMENT_SHORT[item.ai.alignment_with_rules];
            const scene = SCENARIO_SHORT[item.ai.scenario];
            const bias = item.ai.trade_bias
              ? BIAS_SHORT[item.ai.trade_bias.direction]
              : null;
            return (
              <li key={`${item.ts}-${item.fingerprint}`}>
                <Link
                  href={`/te-ai/${item.coin}/${item.ts}`}
                  className="block rounded-md border border-slate-800 bg-slate-950/40 hover:border-blue-500/50 hover:bg-slate-900/60 transition px-2.5 py-2"
                >
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div className="flex items-center gap-2 flex-wrap min-w-0">
                      <span className={`text-[10px] font-semibold ${align.color}`}>
                        {align.label}
                      </span>
                      <span className="text-[10px] text-slate-400">
                        {scene}
                      </span>
                      {bias && (
                        <span className={`text-[10px] ${bias.color}`}>
                          · {bias.label}
                        </span>
                      )}
                      {item.ai.confidence > 0 && (
                        <span className="text-[10px] text-slate-500 font-mono">
                          · conf {Math.round(item.ai.confidence * 100)}%
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2 text-[10px] text-slate-500 shrink-0">
                      <span className="font-mono">{formatPrice(item.price)}</span>
                      <span>·</span>
                      <span>{formatTime(item.ts)}</span>
                    </div>
                  </div>
                  <div className="mt-1 text-[12px] text-slate-300 leading-snug line-clamp-2">
                    {item.ai.summary_cn || "（AI 未给出摘要）"}
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      )}

      {items.length > 5 && (
        <div className="mt-2 text-center">
          <button
            onClick={() => setExpanded((v) => !v)}
            className="text-[11px] text-slate-400 hover:text-slate-200 underline underline-offset-2"
          >
            {expanded ? "收起（只看最近 5 条）" : `展开全部 ${items.length} 条`}
          </button>
        </div>
      )}
    </div>
  );
}
