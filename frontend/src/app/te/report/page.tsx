"use client";

/**
 * TrendExhaustion · 准确率日报页面（P0-B）
 *
 * 读取后端 /api/te/reports 的日报索引 + /api/te/reports/{date} 的 Markdown，
 * 渲染为简洁可读的页面，支持：
 *   - 日期切换（下拉）
 *   - 一键强制重新生成（regenerate=true）
 *   - "一键复制 AI Review Prompt"（下方固定按钮）
 *   - "复制全文"（用于发给 AI 做复核）
 *
 * 该页面不做复杂 Markdown 语法解析，采用行级渲染 + 表格识别，够用即可。
 */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/constants";

type ReportListItem = {
  date: string;
  size: number;
  mtime: number;
};

type ReportListResponse = {
  reports: ReportListItem[];
  shadow_dates: string[];
  logger_stats: {
    started: boolean;
    written: number;
    dropped: number;
    queue_size: number;
    open_files: number;
  };
};

type ReportResponse = {
  date: string;
  markdown: string | null;
  exists: boolean;
  regenerated?: boolean;
};

function extractAiPrompt(md: string): string {
  // 抓取 "发给 AI 复核用 Prompt" 段落里第一个 ``` 块
  const idx = md.indexOf("发给 AI 复核用 Prompt");
  if (idx < 0) return "";
  const rest = md.slice(idx);
  const m = rest.match(/```([\s\S]*?)```/);
  return (m?.[1] || "").trim();
}

