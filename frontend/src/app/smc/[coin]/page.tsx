"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import type {
  SMCConfirmation,
  SMCHorizon,
  SMCLiquidityPool,
  SMCSnapshot,
  SMCStructureEvent,
  SMCTargetZone,
  SMCZone,
} from "@/lib/types";

const OBS_LABEL: Record<string, string> = {
  long_watch: "做多观察",
  short_watch: "做空观察",
  wait: "等待",
};

const STATE_LABEL: Record<string, string> = {
  candidate: "候选",
  raid_detected: "已扫流动性",
  mss_confirmed: "结构已确认",
  entry_zone_active: "观察区激活",
  invalidated: "失效",
  expired: "过期",
};

const KIND_LABEL: Record<string, string> = {
  liquidity: "流动性",
  order_block: "主力建仓区",
  fair_value_gap: "失衡回补区",
  breaker: "破坏块",
  po3: "PO3 区间",
  fib_ote: "OTE 回撤区",
  turnover_sr: "换手位",
  liq_map_above: "清算带",
  liq_map_below: "清算带",
  liq_heatmap: "清算热区",
  equal_highs_lows: "等高/等低",
  fvg_rebalance: "失衡中线",
};

const ROLE_LABEL: Record<string, string> = {
  buy_side_liquidity: "上方止损/追多区",
  sell_side_liquidity: "下方止损/追空区",
  bullish_demand: "做多观察区",
  bearish_supply: "做空观察区",
  support: "支撑",
  resistance: "阻力",
  neutral: "中性",
};

type PriceItem = {
  id: string;
  side: "above" | "below";
  price: number;
  priceFrom?: number;
  priceTo?: number;
  distancePct: number;
  strength: number;
  title: string;
  plain: string;
  source: string;
  detail?: string;
  state?: string;
};

function fmtPrice(v?: number | null) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 1 }) : v.toFixed(4);
}

function fmtPct(v?: number | null) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function rangeLabel(from?: number, to?: number, price?: number) {
  if (from && to && Math.abs(from - to) > 0) return `${fmtPrice(from)} - ${fmtPrice(to)}`;
  return fmtPrice(price);
}

function observationTone(snapshot: SMCSnapshot | null) {
  if (!snapshot) return "border-slate-700 text-slate-300";
  if (snapshot.observation === "long_watch") return "border-emerald-500/60 text-emerald-100";
  if (snapshot.observation === "short_watch") return "border-rose-500/60 text-rose-100";
  return "border-slate-700 text-slate-200";
}

function Section({
  title,
  right,
  children,
  className = "",
}: {
  title: string;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`min-w-0 border border-slate-800 bg-slate-900/40 ${className}`}>
      <div className="flex items-center justify-between gap-3 border-b border-slate-800 px-3 py-2">
        <div className="text-[11px] font-semibold text-slate-300">{title}</div>
        {right}
      </div>
      <div className="p-3">{children}</div>
    </section>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="py-4 text-center text-[11px] text-slate-600">{text}</div>;
}

function zonePlain(z: SMCZone) {
  if (z.role === "bullish_demand" || z.role === "support") {
    return "价格回到这里若止跌，才有做多观察价值。";
  }
  if (z.role === "bearish_supply" || z.role === "resistance") {
    return "价格反抽到这里若承压，才有做空观察价值。";
  }
  if (z.role === "buy_side_liquidity") return "上方容易吸引追多和空头止损。";
  if (z.role === "sell_side_liquidity") return "下方容易触发多头止损和追空。";
  return "中性观察区，等待结构确认。";
}

function poolPlain(p: SMCLiquidityPool) {
  return p.side === "buy_side"
    ? "上方止损/追多集中，先被扫后跌回才偏空。"
    : "下方止损/追空集中，先被扫后收回才偏多。";
}

