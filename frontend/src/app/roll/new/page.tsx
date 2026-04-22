"use client";

/**
 * /roll/new · 新建滚仓计划
 *
 * 流程：
 *   1. 选模板（肥仔派 / 李法师派 / 金字塔 / 保守 / 自定义）
 *   2. 填持仓参数（币种 / 方向 / margin_mode / 杠杆 / 入场 / 保证金 / 止损 / 备注）
 *   3. 预览账户占用：该笔 margin_usd / total_account_usd
 *   4. 提交 → 成功后跳回总览
 *
 * 硬约束：
 *   - leverage ∈ [1, 125]
 *   - long 止损 < 入场；short 止损 > 入场
 *   - 保证金不得超过单计划 max_margin_pct_of_account × 账户总额
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { SUPPORTED_COINS } from "@/lib/constants";
import { useRollStore } from "@/stores/rollStore";
import type {
  CreatePositionReq,
  MarginMode,
  RollTemplate,
  Side,
} from "@/lib/rollTypes";

export default function NewRollPlanPage() {
  const router = useRouter();

  const templates = useRollStore((s) => s.templates);
  const settings = useRollStore((s) => s.settings);
  const loadTemplates = useRollStore((s) => s.loadTemplates);
  const loadSettings = useRollStore((s) => s.loadSettings);
  const createPosition = useRollStore((s) => s.createPosition);

  useEffect(() => {
    if (templates.length === 0) loadTemplates();
    if (!settings) loadSettings();
  }, [templates.length, settings, loadTemplates, loadSettings]);

  // 选中模板：null 表示"还未显式选择"，用派生的第一项兜底
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null);
  const templateId = selectedTemplateId ?? templates[0]?.id ?? "";

  // 用户是否手动覆盖过 margin_mode；未覆盖时跟随模板推荐
  const [marginModeOverride, setMarginModeOverride] = useState<MarginMode | null>(null);

  const [coin, setCoin] = useState<string>(SUPPORTED_COINS[0]);
  const [side, setSide] = useState<Side>("long");
  const [leverage, setLeverage] = useState<number>(10);
  const [entryPrice, setEntryPrice] = useState<string>("");
  const [marginUsd, setMarginUsd] = useState<string>("");
  const [stopLoss, setStopLoss] = useState<string>("");
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const tpl = useMemo<RollTemplate | undefined>(
    () => templates.find((t) => t.id === templateId),
    [templates, templateId],
  );

  const marginMode: MarginMode =
    marginModeOverride ?? tpl?.recommended_margin_mode ?? "isolated";

  const handleSelectTemplate = (id: string) => {
    setSelectedTemplateId(id);
    setMarginModeOverride(null); // 新模板 → 跟随推荐
  };
  const handleSelectMarginMode = (m: MarginMode) => {
    setMarginModeOverride(m);
  };

  const totalAccount = settings?.total_account_usd ?? 10000;

  const margin = Number(marginUsd) || 0;
  const entry = Number(entryPrice) || 0;
  const sl = stopLoss ? Number(stopLoss) : null;

  const singlePlanCap = tpl
    ? totalAccount * tpl.max_margin_pct_of_account
    : totalAccount * 0.3;
  const marginPctOfAccount = totalAccount > 0 ? (margin / totalAccount) * 100 : 0;
  const marginOverCap = margin > singlePlanCap;

  const slInvalid = (() => {
    if (sl === null || !entry) return false;
    if (side === "long") return sl >= entry;
    return sl <= entry;
  })();

  const formValid =
    !!tpl &&
    !!coin &&
    leverage >= 1 &&
    leverage <= 125 &&
    entry > 0 &&
    margin > 0 &&
    !marginOverCap &&
    !slInvalid;

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!tpl || !formValid || submitting) return;
    setErr(null);
    setSubmitting(true);
    try {
      const req: CreatePositionReq = {
        coin,
        side,
        margin_mode: marginMode,
        leverage,
        entry_price: entry,
        margin_usd: margin,
        template_id: tpl.id,
        name: name.trim(),
        note: note.trim(),
        stop_loss: sl,
      };
      const { position } = await createPosition(req);
      router.push(`/roll/${position.id}`);
    } catch (e2) {
      setErr((e2 as Error).message);
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <Link
          href="/roll"
          className="text-[12px] text-slate-400 hover:text-slate-200"
        >
          ← 返回总览
        </Link>
        <h1 className="mt-2 text-xl font-semibold">新建滚仓计划</h1>
        <p className="mt-1 text-[12px] text-slate-500">
          引擎只负责评估与提醒，真实下单仍由你在交易所完成。本地声明的持仓需与交易所保持一致。
        </p>
      </div>

      <form
        onSubmit={onSubmit}
        className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr,320px]"
      >
        {/* 左栏：表单 */}
        <div className="space-y-4">
          <TemplatePicker
            templates={templates}
            selected={templateId}
            onChange={handleSelectTemplate}
          />

          <Card title="基础信息">
            <div className="grid grid-cols-2 gap-3">
              <Field label="币种">
                <select
                  value={coin}
                  onChange={(e) => setCoin(e.target.value)}
                  className="roll-input"
                >
                  {SUPPORTED_COINS.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="方向">
                <div className="flex gap-2">
                  {(["long", "short"] as Side[]).map((s) => (
                    <button
                      type="button"
                      key={s}
                      onClick={() => setSide(s)}
                      className={[
                        "flex-1 rounded-md border px-2 py-1.5 text-[13px] transition",
                        side === s
                          ? s === "long"
                            ? "border-emerald-500 bg-emerald-900/30 text-emerald-200"
                            : "border-rose-500 bg-rose-900/30 text-rose-200"
                          : "border-slate-700 text-slate-400 hover:border-slate-500",
                      ].join(" ")}
                    >
                      {s === "long" ? "做多" : "做空"}
                    </button>
                  ))}
                </div>
              </Field>

              <Field label="保证金模式">
                <div className="flex gap-2">
                  {(["isolated", "cross"] as MarginMode[]).map((m) => (
                    <button
                      type="button"
                      key={m}
                      onClick={() => handleSelectMarginMode(m)}
                      className={[
                        "flex-1 rounded-md border px-2 py-1.5 text-[13px] transition",
                        marginMode === m
                          ? "border-sky-500 bg-sky-900/30 text-sky-200"
                          : "border-slate-700 text-slate-400 hover:border-slate-500",
                      ].join(" ")}
                    >
                      {m === "isolated" ? "逐仓" : "全仓"}
                    </button>
                  ))}
                </div>
              </Field>

              <Field label="杠杆倍数">
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    min={1}
                    max={125}
                    value={leverage}
                    onChange={(e) => setLeverage(Number(e.target.value) || 1)}
                    className="roll-input w-20"
                  />
                  <span className="text-[11px] text-slate-500">
                    {tpl ? `模板目标 ${tpl.target_leverage.toFixed(1)}x` : "1 - 125"}
                  </span>
                </div>
              </Field>
            </div>
          </Card>

          <Card title="入场参数">
            <div className="grid grid-cols-2 gap-3">
              <Field label="入场均价 (USD)">
                <input
                  type="number"
                  min={0}
                  step="any"
                  value={entryPrice}
                  onChange={(e) => setEntryPrice(e.target.value)}
                  placeholder="如 60000"
                  className="roll-input"
                />
              </Field>

              <Field label="保证金 (USD)" hint={`单计划上限 ${singlePlanCap.toFixed(0)} USD`}>
                <input
                  type="number"
                  min={0}
                  step="any"
                  value={marginUsd}
                  onChange={(e) => setMarginUsd(e.target.value)}
                  placeholder="如 1000"
                  className={[
                    "roll-input",
                    marginOverCap ? "border-rose-500 focus:border-rose-400" : "",
                  ].join(" ")}
                />
              </Field>

              <Field
                label="初始止损 (可选)"
                hint={
                  side === "long"
                    ? "做多：止损必须小于入场均价"
                    : "做空：止损必须大于入场均价"
                }
              >
                <input
                  type="number"
                  min={0}
                  step="any"
                  value={stopLoss}
                  onChange={(e) => setStopLoss(e.target.value)}
                  placeholder="留空 = 无止损"
                  className={[
                    "roll-input",
                    slInvalid ? "border-rose-500 focus:border-rose-400" : "",
                  ].join(" ")}
                />
              </Field>

              <Field label="计划别名 (可选)">
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="如 BTC 大周期滚仓-1"
                  className="roll-input"
                />
              </Field>
            </div>

            <Field label="备注 (可选)">
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="当前行情假设、入场逻辑、预期周期等"
                rows={2}
                className="roll-input resize-none"
              />
            </Field>
          </Card>

          {err && (
            <div className="rounded-md border border-rose-600/40 bg-rose-950/40 px-3 py-2 text-[12px] text-rose-200">
              提交失败：{err}
            </div>
          )}

          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={!formValid || submitting}
              className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
            >
              {submitting ? "提交中…" : "创建计划"}
            </button>
            <Link
              href="/roll"
              className="text-[12px] text-slate-400 hover:text-slate-200"
            >
              取消
            </Link>
          </div>
        </div>

        {/* 右栏：模板详情 + 账户占用概览 */}
        <aside className="space-y-4 text-[12px]">
          <Card title="账户占用">
            <Stat
              label="账户总额"
              value={`${totalAccount.toLocaleString()} USD`}
              hint="全局设置，可在 /roll/settings 调整"
            />
            <Stat
              label="本计划保证金"
              value={margin > 0 ? `${margin.toFixed(2)} USD` : "-"}
              hint={
                margin > 0
                  ? `占账户 ${marginPctOfAccount.toFixed(2)}%`
                  : undefined
              }
            />
            <Stat
              label="单计划上限"
              value={`${singlePlanCap.toFixed(0)} USD`}
              hint={tpl ? `模板 max_margin_pct_of_account = ${(tpl.max_margin_pct_of_account * 100).toFixed(0)}%` : ""}
              warn={marginOverCap}
            />
          </Card>

          {tpl && <TemplateDetails tpl={tpl} />}
        </aside>
      </form>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 子组件
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function TemplatePicker({
  templates,
  selected,
  onChange,
}: {
  templates: RollTemplate[];
  selected: string;
  onChange: (id: string) => void;
}) {
  if (templates.length === 0) {
    return (
      <Card title="选择策略模板">
        <div className="text-[12px] text-slate-500">模板加载中…</div>
      </Card>
    );
  }
  return (
    <Card title="选择策略模板">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {templates.map((t) => {
          const active = t.id === selected;
          return (
            <button
              type="button"
              key={t.id}
              onClick={() => onChange(t.id)}
              className={[
                "rounded-md border px-3 py-2 text-left transition",
                active
                  ? "border-emerald-500 bg-emerald-900/20"
                  : "border-slate-700 bg-slate-900/60 hover:border-slate-500",
              ].join(" ")}
            >
              <div className="flex items-center justify-between">
                <span className="font-semibold">{t.name}</span>
                {t.builtin && (
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                    内置
                  </span>
                )}
              </div>
              <div className="mt-1 text-[11px] text-slate-500 line-clamp-2">
                {t.description || t.id}
              </div>
              <div className="mt-2 flex flex-wrap gap-1 text-[10px] text-slate-400">
                <span className="rounded bg-slate-800 px-1.5 py-0.5">
                  {t.add_mode}
                </span>
                <span className="rounded bg-slate-800 px-1.5 py-0.5">
                  杠杆 {t.target_leverage.toFixed(1)}x
                </span>
                <span className="rounded bg-slate-800 px-1.5 py-0.5">
                  加仓上限 {t.max_add_times} 次
                </span>
              </div>
            </button>
          );
        })}
      </div>
    </Card>
  );
}

function TemplateDetails({ tpl }: { tpl: RollTemplate }) {
  return (
    <Card title={`模板默认 · ${tpl.name}`}>
      <Stat label="加仓模式" value={tpl.add_mode} />
      {tpl.add_mode === "pyramid_decay" && (
        <Stat
          label="金字塔衰减"
          value={`× ${tpl.pyramid_decay_ratio.toFixed(2)}`}
        />
      )}
      {tpl.add_mode === "layered_independent" && (
        <Stat
          label="每层比例"
          value={`${(tpl.layered_pct_of_account * 100).toFixed(1)}% of 账户`}
        />
      )}
      {tpl.add_mode === "fixed_ratio" && (
        <Stat
          label="加仓比例"
          value={`× ${tpl.fixed_ratio_of_position.toFixed(2)} of 当前仓位`}
        />
      )}
      <Stat label="最少浮盈触发" value={`${tpl.min_profit_pct_to_add.toFixed(1)}%`} />
      <Stat label="最大加仓次数" value={`${tpl.max_add_times}`} />
      <div className="my-2 border-t border-slate-800" />
      <Stat
        label="闸门 A · 均价距"
        value={`≥ ${tpl.gates.min_avg_distance_pct.toFixed(1)}%`}
      />
      <Stat
        label="闸门 B · 爆仓距"
        value={`≥ ${tpl.gates.min_liq_distance_pct.toFixed(1)}%`}
      />
      <Stat
        label="闸门 C · 有效杠杆"
        value={`≤ ${tpl.gates.max_eff_leverage.toFixed(1)}x`}
      />
      <div className="my-2 border-t border-slate-800" />
      <Stat
        label="置信度阈值"
        value={`full ${tpl.thresholds.full_add.toFixed(0)} / half ${tpl.thresholds.half_add.toFixed(0)} / small ${tpl.thresholds.small_add.toFixed(0)}`}
      />
      <Stat
        label="减仓阈值"
        value={`full ${tpl.thresholds.full_reduce.toFixed(0)} / half ${tpl.thresholds.half_reduce.toFixed(0)}`}
      />
      <div className="my-2 border-t border-slate-800" />
      <Stat
        label="默认加仓触发器"
        value={tpl.default_add_triggers.length.toString()}
        hint={tpl.default_add_triggers.join(" · ") || "—"}
      />
      <Stat
        label="默认减仓信号"
        value={tpl.default_reduce_signals.length.toString()}
        hint={tpl.default_reduce_signals.join(" · ") || "—"}
      />
    </Card>
  );
}

function Card({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40">
      <div className="border-b border-slate-800 px-3 py-2 text-[12px] font-semibold text-slate-300">
        {title}
      </div>
      <div className="space-y-3 p-3">{children}</div>
    </section>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] text-slate-400">{label}</span>
      {children}
      {hint && <span className="text-[10px] text-slate-500">{hint}</span>}
    </label>
  );
}

function Stat({
  label,
  value,
  hint,
  warn,
}: {
  label: string;
  value: string;
  hint?: string;
  warn?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="text-[11px] text-slate-400">{label}</div>
        {hint && (
          <div className="mt-0.5 text-[10px] text-slate-500 break-all">{hint}</div>
        )}
      </div>
      <div
        className={[
          "shrink-0 font-mono text-[12px]",
          warn ? "text-rose-300" : "text-slate-200",
        ].join(" ")}
      >
        {value}
      </div>
    </div>
  );
}