function MarkdownView({ md }: { md: string }) {
  // 轻量渲染：# 标题、- 列表、表格、代码块
  const lines = md.split("\n");
  const blocks: React.ReactNode[] = [];
  let i = 0;
  let key = 0;

  const pushKey = () => `blk-${key++}`;

  while (i < lines.length) {
    const line = lines[i];
    // 代码块
    if (line.startsWith("```")) {
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) {
        buf.push(lines[i]);
        i += 1;
      }
      i += 1;
      blocks.push(
        <pre
          key={pushKey()}
          className="my-3 overflow-auto rounded-md bg-slate-900/80 border border-slate-800 p-3 text-[12px] leading-relaxed text-slate-300 whitespace-pre-wrap"
        >
          {buf.join("\n")}
        </pre>,
      );
      continue;
    }
    // 表格
    if (line.trim().startsWith("|")) {
      const tableLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        tableLines.push(lines[i]);
        i += 1;
      }
      if (tableLines.length >= 2) {
        const header = tableLines[0]
          .trim()
          .replace(/^\|/, "")
          .replace(/\|$/, "")
          .split("|")
          .map((c) => c.trim());
        const bodyLines = tableLines.slice(2); // 跳过分隔行
        blocks.push(
          <div
            key={pushKey()}
            className="my-3 overflow-x-auto rounded-md border border-slate-800"
          >
            <table className="w-full text-[12px]">
              <thead className="bg-slate-900/80">
                <tr>
                  {header.map((h, idx) => (
                    <th
                      key={idx}
                      className="px-2 py-1.5 text-left font-medium text-slate-300 border-b border-slate-800"
                    >
                      {h.replace(/`/g, "")}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bodyLines.map((bl, rIdx) => {
                  const cells = bl
                    .trim()
                    .replace(/^\|/, "")
                    .replace(/\|$/, "")
                    .split("|")
                    .map((c) => c.trim());
                  return (
                    <tr
                      key={rIdx}
                      className="border-b border-slate-800/50 hover:bg-slate-900/40"
                    >
                      {cells.map((c, cIdx) => (
                        <td
                          key={cIdx}
                          className="px-2 py-1 text-slate-300 font-mono"
                          dangerouslySetInnerHTML={{
                            __html: c
                              .replace(/</g, "&lt;")
                              .replace(/>/g, "&gt;")
                              .replace(/`([^`]+)`/g, '<span class="text-amber-300">$1</span>')
                              .replace(/\*\*([^*]+)\*\*/g, '<strong class="text-slate-100">$1</strong>'),
                          }}
                        />
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>,
        );
        continue;
      }
    }
    // 标题
    if (line.startsWith("# ")) {
      blocks.push(
        <h1 key={pushKey()} className="mt-6 mb-3 text-2xl font-bold text-slate-100">
          {line.slice(2)}
        </h1>,
      );
      i += 1;
      continue;
    }
    if (line.startsWith("## ")) {
      blocks.push(
        <h2 key={pushKey()} className="mt-5 mb-2 text-lg font-semibold text-blue-300">
          {line.slice(3)}
        </h2>,
      );
      i += 1;
      continue;
    }
    if (line.startsWith("### ")) {
      blocks.push(
        <h3 key={pushKey()} className="mt-4 mb-1 text-sm font-semibold text-slate-300">
          {line.slice(4)}
        </h3>,
      );
      i += 1;
      continue;
    }
    // 列表
    if (line.startsWith("- ")) {
      const buf: string[] = [];
      while (i < lines.length && lines[i].startsWith("- ")) {
        buf.push(lines[i].slice(2));
        i += 1;
      }
      blocks.push(
        <ul key={pushKey()} className="my-2 space-y-1 list-disc pl-5 text-[13px] text-slate-300">
          {buf.map((b, idx) => (
            <li
              key={idx}
              dangerouslySetInnerHTML={{
                __html: b
                  .replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;")
                  .replace(/`([^`]+)`/g, '<code class="text-amber-300 bg-slate-900/60 px-1 rounded">$1</code>')
                  .replace(/\*\*([^*]+)\*\*/g, '<strong class="text-slate-100">$1</strong>'),
              }}
            />
          ))}
        </ul>,
      );
      continue;
    }
    if (line.startsWith("---")) {
      blocks.push(<hr key={pushKey()} className="my-4 border-slate-800" />);
      i += 1;
      continue;
    }
    if (line.trim() === "") {
      i += 1;
      continue;
    }
    // 普通段落
    blocks.push(
      <p
        key={pushKey()}
        className="my-2 text-[13px] leading-relaxed text-slate-300"
        dangerouslySetInnerHTML={{
          __html: line
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/`([^`]+)`/g, '<code class="text-amber-300 bg-slate-900/60 px-1 rounded">$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong class="text-slate-100">$1</strong>')
            .replace(/_([^_]+)_/g, '<em class="text-slate-400">$1</em>'),
        }}
      />,
    );
    i += 1;
  }
  return <>{blocks}</>;
}

export default function TeReportPage() {
  const [list, setList] = useState<ReportListResponse | null>(null);
  const [date, setDate] = useState<string>("");
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string>("");
  const [copyMsg, setCopyMsg] = useState<string>("");

  const fetchList = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/api/te/reports`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = (await r.json()) as ReportListResponse;
      setList(j);
      if (!date) {
        const firstDate =
          j.reports[0]?.date || j.shadow_dates[0] || "";
        if (firstDate) setDate(firstDate);
      }
    } catch (e) {
      setErr((e as Error).message);
    }
  }, [date]);

  const fetchReport = useCallback(
    async (d: string, regenerate = false) => {
      if (!d) return;
      setLoading(true);
      setErr("");
      try {
        const url = regenerate
          ? `${API_BASE}/api/te/reports/${d}?regenerate=true`
          : `${API_BASE}/api/te/reports/${d}`;
        const r = await fetch(url, { cache: "no-store" });
        if (!r.ok) {
          if (r.status === 404) {
            setReport({ date: d, markdown: null, exists: false });
            setErr("该日尚无日报，点击「强制重新生成」尝试基于影子日志生成。");
            return;
          }
          throw new Error(`HTTP ${r.status}`);
        }
        const j = (await r.json()) as ReportResponse;
        setReport(j);
      } catch (e) {
        setErr((e as Error).message);
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    fetchList();
  }, [fetchList]);

  useEffect(() => {
    if (date) fetchReport(date);
  }, [date, fetchReport]);

  const aiPrompt = useMemo(
    () => (report?.markdown ? extractAiPrompt(report.markdown) : ""),
    [report],
  );

  const copyText = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyMsg(`✅ 已复制${label}`);
      setTimeout(() => setCopyMsg(""), 2000);
    } catch {
      setCopyMsg("❌ 复制失败，请手动选中");
      setTimeout(() => setCopyMsg(""), 2000);
    }
  };

  const availableDates = useMemo(() => {
    if (!list) return [];
    const set = new Set<string>();
    list.reports.forEach((r) => set.add(r.date));
    list.shadow_dates.forEach((d) => set.add(d));
    return Array.from(set).sort().reverse();
  }, [list]);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200">
      <div className="max-w-5xl mx-auto px-4 py-6">
        {/* 顶部 */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <Link
              href="/"
              className="text-sm text-slate-400 hover:text-slate-200"
            >
              ← 返回大屏
            </Link>
            <h1 className="text-xl font-bold">📊 趋势衰竭 · 准确率日报</h1>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-sm"
            >
              {availableDates.length === 0 && (
                <option value="">（无可用数据）</option>
              )}
              {availableDates.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
            <button
              onClick={() => fetchReport(date, true)}
              disabled={!date || loading}
              className="rounded border border-slate-700 bg-slate-900 hover:bg-slate-800 px-2 py-1 text-xs text-slate-300 disabled:opacity-50"
              title="基于影子日志重新跑打标脚本"
            >
              🔄 强制重新生成
            </button>
          </div>
        </div>

        {/* 影子日志状态 */}
        {list && (
          <div className="mb-4 rounded-md border border-slate-800 bg-slate-900/60 px-3 py-2 text-[11px] text-slate-400 flex flex-wrap items-center gap-3">
            <span>
              影子日志：
              <span
                className={
                  list.logger_stats.started
                    ? "text-emerald-300"
                    : "text-amber-300"
                }
              >
                {list.logger_stats.started ? "运行中" : "未启动"}
              </span>
            </span>
            <span>已写 <span className="text-slate-200">{list.logger_stats.written}</span> 条</span>
            {list.logger_stats.dropped > 0 && (
              <span className="text-amber-300">丢弃 {list.logger_stats.dropped}</span>
            )}
            <span>队列 {list.logger_stats.queue_size}</span>
            <span>影子覆盖：{list.shadow_dates.length} 天</span>
            <span>日报：{list.reports.length} 份</span>
          </div>
        )}

        {/* 错误 / 加载 */}
        {err && (
          <div className="mb-3 rounded-md border border-amber-700/60 bg-amber-900/20 px-3 py-2 text-sm text-amber-200">
            ⚠ {err}
          </div>
        )}
        {loading && <div className="text-sm text-slate-500">加载中…</div>}

        {/* 复制工具栏 */}
        {report?.markdown && (
          <div className="sticky top-0 z-10 -mx-4 px-4 py-2 bg-slate-950/95 backdrop-blur border-b border-slate-800 flex items-center gap-2 flex-wrap">
            <button
              onClick={() => copyText(aiPrompt, "AI Prompt")}
              disabled={!aiPrompt}
              className="rounded-md bg-blue-600 hover:bg-blue-500 px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
              title="复制「发给 AI 复核用 Prompt」区块，粘贴到 Cursor/ChatGPT"
            >
              📋 复制 AI Prompt
            </button>
            <button
              onClick={() => copyText(report.markdown || "", "全文")}
              className="rounded-md border border-slate-700 bg-slate-900 hover:bg-slate-800 px-3 py-1 text-xs text-slate-200"
              title="复制整份 Markdown 日报"
            >
              📄 复制全文
            </button>
            {copyMsg && (
              <span className="text-xs text-emerald-300">{copyMsg}</span>
            )}
            <span className="ml-auto text-[11px] text-slate-500">
              把复制的内容直接粘贴给 AI，就能做事后复核与调优建议
            </span>
          </div>
        )}

        {/* Markdown 正文 */}
        {report?.markdown ? (
          <div className="mt-4">
            <MarkdownView md={report.markdown} />
          </div>
        ) : (
          !loading && (
            <div className="mt-10 text-center text-slate-500">
              <div className="text-4xl mb-2">📝</div>
              <div>
                {availableDates.length === 0
                  ? "暂无影子日志或日报，模块刚上线，请等待 1-2 小时采集数据后刷新。"
                  : "该日期下尚无日报，可点击「强制重新生成」。"}
              </div>
            </div>
          )
        )}
      </div>
    </div>
  );
}
