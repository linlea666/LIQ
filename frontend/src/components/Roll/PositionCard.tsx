"use client";

/**
 * PositionCard · 持仓概况大卡片
 *
 * 结构：
 *   [ 顶部 ] 币种徽章 + action/urgency/dataQuality + 模板/杠杆/逐全仓
 *   [ 关键指标行 ] 现价 / 均价 / 持仓量 / 保证金 / 有效杠杆 / 浮盈
 *   [ 关键距离 ] 距爆仓% / 距止损% / 均价距现价%（带色阶警告）
 *   [ 白话 ]     headline_cn（大） + detail_cn（灰）
 *
 * 数据来源：UserPosition（恒有）+ RollSignal（可能为 undefined，首轮评估前）
 */

import type { RollPlan, RollSignal, UserPosition } from "@/lib/rollTypes";
import {
  ActionBadge,
  DataQualityBadge,
  UrgencyBadge,
} from "./SignalBadges";

interface Props {
  position: UserPosition;
  signal: RollSignal | undefined;
  plan: RollPlan | undefined;
}

export default function PositionCard({ position, signal, plan }: Props) {
  const isLong = position.side === "long";
  const price = signal?.current_price ?? position.entry_price;
  const pnlPct = signal?.unrealized_pnl_pct ?? 0;
  const pnlUsd = signal?.unrealized_pnl_usd ?? 0;
  const effLev = signal?.effective_leverage ?? position.leverage;

  const liqDistPct = signal?.distance_to_liq_pct ?? null;
  const slDistPct = signal?.distance_to_sl_pct ?? null;
  // 均价距现价：做多时 (price - entry)/price；做空相反。这里直接用 pnlPct 符号 × 杠杆逆推不可靠；
  // 改用 (entry_price 相对 price 的距离)
  const avgVsPricePct =
    price > 0
      ? ((price - position.entry_price) / price) * 100 * (isLong ? 1 : -1)
      : 0;

  return (
    <section className="rounded-lg border border-slate-800 bg-gradient-to-b from-slate-900/70 to-slate-900/30">
      {/* ── 顶部 ── */}
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-4 py-3">
        <div className="flex items-center gap-3">
          <div
            className={[
              "flex h-11 w-11 items-center justify-center rounded-full text-[13px] font-bold",
              isLong
                ? "bg-emerald-900/50 text-emerald-300"
                : "bg-rose-900/50 text-rose-300",
            ].join(" ")}
          >
            {isLong ? "多" : "空"}
          </div>
          <div>
            <div className="flex items-baseline gap-2">
              <span className="text-lg font-semibold text-slate-100">
                {position.coin}
              </span>
              <span className="text-[11px] text-slate-500">
                {position.margin_mode === "isolated" ? "逐仓" : "全仓"} · {position.leverage}x
                {plan?.template_id && (
                  <>
                    {" · "}
                    <span className="rounded bg-slate-800 px-1 py-0.5 text-[10px] text-slate-400">
                      {plan.template_id}
                    </span>
                  </>
                )}
              </span>
            </div>
            {position.note && (
              <div className="mt-0.5 max-w-[480px] truncate text-[11px] text-slate-500">
                💬 {position.note}
              </div>
            )}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {signal ? (
            <>
              <ActionBadge action={signal.action} />
              <UrgencyBadge urgency={signal.urgency} />
              <DataQualityBadge
                quality={signal.data_quality}
                missing={signal.missing_inputs}
              />
            </>
          ) : (
            <span className="text-[11px] text-slate-500">
              等待引擎首轮评估…
            </span>
          )}
        </div>
      </header>

      {/* ── 关键指标行 ── */}
      <div className="grid grid-cols-2 gap-3 border-b border-slate-800 px-4 py-3 sm:grid-cols-3 md:grid-cols-6">
        <Metric label="现价" value={price.toLocaleString()} />
        <Metric label="均价" value={position.entry_price.toLocaleString()} />
        <Metric label="数量" value={position.position_size.toFixed(6)} />
        <Metric
          label="保证金"
          value={`${position.margin_used_usd.toFixed(2)} USD`}
        />
        <Metric label="有效杠杆" value={`${effLev.toFixed(2)}x`} />
        <Metric
          label="浮盈"
          value={`${pnlUsd >= 0 ? "+" : ""}${pnlUsd.toFixed(2)}`}
          sub={`${pnlUsd >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%`}
          tone={pnlPct >= 0 ? "up" : "down"}
        />
      </div>

      {/* ── 关键距离 ── */}
      <div className="grid grid-cols-1 gap-3 border-b border-slate-800 px-4 py-3 sm:grid-cols-3">
        <DistanceCell
          label="距爆仓"
          pct={liqDistPct}
          warnAt={10}
          critAt={5}
          goodAbove
          helpText="引擎以当前价与估算爆仓价的相对距离为准。过近时优先提示减仓或离场。"
        />
        <DistanceCell
          label="距止损"
          pct={slDistPct}
          warnAt={2}
          critAt={0.5}
          goodAbove
          helpText="越近越容易被扫止损；配合关键位使用更稳妥。"
        />
        <DistanceCell
          label="均价距现价"
          pct={avgVsPricePct}
          warnAt={null}
          critAt={null}
          goodAbove
          helpText="正值=持有方向处于获利状态；负值=现价已被均价套住。"
        />
      </div>

      {/* ── 白话 ── */}
      {signal?.headline_cn && (
        <div className="px-4 py-3">
          <div className="text-[13px] text-slate-100">
            {signal.headline_cn}
          </div>
          {signal.detail_cn && (
            <div className="mt-1 whitespace-pre-wrap text-[12px] text-slate-400">
              {signal.detail_cn}
            </div>
          )}
        </div>
      )}

      {/* ── 止损显示 ── */}
      <div className="flex flex-wrap items-center gap-4 border-t border-slate-800 px-4 py-2 text-[11px] text-slate-400">
        <span>
          止损：{" "}
          <span className="font-mono text-slate-200">
            {position.stop_loss
              ? position.stop_loss.toLocaleString()
              : "未设置"}
          </span>
          {position.initial_stop_loss &&
            position.stop_loss !== position.initial_stop_loss && (
              <span className="ml-1 text-[10px] text-slate-500">
                (初始 {position.initial_stop_loss.toLocaleString()})
              </span>
            )}
        </span>
        <span>
          爆仓价：{" "}
          <span className="font-mono text-slate-200">
            {position.liq_price ? position.liq_price.toLocaleString() : "-"}
          </span>
        </span>
        <span className="text-[10px] text-slate-500">
          更新 {new Date(position.updated_at * 1000).toLocaleTimeString("zh-CN", { hour12: false })}
        </span>
      </div>
    </section>
  );
}

