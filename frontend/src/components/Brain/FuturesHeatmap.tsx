"use client";

/**
 * Phase C · 合约流动性堆积模块
 * ------------------------------------------------------------
 * 数据源：TradingBrainSnapshot.fut_book
 *   - bins_above / bins_below：合约侧厚度（current_usd - spot_usd）按距离分桶
 *   - magnets：清算簇 + max_pain（叠加在堆积图上的磁吸目标层）
 *
 * 视觉哲学：
 *   - 横向柱状热力图：每根柱子代表一个 bin，长度 = 合约 USD 厚度
 *   - 现价线居中（上方柱朝右，下方柱朝右；用红/绿色区分上/下方）
 *   - 颜色阶梯：合约厚度按 bin 内最大值归一化 → 0.3 → 0.85 alpha
 *   - 磁铁叠加：在对应 bin 行尾部画一个磁铁标记（◆ + 距离 + USD）
 *   - 极致简洁：不显示交易指令、不重新打分；点击 wall_zone_id 联动选中
 */
import { useMemo, useState } from "react";
import type { BrainFutBin, BrainFutBook, BrainFutMagnet } from "@/lib/types";
import { formatPrice, formatCnUsd } from "@/lib/format";

interface Props {
  futBook: BrainFutBook | null;
  coin: string;
  onSelectZone?: (wallZoneId: string) => void;
}

const BRACKET_LABEL: Record<string, string> = {
  near: "近 · ≤0.5%",
  mid: "中 · 0.5–2%",
  far: "远 · 2–5%",
};

function fmtUsd(usd: number): string {
  if (!usd || usd <= 0) return "—";
  return formatCnUsd(usd);
}

function magnetLabel(m: BrainFutMagnet): string {
  switch (m.magnet_kind) {
    case "liq_cluster": return "清算簇";
    case "max_pain_long": return "多头痛点";
    case "max_pain_short": return "空头痛点";
    case "leverage_magnet": return "杠杆磁铁";
    default: return "磁铁";
  }
}

