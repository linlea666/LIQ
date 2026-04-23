"use client";

/**
 * PromptDebugDrawer · MAA Prompt 透明度抽屉
 *
 * 展示本轮完整的 AI 交互现场，4 个 tab：
 *   1. System Prompt  —— 角色设定 / 6 步思考流程 / 场景词典 / 输出 schema
 *   2. User Prompt    —— 本轮喂给 AI 的 facts 渲染（§0 前情提要 / §1-§6）
 *   3. AI CoT         —— DeepSeek-reasoner 的 Chain-of-Thought 原文
 *   4. AI Raw Output  —— AI 返回的原始 JSON 文本
 *
 * 额外信息条：model / tokens / latency / parse_ok
 */

import { useState } from "react";
import type { MarketActionReport } from "@/lib/types";

interface Props {
  open: boolean;
  onClose: () => void;
  report: MarketActionReport;
}

type TabId = "system" | "user" | "cot" | "raw";

const TAB_LABELS: Record<TabId, string> = {
  system: "System",
  user: "User Prompt",
  cot: "AI 思维链 (CoT)",
  raw: "AI 原始输出",
};

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  if (!text) return null;
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        });
      }}
      className="px-2 py-1 text-[11px] rounded border border-slate-700 text-slate-300 hover:bg-slate-800 transition"
    >
      {done ? "已复制" : "复制"}
    </button>
  );
}

export default function PromptDebugDrawer({ open, onClose, report }: Props) {
  const [tab, setTab] = useState<TabId>("user");
  const pd = report.prompt_debug;

  if (!open) return null;

  const unavailable = !pd;

  const contentByTab: Record<TabId, string> = {
    system: pd?.system ?? "",
    user: pd?.user ?? "",
    cot: pd?.ai_reasoning_content ?? "",
    raw: pd?.ai_raw_response ?? "",
  };

  const text = contentByTab[tab];

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* backdrop */}
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      />
      {/* drawer */}
      <div className="relative w-full max-w-3xl h-full bg-slate-950 border-l border-slate-800 flex flex-col shadow-2xl">
        {/* header */}
        <div className="shrink-0 px-4 py-3 border-b border-slate-800 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-sm font-semibold text-slate-200">
              Prompt 透明度 · {report.coin}
            </span>
            {pd && (
              <span className="text-[11px] text-slate-500 font-mono">
                {pd.model} · {pd.tokens_prompt ?? "—"} in / {pd.tokens_completion ?? "—"} out
                {pd.tokens_reasoning != null && ` / ${pd.tokens_reasoning} CoT`}
                {pd.latency_ms != null && ` · ${(pd.latency_ms / 1000).toFixed(1)}s`}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-200 text-lg leading-none w-8 h-8 rounded hover:bg-slate-800"
          >
            ×
          </button>
        </div>

        {unavailable ? (
          <div className="flex-1 flex items-center justify-center text-slate-400 text-sm px-6 text-center">
            Prompt 调试信息不可用。请确认后端配置
            <code className="mx-1 px-1 rounded bg-slate-800 text-slate-200">
              market_action.include_prompt_in_api: true
            </code>
            ，并在 REST 请求中使用
            <code className="mx-1 px-1 rounded bg-slate-800 text-slate-200">slim=0</code>。
          </div>
        ) : (
          <>
            {/* tabs */}
            <div className="shrink-0 flex border-b border-slate-800">
              {(Object.keys(TAB_LABELS) as TabId[]).map((id) => {
                const isActive = tab === id;
                const isEmpty = !contentByTab[id];
                return (
                  <button
                    key={id}
                    onClick={() => setTab(id)}
                    disabled={isEmpty}
                    className={`px-4 py-2 text-xs font-medium border-b-2 transition ${
                      isActive
                        ? "text-blue-400 border-blue-400"
                        : isEmpty
                        ? "text-slate-700 border-transparent cursor-not-allowed"
                        : "text-slate-400 border-transparent hover:text-slate-200"
                    }`}
                    title={isEmpty ? "该 tab 在本轮无内容" : ""}
                  >
                    {TAB_LABELS[id]}
                    {isEmpty && <span className="ml-1 text-[10px]">·空</span>}
                  </button>
                );
              })}
              <div className="flex-1" />
              <div className="flex items-center gap-2 px-3">
                <CopyButton text={text} />
              </div>
            </div>

            {/* body */}
            <div className="flex-1 overflow-auto">
              {tab === "user" && pd?.sections && pd.sections.length > 0 && (
                <div className="sticky top-0 z-10 bg-slate-950/95 border-b border-slate-800 px-4 py-2 text-[10px] text-slate-500 flex gap-2 flex-wrap">
                  <span>章节：</span>
                  {pd.sections.map((s) => (
                    <span key={s.anchor} className="font-mono text-slate-400">
                      {s.anchor} {s.title}
                    </span>
                  ))}
                </div>
              )}
              <pre className="px-4 py-3 text-[12px] leading-relaxed text-slate-300 whitespace-pre-wrap font-mono break-words">
                {text || "（空）"}
              </pre>
              {tab === "cot" && !text && (
                <div className="px-4 pb-4 text-[11px] text-slate-500 italic">
                  DeepSeek-reasoner 的思维链；非 reasoner 模型此字段为空。
                </div>
              )}
              {tab === "raw" && pd?.parse_error && (
                <div className="px-4 py-2 text-[11px] text-rose-400 bg-rose-950/30 border-t border-rose-900 whitespace-pre-wrap">
                  解析错误：{pd.parse_error}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