function Metric({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "up" | "down";
}) {
  const mainTone =
    tone === "up"
      ? "text-emerald-300"
      : tone === "down"
      ? "text-rose-300"
      : "text-slate-100";
  return (
    <div className="min-w-0">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`mt-0.5 font-mono text-[14px] ${mainTone}`}>{value}</div>
      {sub && <div className={`mt-0.5 font-mono text-[10px] ${mainTone}`}>{sub}</div>}
    </div>
  );
}

function DistanceCell({
  label,
  pct,
  warnAt,
  critAt,
  goodAbove,
  helpText,
}: {
  label: string;
  pct: number | null;
  warnAt: number | null;
  critAt: number | null;
  goodAbove: boolean;
  helpText: string;
}) {
  if (pct === null || !Number.isFinite(pct)) {
    return (
      <div className="rounded border border-slate-800 bg-slate-900/50 px-3 py-2">
        <div className="text-[10px] text-slate-500">{label}</div>
        <div className="mt-0.5 font-mono text-[14px] text-slate-500">-</div>
      </div>
    );
  }

  const abs = Math.abs(pct);
  let tone = "text-slate-100";
  let barTone = "bg-slate-700";
  if (critAt !== null && abs <= critAt && goodAbove) {
    tone = "text-rose-300";
    barTone = "bg-rose-600";
  } else if (warnAt !== null && abs <= warnAt && goodAbove) {
    tone = "text-amber-300";
    barTone = "bg-amber-600";
  } else if (pct < 0 && goodAbove) {
    tone = "text-rose-300";
    barTone = "bg-rose-600";
  } else {
    tone = "text-emerald-300";
    barTone = "bg-emerald-600";
  }

  const barPct = Math.max(2, Math.min(100, abs * 3)); // 可视化：每 1% 占 3%

  return (
    <div
      className="rounded border border-slate-800 bg-slate-900/50 px-3 py-2"
      title={helpText}
    >
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] text-slate-500">{label}</span>
        <span className={`font-mono text-[14px] ${tone}`}>
          {pct >= 0 ? "+" : ""}
          {pct.toFixed(2)}%
        </span>
      </div>
      <div className="mt-1 h-1 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full ${barTone}`}
          style={{ width: `${barPct}%` }}
        />
      </div>
    </div>
  );
}
