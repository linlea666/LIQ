"use client";

import { useState } from "react";
import type { TradeSetupCandidate } from "@/lib/types";
import { formatPrice } from "@/lib/format";
import { translateDirection, translateSetupState } from "./types";

interface Props {
  setup: TradeSetupCandidate;
  coin: string;
  onSelectZone?: (zoneId: string) => void;
}

const SETUP_TYPE_CN: Record<string, string> = {
  support_limit_probe: "支撑限价试错",
  resistance_limit_probe: "阻力限价试错",
  fake_break_reclaim_long: "扫破支撑收回",
  fake_break_reclaim_short: "扫破阻力回落",
};

const DIR_TONE: Record<string, string> = {
  long: "border-emerald-700/60 bg-emerald-950/30 text-emerald-200",
  short: "border-rose-700/60 bg-rose-950/30 text-rose-200",
  neutral: "border-slate-700/60 bg-slate-800/40 text-slate-300",
};

function ScoreDot({ score }: { score: number }) {
  const clamped = Math.max(0, Math.min(1, score));
  const hue = Math.round(clamped * 120);
  return (
    <div className="flex w-full items-center gap-1.5">
      <div className="h-1.5 flex-1 overflow-hidden rounded bg-slate-800">
        <div
          className="h-full rounded"
          style={{
            width: `${clamped * 100}%`,
            background: `hsl(${hue}, 70%, 50%)`,
          }}
        />
      </div>
      <span className="font-mono text-[10px] tabular-nums text-slate-300">
        {score.toFixed(2)}
      </span>
    </div>
  );
}

export default function SetupCard({ setup, coin, onSelectZone }: Props) {
  const [expanded, setExpanded] = useState(false);
  const dirCn = translateDirection(setup.direction);
  const cn = SETUP_TYPE_CN[setup.setup_type] ?? setup.setup_type;
  const t1 = setup.targets[0];

  return (
    <article className="rounded border border-slate-800 bg-slate-900/70 p-2 text-[11px]">
      <header className="flex items-center justify-between gap-1">
        <div className="flex items-center gap-1.5">
          <span className={`rounded border px-1.5 py-0.5 text-[10px] ${DIR_TONE[setup.direction]}`}>
            {dirCn}
          </span>
          <span className="text-slate-400">{cn}</span>
        </div>
        <span className="text-[9px] text-slate-600">
          {translateSetupState(setup.state.name)}
        </span>
      </header>

      <button
        type="button"
        onClick={() => onSelectZone?.(setup.zone_id)}
        className="mt-1 block w-full text-left font-mono text-[12px] tabular-nums text-slate-200 hover:text-blue-300"
      >
        入场 {setup.entry_styles[0]?.entry_zone[0].toFixed(0)}
        {" – "}
        {setup.entry_styles[0]?.entry_zone[1].toFixed(0)}
      </button>

      <div className="mt-1 grid grid-cols-2 gap-x-2 text-[10px] text-slate-500 tabular-nums">
        <div>
          <div className="text-rose-400/80">硬止损</div>
          <div className="font-mono text-slate-300">{formatPrice(setup.risk_plan.hard_stop, coin)}</div>
        </div>
        {t1 && (
          <div>
            <div className="text-emerald-400/80">T1（RR {t1.rr.toFixed(1)}）</div>
            <div className="font-mono text-slate-300">{formatPrice(t1.price, coin)}</div>
          </div>
        )}
      </div>

      <div className="mt-1 space-y-1">
        <div className="flex items-baseline gap-1.5">
          <span className="w-12 text-[9px] uppercase tracking-wider text-slate-500">机会</span>
          <ScoreDot score={setup.opportunity_score} />
        </div>
        <div className="flex items-baseline gap-1.5">
          <span className="w-12 text-[9px] uppercase tracking-wider text-slate-500">不对称</span>
          <ScoreDot score={setup.asymmetry_score} />
        </div>
      </div>

      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="mt-1.5 w-full rounded border border-slate-800 px-2 py-0.5 text-[10px] text-slate-500 hover:text-slate-300"
      >
        {expanded ? "收起 ▲" : `展开（双方案 / 目标 / 取消条件）▼`}
      </button>

      {expanded && (
        <div className="mt-1.5 space-y-2 border-t border-slate-800 pt-1.5">
          <section>
            <h5 className="text-[9px] uppercase tracking-wider text-slate-500">两套观察方案</h5>
            <ul className="mt-1 space-y-1">
              {setup.entry_styles.map((s) => (
                <li key={s.style} className="rounded border border-slate-800 px-1.5 py-1">
                  <div className="flex items-center justify-between text-[10px]">
                    <span className="text-slate-300">
                      {s.style === "aggressive" ? "激进" : "稳健"}
                    </span>
                    <span className="font-mono text-slate-400 tabular-nums">
                      {s.entry_zone[0].toFixed(0)} – {s.entry_zone[1].toFixed(0)}
                    </span>
                  </div>
                  {s.requires.length > 0 && (
                    <ul className="mt-0.5 list-inside list-disc text-[10px] text-slate-500">
                      {s.requires.map((r) => <li key={r}>{r}</li>)}
                    </ul>
                  )}
                  {s.risk_note && (
                    <p className="mt-0.5 text-[10px] text-amber-300/80">⚠ {s.risk_note}</p>
                  )}
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h5 className="text-[9px] uppercase tracking-wider text-slate-500">目标</h5>
            <ul className="mt-1 grid grid-cols-1 gap-0.5 text-[10px] tabular-nums">
              {setup.targets.map((t, i) => (
                <li key={`${setup.setup_id}-t${i}`} className="flex justify-between text-slate-400">
                  <span>T{i + 1} · {t.note}</span>
                  <span className="font-mono text-slate-300">
                    {formatPrice(t.price, coin)} · RR {t.rr.toFixed(1)}
                  </span>
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h5 className="text-[9px] uppercase tracking-wider text-slate-500">失效结构</h5>
            <ul className="mt-1 space-y-0.5 text-[10px] text-slate-400 tabular-nums">
              <li>软失效：{formatPrice(setup.risk_plan.soft_invalidation, coin)}</li>
              <li>硬止损：{formatPrice(setup.risk_plan.hard_stop, coin)}</li>
              <li className="text-slate-500">{setup.risk_plan.structural_invalidation}</li>
            </ul>
          </section>

          <section>
            <h5 className="text-[9px] uppercase tracking-wider text-slate-500">
              取消条件（人工参考 · 系统仅自动执行硬止损 / Regime 反转 / 墙撤出）
            </h5>
            <ul className="mt-1 flex flex-wrap gap-1">
              {setup.cancel_conditions.map((c) => (
                <li
                  key={c}
                  className="rounded bg-slate-800/60 px-1.5 py-0.5 text-[10px] text-slate-400"
                >
                  {c}
                </li>
              ))}
            </ul>
          </section>

          {setup.state.pending_reason && (
            <p className="text-[10px] text-slate-500">
              当前等待：{setup.state.pending_reason}
            </p>
          )}
        </div>
      )}
    </article>
  );
}
