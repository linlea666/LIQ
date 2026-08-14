import type { DataMeta, TradingBrainSnapshot } from "@/lib/types";
import { formatPrice } from "@/lib/format";

interface Props {
  snap: TradingBrainSnapshot;
  loading: boolean;
}

type Tone = "default" | "warn" | "good" | "bad" | "neutral";

function Chip({ label, value, tone = "default", title }: {
  label: string;
  value: React.ReactNode;
  tone?: Tone;
  title?: string;
}) {
  const toneCls = {
    default: "border-slate-700/80 bg-slate-900/70 text-slate-200",
    warn: "border-amber-700/60 bg-amber-950/30 text-amber-200",
    good: "border-emerald-700/60 bg-emerald-950/30 text-emerald-200",
    bad: "border-rose-700/60 bg-rose-950/30 text-rose-200",
    neutral: "border-slate-700/60 bg-slate-800/60 text-slate-300",
  }[tone];
  return (
    <div
      className={`flex items-baseline gap-1.5 rounded border px-2 py-1 text-[11px] tabular-nums ${toneCls}`}
      title={title}
    >
      <span className="text-[9px] tracking-wider opacity-60">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

function cvdLabel(trend: string): { text: string; tone: Tone } {
  if (trend === "rising") return { text: "主动买入 ↑", tone: "good" };
  if (trend === "declining") return { text: "主动卖出 ↓", tone: "bad" };
  return { text: "方向不明", tone: "neutral" };
}

function sourceStatus(meta?: DataMeta): { text: string; tone: Tone } {
  if (!meta || meta.status === "missing") return { text: "数据缺失", tone: "bad" };
  if (meta.status === "stale") return { text: "数据偏旧", tone: "warn" };
  if (meta.status === "pending") return { text: "本周期未收盘", tone: "warn" };
  return { text: "数据正常", tone: "good" };
}

function regimeLabel(description: string, raw: string): string {
  if (description) {
    return description
      .replace(/bullish/gi, "偏多")
      .replace(/bearish/gi, "偏空")
      .replace(/neutral/gi, "中性");
  }
  const labels: Record<string, string> = {
    trend_up: "上升趋势",
    trend_down: "下降趋势",
    range: "区间震荡",
    squeeze: "波动挤压",
    volatile: "高波动",
  };
  return labels[raw] || "结构暂不明确";
}

const GRADE_LABEL = {
  strong: "证据一致度高",
  medium: "证据有分化",
  weak: "证据较弱",
  insufficient: "证据不足",
} as const;

export default function TopStrip({ snap, loading }: Props) {
  const c = snap.context;
  const dq = snap.data_quality;
  const read = c.market_read;
  const cvdSpot = cvdLabel(c.cvd_spot_trend ?? "");
  const cvdFut = cvdLabel(c.cvd_contract_trend ?? "");
  const readTone = read.bias === "bullish" ? "good" : read.bias === "bearish" ? "bad" : read.bias === "insufficient" ? "warn" : "neutral";
  const readCls = {
    good: "border-emerald-600/70 bg-emerald-950/35 text-emerald-100",
    bad: "border-rose-600/70 bg-rose-950/35 text-rose-100",
    warn: "border-amber-600/70 bg-amber-950/35 text-amber-100",
    neutral: "border-slate-600/70 bg-slate-900/60 text-slate-100",
    default: "border-slate-600/70 bg-slate-900/60 text-slate-100",
  }[readTone];

  const oiTone: Tone =
    read.leverage_state === "deleveraging" || read.leverage_state === "leverage_building"
      ? "warn"
      : read.leverage_state === "conflict" || read.leverage_state === "unavailable"
        ? "bad"
        : "neutral";
  const fundingTone: Tone =
    read.funding_state === "long_crowded" || read.funding_state === "short_crowded"
      ? "warn"
      : read.funding_state === "unavailable" ? "bad" : "neutral";

  return (
    <div className="space-y-1.5 px-3 py-2">
      <div className={`rounded-md border px-3 py-2 ${readCls}`}>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] font-semibold">{read.title}</span>
          <span className="rounded-full border border-current/20 px-2 py-0.5 text-[10px] opacity-80">
            {GRADE_LABEL[read.evidence_grade]}
          </span>
          {loading && <span className="text-[10px] opacity-60">刷新中…</span>}
        </div>
        <p className="mt-1 text-[11px] leading-relaxed opacity-85">{read.summary}</p>
        {read.cautions.length > 0 && (
          <p className="mt-1 text-[10px] leading-relaxed opacity-65">{read.cautions.join(" · ")}</p>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <Chip label="现价" value={formatPrice(snap.last_price, snap.coin)} />
        <Chip
          label="平均波动(ATR)"
          value={snap.atr.toFixed(2)}
          title="ATR 是近期平均波动大小，不表示涨跌方向"
        />
        {c.regime && (
          <Chip
            label="市场状态"
            value={regimeLabel(c.regime_description, c.regime)}
            tone={c.regime.includes("trend") ? "warn" : "neutral"}
            title={`高级字段：${c.regime}`}
          />
        )}
        <Chip
          label="现货主动流"
          value={cvdSpot.text}
          tone={cvdSpot.tone}
          title="看现货主动买卖；上升不等于机构长期吸筹"
        />
        <Chip
          label="合约主动流"
          value={cvdFut.text}
          tone={cvdFut.tone}
          title="看合约主动买卖；买入也可能来自空头回补"
        />
        <Chip
          label="持仓(OI) 1h"
          value={c.oi_delta_1h_pct == null ? "不可用" : `${c.oi_delta_1h_pct >= 0 ? "+" : ""}${c.oi_delta_1h_pct.toFixed(3)}%`}
          tone={oiTone}
          title="OI 只表示合约总持仓增减，本身不表示偏多偏空"
        />
        <Chip
          label="资金费/8h"
          value={c.funding_rate_8h_pct == null ? "不可用" : `${c.funding_rate_8h_pct >= 0 ? "+" : ""}${c.funding_rate_8h_pct.toFixed(4)}% · ${c.funding_interpretation || "未定"}`}
          tone={fundingTone}
          title="资金费反映多空拥挤和持仓成本，不是单独的涨跌信号"
        />
        {dq.usd_usdt_basis_pct != null && (
          <Chip
            label="美元/稳定币价差"
            value={`${dq.usd_usdt_basis_pct.toFixed(4)}%`}
            tone={Math.abs(dq.usd_usdt_basis_pct) > 0.05 ? "warn" : "neutral"}
          />
        )}
        {c.nearest_magnet_below != null && (
          <Chip label="可能吸引价(下)" value={formatPrice(c.nearest_magnet_below, snap.coin)} tone="neutral" title="清算密集可能吸引价格靠近，不是支撑，也不是保证到达" />
        )}
        {c.nearest_magnet_above != null && (
          <Chip label="可能吸引价(上)" value={formatPrice(c.nearest_magnet_above, snap.coin)} tone="neutral" title="清算密集可能吸引价格靠近，不是阻力，也不是保证到达" />
        )}
        <span className="ml-auto text-[10px] text-slate-500">
          {`更新于 ${new Date(snap.ts * 1000).toLocaleTimeString("zh-CN")}`}
        </span>
      </div>

      <details className="rounded border border-slate-800/80 bg-slate-950/40 px-2 py-1 text-[10px] text-slate-400">
        <summary className="cursor-pointer select-none text-slate-500">查看数据状态与原始字段</summary>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {Object.entries(dq.context_sources ?? {}).map(([key, meta]) => {
            const status = sourceStatus(meta);
            return (
              <Chip
                key={key}
                label={key}
                value={`${status.text}${meta.staleness_sec ? ` · ${meta.staleness_sec}s` : ""}`}
                tone={status.tone}
                title={`来源：${meta.source || "未知"}；as_of=${meta.as_of || "无"}`}
              />
            );
          })}
          <Chip label="Regime原始值" value={c.regime || "—"} tone="neutral" />
          <Chip label="现货CVD原始值" value={c.cvd_spot_trend || "—"} tone="neutral" />
          <Chip label="合约CVD原始值" value={c.cvd_contract_trend || "—"} tone="neutral" />
        </div>
      </details>
    </div>
  );
}