function targetPlain(t: SMCTargetZone) {
  if (t.side === "above") return "上方目标/磁吸区，接近后观察是否扫损失败。";
  if (t.side === "below") return "下方目标/磁吸区，接近后观察是否扫损收回。";
  return "中性目标区。";
}

function buildPriceItems(snapshot: SMCSnapshot): { above: PriceItem[]; below: PriceItem[] } {
  const items: PriceItem[] = [];
  const push = (item: PriceItem) => {
    if (!Number.isFinite(item.price) || Math.abs(item.distancePct) > 15) return;
    items.push(item);
  };

  for (const z of snapshot.zones) {
    push({
      id: `zone-${z.zone_id}`,
      side: z.midpoint >= snapshot.last_price ? "above" : "below",
      price: z.midpoint,
      priceFrom: z.price_from,
      priceTo: z.price_to,
      distancePct: z.distance_pct,
      strength: z.strength,
      title: `${KIND_LABEL[z.kind] ?? z.kind} · ${ROLE_LABEL[z.role] ?? z.role}`,
      plain: zonePlain(z),
      source: z.timeframe,
      detail: z.evidence[0] || z.notes[0],
      state: STATE_LABEL[z.state] ?? z.state,
    });
  }

  for (const p of snapshot.liquidity_pools) {
    push({
      id: `pool-${p.pool_id}`,
      side: p.price >= snapshot.last_price ? "above" : "below",
      price: p.price,
      priceFrom: p.price_from,
      priceTo: p.price_to,
      distancePct: p.distance_pct,
      strength: p.strength,
      title: p.side === "buy_side" ? "上方流动性" : "下方流动性",
      plain: poolPlain(p),
      source: `${KIND_LABEL[p.source] ?? p.source} · ${p.timeframe}`,
      detail: p.evidence[0],
      state: p.swept ? "已扫" : "未扫",
    });
  }

  for (const t of snapshot.targets) {
    push({
      id: `target-${t.kind}-${t.price}`,
      side: t.price >= snapshot.last_price ? "above" : "below",
      price: t.price,
      distancePct: t.distance_pct,
      strength: 45,
      title: `${KIND_LABEL[t.kind] ?? t.kind} · 目标观察`,
      plain: targetPlain(t),
      source: t.side === "above" ? "上方目标" : "下方目标",
      detail: t.note,
    });
  }

  const deduped = Array.from(
    new Map(items.map((item) => [`${item.side}:${Math.round(item.price * 100)}`, item])).values(),
  );
  const above = deduped
    .filter((item) => item.side === "above")
    .sort((a, b) => Math.abs(a.distancePct) - Math.abs(b.distancePct) || b.strength - a.strength)
    .slice(0, 8);
  const below = deduped
    .filter((item) => item.side === "below")
    .sort((a, b) => Math.abs(a.distancePct) - Math.abs(b.distancePct) || b.strength - a.strength)
    .slice(0, 8);
  return { above, below };
}

function ActionBrief({ snapshot }: { snapshot: SMCSnapshot }) {
  const primary =
    snapshot.observation === "long_watch"
      ? "等回踩需求区，确认收回后再看多。"
      : snapshot.observation === "short_watch"
        ? "等反抽供应区，确认跌回后再看空。"
        : "先等待扫损和结构转换，不追涨杀跌。";
  const risk =
    snapshot.setup_state === "raid_detected"
      ? "已经出现扫流动性，但还需要结构确认或入场区配合。"
      : snapshot.setup_state === "mss_confirmed"
        ? "结构已切换，重点等价格回到合理观察区。"
        : "三件套还没齐，当前只做位置标记。";
  return (
    <section className={`border bg-slate-900/40 p-3 ${observationTone(snapshot)}`}>
      <div className="grid gap-3 lg:grid-cols-[1.35fr_0.75fr_0.75fr_0.85fr]">
        <div>
          <div className="text-[11px] text-slate-500">当前动作</div>
          <div className="mt-1 text-2xl font-semibold">{OBS_LABEL[snapshot.observation]}</div>
          <p className="mt-2 text-[13px] leading-relaxed text-slate-200">{primary}</p>
          <p className="mt-1 text-[12px] leading-relaxed text-slate-500">{risk}</p>
        </div>
        <Metric label="现价" value={fmtPrice(snapshot.last_price)} sub={snapshot.horizon === "intraday" ? "日内视角" : "波段视角"} />
        <Metric label="状态" value={STATE_LABEL[snapshot.setup_state]} sub={`置信度 ${snapshot.confidence}/100`} />
        <Metric label="失效价" value={fmtPrice(snapshot.invalidation_price)} sub={`数据 ${snapshot.data_quality.status} · ${snapshot.data_quality.score}`} />
      </div>
    </section>
  );
}

