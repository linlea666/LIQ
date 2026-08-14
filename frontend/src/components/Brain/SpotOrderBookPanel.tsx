"use client";

/**
 * Phase B · 现货订单簿模块
 * ------------------------------------------------------------
 * 数据源：TradingBrainSnapshot.spot_book（后端从 OrderbookPressureSnapshot
 *         walls_above / walls_below 抽取，按距离分桶）。
 *
 * 展示规则（与产品确认）：
 *   - 上方卖墙 + 下方买墙；按 |distance_pct| 升序
 *   - 三段折叠：near (≤0.5%) / mid (0.5–2%) / far (2–5%)
 *     · near + mid 默认展开
 *     · far 默认折叠（点击展开），适合中短期仓位远端流动性观察
 *   - 每条横向厚度条：现货 (绿) + 合约 (蓝)，按总 USD 等比缩放
 *   - 现货占比 chip：spot_usd / total_usd
 *   - 双源 / Coinbase 共振 / trust_score 单独标记（不重打分）
 *
 * 不做的事：
 *   - 不重新评分（铁律）；所有展示字段直接复用 BrainSpotBookItem
 *   - 不输出交易指令；click → 通过 wall_zone_id 联动 PriceAxisMap 选中
 */
import { useMemo, useState } from "react";
import type { BrainSpotBook, BrainSpotBookItem } from "@/lib/types";
import { formatPrice, formatCnUsd } from "@/lib/format";

interface Props {
  spotBook: BrainSpotBook | null;
  coin: string;
  onSelectZone?: (wallZoneId: string) => void;
}

const BRACKET_LABEL: Record<string, string> = {
  near: "近 · ≤0.5%",
  mid: "中 · 0.5–2%",
  far: "远 · 2–5%",
};

const BRACKET_HINT: Record<string, string> = {
  near: "短线即时关注",
  mid: "中短期仓位区",
  far: "战略观察 · 远端流动性",
};

function fmtUsd(usd: number): string {
  if (!usd || usd <= 0) return "—";
  return formatCnUsd(usd);
}

// 方案 C：单档机构大单门槛（实测 BTC 4464万 / ETH 2245万 / SOL 1113万 → 100万 适中）
const INSTITUTION_SINGLE_USD_THRESHOLD = 1_000_000;