function HeatRow({
  bin, coin, maxUsd, attachedMagnet, onSelectZone,
}: {
  bin: BrainFutBin;
  coin: string;
  maxUsd: number;
  attachedMagnet: BrainFutMagnet | null;
  onSelectZone?: (id: string) => void;
}) {
  const len = maxUsd > 0 ? Math.max(2, (bin.futures_usd / maxUsd) * 100) : 0;
  const isAsk = bin.side === "ask";

  // 合约厚度归一化决定颜色强度（0.30–0.85）
  const intensity = maxUsd > 0
    ? Math.max(0.30, Math.min(0.85, 0.30 + (bin.futures_usd / maxUsd) * 0.55))
    : 0.30;
  const baseColor = isAsk ? "239, 68, 68" : "16, 185, 129"; // rose / emerald RGB
  const fill = `rgba(${baseColor}, ${intensity})`;

  const sweepHigh = bin.sweep_attractiveness >= 0.55;

  // 档位 2A：长/短窗口对比
  const max1h = bin.max_usd_1h ?? 0;
  const max8h = bin.max_usd_8h ?? 0;
  const pers8h = bin.persistence_score_8h ?? 0;
  const weakerThan1h = max1h > 0 && bin.total_usd < max1h * 0.70;
  const stronger8h = max8h > 0 && max1h > 0 && max8h > max1h * 1.30;

  const titleText =
    `合约堆积 · ${bin.dominant_role}\n` +
    `当前 ${fmtUsd(bin.futures_usd)}（总 ${fmtUsd(bin.total_usd)}）\n` +
    (max1h > 0 ? `1h 峰值 ${fmtUsd(max1h)}　持续 ${(bin.persistence_score * 100).toFixed(0)}%\n` : "") +
    (max8h > 0 ? `8h 峰值 ${fmtUsd(max8h)}　持续 ${(pers8h * 100).toFixed(0)}%\n` : "") +
    `打穿风险 ${(bin.break_through_risk * 100).toFixed(0)}%`;

  return (
    <div
      className={`group flex cursor-pointer items-center gap-2 rounded border border-slate-800/70 bg-slate-900/40 px-2 py-1.5 transition hover:border-slate-600 hover:bg-slate-800/60 ${
        bin.is_attached_magnet ? "border-l-2 border-l-amber-400" : ""
      }`}
      onClick={() => bin.wall_zone_id && onSelectZone?.(bin.wall_zone_id)}
      title={titleText}
    >
      <div className="w-[78px] shrink-0 tabular-nums">
        <div className={`text-[12px] font-semibold ${isAsk ? "text-rose-300" : "text-emerald-300"}`}>
          {formatPrice(bin.price, coin)}
        </div>
        <div className={`text-[10px] ${isAsk ? "text-rose-200/80" : "text-emerald-200/80"}`}>
          {bin.distance_pct >= 0 ? "+" : ""}{bin.distance_pct.toFixed(2)}%
        </div>
      </div>

      <div className="flex-1 min-w-0">
        <div className="relative h-3.5 w-full overflow-hidden rounded bg-slate-800/50">
          <div
            className="h-full rounded transition-all"
            style={{ width: `${len}%`, background: fill }}
          />
          {bin.is_attached_magnet && attachedMagnet && (
            <div
              className="pointer-events-none absolute inset-y-0 flex items-center"
              style={{ left: `calc(${len}% + 4px)` }}
              title={`${magnetLabel(attachedMagnet)} ${formatPrice(attachedMagnet.price, coin)} · ${fmtUsd(attachedMagnet.usd)}`}
            >
              <span className="rounded bg-amber-500/90 px-1 text-[10px] font-bold leading-none text-amber-950">◆</span>
            </div>
          )}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-slate-400">
          <span className="tabular-nums text-slate-300">{fmtUsd(bin.futures_usd)}</span>
          {sweepHigh && (
            <span className="rounded bg-amber-900/60 px-1 text-amber-200">
              扫单磁铁 {(bin.sweep_attractiveness * 100).toFixed(0)}
            </span>
          )}
          {bin.break_through_risk >= 0.55 && (
            <span className="rounded bg-rose-900/60 px-1 text-rose-200">
              易打穿 {(bin.break_through_risk * 100).toFixed(0)}
            </span>
          )}
          {bin.persistence_score >= 0.6 && (
            <span className="rounded border border-slate-700 px-1 text-slate-300">
              持续 {(bin.persistence_score * 100).toFixed(0)}%
            </span>
          )}
          {weakerThan1h && (
            <span
              className="rounded bg-amber-900/50 px-1 text-amber-200"
              title="1h 峰值更厚，墙正在变薄"
            >
              1h 峰 {fmtUsd(max1h)}
            </span>
          )}
          {stronger8h && (
            <span
              className="rounded bg-sky-900/50 px-1 text-sky-200"
              title="过去 8h 历史更厚（中期布墙，结构更稳）"
            >
              8h 峰 {fmtUsd(max8h)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function MagnetItem({ m, coin }: { m: BrainFutMagnet; coin: string }) {
  const isAbove = m.side === "above";
  return (
    <div
      className={`flex items-center gap-2 rounded border px-2 py-1 text-[11px] tabular-nums ${
        isAbove
          ? "border-rose-800/60 bg-rose-950/20 text-rose-200"
          : "border-emerald-800/60 bg-emerald-950/20 text-emerald-200"
      }`}
    >
      <span className="font-bold text-amber-300">◆</span>
      <span className="font-semibold">{formatPrice(m.price, coin)}</span>
      <span className="opacity-70">
        {m.distance_pct >= 0 ? "+" : ""}{m.distance_pct.toFixed(2)}%
      </span>
      <span className="ml-auto rounded bg-slate-900/40 px-1 text-[10px]">
        {magnetLabel(m)}
      </span>
      <span className="text-slate-300">{fmtUsd(m.usd)}</span>
      {m.leverage_hint && (
        <span className="rounded bg-slate-800 px-1 text-[10px]">{m.leverage_hint}</span>
      )}
    </div>
  );
}

function BracketSection({
  bracket, items, coin, maxUsd, magnets, defaultOpen, onSelectZone,
}: {
  bracket: string;
  items: BrainFutBin[];
  coin: string;
  maxUsd: number;
  magnets: BrainFutMagnet[];
  defaultOpen: boolean;
  onSelectZone?: (id: string) => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (!items.length) return null;
  const findMagnet = (b: BrainFutBin): BrainFutMagnet | null => {
    if (!b.is_attached_magnet) return null;
    const tol = Math.max(0.10, Math.abs(b.distance_pct) * 0.05);
    return magnets.find((m) => Math.abs(m.distance_pct - b.distance_pct) <= tol) ?? null;
  };
  return (
    <div className="border-t border-slate-800/60 first:border-t-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-2 py-1 text-left hover:bg-slate-800/40"
      >
        <div className="flex items-center gap-2">
          <span className={`text-[10px] transition ${open ? "rotate-90" : ""}`}>▶</span>
          <span className="text-[11px] font-medium text-slate-200">{BRACKET_LABEL[bracket] ?? bracket}</span>
        </div>
        <span className="text-[10px] tabular-nums text-slate-500">{items.length} 项</span>
      </button>
      {open && (
        <div className="space-y-1 px-2 pb-2">
          {items.map((b) => (
            <HeatRow
              key={`${b.wall_zone_id}-${b.price}`}
              bin={b}
              coin={coin}
              maxUsd={maxUsd}
              attachedMagnet={findMagnet(b)}
              onSelectZone={onSelectZone}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Side({
  title, subtitle, items, coin, maxUsd, magnets, onSelectZone, accentClass,
}: {
  title: string;
  subtitle: string;
  items: BrainFutBin[];
  coin: string;
  maxUsd: number;
  magnets: BrainFutMagnet[];
  onSelectZone?: (id: string) => void;
  accentClass: string;
}) {
  const groups = useMemo(() => {
    const g: Record<string, BrainFutBin[]> = { near: [], mid: [], far: [] };
    for (const it of items) g[it.bracket]?.push(it);
    return g;
  }, [items]);
  return (
    <div className="flex-1 min-w-0">
      <div className={`flex items-center gap-2 border-b px-2 py-1.5 ${accentClass}`}>
        <span className="text-[12px] font-semibold">{title}</span>
        <span className="text-[10px] opacity-70">{subtitle}</span>
        <span className="ml-auto text-[10px] opacity-60 tabular-nums">{items.length}</span>
      </div>
      <BracketSection bracket="near" items={groups.near} coin={coin} maxUsd={maxUsd} magnets={magnets} defaultOpen onSelectZone={onSelectZone} />
      <BracketSection bracket="mid" items={groups.mid} coin={coin} maxUsd={maxUsd} magnets={magnets} defaultOpen onSelectZone={onSelectZone} />
      <BracketSection bracket="far" items={groups.far} coin={coin} maxUsd={maxUsd} magnets={magnets} defaultOpen={false} onSelectZone={onSelectZone} />
    </div>
  );
}

export default function FuturesHeatmap({ futBook, coin, onSelectZone }: Props) {
  const maxUsd = useMemo(() => {
    if (!futBook) return 0;
    const all = [...futBook.bins_above, ...futBook.bins_below];
    if (!all.length) return 0;
    return Math.max(...all.map((x) => x.futures_usd), 1);
  }, [futBook]);

  if (!futBook) {
    return (
      <div className="flex h-full items-center justify-center px-3 py-6 text-[11px] text-slate-500">
        合约堆积数据未就绪
      </div>
    );
  }

  const { bins_above, bins_below, magnets } = futBook;
  const hasData = bins_above.length || bins_below.length || magnets.length;
  if (!hasData) {
    return (
      <div className="flex h-full items-center justify-center px-3 py-6 text-[11px] text-slate-500">
        当前 5% 内无合约堆积或磁铁
      </div>
    );
  }

  // 仅磁铁、无合约 bin 时退化为磁铁列表视图
  if (!bins_above.length && !bins_below.length) {
    const above = magnets.filter((m) => m.side === "above");
    const below = magnets.filter((m) => m.side === "below");
    return (
      <div className="flex h-full flex-col">
        <div className="flex items-center justify-between border-b border-slate-800 bg-slate-900/40 px-3 py-1.5">
          <span className="text-[12px] font-semibold text-slate-100">合约 · 仅磁铁</span>
          <span className="text-[10px] text-slate-500">挂单热力图未就绪</span>
        </div>
        <div className="flex flex-1 min-h-0 gap-2 overflow-y-auto px-2 py-2">
          <div className="flex-1 space-y-1">
            <div className="text-[10px] uppercase tracking-wide text-rose-300/70">上方磁铁</div>
            {above.length
              ? above.map((m) => <MagnetItem key={`a-${m.price}`} m={m} coin={coin} />)
              : <div className="text-[10px] text-slate-600">—</div>}
          </div>
          <div className="flex-1 space-y-1">
            <div className="text-[10px] uppercase tracking-wide text-emerald-300/70">下方磁铁</div>
            {below.length
              ? below.map((m) => <MagnetItem key={`b-${m.price}`} m={m} coin={coin} />)
              : <div className="text-[10px] text-slate-600">—</div>}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-800 bg-slate-900/40 px-3 py-1.5">
        <div className="flex items-baseline gap-2">
          <span className="text-[12px] font-semibold text-slate-100">合约堆积</span>
          <span className="text-[10px] text-slate-500">合约侧厚度热力 + 磁铁叠加</span>
        </div>
        <div className="flex items-center gap-3 text-[10px] text-slate-400">
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-3 rounded bg-rose-500/70" /> 上方
          </span>
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-3 rounded bg-emerald-500/70" /> 下方
          </span>
          <span className="flex items-center gap-1">
            <span className="text-amber-400">◆</span> 磁铁
          </span>
        </div>
      </div>
      <div className="flex flex-1 min-h-0 overflow-y-auto">
        <Side
          title="上方堆积"
          subtitle="阻力 / 多头清算磁铁"
          items={bins_above}
          coin={coin}
          maxUsd={maxUsd}
          magnets={magnets}
          onSelectZone={onSelectZone}
          accentClass="border-rose-900/60 bg-rose-950/20 text-rose-200"
        />
        <div className="w-px shrink-0 bg-slate-800" />
        <Side
          title="下方堆积"
          subtitle="支撑 / 空头清算磁铁"
          items={bins_below}
          coin={coin}
          maxUsd={maxUsd}
          magnets={magnets}
          onSelectZone={onSelectZone}
          accentClass="border-emerald-900/60 bg-emerald-950/20 text-emerald-200"
        />
      </div>
    </div>
  );
}
