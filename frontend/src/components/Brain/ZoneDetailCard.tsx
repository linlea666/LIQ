/**
 * 争夺区/防守区详情卡（W4-T1 阶段 2 升级）
 * ------------------------------------------------------------
 * 改动：
 *   1. 顶部新增"现货 / 合约 / 清算"三层厚度条（复用 SpotOrderBookPanel 3 色规范：
 *      Binance 绿 / Coinbase 金 / 合约蓝；清算磁铁紫色叠加角标）
 *   2. 加入 4 个独立 chip：★ 机构 / 双源 / Coinbase 共振 / 持续性
 *      （把原本埋在 evidence 文字里的硬证据视觉化）
 *   3. ScoreBar 阈值色阶：信任类 < 0.4 红 / 0.4-0.7 黄 / ≥ 0.7 绿；
 *      风险类反向；hover tooltip 显示档位语义
 *
 * 数据原则（用户原则 5/6）：
 *   - 不动后端数据源：通过 zone.wall_zone_ids 反查 BrainSpotBook / BrainFutBook
 *   - 不重打分：所有数值直接复用现有字段（W4-T1 阶段 1 的后端权重已生效）
 */
import type {
  BrainPriceZone,
  BrainSpotBook,
  BrainSpotBookItem,
  BrainFutBook,
  BrainFutBin,
  BrainFutMagnet,
} from "@/lib/types";
import { formatPrice, formatCnUsd } from "@/lib/format";
import { ROLE_COLORS } from "./types";

interface Props {
  zone: BrainPriceZone | null;
  coin: string;
  spotBook?: BrainSpotBook | null;
  futBook?: BrainFutBook | null;
}

// 与 SpotOrderBookPanel 保持一致的机构级单档阈值（与后端 SR ladder 1M 档对齐）
const INSTITUTION_SINGLE_USD_THRESHOLD = 1_000_000;

// ─────────────────────────────────────────────────────────────────────
// ScoreBar：阈值色阶 + tooltip
// ─────────────────────────────────────────────────────────────────────
type ScoreKind = "trust" | "risk";

function trustColor(value: number): { bar: string; text: string; level: string } {
  if (value >= 0.7) return { bar: "bg-emerald-500/80", text: "text-emerald-300", level: "强" };
  if (value >= 0.4) return { bar: "bg-amber-500/80", text: "text-amber-300", level: "中" };
  return { bar: "bg-rose-500/70", text: "text-rose-300", level: "弱" };
}

function riskColor(value: number): { bar: string; text: string; level: string } {
  if (value >= 0.7) return { bar: "bg-rose-500/80", text: "text-rose-300", level: "高" };
  if (value >= 0.4) return { bar: "bg-amber-500/80", text: "text-amber-300", level: "中" };
  return { bar: "bg-emerald-500/70", text: "text-emerald-300", level: "低" };
}

