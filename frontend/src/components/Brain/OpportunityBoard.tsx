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
