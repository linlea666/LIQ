"use client";

import type { TradeSetupCandidate } from "@/lib/types";
import SetupCard from "./SetupCard";

interface Props {
  opportunities: TradeSetupCandidate[];
  coin: string;
  onSelectZone?: (zoneId: string) => void;
}

const COL_DEFS: Array<{
  key: string;
  title: string;
  states: string[];
  tone: string;
}> = [
  { key: "wait", title: "等待触发", states: ["forming", "waiting_for_trigger"], tone: "border-blue-800/60" },
  { key: "trig", title: "已触发", states: ["triggered", "confirmation_pending", "confirmed"], tone: "border-amber-800/60" },
  { key: "end",  title: "失效 / 冷却 / 已取消", states: ["invalidated", "cancelled", "missed", "cooldown"], tone: "border-slate-800" },
];

export default function OpportunityBoard({
  opportunities, coin, onSelectZone,
}: Props) {
  const cols = COL_DEFS.map((c) => ({
    ...c,
    items: opportunities.filter((o) => c.states.includes(o.state.name)),
  }));

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-baseline justify-between border-b border-slate-800 px-3 py-1.5">
        <h3 className="text-[10px] uppercase tracking-wider text-slate-500">
          机会雷达
        </h3>
        <span className="text-[10px] text-slate-600">
          {opportunities.length} 项观察区 · 不构成交易指令
        </span>
      </header>
      <div className="flex flex-1 min-h-0 flex-col gap-2 overflow-y-auto p-2">
        {/* P1-B：全部 0 项时显示解释文案，避免用户误以为系统坏了 */}
        {opportunities.length === 0 && (
          <section className="rounded border border-slate-700/50 bg-slate-900/40 p-2.5">
            <h4 className="text-[11px] font-medium text-slate-300">
              当前无符合「高盈亏比」门槛的观察机会
            </h4>
            <p className="mt-1.5 text-[10px] leading-snug text-slate-400">
              系统已开启严格门槛：T1 RR ≥ 2.0 / 信任分 ≥ 0.7 / 数据置信度 ≥ 0.75。
              0 项不代表「行情没机会」，而是当前结构未达到值得限价试错或扫破观察的水平。
            </p>
            <ul className="mt-2 space-y-0.5 text-[10px] leading-snug text-slate-500">
              <li>· 距离最近强 zone 的 RR 不够（最近 zone 太近或方向不对）</li>
              <li>· 信任分未达门槛（缺少现货墙 / 双源 / Coinbase 共振硬证据）</li>
              <li>· 数据置信度未达门槛（部分数据源 stale 或 missing）</li>
              <li>· regime 与方向冲突（trend_down 屏蔽做多 / trend_up 屏蔽做空）</li>
            </ul>
            <p className="mt-2 text-[10px] leading-snug text-emerald-400/80">
              建议：等结构形成（关注 ZoneDetailCard 的「硬证据 chip」出现 ★ 机构 / 双源 /
              Coinbase 共振时再观察）
            </p>
          </section>
        )}
        {cols.map((col) => (
          <section key={col.key} className={`rounded border ${col.tone} bg-slate-900/30 p-1.5`}>
            <div className="mb-1 flex items-baseline justify-between px-0.5">
              <h4 className="text-[10px] font-medium text-slate-400">{col.title}</h4>
              <span className="text-[9px] text-slate-600">{col.items.length}</span>
            </div>
            {col.items.length === 0 ? (
              <p className="px-0.5 py-1 text-[10px] text-slate-600">无</p>
            ) : (
              <ul className="space-y-1.5">
                {col.items.map((o) => (
                  <li key={o.setup_id}>
                    <SetupCard setup={o} coin={coin} onSelectZone={onSelectZone} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