function ScoreBar({
  label,
  value,
  kind,
  hint,
}: {
  label: string;
  value: number;
  kind: ScoreKind;
  hint?: string;
}) {
  const v = Math.max(0, Math.min(1, value));
  const pct = v * 100;
  const c = kind === "trust" ? trustColor(v) : riskColor(v);
  const tip = hint
    ? `${hint}\n当前 ${v.toFixed(2)} (${c.level})`
    : `当前 ${v.toFixed(2)} (${c.level})`;
  return (
    <div title={tip}>
      <div className="flex items-baseline justify-between text-[10px]">
        <span className="text-slate-500">{label}</span>
        <span className={`tabular-nums ${c.text}`}>
          {v.toFixed(2)}
          <span className="ml-1 text-[9px] text-slate-500">{c.level}</span>
        </span>
      </div>
      <div className="mt-0.5 h-1 w-full overflow-hidden rounded bg-slate-800">
        <div className={`h-full ${c.bar}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 现货/合约/清算三层厚度条
// ─────────────────────────────────────────────────────────────────────
interface LiquidityBreakdown {
  binance_usd: number;
  coinbase_usd: number;
  futures_usd: number;
  cb_max_single_usd: number;
  is_dual_source: boolean;
  has_coinbase: boolean;
  persistence_score: number;
  liq_magnet_usd: number;
  liq_magnet_label: string | null;
  spot_items: BrainSpotBookItem[];
  fut_bins: BrainFutBin[];
}

function aggregateLiquidity(
  zone: BrainPriceZone,
  spotBook?: BrainSpotBook | null,
  futBook?: BrainFutBook | null,
): LiquidityBreakdown {
  const wallIdSet = new Set(zone.wall_zone_ids);
  const spotItems: BrainSpotBookItem[] = [];
  const futBins: BrainFutBin[] = [];
  let binance = 0;
  let coinbase = 0;
  let futures = 0;
  let cbMaxSingle = 0;
  let dualSource = false;
  let hasCoinbase = false;
  let maxPersistence = 0;

  // 同一 wall_zone_id 可能同时出现在 spot_book 和 fut_book（同一墙的不同视图），
  // futures_usd 在 BrainSpotBookItem 已含；fut_book 循环时跳过已累加的 ID 防止双计
  const seenInSpot = new Set<string>();
  if (spotBook) {
    for (const item of [...spotBook.bids, ...spotBook.asks]) {
      if (!wallIdSet.has(item.wall_zone_id)) continue;
      spotItems.push(item);
      seenInSpot.add(item.wall_zone_id);
      const cb = item.coinbase_spot_usd ?? 0;
      const bin = item.binance_spot_usd ?? Math.max(0, item.spot_usd - cb);
      binance += bin;
      coinbase += cb;
      futures += item.futures_usd;
      cbMaxSingle = Math.max(cbMaxSingle, item.coinbase_max_single_order_usd ?? 0);
      if (item.is_dual_source) dualSource = true;
      if (item.has_coinbase) hasCoinbase = true;
      maxPersistence = Math.max(maxPersistence, item.persistence_score ?? 0);
    }
  }

  // 仅在 fut_book 中独立出现（如纯合约磁铁、关键位下方未达现货 wall_min 的合约堆积）
  // 的墙才补充累加，避免和 spot 路径重复
  if (futBook) {
    for (const bin of [...futBook.bins_above, ...futBook.bins_below]) {
      if (!wallIdSet.has(bin.wall_zone_id)) continue;
      futBins.push(bin);
      maxPersistence = Math.max(maxPersistence, bin.persistence_score);
      if (!seenInSpot.has(bin.wall_zone_id)) {
        futures += bin.futures_usd;
      }
    }
  }

  // 清算磁铁：磁铁价格落在 zone 范围内即视为命中（取 USD 最大者作为代表）
  const MAGNET_KIND_LABEL: Record<BrainFutMagnet["magnet_kind"], string> = {
    liq_cluster: "清算簇",
    max_pain_long: "多头痛点磁铁",
    max_pain_short: "空头痛点磁铁",
    leverage_magnet: "杠杆磁铁",
    other: "磁铁",
  };
  let liqMagnetUsd = 0;
  let liqMagnetLabel: string | null = null;
  if (futBook?.magnets) {
    for (const m of futBook.magnets) {
      if (m.price < zone.price_low || m.price > zone.price_high) continue;
      if (m.usd > liqMagnetUsd) {
        liqMagnetUsd = m.usd;
        liqMagnetLabel = MAGNET_KIND_LABEL[m.magnet_kind] ?? "磁铁";
      }
    }
  }

  return {
    binance_usd: binance,
    coinbase_usd: coinbase,
    futures_usd: futures,
    cb_max_single_usd: cbMaxSingle,
    is_dual_source: dualSource,
    has_coinbase: hasCoinbase,
    persistence_score: maxPersistence,
    liq_magnet_usd: liqMagnetUsd,
    liq_magnet_label: liqMagnetLabel,
    spot_items: spotItems,
    fut_bins: futBins,
  };
}

function LiquidityStackBar({ b }: { b: LiquidityBreakdown }) {
  const total = b.binance_usd + b.coinbase_usd + b.futures_usd;
  if (total <= 0) {
    return (
      <div className="rounded border border-slate-800 bg-slate-900/40 px-2 py-2 text-[10px] text-slate-500">
        该区无活跃订单簿厚度（可能仅命中关键位/清算磁铁）
      </div>
    );
  }
  const binPct = (b.binance_usd / total) * 100;
  const cbPct = (b.coinbase_usd / total) * 100;
  const futPct = (b.futures_usd / total) * 100;
  return (
    <div>
      <div className="flex items-baseline justify-between text-[10px] text-slate-500">
        <span>现货 / 合约 / 清算流动性构成</span>
        <span className="tabular-nums text-slate-400">合计 {formatCnUsd(total)}</span>
      </div>
      <div className="mt-1 flex h-3 w-full overflow-hidden rounded bg-slate-800">
        {b.binance_usd > 0 && (
          <div
            className="h-full bg-emerald-500/80"
            style={{ width: `${binPct}%` }}
            title={`Binance 现货 5m 累积：${formatCnUsd(b.binance_usd)}（散户聚集为主）`}
          />
        )}
        {b.coinbase_usd > 0 && (
          <div
            className="h-full bg-amber-400/85"
            style={{ width: `${cbPct}%` }}
            title={`Coinbase 现货瞬时：${formatCnUsd(b.coinbase_usd)}（机构 footprint 通道）`}
          />
        )}
        {b.futures_usd > 0 && (
          <div
            className="h-full bg-sky-500/70"
            style={{ width: `${futPct}%` }}
            title={`合约堆积：${formatCnUsd(b.futures_usd)}（杠杆资金）`}
          />
        )}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] tabular-nums">
        {b.binance_usd > 0 && (
          <span className="text-emerald-300">
            <span className="mr-1 inline-block h-1.5 w-1.5 rounded-sm bg-emerald-500/80" />
            Bin {formatCnUsd(b.binance_usd)}
          </span>
        )}
        {b.coinbase_usd > 0 && (
          <span className="text-amber-300">
            <span className="mr-1 inline-block h-1.5 w-1.5 rounded-sm bg-amber-400/85" />
            CB {formatCnUsd(b.coinbase_usd)}
          </span>
        )}
        {b.futures_usd > 0 && (
          <span className="text-sky-300">
            <span className="mr-1 inline-block h-1.5 w-1.5 rounded-sm bg-sky-500/70" />
            合约 {formatCnUsd(b.futures_usd)}
          </span>
        )}
        {b.liq_magnet_usd > 0 && (
          <span
            className="text-fuchsia-300"
            title={`${b.liq_magnet_label} ${formatCnUsd(b.liq_magnet_usd)}`}
          >
            <span className="mr-1 inline-block h-1.5 w-1.5 rounded-sm bg-fuchsia-500/80" />
            清算 {formatCnUsd(b.liq_magnet_usd)}
          </span>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 4 chip 行：机构★ / 双源 / Coinbase 共振 / 持续性
// ─────────────────────────────────────────────────────────────────────
function HardEvidenceChips({ b }: { b: LiquidityBreakdown }) {
  const isInstitutional = b.cb_max_single_usd >= INSTITUTION_SINGLE_USD_THRESHOLD;
  const chips: { key: string; label: string; cls: string; tip: string }[] = [];

  if (isInstitutional) {
    chips.push({
      key: "inst",
      label: `★ 机构 ${formatCnUsd(b.cb_max_single_usd)}`,
      cls: "border-amber-400 bg-amber-400/15 text-amber-300",
      tip: `Coinbase 单档孤立大单 ≥ 100万 USD（机构 footprint 硬证据，SR +0.10 已计入支撑/阻力信任）`,
    });
  }
  if (b.is_dual_source) {
    chips.push({
      key: "dual",
      label: "双源",
      cls: "border-fuchsia-400 bg-fuchsia-400/15 text-fuchsia-300",
      tip: "现货 + 合约 5m 同价位均有 ≥ wall_min 厚度（最强单一硬证据，SR +0.30 已计入）",
    });
  }
  if (b.has_coinbase) {
    chips.push({
      key: "cb",
      label: "Coinbase 共振",
      cls: "border-yellow-400 bg-yellow-400/10 text-yellow-300",
      tip: "Coinbase 同价位通过 30% wall_min 门槛（机构资金独立验证维度，SR +0.10 已计入）",
    });
  }
  if (b.persistence_score >= 0.7) {
    chips.push({
      key: "persist",
      label: `持续 ${(b.persistence_score * 100).toFixed(0)}%`,
      cls: "border-emerald-400 bg-emerald-400/10 text-emerald-300",
      tip: `墙在最近 1h 内可见 ≥ 70% 时间（持续性硬证据，SR +0.10 已计入）`,
    });
  }
  if (b.liq_magnet_usd > 0) {
    chips.push({
      key: "liq",
      label: b.liq_magnet_label ?? "清算磁铁",
      cls: "border-fuchsia-400 bg-fuchsia-400/10 text-fuchsia-300",
      tip: `区内含清算磁铁 ${formatCnUsd(b.liq_magnet_usd)}（属于扫单吸引来源，与支撑/阻力博弈）`,
    });
  }
  if (chips.length === 0) {
    return (
      <div className="text-[10px] text-slate-500">
        无机构 / 双源 / Coinbase / 持续性 / 清算硬证据
      </div>
    );
  }
  return (
    <div className="flex flex-wrap gap-1">
      {chips.map((c) => (
        <span
          key={c.key}
          className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] ${c.cls}`}
          title={c.tip}
        >
          {c.label}
        </span>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 主组件
// ─────────────────────────────────────────────────────────────────────
export default function ZoneDetailCard({ zone, coin, spotBook, futBook }: Props) {
  if (!zone) {
    return (
      <div className="flex h-full items-center justify-center text-[11px] text-slate-500">
        在左侧价格轴选中一个价格区查看详情
      </div>
    );
  }
  const role = ROLE_COLORS[zone.dominant_role] ?? ROLE_COLORS.other;
  const breakdown = aggregateLiquidity(zone, spotBook, futBook);

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

      {/* 阶段 2.1 + 2.2：流动性分层条 + 硬证据 chip */}
      <section className="space-y-2">
        <LiquidityStackBar b={breakdown} />
        <HardEvidenceChips b={breakdown} />
      </section>

      {/* 阶段 2.3：ScoreBar 阈值色阶 + tooltip */}
      <section className="grid grid-cols-2 gap-x-4 gap-y-2">
        <ScoreBar
          label="支撑信任（已校准）"
          value={zone.support_trust}
          kind="trust"
          hint="支撑信任 = 强度 × (1 − 0.5 × 脆性)；≥0.7 强 / 0.4-0.7 中 / <0.4 弱"
        />
        <ScoreBar
          label="阻力信任（已校准）"
          value={zone.resistance_trust}
          kind="trust"
          hint="阻力信任 = 强度 × (1 − 0.5 × 脆性)；≥0.7 强 / 0.4-0.7 中 / <0.4 弱"
        />
        <ScoreBar
          label="扫单吸引"
          value={zone.sweep_attractiveness}
          kind="risk"
          hint="价格被打到此处的吸引力；≥0.7 高（容易被扫）/ 0.4-0.7 中 / <0.4 低"
        />
        <ScoreBar
          label="打穿风险"
          value={zone.break_through_risk}
          kind="risk"
          hint={
            "墙体被吃穿继续延伸的概率（结合扫单吸引 + 撤墙风险 + 反向 CVD）；" +
            "\n≥0.7 高（不建议提前接，等扫单反应）" +
            "\n0.4-0.7 中（缩小仓位限价试错）" +
            "\n<0.4 低（限价试错较稳）"
          }
        />
        <ScoreBar
          label="数据可信度"
          value={zone.data_confidence}
          kind="trust"
          hint="该区底层数据完整度与新鲜度；<0.4 数据不足时所有评分应打折看待"
        />
      </section>

      {(zone.support_strength != null || zone.resistance_strength != null) && (
        <section>
          <h4 className="text-[10px] uppercase tracking-wider text-slate-500">
            评分透明化（强度 vs 脆性）
          </h4>
          <p className="mt-0.5 text-[10px] text-slate-500 leading-snug">
            校准信任 = 强度 × (1 − 0.5 × 脆性)；脆性来自同区合约层 active_attack / 撤墙风险
          </p>
          <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-2">
            {(zone.support_strength ?? 0) > 0 && (
              <>
                <ScoreBar
                  label="支撑强度（硬证据）"
                  value={zone.support_strength ?? 0}
                  kind="trust"
                />
                <ScoreBar
                  label="支撑脆性（攻击信号）"
                  value={zone.support_fragility ?? 0}
                  kind="risk"
                />
              </>
            )}
            {(zone.resistance_strength ?? 0) > 0 && (
              <>
                <ScoreBar
                  label="阻力强度（硬证据）"
                  value={zone.resistance_strength ?? 0}
                  kind="trust"
                />
                <ScoreBar
                  label="阻力脆性（攻击信号）"
                  value={zone.resistance_fragility ?? 0}
                  kind="risk"
                />
              </>
            )}
          </div>
        </section>
      )}

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
