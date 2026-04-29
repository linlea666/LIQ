import type { BrainPriceZone } from "@/lib/types";
import { formatPrice } from "@/lib/format";
import { ROLE_COLORS } from "./types";

interface Props {
  zone: BrainPriceZone | null;
  coin: string;
}

function ScoreBar({ label, value }: { label: string; value: number }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return (
    <div>
      <div className="flex items-baseline justify-between text-[10px] text-slate-500">
        <span>{label}</span>
        <span className="tabular-nums text-slate-300">{value.toFixed(2)}</span>
      </div>
      <div className="mt-0.5 h-1 w-full overflow-hidden rounded bg-slate-800">
        <div
          className="h-full bg-blue-500/70"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function ZoneDetailCard({ zone, coin }: Props) {
  if (!zone) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
        在左侧价格轴选中一个价格区查看详情
      </div>
    );
  }
  const role = ROLE_COLORS[zone.dominant_role] ?? ROLE_COLORS.other;
  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3">
      <header className="space-y-1">
        <div className="flex items-baseline justify-between">
          <h3 className="font-mono text-base font-semibold text-slate-100">
            {formatPrice(zone.price_mid, coin)}
          </h3>
          <span className="text-[11px] text-slate-500 tabular-nums">
            距现价 {zone.distance_pct >= 0 ? "+" : ""}
            {zone.distance_pct.toFixed(2)}%
          </span>
        </div>
        <div className="flex items-center gap-2 text-[11px]">
          <span
            className={`rounded border px-1.5 py-0.5 ${role.border} ${role.bg} ${role.text}`}
          >
            {role.label}
          </span>
          <span className="text-slate-400">{zone.dominant_label}</span>
        </div>
        <p className="text-[10px] tabular-nums text-slate-600">
          [{zone.price_low.toFixed(2)} – {zone.price_high.toFixed(2)}]
        </p>
      </header>

      <section className="grid grid-cols-2 gap-x-4 gap-y-2">
        <ScoreBar label="支撑信任" value={zone.support_trust} />
        <ScoreBar label="阻力信任" value={zone.resistance_trust} />
        <ScoreBar label="扫单吸引" value={zone.sweep_attractiveness} />
        <ScoreBar label="打穿风险评分" value={zone.break_through_risk} />
        <ScoreBar label="数据可信度" value={zone.data_confidence} />
      </section>

      {zone.layer_notes.length > 0 && (
        <section>
          <h4 className="text-[10px] uppercase tracking-wider text-slate-500">分层说明</h4>
          <ul className="mt-1 space-y-0.5 text-[11px] text-slate-400">
            {zone.layer_notes.map((n) => (
              <li key={n} className="leading-snug">{n}</li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h4 className="text-[10px] uppercase tracking-wider text-slate-500">证据链</h4>
        <ul className="mt-1 space-y-1 text-[11px] text-slate-300">
          {zone.evidence.map((e, i) => (
            <li
              key={`${zone.zone_id}-ev-${i}`}
              className="rounded border border-slate-800 bg-slate-900/50 px-2 py-1 leading-snug"
            >
              {e}
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h4 className="text-[10px] uppercase tracking-wider text-slate-500">情景</h4>
        <dl className="mt-1 space-y-1 text-[11px] text-slate-400">
          <div>
            <dt className="text-emerald-400">守住</dt>
            <dd className="mt-0.5">{zone.scenario.if_hold}</dd>
          </div>
          <div>
            <dt className="text-rose-400">失守</dt>
            <dd className="mt-0.5">{zone.scenario.if_break}</dd>
          </div>
          <div>
            <dt className="text-slate-500">失效条件</dt>
            <dd className="mt-0.5">{zone.scenario.invalidates_if}</dd>
          </div>
        </dl>
      </section>
    </div>
  );
}
