"use client";

/**
 * V1 vs V2 关键位行为对比页面（V3-M3 · 2026-04）
 *
 * 路由：/levels/[coin]/v1v2-compare
 *
 * 数据流：
 *   GET /api/key-levels/v1v2-stats/{coin}?window_hours=...&tier=...&v2_threshold=...
 *   → V1V2StatsResponse → 三张 V1V2CompareCard
 *
 * 入口：从 /levels/[coin] 全景页顶部 "⚖ V1/V2 对比" 按钮跳转。
 *
 * 设计：
 *   - 顶部参数区：window / tier / v2_threshold 可调（即时重新拉取）
 *   - 主体：3 张维度卡片（反弹质量 / 突破阶段 / 假破回收）
 *   - 顶部摘要：样本数 / 历史快照数 / 推荐切换的维度数
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";
import V1V2CompareCard from "@/components/Levels/V1V2CompareCard";
import type { V1V2StatsResponse } from "@/lib/types";

const TIER_OPTIONS = ["S", "A", "B", "C"] as const;
const WINDOW_OPTIONS = [
  { label: "1h", hours: 1 },
  { label: "4h", hours: 4 },
  { label: "12h", hours: 12 },
  { label: "24h", hours: 24 },
];

export default function V1V2ComparePage() {
  const params = useParams();
  const coin = (params.coin as string)?.toUpperCase() ?? "BTC";

  // ── 控制参数 ──
  const [windowHours, setWindowHours] = useState(4);
  const [tierFilter, setTierFilter] = useState<string[]>([]);  // 空 = 全部
  const [v2Threshold, setV2Threshold] = useState(0.5);

  const [data, setData] = useState<V1V2StatsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const queryString = useMemo(() => {
    const sp = new URLSearchParams();
    sp.set("window_hours", String(windowHours));
    sp.set("v2_threshold", String(v2Threshold));
    if (tierFilter.length > 0) sp.set("tier", tierFilter.join(","));
    return sp.toString();
  }, [windowHours, v2Threshold, tierFilter]);

  // 手动刷新通过 nonce 触发 effect 重跑（避免在 effect 内调用单独的 load 函数，
  // 这是 react-hooks/set-state-in-effect 推荐的写法）。
  const [refreshNonce, setRefreshNonce] = useState(0);
  const reload = useCallback(() => setRefreshNonce((n) => n + 1), []);

  // 标准数据获取模式（fetch on mount + 参数变化重拉 + 取消旧请求），
  // 项目尚未引入 SWR / React Query；此处沿用其它页面（如 [coin]/page.tsx）的写法。
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    const ctrl = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError("");
    fetch(`${API_BASE}/api/key-levels/v1v2-stats/${coin}?${queryString}`, {
      signal: ctrl.signal,
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<V1V2StatsResponse>;
      })
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled || ctrl.signal.aborted) return;
        setError(e instanceof Error ? e.message : "加载失败");
        setLoading(false);
      });
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [coin, queryString, refreshNonce]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const toggleTier = (t: string) => {
    setTierFilter((prev) =>
      prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t]
    );
  };

  const sigBetterCount = data
    ? [
        data.stats.bounce_quality.is_v2_significantly_better,
        data.stats.breakout_stage.is_v2_significantly_better,
        data.stats.fake_break.is_v2_significantly_better,
      ].filter(Boolean).length
    : 0;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur border-b border-slate-800">
        <div className="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <Link
              href={`/levels/${coin}`}
              className="text-blue-400 hover:text-blue-300 text-sm shrink-0"
            >
              ← 返回 {coin} 全景
            </Link>
            <div>
              <h1 className="text-lg font-bold text-white">
                {coin} V1 / V2 行为评估对比
              </h1>
              <div className="text-xs text-slate-500 mt-0.5">
                M2.5 双轨观测层 → M3 数据驱动决策
              </div>
            </div>
          </div>
          <button
            onClick={reload}
            disabled={loading}
            className="px-3 py-1 text-xs border border-slate-600 rounded hover:border-slate-400 hover:text-white transition disabled:opacity-50"
          >
            {loading ? "刷新中..." : "刷新"}
          </button>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-6 space-y-6">
        {/* 参数控制区 */}
        <section className="rounded-lg border border-slate-700/40 bg-slate-900/40 p-4">
          <h2 className="text-sm font-semibold text-white mb-3">回测参数</h2>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div>
              <div className="text-[11px] text-slate-500 mb-1">事后窗口（事件 N 小时后判真相）</div>
              <div className="flex gap-1">
                {WINDOW_OPTIONS.map((w) => (
                  <button
                    key={w.hours}
                    onClick={() => setWindowHours(w.hours)}
                    className={`px-2 py-1 text-xs rounded border transition ${
                      windowHours === w.hours
                        ? "border-purple-500 bg-purple-500/20 text-purple-200"
                        : "border-slate-700 text-slate-400 hover:border-slate-500"
                    }`}
                  >
                    {w.label}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <div className="text-[11px] text-slate-500 mb-1">强度过滤（多选；留空=全部）</div>
              <div className="flex gap-1">
                {TIER_OPTIONS.map((t) => (
                  <button
                    key={t}
                    onClick={() => toggleTier(t)}
                    className={`px-2 py-1 text-xs rounded border transition w-9 ${
                      tierFilter.includes(t)
                        ? "border-purple-500 bg-purple-500/20 text-purple-200"
                        : "border-slate-700 text-slate-400 hover:border-slate-500"
                    }`}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <div className="text-[11px] text-slate-500 mb-1">
                V2 二分类阈值: <span className="text-slate-300 font-mono">{v2Threshold.toFixed(2)}</span>
              </div>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={v2Threshold}
                onChange={(e) => setV2Threshold(parseFloat(e.target.value))}
                className="w-full accent-purple-500"
              />
            </div>
          </div>
        </section>

        {/* 状态摘要 */}
        {error && (
          <section className="rounded border border-rose-700/40 bg-rose-900/20 p-3 text-sm text-rose-300">
            加载失败: {error}
          </section>
        )}

        {data && !error && (
          <>
            <section className="rounded-lg border border-slate-700/40 bg-slate-900/40 p-4">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
                <Stat label="历史快照数" value={data.history_size ?? 0} />
                <Stat label="配对样本总数" value={data.total_records} />
                <Stat
                  label="V2 显著优胜维度"
                  value={`${sigBetterCount} / 3`}
                  cls={sigBetterCount > 0 ? "text-emerald-300" : "text-slate-300"}
                />
                <Stat
                  label="过滤"
                  value={
                    (data.tier_filter.length > 0 ? data.tier_filter.join(",") : "全部") +
                    " · " + (data.params.future_window_sec / 3600) + "h"
                  }
                />
              </div>
              {(data.history_size ?? 0) === 0 && (
                <div className="mt-3 p-2 rounded bg-amber-900/15 text-amber-300 text-[11px]">
                  ⚠ 暂无历史快照。请等待 _auto_kl_snapshot_loop 持续追加，或在线运行系统至少 1 小时。
                </div>
              )}
              {(data.history_size ?? 0) > 0 && data.total_records < 30 && (
                <div className="mt-3 p-2 rounded bg-amber-900/15 text-amber-300 text-[11px]">
                  ⏳ 配对样本不足 30 条，所有维度结论均不可信；需更长的历史积累。
                </div>
              )}
            </section>

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <V1V2CompareCard stats={data.stats.bounce_quality} />
              <V1V2CompareCard stats={data.stats.breakout_stage} />
              <V1V2CompareCard stats={data.stats.fake_break} />
            </div>

            <section className="rounded-lg border border-slate-700/40 bg-slate-900/40 p-4 text-[11px] text-slate-500 leading-relaxed">
              <h3 className="text-sm font-semibold text-slate-300 mb-2">📖 怎么解读这个页面</h3>
              <ul className="space-y-1 list-disc list-inside">
                <li><b>样本不足 (n &lt; 30)</b>：结论不可信；需更长时间积累或扩大 tier 过滤</li>
                <li><b>V2 显著优于 V1</b>：Δaccuracy ≥ 0.05 且 χ² 检验 p &lt; 0.05；可考虑 M4 切换</li>
                <li><b>χ² / p-value</b>：检验 V1 vs V2 准确率差异是否显著（p &lt; 0.05 = 显著）</li>
                <li><b>剔除模糊样本</b>：未来价格仅小幅偏移（&lt; 0.3×ATR）的样本不计入分母，避免噪声</li>
                <li>本页**不影响**实盘信号；切换决策由 M4 阶段在统计稳定后人工拍板</li>
              </ul>
            </section>
          </>
        )}
      </main>
    </div>
  );
}

function Stat({
  label, value, cls = "text-slate-300",
}: { label: string; value: string | number; cls?: string }) {
  return (
    <div>
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className={`text-base font-semibold mt-0.5 ${cls}`}>{value}</div>
    </div>
  );
}