function ItemRow({
  item, coin, maxUsd, onSelectZone,
}: {
  item: BrainSpotBookItem;
  coin: string;
  maxUsd: number;
  onSelectZone?: (id: string) => void;
}) {
  const totalPct = maxUsd > 0 ? Math.max(2, (item.total_usd / maxUsd) * 100) : 0;
  const isAsk = item.side === "ask";
  const sideTone = isAsk ? "text-rose-300" : "text-emerald-300";
  const distTone = isAsk ? "text-rose-200/80" : "text-emerald-200/80";

  // 方案 C：现货双源拆分（Binance 5m 累积 vs Coinbase 瞬时机构 footprint）
  // 旧后端无双源拆分时回退用 spot_usd 全部归 Binance（向后兼容）
  const binanceUsd = item.binance_spot_usd ?? Math.max(0, item.spot_usd - (item.coinbase_spot_usd ?? 0));
  const coinbaseUsd = item.coinbase_spot_usd ?? 0;
  const cbMaxSingle = item.coinbase_max_single_order_usd ?? 0;
  const isInstitutional = cbMaxSingle >= INSTITUTION_SINGLE_USD_THRESHOLD;
  // 厚度条 3 色比例（按 total_usd 归一）
  const safeTotal = item.total_usd > 0 ? item.total_usd : 1;
  const binPct = Math.max(0, Math.min(100, (binanceUsd / safeTotal) * 100));
  const cbPct = Math.max(0, Math.min(100 - binPct, (coinbaseUsd / safeTotal) * 100));
  const futPct = Math.max(0, 100 - binPct - cbPct);

  // 档位 2A：长/短窗口对比
  const max1h = item.max_usd_1h ?? 0;
  const max8h = item.max_usd_8h ?? 0;
  const pers1h = item.persistence_score ?? 0;
  const pers8h = item.persistence_score_8h ?? 0;
  const weakerThan1h = max1h > 0 && item.total_usd < max1h * 0.70;
  const stronger8h = max8h > 0 && max1h > 0 && max8h > max1h * 1.30;

  const titleText =
    `墙区 · ${item.dominant_role}\n` +
    `当前 ${fmtUsd(item.total_usd)}\n` +
    `  Binance 现货 ${fmtUsd(binanceUsd)}（5m 累积，散户聚集为主）\n` +
    (coinbaseUsd > 0
      ? `  Coinbase 现货 ${fmtUsd(coinbaseUsd)}（瞬时快照，机构 footprint）\n`
      : "") +
    (cbMaxSingle > 0
      ? `  Coinbase 单档最大 ${fmtUsd(cbMaxSingle)}${isInstitutional ? "  ★ 机构级" : ""}\n`
      : "") +
    `  合约 ${fmtUsd(item.futures_usd)}\n` +
    (max1h > 0 ? `1h 峰值 ${fmtUsd(max1h)}　持续 ${(pers1h * 100).toFixed(0)}%\n` : "") +
    (max8h > 0 ? `8h 峰值 ${fmtUsd(max8h)}　持续 ${(pers8h * 100).toFixed(0)}%\n` : "") +
    `点击查看墙区详情`;

  return (
    <div
      className={`group flex cursor-pointer items-center gap-2 rounded border border-slate-800/70 bg-slate-900/40 px-2 py-1.5 transition hover:border-slate-600 hover:bg-slate-800/60 ${
        item.dominant_role === "institutional_footprint" ? "border-l-2 border-l-amber-400" :
        item.dominant_role === "dual_battleground" ? "border-l-2 border-l-fuchsia-400" :
        isInstitutional ? "border-l-2 border-l-amber-400" :
        ""
      }`}
      onClick={() => item.wall_zone_id && onSelectZone?.(item.wall_zone_id)}
      title={titleText}
    >
      <div className="w-[78px] shrink-0 tabular-nums">
        <div className={`text-[12px] font-semibold ${sideTone}`}>
          {formatPrice(item.price, coin)}
        </div>
        <div className={`text-[10px] ${distTone}`}>
          {item.distance_pct >= 0 ? "+" : ""}{item.distance_pct.toFixed(2)}%
        </div>
      </div>

      <div className="flex-1 min-w-0">
        {/* 厚度条 · 3 色拆分：Binance 绿 / Coinbase 金 / 合约蓝 */}
        <div className="relative h-3 w-full overflow-hidden rounded bg-slate-800/60">
          <div
            className="absolute inset-y-0 left-0 flex"
            style={{ width: `${totalPct}%` }}
          >
            <div className="h-full bg-emerald-500/70" style={{ width: `${binPct}%` }} />
            <div className="h-full bg-amber-400/85" style={{ width: `${cbPct}%` }} />
            <div className="h-full bg-blue-500/55" style={{ width: `${futPct}%` }} />
          </div>
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-slate-400">
          <span className="tabular-nums text-slate-300">{fmtUsd(item.total_usd)}</span>
          {binanceUsd > 0 && (
            <span className="tabular-nums text-emerald-300/90">
              Bin {fmtUsd(binanceUsd)}
            </span>
          )}
          {coinbaseUsd > 0 && (
            <span className="tabular-nums text-amber-300/90" title="Coinbase 现货厚度（机构资金 footprint）">
              CB {fmtUsd(coinbaseUsd)}
            </span>
          )}
          {isInstitutional && (
            <span
              className="rounded bg-amber-500/30 px-1 font-semibold text-amber-200"
              title="Coinbase 单档机构级孤立大单（≥ 100 万 USD）"
            >
              ★ 机构 {fmtUsd(cbMaxSingle)}
            </span>
          )}
          {item.is_dual_source && <span className="rounded bg-fuchsia-900/60 px-1 text-fuchsia-200">双源</span>}
          {item.strength_tier && item.strength_tier !== "C" && (
            <span className="rounded border border-slate-600 px-1 text-slate-300">{item.strength_tier}</span>
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

function BracketSection({
  bracket, items, coin, maxUsd, defaultOpen, onSelectZone,
}: {
  bracket: string;
  items: BrainSpotBookItem[];
  coin: string;
  maxUsd: number;
  defaultOpen: boolean;
  onSelectZone?: (id: string) => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (!items.length) return null;
  return (
    <div className="border-t border-slate-800/60 first:border-t-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-2 py-1 text-left hover:bg-slate-800/40"
      >
        <div className="flex items-center gap-2">
          <span className={`text-[10px] transition ${open ? "rotate-90" : ""}`}>▶</span>
          <span className="text-[11px] font-medium text-slate-200">{BRACKET_LABEL[bracket] ?? bracket}</span>
          <span className="text-[10px] text-slate-500">{BRACKET_HINT[bracket] ?? ""}</span>
        </div>
        <span className="text-[10px] tabular-nums text-slate-500">{items.length} 项</span>
      </button>
      {open && (
        <div className="space-y-1 px-2 pb-2">
          {items.map((it) => (
            <ItemRow
              key={`${it.wall_zone_id}-${it.price}`}
              item={it}
              coin={coin}
              maxUsd={maxUsd}
              onSelectZone={onSelectZone}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Side({
  title, subtitle, items, coin, maxUsd, onSelectZone, accentClass,
}: {
  title: string;
  subtitle: string;
  items: BrainSpotBookItem[];
  coin: string;
  maxUsd: number;
  onSelectZone?: (id: string) => void;
  accentClass: string;
}) {
  const groups = useMemo(() => {
    const g: Record<string, BrainSpotBookItem[]> = { near: [], mid: [], far: [] };
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
      <BracketSection bracket="near" items={groups.near} coin={coin} maxUsd={maxUsd} defaultOpen onSelectZone={onSelectZone} />
      <BracketSection bracket="mid" items={groups.mid} coin={coin} maxUsd={maxUsd} defaultOpen onSelectZone={onSelectZone} />
      <BracketSection bracket="far" items={groups.far} coin={coin} maxUsd={maxUsd} defaultOpen={false} onSelectZone={onSelectZone} />
    </div>
  );
}

export default function SpotOrderBookPanel({ spotBook, coin, onSelectZone }: Props) {
  const maxUsd = useMemo(() => {
    if (!spotBook) return 0;
    const all = [...spotBook.asks, ...spotBook.bids];
    if (!all.length) return 0;
    return Math.max(...all.map((x) => x.total_usd), 1);
  }, [spotBook]);

  if (!spotBook) {
    return (
      <div className="flex h-full items-center justify-center px-3 py-6 text-[11px] text-slate-500">
        现货订单簿数据未就绪
      </div>
    );
  }
  if (!spotBook.asks.length && !spotBook.bids.length) {
    return (
      <div className="flex h-full items-center justify-center px-3 py-6 text-[11px] text-slate-500">
        当前无 5% 内的现货墙
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-slate-800 bg-slate-900/40 px-3 py-1.5">
        <div className="flex items-baseline gap-2">
          <span className="text-[12px] font-semibold text-slate-100">现货订单簿</span>
          <span className="text-[10px] text-slate-500">Binance 5m / Coinbase 瞬时 / 合约</span>
        </div>
        <div className="flex items-center gap-2 text-[10px] text-slate-400">
          <span className="flex items-center gap-1" title="Binance 现货 5m 累积厚度（散户聚集为主）">
            <span className="inline-block h-2 w-3 rounded bg-emerald-500/70" /> Binance
          </span>
          <span className="flex items-center gap-1" title="Coinbase 现货瞬时厚度（机构 footprint）">
            <span className="inline-block h-2 w-3 rounded bg-amber-400/85" /> Coinbase
          </span>
          <span className="flex items-center gap-1" title="合约挂单">
            <span className="inline-block h-2 w-3 rounded bg-blue-500/55" /> 合约
          </span>
          <span className="flex items-center gap-1" title="单档 ≥ 100 万 USD 机构级孤立大单">
            <span className="font-semibold text-amber-300">★</span> 机构
          </span>
        </div>
      </div>
      <div className="flex flex-1 min-h-0 overflow-y-auto">
        <Side
          title="上方卖墙"
          subtitle="阻力 / 短空目标"
          items={spotBook.asks}
          coin={coin}
          maxUsd={maxUsd}
          onSelectZone={onSelectZone}
          accentClass="border-rose-900/60 bg-rose-950/20 text-rose-200"
        />
        <div className="w-px shrink-0 bg-slate-800" />
        <Side
          title="下方买墙"
          subtitle="支撑 / 短多观察"
          items={spotBook.bids}
          coin={coin}
          maxUsd={maxUsd}
          onSelectZone={onSelectZone}
          accentClass="border-emerald-900/60 bg-emerald-950/20 text-emerald-200"
        />
      </div>
    </div>
  );
}
