"use client";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";

interface LogEntry {
  ts: number;
  time: string;
  level: string;
  name: string;
  msg: string;
}

const LEVEL_COLORS: Record<string, string> = {
  INFO: "text-blue-400",
  WARNING: "text-yellow-400",
  ERROR: "text-red-400",
  DEBUG: "text-slate-500",
};

export default function LogsPage() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [levelFilter, setLevelFilter] = useState<string>("");
  const [keyword, setKeyword] = useState<string>("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [health, setHealth] = useState<{
    status: string;
    sources: { name: string; status: string; latency_ms: number; error_count: number }[];
    smc_monitor?: {
      status: string;
      nansen_enabled: boolean;
      market_breadth: { status: string; score: number; ts: number };
      coins: Array<{
        coin: string;
        nansen_perp_age_sec: number | null;
        nansen_flow_age_sec: number | null;
        nansen_errors: Record<string, string>;
        intraday: null | {
          observation: string;
          setup_state: string;
          confidence: number;
          data_quality: string;
          data_quality_score: number;
          missing: string[];
          degraded: string[];
          zones: number;
          liquidity_pools: number;
        };
        swing: null | {
          observation: string;
          setup_state: string;
          confidence: number;
          data_quality: string;
          data_quality_score: number;
          missing: string[];
          degraded: string[];
          zones: number;
          liquidity_pools: number;
        };
      }>;
    };
    strategic_available: boolean;
    ai_provider: string;
  } | null>(null);

  const fetchLogs = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: "300" });
      if (levelFilter) params.set("level", levelFilter);
      if (keyword) params.set("keyword", keyword);
      const res = await fetch(`${API_BASE}/api/logs?${params}`);
      if (res.ok) {
        const data = await res.json();
        setLogs(data.logs);
      }
    } catch { /* silent */ }
  }, [levelFilter, keyword]);

  const fetchHealth = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/health`);
      if (res.ok) setHealth(await res.json());
    } catch { /* silent */ }
  }, []);

  useEffect(() => {
    const firstRun = window.setTimeout(() => {
      void fetchLogs();
      void fetchHealth();
    }, 0);
    if (!autoRefresh) {
      return () => window.clearTimeout(firstRun);
    }
    const t1 = setInterval(fetchLogs, 3000);
    const t2 = setInterval(fetchHealth, 10000);
    return () => {
      window.clearTimeout(firstRun);
      clearInterval(t1);
      clearInterval(t2);
    };
  }, [fetchLogs, fetchHealth, autoRefresh]);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300">
      <header className="border-b border-slate-700 bg-slate-900/80 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link href="/" className="text-blue-400 hover:text-blue-300 text-sm">← 返回大屏</Link>
          <h1 className="text-lg font-bold text-white">LIQ 运行日志</h1>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <label className="flex items-center gap-1 cursor-pointer">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="rounded"
            />
            <span>自动刷新 (3s)</span>
          </label>
          <button
            onClick={fetchLogs}
            className="px-3 py-1 bg-slate-700 hover:bg-slate-600 rounded text-white"
          >
            刷新
          </button>
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-6 py-4 space-y-4">
        {/* System Status */}
        {health && (
          <div className="bg-slate-900 border border-slate-700 rounded-lg p-4">
            <h2 className="text-sm font-semibold text-white mb-2">系统状态</h2>
            <div className="flex flex-wrap gap-4 text-xs">
              <span>引擎: <span className="text-green-400">{health.status}</span></span>
              <span>Strategic: <span className={health.strategic_available ? "text-green-400" : "text-red-400"}>
                {health.strategic_available ? `可用 (${health.ai_provider})` : "不可用"}
              </span></span>
              {health.sources?.map((s) => (
                <span key={s.name}>
                  {s.name}: <span className={
                    s.status === "connected" ? "text-green-400" :
                    s.status === "degraded" ? "text-yellow-400" : "text-red-400"
                  }>{s.status}</span>
                  {s.latency_ms > 0 && <span className="text-slate-600"> ({s.latency_ms.toFixed(0)}ms)</span>}
                  {s.error_count > 0 && <span className="text-red-400"> err:{s.error_count}</span>}
                </span>
              ))}
            </div>
            {health.smc_monitor && (
              <div className="mt-4 border-t border-slate-800 pt-3">
                <div className="mb-2 flex items-center justify-between gap-3">
                  <h3 className="text-xs font-semibold text-slate-200">SMC 落地监测</h3>
                  <div className="text-[11px] text-slate-500">
                    Nansen {health.smc_monitor.nansen_enabled ? "enabled" : "missing"} · breadth {health.smc_monitor.market_breadth.status}
                    {typeof health.smc_monitor.market_breadth.score === "number" && (
                      <span> ({health.smc_monitor.market_breadth.score.toFixed(1)})</span>
                    )}
                  </div>
                </div>
                <div className="grid gap-2 md:grid-cols-2">
                  {health.smc_monitor.coins.map((c) => (
                    <div key={c.coin} className="border border-slate-800 bg-slate-950/40 p-2">
                      <div className="mb-2 flex items-center justify-between">
                        <span className="text-xs font-semibold text-white">{c.coin}</span>
                        <Link href={`/smc/${c.coin}`} className="text-[11px] text-blue-400 hover:text-blue-300">
                          打开 SMC
                        </Link>
                      </div>
                      <div className="grid grid-cols-2 gap-2 text-[11px]">
                        {(["intraday", "swing"] as const).map((h) => {
                          const snap = c[h];
                          return (
                            <div key={h} className="bg-slate-900/70 p-2">
                              <div className="mb-1 text-slate-500">{h === "intraday" ? "日内" : "波段"}</div>
                              {snap ? (
                                <>
                                  <div>{snap.observation} · {snap.setup_state}</div>
                                  <div className="text-slate-500">
                                    conf {snap.confidence} · dq {snap.data_quality}/{snap.data_quality_score}
                                  </div>
                                  <div className="text-slate-500">
                                    zones {snap.zones} · pools {snap.liquidity_pools}
                                  </div>
                                  {(snap.missing.length > 0 || snap.degraded.length > 0) && (
                                    <div className="mt-1 truncate text-yellow-400" title={[...snap.missing, ...snap.degraded].join(", ")}>
                                      {[...snap.missing, ...snap.degraded].join(", ")}
                                    </div>
                                  )}
                                </>
                              ) : (
                                <span className="text-red-400">未生成</span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                      <div className="mt-2 text-[10px] text-slate-600">
                        perp age {c.nansen_perp_age_sec ?? "-"}s · flow age {c.nansen_flow_age_sec ?? "-"}s
                        {Object.keys(c.nansen_errors || {}).length > 0 && (
                          <span className="text-yellow-400"> · {Object.entries(c.nansen_errors).map(([k, v]) => `${k}:${v}`).join(", ")}</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Filters */}
        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-500">级别:</span>
          {["", "INFO", "WARNING", "ERROR"].map((l) => (
            <button
              key={l}
              onClick={() => setLevelFilter(l)}
              className={`px-2 py-0.5 text-xs rounded ${
                levelFilter === l ? "bg-blue-600 text-white" : "bg-slate-800 text-slate-400 hover:text-white"
              }`}
            >
              {l || "全部"}
            </button>
          ))}
          <input
            type="text"
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索关键词..."
            className="ml-2 px-3 py-1 bg-slate-800 border border-slate-700 rounded text-xs text-white placeholder:text-slate-600 w-48"
          />
          <span className="ml-auto text-xs text-slate-600">{logs.length} 条</span>
        </div>

        {/* Log Table */}
        <div className="bg-slate-900 border border-slate-700 rounded-lg overflow-hidden">
          <div className="max-h-[calc(100vh-280px)] overflow-y-auto font-mono text-xs">
            {logs.length === 0 ? (
              <div className="p-8 text-center text-slate-600">暂无日志</div>
            ) : (
              logs.map((log, i) => (
                <div
                  key={i}
                  className={`flex gap-3 px-3 py-1 border-b border-slate-800/50 hover:bg-slate-800/30 ${
                    log.level === "ERROR" ? "bg-red-950/20" :
                    log.level === "WARNING" ? "bg-yellow-950/10" : ""
                  }`}
                >
                  <span className="text-slate-600 whitespace-nowrap shrink-0 w-[170px]">
                    {log.time.slice(1, 20)}
                  </span>
                  <span className={`w-[60px] shrink-0 font-bold ${LEVEL_COLORS[log.level] || "text-slate-400"}`}>
                    {log.level}
                  </span>
                  <span className="text-slate-500 w-[140px] shrink-0 truncate">{log.name}</span>
                  <span className="text-slate-300 break-all">{log.msg}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