function Metric({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="border border-slate-800 bg-slate-950/45 p-3">
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className="mt-1 truncate text-lg font-semibold text-slate-100">{value}</div>
      <div className="mt-2 text-[11px] text-slate-500">{sub}</div>
    </div>
  );
}

function PriceItemRow({ item }: { item: PriceItem }) {
  const sideClass = item.side === "above" ? "border-rose-900/50 bg-rose-950/15" : "border-emerald-900/50 bg-emerald-950/15";
  const textClass = item.side === "above" ? "text-rose-200" : "text-emerald-200";
  return (
    <div className={`border p-2 ${sideClass}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className={`truncate text-[12px] font-semibold ${textClass}`}>{item.title}</div>
          <div className="mt-1 text-[11px] text-slate-400">{rangeLabel(item.priceFrom, item.priceTo, item.price)}</div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-[11px] text-slate-200">{fmtPct(item.distancePct)}</div>
          <div className="text-[10px] text-slate-500">强度 {Math.round(item.strength)}</div>
        </div>
      </div>
      <div className="mt-2 text-[11px] leading-relaxed text-slate-300">{item.plain}</div>
      <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-slate-600">
        <span className="truncate">{item.source}</span>
        {item.state && <span className="shrink-0">{item.state}</span>}
      </div>
      {item.detail && <div className="mt-1 truncate text-[10px] text-slate-500">{item.detail}</div>}
    </div>
  );
}

function PriceMap({ snapshot }: { snapshot: SMCSnapshot }) {
  const { above, below } = buildPriceItems(snapshot);
  const nearestResistance = above.find((i) => i.title.includes("阻力") || i.title.includes("做空") || i.title.includes("上方"));
  const nearestSupport = below.find((i) => i.title.includes("支撑") || i.title.includes("做多") || i.title.includes("下方"));
  return (
    <Section
      title="价格地图"
      right={<span className="text-[10px] text-slate-500">从近到远</span>}
      className="xl:col-span-2"
    >
      <div className="grid gap-3 lg:grid-cols-[1fr_180px_1fr]">
        <div>
          <div className="mb-2 text-[11px] font-semibold text-rose-200">上方：阻力 / 顶部 / 扫空止损区</div>
          <div className="space-y-2">{above.length ? above.map((item) => <PriceItemRow key={item.id} item={item} />) : <Empty text="上方暂无关键区" />}</div>
        </div>
        <div className="flex flex-col justify-center border border-slate-800 bg-slate-950/45 p-3 text-center">
          <div className="text-[10px] text-slate-500">最近阻力</div>
          <div className="mt-1 text-[13px] font-semibold text-rose-200">{nearestResistance ? rangeLabel(nearestResistance.priceFrom, nearestResistance.priceTo, nearestResistance.price) : "-"}</div>
          <div className="my-4 border-y border-slate-800 py-4">
            <div className="text-[10px] text-slate-500">当前价格</div>
            <div className="mt-1 text-xl font-semibold text-slate-100">{fmtPrice(snapshot.last_price)}</div>
          </div>
          <div className="text-[10px] text-slate-500">最近支撑</div>
          <div className="mt-1 text-[13px] font-semibold text-emerald-200">{nearestSupport ? rangeLabel(nearestSupport.priceFrom, nearestSupport.priceTo, nearestSupport.price) : "-"}</div>
        </div>
        <div>
          <div className="mb-2 text-[11px] font-semibold text-emerald-200">下方：支撑 / 底部 / 扫多止损区</div>
          <div className="space-y-2">{below.length ? below.map((item) => <PriceItemRow key={item.id} item={item} />) : <Empty text="下方暂无关键区" />}</div>
        </div>
      </div>
    </Section>
  );
}

function Playbook({ snapshot }: { snapshot: SMCSnapshot }) {
  const lastRaid = snapshot.structure.find((event) => event.kind === "liquidity_raid");
  const lastMss = snapshot.structure.find((event) => event.kind === "mss");
  const demand = snapshot.zones.find((z) => z.role === "bullish_demand" || z.role === "support");
  const supply = snapshot.zones.find((z) => z.role === "bearish_supply" || z.role === "resistance");
  return (
    <Section title="交易剧本">
      <div className="space-y-2">
        <Scenario
          title="多头剧本"
          active={snapshot.observation === "long_watch"}
          tone="long"
          steps={[
            "先扫下方多头止损区",
            lastRaid?.direction === "bullish" ? "扫后收回已出现" : "等待收回确认",
            lastMss?.direction === "bullish" ? "多头结构已切换" : "等待 MSS",
            demand ? `回踩 ${rangeLabel(demand.price_from, demand.price_to, demand.midpoint)}` : "等待需求区形成",
          ]}
        />
        <Scenario
          title="空头剧本"
          active={snapshot.observation === "short_watch"}
          tone="short"
          steps={[
            "先扫上方空头止损区",
            lastRaid?.direction === "bearish" ? "扫后跌回已出现" : "等待跌回确认",
            lastMss?.direction === "bearish" ? "空头结构已切换" : "等待 MSS",
            supply ? `反抽 ${rangeLabel(supply.price_from, supply.price_to, supply.midpoint)}` : "等待供应区形成",
          ]}
        />
        <Scenario
          title="等待剧本"
          active={snapshot.observation === "wait"}
          tone="wait"
          steps={[
            "不追当前K线",
            STATE_LABEL[snapshot.setup_state],
            "等扫损 + MSS + OB/FVG/OTE 合流",
            snapshot.contradictions[0]?.note || "暂无强反证",
          ]}
        />
      </div>
    </Section>
  );
}

function Scenario({
  title,
  active,
  tone,
  steps,
}: {
  title: string;
  active: boolean;
  tone: "long" | "short" | "wait";
  steps: string[];
}) {
  const activeClass = tone === "long" ? "border-emerald-600/60 bg-emerald-950/20" : tone === "short" ? "border-rose-600/60 bg-rose-950/20" : "border-blue-600/50 bg-blue-950/15";
  return (
    <div className={`border p-2 ${active ? activeClass : "border-slate-800 bg-slate-950/35"}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-[12px] font-semibold text-slate-100">{title}</div>
        {active && <span className="text-[10px] text-blue-200">当前</span>}
      </div>
      <div className="mt-2 space-y-1">
        {steps.map((step, idx) => (
          <div key={`${title}-${idx}`} className="flex gap-2 text-[11px] leading-relaxed text-slate-400">
            <span className="mt-0.5 h-4 w-4 shrink-0 border border-slate-700 text-center text-[9px] leading-4 text-slate-500">{idx + 1}</span>
            <span>{step}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function EventList({ events }: { events: SMCStructureEvent[] }) {
  if (!events.length) return <Empty text="暂无结构事件" />;
  return (
    <div className="space-y-2">
      {events.slice(0, 8).map((event) => (
        <div key={event.event_id} className="flex items-start justify-between gap-3 border-b border-slate-800/70 pb-2 last:border-0 last:pb-0">
          <div className="min-w-0">
            <div className="text-[12px] text-slate-200">{event.kind} · {event.direction}</div>
            <div className="mt-0.5 truncate text-[10px] text-slate-500">{event.note}</div>
          </div>
          <div className="shrink-0 text-right text-[11px] text-slate-400">
            <div>{fmtPrice(event.price)}</div>
            <div>{event.timeframe}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function ConfirmationList({ items }: { items: SMCConfirmation[] }) {
  if (!items.length) return <Empty text="暂无确认项" />;
  return (
    <div className="space-y-2">
      {items.slice(0, 10).map((item, idx) => (
        <div key={`${item.source}-${idx}`} className="flex items-start justify-between gap-3 border-b border-slate-800/70 pb-2 last:border-0 last:pb-0">
          <div className="min-w-0">
            <div className="text-[12px] text-slate-200">{item.source}</div>
            <div className="mt-0.5 text-[10px] leading-relaxed text-slate-500">{item.note}</div>
          </div>
          <div className="shrink-0 text-right">
            <div className={item.direction === "bullish" ? "text-[11px] text-emerald-300" : item.direction === "bearish" ? "text-[11px] text-rose-300" : "text-[11px] text-slate-400"}>
              {item.direction}
            </div>
            <div className="text-[10px] text-slate-500">{item.score_delta > 0 ? "+" : ""}{item.score_delta.toFixed(1)}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function TargetList({ targets }: { targets: SMCTargetZone[] }) {
  if (!targets.length) return <Empty text="暂无目标观察区" />;
  return (
    <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
      {targets.map((target, idx) => (
        <div key={`${target.kind}-${idx}`} className="border border-slate-800 bg-slate-950/35 p-2">
          <div className="flex items-center justify-between">
            <span className="text-[12px] text-slate-200">{KIND_LABEL[target.kind] ?? target.kind}</span>
            <span className="text-[11px] text-slate-500">{fmtPct(target.distance_pct)}</span>
          </div>
          <div className="mt-1 text-sm font-semibold text-slate-100">{fmtPrice(target.price)}</div>
          <div className="mt-1 text-[10px] text-slate-500">{target.note}</div>
        </div>
      ))}
    </div>
  );
}

export default function SMCPage() {
  const params = useParams();
  const coin = ((params.coin as string) || "BTC").toUpperCase();
  const [horizon, setHorizon] = useState<SMCHorizon>("intraday");
  const key = `${coin}:${horizon}`;

  const snap = useMarketStore((s) => s.smcByKey[key] ?? null);
  const loading = useMarketStore((s) => s.smcLoadingByKey[key] ?? false);
  const err = useMarketStore((s) => s.smcErrorByKey[key] ?? null);
  const breadth = useMarketStore((s) => s.smcMarketBreadth);
  const loadSMC = useMarketStore((s) => s.loadSMC);
  const loadBreadth = useMarketStore((s) => s.loadSMCMarketBreadth);

  useEffect(() => {
    loadSMC(coin, horizon).catch(() => undefined);
    loadBreadth().catch(() => undefined);
    const timer = setInterval(() => {
      loadSMC(coin, horizon).catch(() => undefined);
      loadBreadth().catch(() => undefined);
    }, 60_000);
    return () => clearInterval(timer);
  }, [coin, horizon, loadSMC, loadBreadth]);

  const orderFlowConfirmations = useMemo(
    () => (snap?.confirmations ?? []).filter((item) => !item.source.startsWith("nansen")),
    [snap],
  );
  const nansenConfirmations = useMemo(
    () => (snap?.confirmations ?? []).filter((item) => item.source.startsWith("nansen")),
    [snap],
  );

  return (
    <div className="smc-page min-h-screen bg-slate-950 text-slate-200">
      <header className="sticky top-0 z-10 border-b border-slate-800 bg-slate-900/95 px-4 py-3 backdrop-blur">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <Link href="/" className="text-[12px] text-slate-500 hover:text-slate-300">
              ← 仪表盘
            </Link>
            <h1 className="text-sm font-semibold text-slate-100">
              SMC 聪明钱分析 · <span className="text-blue-300">{coin}</span>
            </h1>
            {loading && <span className="text-[10px] text-slate-600">刷新中</span>}
          </div>
          <div className="flex border border-slate-700 bg-slate-950 p-0.5">
            {(["intraday", "swing"] as SMCHorizon[]).map((item) => (
              <button
                key={item}
                onClick={() => setHorizon(item)}
                className={`px-3 py-1 text-[12px] ${horizon === item ? "bg-blue-600 text-white" : "text-slate-400 hover:text-slate-100"}`}
              >
                {item === "intraday" ? "日内" : "波段"}
              </button>
            ))}
          </div>
        </div>
      </header>

      {err && (
        <div className="border-b border-rose-900/40 bg-rose-950/30 px-4 py-2 text-center text-[12px] text-rose-200">
          {err}
        </div>
      )}

      {!snap && !err && (
        <div className="flex min-h-[60vh] items-center justify-center text-[12px] text-slate-500">
          加载 SMC 视图…
        </div>
      )}

      {snap && (
        <main className="mx-auto flex max-w-[1560px] flex-col gap-3 px-3 py-3">
          <ActionBrief snapshot={snap} />

          <div className="grid gap-3 xl:grid-cols-[1fr_360px]">
            <PriceMap snapshot={snap} />
            <Playbook snapshot={snap} />
          </div>

          <div className="grid gap-3 xl:grid-cols-[1fr_1fr_1fr]">
            <Section title="结构事件">
              <EventList events={snap.structure} />
            </Section>
            <Section title="订单流确认">
              <ConfirmationList items={orderFlowConfirmations} />
            </Section>
            <Section title="Nansen 确认">
              <div className="mb-3 grid grid-cols-3 gap-2 text-center">
                <Metric label="状态" value={snap.smart_money.status} sub="聪明钱层" />
                <Metric label="偏向" value={snap.smart_money.bias} sub="资金方向" />
                <Metric label="宽度" value={breadth ? breadth.breadth_score.toFixed(1) : "-"} sub={breadth?.status ?? "missing"} />
              </div>
              <ConfirmationList items={nansenConfirmations} />
            </Section>
          </div>

          <div className="grid gap-3 xl:grid-cols-[1fr_1fr]">
            <Section title="目标观察区">
              <TargetList targets={snap.targets} />
            </Section>
            <Section title="反证与数据质量">
              <div className="grid gap-3 md:grid-cols-2">
                <div className="space-y-2">
                  {snap.contradictions.length ? snap.contradictions.map((item, idx) => (
                    <div key={`${item.source}-${idx}`} className="border border-slate-800 bg-slate-950/35 p-2">
                      <div className="text-[12px] text-amber-200">{item.source} · {item.severity}</div>
                      <div className="mt-1 text-[10px] leading-relaxed text-slate-500">{item.note}</div>
                    </div>
                  )) : <Empty text="暂无明确反证" />}
                </div>
                <div className="space-y-2 text-[11px] text-slate-500">
                  <div className="border border-slate-800 bg-slate-950/35 p-2">
                    缺失：{snap.data_quality.missing.length ? snap.data_quality.missing.join(", ") : "无"}
                  </div>
                  <div className="border border-slate-800 bg-slate-950/35 p-2">
                    降级：{snap.data_quality.degraded.length ? snap.data_quality.degraded.join(", ") : "无"}
                  </div>
                  <div className="border border-slate-800 bg-slate-950/35 p-2">
                    说明：{snap.data_quality.notes.join("；")}
                  </div>
                </div>
              </div>
            </Section>
          </div>
        </main>
      )}
    </div>
  );
}
