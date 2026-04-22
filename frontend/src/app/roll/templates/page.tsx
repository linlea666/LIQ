"use client";

/**
 * /roll/templates · 模板管理（Step 8 完整 CRUD）
 *
 * 布局：左侧列表 + 右侧编辑器（按选中 id 重挂载）
 *
 * 交互：
 *   - builtin 模板只读，提供"派生副本以自定义"按钮
 *   - custom:xxx 可编辑 / 删除 / 重命名
 *   - 保存：直接把编辑器的字段集合作为 patch 透传给 PUT /api/roll/templates/{id}
 *
 * 硬约束（均由后端 validate_template 兜底，前端提前提示）：
 *   - 阈值范围 / 严格递减
 *   - 闸门范围
 *   - add_mode 特定参数范围
 *   - max_add_times ∈ [1,10]，trail_sl_after_add_n ∈ [1, max_add_times]
 */

import { useEffect, useMemo, useState } from "react";

import { useRollStore } from "@/stores/rollStore";
import type {
  AddMode,
  AddTrigger,
  MarginMode,
  ReduceSignal,
  RollTemplate,
} from "@/lib/rollTypes";

const ADD_MODE_LABEL: Record<AddMode, string> = {
  passive_deleveraging: "被动去杠杆（肥仔派）",
  pyramid_decay: "金字塔衰减",
  layered_independent: "分层独立（李法师派）",
  fixed_ratio: "定比例",
};

const ADD_TRIGGER_LABEL: Record<AddTrigger, string> = {
  structure_breakout_retest: "结构突破回踩",
  key_level_bounce: "关键位反弹",
  ema_pullback_reclaim: "均线回踩重新站稳",
  float_profit_pct: "浮盈达到阈值",
  squeeze_release: "Squeeze 释放方向确认",
  range_boundary_reversal: "区间边界反转",
  fake_break_reversal: "假突破回收反手",
};

const REDUCE_SIGNAL_LABEL: Record<ReduceSignal, string> = {
  long_upper_wick: "长上影",
  long_lower_wick: "长下影",
  cvd_bear_div: "CVD 顶背离",
  cvd_bull_div: "CVD 底背离",
  sweep_fail_to_hold: "扫盘未站稳",
  exhaustion_warn: "动能衰竭预警",
  volume_stall_at_extreme: "极值位缩量",
  fake_break: "假突破",
  structure_choch_against: "结构 CHoCH 反向",
  funding_extreme: "资金费率极端",
  reversal_pattern: "反转 K 线形态",
};

export default function RollTemplatesPage() {
  const templates = useRollStore((s) => s.templates);
  const loadTemplates = useRollStore((s) => s.loadTemplates);
  const loading = useRollStore((s) => s.templatesLoading);
  const deriveTemplate = useRollStore((s) => s.deriveTemplate);

  const [selectedId, setSelectedId] = useState<string>("");
  const [deriveOpen, setDeriveOpen] = useState(false);
  const [deriveSource, setDeriveSource] = useState<string>("");
  const [deriveNewId, setDeriveNewId] = useState<string>("custom:");
  const [deriveNewName, setDeriveNewName] = useState<string>("");
  const [deriving, setDeriving] = useState(false);
  const [deriveErr, setDeriveErr] = useState<string | null>(null);

  useEffect(() => {
    loadTemplates();
  }, [loadTemplates]);

  const effectiveId = selectedId || templates[0]?.id || "";
  const selected = useMemo(
    () => templates.find((t) => t.id === effectiveId),
    [templates, effectiveId],
  );

  const handleOpenDerive = (sourceId: string) => {
    setDeriveSource(sourceId);
    setDeriveNewId("custom:");
    setDeriveNewName("");
    setDeriveErr(null);
    setDeriveOpen(true);
  };

  const handleDerive = async () => {
    const id = deriveNewId.trim();
    const name = deriveNewName.trim();
    if (!id.startsWith("custom:") || id === "custom:") {
      setDeriveErr('id 必须以 "custom:" 前缀，如 custom:my-bull');
      return;
    }
    if (!name) {
      setDeriveErr("请填写模板名称");
      return;
    }
    setDeriving(true);
    setDeriveErr(null);
    try {
      const tpl = await deriveTemplate({
        source_id: deriveSource,
        new_id: id,
        new_name: name,
      });
      setDeriveOpen(false);
      setSelectedId(tpl.id);
    } catch (e) {
      setDeriveErr((e as Error).message);
    } finally {
      setDeriving(false);
    }
  };

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[280px,1fr]">
      {/* ── 左侧：模板列表 ── */}
      <aside className="space-y-2">
        <div className="flex items-center justify-between">
          <h1 className="text-base font-semibold">模板</h1>
          <span className="text-[11px] text-slate-500">
            {templates.length} 套
          </span>
        </div>

        <div className="rounded-lg border border-slate-800 bg-slate-900/40">
          {loading && templates.length === 0 ? (
            <div className="py-6 text-center text-[12px] text-slate-500">
              加载中…
            </div>
          ) : (
            <ul className="divide-y divide-slate-800">
              {templates.map((t) => {
                const active = t.id === effectiveId;
                return (
                  <li key={t.id}>
                    <button
                      type="button"
                      onClick={() => setSelectedId(t.id)}
                      className={[
                        "flex w-full flex-col items-start gap-0.5 px-3 py-2 text-left transition",
                        active
                          ? "bg-emerald-900/20"
                          : "hover:bg-slate-800/50",
                      ].join(" ")}
                    >
                      <div className="flex w-full items-baseline justify-between gap-2">
                        <span
                          className={[
                            "truncate text-[13px]",
                            active ? "text-emerald-200" : "text-slate-100",
                          ].join(" ")}
                        >
                          {t.name}
                        </span>
                        {t.builtin ? (
                          <span className="shrink-0 rounded bg-sky-900/40 px-1.5 py-0.5 text-[10px] text-sky-300">
                            内置
                          </span>
                        ) : (
                          <span className="shrink-0 rounded bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-300">
                            自定义
                          </span>
                        )}
                      </div>
                      <span className="truncate text-[10px] text-slate-500">
                        {t.id}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {selected && (
          <button
            type="button"
            onClick={() => handleOpenDerive(selected.id)}
            className="w-full rounded-md border border-emerald-600/50 bg-emerald-950/40 px-3 py-2 text-[12px] text-emerald-200 transition hover:bg-emerald-900/50"
          >
            + 基于「{selected.name}」派生副本
          </button>
        )}
      </aside>

      {/* ── 右侧：编辑器 ── */}
      <div>
        {selected ? (
          <TemplateEditor
            key={selected.id}
            template={selected}
            onDerive={() => handleOpenDerive(selected.id)}
          />
        ) : (
          <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-6 text-center text-[12px] text-slate-500">
            选择或派生一个模板开始编辑
          </div>
        )}
      </div>

      {/* ── 派生弹窗 ── */}
      {deriveOpen && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 px-4"
          onClick={() => !deriving && setDeriveOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-lg border border-slate-700 bg-slate-900 p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-base font-semibold">
              派生自定义模板
            </h2>
            <p className="mt-1 text-[11px] text-slate-500">
              源模板：<span className="text-slate-300">{deriveSource}</span>
            </p>
            <div className="mt-3 space-y-3 text-[12px]">
              <label className="flex flex-col gap-1">
                <span className="text-slate-400">新模板 id</span>
                <input
                  className="roll-input"
                  value={deriveNewId}
                  onChange={(e) => setDeriveNewId(e.target.value)}
                  placeholder="custom:my-strategy"
                />
                <span className="text-[10px] text-slate-500">
                  必须以 <code>custom:</code> 前缀
                </span>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-slate-400">显示名称</span>
                <input
                  className="roll-input"
                  value={deriveNewName}
                  onChange={(e) => setDeriveNewName(e.target.value)}
                  placeholder="例：我的 4H 趋势滚仓"
                />
              </label>
              {deriveErr && (
                <div className="rounded border border-rose-600/40 bg-rose-950/40 px-2 py-1 text-[11px] text-rose-200">
                  ❌ {deriveErr}
                </div>
              )}
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setDeriveOpen(false)}
                disabled={deriving}
                className="rounded-md border border-slate-700 px-3 py-1.5 text-[12px] text-slate-300 hover:bg-slate-800"
              >
                取消
              </button>
              <button
                onClick={handleDerive}
                disabled={deriving}
                className="rounded-md bg-emerald-600 px-4 py-1.5 text-[12px] font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
              >
                {deriving ? "创建中…" : "创建"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// TemplateEditor
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

interface TplDraft {
  name: string;
  description: string;
  recommended_margin_mode: MarginMode;

  add_mode: AddMode;
  target_leverage: number;
  pyramid_decay_ratio: number;
  layered_pct_of_account: number;
  fixed_ratio_of_position: number;

  min_profit_pct_to_add: number;
  max_add_times: number;
  default_add_triggers: AddTrigger[];

  default_reduce_signals: ReduceSignal[];
  reduce_step_size_pct: number;

  trail_sl_after_add_n: number;
  trail_sl_atr_mult: number;

  // gates
  min_avg_distance_pct: number;
  min_liq_distance_pct: number;
  max_eff_leverage: number;
  min_add_margin_usd: number;
  min_add_bar_distance_atr: number;

  // thresholds
  full_add: number;
  half_add: number;
  small_add: number;
  full_reduce: number;
  half_reduce: number;

  max_margin_pct_of_account: number;
}

function templateToDraft(t: RollTemplate): TplDraft {
  return {
    name: t.name,
    description: t.description,
    recommended_margin_mode: t.recommended_margin_mode,

    add_mode: t.add_mode,
    target_leverage: t.target_leverage,
    pyramid_decay_ratio: t.pyramid_decay_ratio,
    layered_pct_of_account: t.layered_pct_of_account,
    fixed_ratio_of_position: t.fixed_ratio_of_position,

    min_profit_pct_to_add: t.min_profit_pct_to_add,
    max_add_times: t.max_add_times,
    default_add_triggers: [...t.default_add_triggers],

    default_reduce_signals: [...t.default_reduce_signals],
    reduce_step_size_pct: t.reduce_step_size_pct,

    trail_sl_after_add_n: t.trail_sl_after_add_n,
    trail_sl_atr_mult: t.trail_sl_atr_mult,

    min_avg_distance_pct: t.gates.min_avg_distance_pct,
    min_liq_distance_pct: t.gates.min_liq_distance_pct,
    max_eff_leverage: t.gates.max_eff_leverage,
    min_add_margin_usd: t.gates.min_add_margin_usd,
    min_add_bar_distance_atr: t.gates.min_add_bar_distance_atr,

    full_add: t.thresholds.full_add,
    half_add: t.thresholds.half_add,
    small_add: t.thresholds.small_add,
    full_reduce: t.thresholds.full_reduce,
    half_reduce: t.thresholds.half_reduce,

    max_margin_pct_of_account: t.max_margin_pct_of_account,
  };
}

function draftToPatch(d: TplDraft): Record<string, unknown> {
  return {
    name: d.name,
    description: d.description,
    recommended_margin_mode: d.recommended_margin_mode,
    add_mode: d.add_mode,
    target_leverage: d.target_leverage,
    pyramid_decay_ratio: d.pyramid_decay_ratio,
    layered_pct_of_account: d.layered_pct_of_account,
    fixed_ratio_of_position: d.fixed_ratio_of_position,
    min_profit_pct_to_add: d.min_profit_pct_to_add,
    max_add_times: d.max_add_times,
    default_add_triggers: d.default_add_triggers,
    default_reduce_signals: d.default_reduce_signals,
    reduce_step_size_pct: d.reduce_step_size_pct,
    trail_sl_after_add_n: d.trail_sl_after_add_n,
    trail_sl_atr_mult: d.trail_sl_atr_mult,
    gates: {
      min_avg_distance_pct: d.min_avg_distance_pct,
      min_liq_distance_pct: d.min_liq_distance_pct,
      max_eff_leverage: d.max_eff_leverage,
      min_add_margin_usd: d.min_add_margin_usd,
      min_add_bar_distance_atr: d.min_add_bar_distance_atr,
    },
    thresholds: {
      full_add: d.full_add,
      half_add: d.half_add,
      small_add: d.small_add,
      full_reduce: d.full_reduce,
      half_reduce: d.half_reduce,
    },
    max_margin_pct_of_account: d.max_margin_pct_of_account,
  };
}

function validateDraft(d: TplDraft): string | null {
  if (!d.name.trim()) return "名称不能为空";
  if (d.max_add_times < 1 || d.max_add_times > 10) return "加仓上限应 ∈ [1, 10]";
  if (d.trail_sl_after_add_n < 1 || d.trail_sl_after_add_n > d.max_add_times)
    return `trail_sl_after_add_n 应 ∈ [1, ${d.max_add_times}]`;
  if (!(d.small_add < d.half_add && d.half_add < d.full_add))
    return "加仓阈值必须严格递减 small < half < full";
  if (!(d.half_reduce < d.full_reduce))
    return "减仓阈值必须严格递减 half < full";
  const ranges: [keyof TplDraft, number, number, string][] = [
    ["full_add", 65, 85, "full_add"],
    ["half_add", 45, 65, "half_add"],
    ["small_add", 25, 45, "small_add"],
    ["full_reduce", 50, 75, "full_reduce"],
    ["half_reduce", 30, 55, "half_reduce"],
    ["min_avg_distance_pct", 1, 8, "闸门 A 均价距"],
    ["min_liq_distance_pct", 5, 30, "闸门 B 爆仓距"],
    ["max_eff_leverage", 2, 30, "闸门 C 有效杠杆"],
    ["min_add_margin_usd", 1, 1000, "最小加仓量"],
  ];
  for (const [k, lo, hi, label] of ranges) {
    const v = Number(d[k]);
    if (!(v >= lo && v <= hi)) return `${label}=${v} 超出范围 [${lo}, ${hi}]`;
  }
  if (d.max_margin_pct_of_account < 0.05 || d.max_margin_pct_of_account > 0.5)
    return "单计划账户占用上限应 ∈ [5%, 50%]";

  if (d.add_mode === "passive_deleveraging" && !(d.target_leverage >= 1.1 && d.target_leverage <= 30))
    return "target_leverage 应 ∈ [1.1, 30]";
  if (d.add_mode === "pyramid_decay" && !(d.pyramid_decay_ratio >= 0.1 && d.pyramid_decay_ratio <= 0.95))
    return "pyramid_decay_ratio 应 ∈ [0.1, 0.95]";
  if (d.add_mode === "layered_independent" && !(d.layered_pct_of_account >= 0.01 && d.layered_pct_of_account <= 0.30))
    return "layered_pct_of_account 应 ∈ [1%, 30%]";
  if (d.add_mode === "fixed_ratio" && !(d.fixed_ratio_of_position >= 0.05 && d.fixed_ratio_of_position <= 0.50))
    return "fixed_ratio_of_position 应 ∈ [5%, 50%]";
  return null;
}

function TemplateEditor({
  template,
  onDerive,
}: {
  template: RollTemplate;
  onDerive: () => void;
}) {
  const updateTemplate = useRollStore((s) => s.updateTemplate);
  const deleteTemplate = useRollStore((s) => s.deleteTemplate);

  const [draft, setDraft] = useState<TplDraft>(() => templateToDraft(template));
  const readonly = template.builtin;

  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const preflightErr = useMemo(() => validateDraft(draft), [draft]);

  const handleSave = async () => {
    setMsg(null);
    setErr(null);
    if (preflightErr) {
      setErr(preflightErr);
      return;
    }
    setSaving(true);
    try {
      await updateTemplate(template.id, { patch: draftToPatch(draft) });
      setMsg("已保存");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setDraft(templateToDraft(template));
    setMsg(null);
    setErr(null);
  };

  const handleDelete = async () => {
    if (!confirm(`确认删除模板「${template.name}」？此操作不可恢复。`)) return;
    setDeleting(true);
    try {
      await deleteTemplate(template.id);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setDeleting(false);
    }
  };

  const patch = <K extends keyof TplDraft>(k: K, v: TplDraft[K]) =>
    setDraft((d) => ({ ...d, [k]: v }));

  const toggleFromSet = <T,>(arr: T[], item: T): T[] =>
    arr.includes(item) ? arr.filter((x) => x !== item) : [...arr, item];

  return (
    <div className="space-y-4">
      {/* ── 顶部状态 ── */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex items-baseline gap-2">
            <h2 className="text-lg font-semibold">{template.name}</h2>
            <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
              {template.id}
            </span>
            {template.builtin && (
              <span className="rounded bg-sky-900/40 px-1.5 py-0.5 text-[10px] text-sky-300">
                内置 · 只读
              </span>
            )}
          </div>
          <p className="mt-0.5 text-[11px] text-slate-500 max-w-2xl">
            {template.description || "（暂无说明）"}
          </p>
        </div>

        <div className="flex gap-2">
          {readonly ? (
            <button
              onClick={onDerive}
              className="rounded-md border border-emerald-600/50 bg-emerald-950/40 px-3 py-1.5 text-[12px] text-emerald-200 hover:bg-emerald-900/50"
            >
              派生副本以自定义
            </button>
          ) : (
            <>
              <button
                onClick={handleReset}
                disabled={saving}
                className="rounded-md border border-slate-700 px-3 py-1.5 text-[12px] text-slate-300 hover:bg-slate-800"
              >
                重置
              </button>
              <button
                onClick={handleDelete}
                disabled={deleting || saving}
                className="rounded-md border border-rose-700/50 bg-rose-950/30 px-3 py-1.5 text-[12px] text-rose-200 hover:bg-rose-900/50 disabled:opacity-50"
              >
                {deleting ? "删除中…" : "删除"}
              </button>
              <button
                onClick={handleSave}
                disabled={saving || !!preflightErr}
                className="rounded-md bg-emerald-600 px-4 py-1.5 text-[12px] font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
              >
                {saving ? "保存中…" : "保存"}
              </button>
            </>
          )}
        </div>
      </div>

      {msg && (
        <div className="rounded border border-emerald-700/40 bg-emerald-950/40 px-3 py-2 text-[12px] text-emerald-200">
          ✓ {msg}
        </div>
      )}
      {err && (
        <div className="rounded border border-rose-700/40 bg-rose-950/40 px-3 py-2 text-[12px] text-rose-200">
          ❌ {err}
        </div>
      )}
      {preflightErr && !err && (
        <div className="rounded border border-amber-700/40 bg-amber-950/30 px-3 py-2 text-[12px] text-amber-200">
          ⚠ {preflightErr}
        </div>
      )}

      <fieldset disabled={readonly} className={readonly ? "opacity-75" : ""}>
        {/* ── 元信息 ── */}
        <Card title="元信息">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="显示名称">
              <input
                className="roll-input"
                value={draft.name}
                onChange={(e) => patch("name", e.target.value)}
              />
            </Field>
            <Field label="推荐保证金模式">
              <div className="flex gap-2">
                {(["isolated", "cross"] as MarginMode[]).map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => patch("recommended_margin_mode", m)}
                    className={[
                      "flex-1 rounded-md border px-2 py-1.5 text-[12px] transition",
                      draft.recommended_margin_mode === m
                        ? "border-sky-500 bg-sky-900/30 text-sky-200"
                        : "border-slate-700 text-slate-400 hover:border-slate-500",
                    ].join(" ")}
                  >
                    {m === "isolated" ? "逐仓" : "全仓"}
                  </button>
                ))}
              </div>
            </Field>
            <Field label="单计划账户占用上限 (%)" hint="[5%, 50%]">
              <input
                type="number"
                min={5}
                max={50}
                step={0.5}
                className="roll-input"
                value={(draft.max_margin_pct_of_account * 100).toFixed(1)}
                onChange={(e) =>
                  patch("max_margin_pct_of_account", Number(e.target.value) / 100)
                }
              />
            </Field>
          </div>
          <Field label="说明">
            <textarea
              rows={2}
              className="roll-input resize-none"
              value={draft.description}
              onChange={(e) => patch("description", e.target.value)}
            />
          </Field>
        </Card>

        {/* ── 加仓模式 ── */}
        <Card title="加仓模式">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {(Object.keys(ADD_MODE_LABEL) as AddMode[]).map((m) => {
              const active = draft.add_mode === m;
              return (
                <button
                  key={m}
                  type="button"
                  onClick={() => patch("add_mode", m)}
                  className={[
                    "rounded-md border px-3 py-2 text-left text-[12px] transition",
                    active
                      ? "border-emerald-500 bg-emerald-900/20"
                      : "border-slate-700 bg-slate-900/60 hover:border-slate-500",
                  ].join(" ")}
                >
                  <div className="flex items-baseline justify-between">
                    <span className="font-medium">{ADD_MODE_LABEL[m]}</span>
                    <code className="text-[10px] text-slate-500">{m}</code>
                  </div>
                </button>
              );
            })}
          </div>

          {draft.add_mode === "passive_deleveraging" && (
            <Field label="维持名义杠杆 target_leverage" hint="[1.1, 30]">
              <input
                type="number"
                min={1.1}
                max={30}
                step={0.1}
                className="roll-input"
                value={draft.target_leverage}
                onChange={(e) => patch("target_leverage", Number(e.target.value))}
              />
            </Field>
          )}
          {draft.add_mode === "pyramid_decay" && (
            <Field label="衰减比例 pyramid_decay_ratio" hint="[0.10, 0.95]">
              <input
                type="number"
                min={0.1}
                max={0.95}
                step={0.05}
                className="roll-input"
                value={draft.pyramid_decay_ratio}
                onChange={(e) =>
                  patch("pyramid_decay_ratio", Number(e.target.value))
                }
              />
            </Field>
          )}
          {draft.add_mode === "layered_independent" && (
            <Field label="每层 / 账户比例 (%)" hint="[1%, 30%]">
              <input
                type="number"
                min={1}
                max={30}
                step={0.5}
                className="roll-input"
                value={(draft.layered_pct_of_account * 100).toFixed(1)}
                onChange={(e) =>
                  patch("layered_pct_of_account", Number(e.target.value) / 100)
                }
              />
            </Field>
          )}
          {draft.add_mode === "fixed_ratio" && (
            <Field label="加仓比例 / 当前仓位 (%)" hint="[5%, 50%]">
              <input
                type="number"
                min={5}
                max={50}
                step={0.5}
                className="roll-input"
                value={(draft.fixed_ratio_of_position * 100).toFixed(1)}
                onChange={(e) =>
                  patch("fixed_ratio_of_position", Number(e.target.value) / 100)
                }
              />
            </Field>
          )}

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Field label="最小浮盈触发 (%)">
              <input
                type="number"
                min={0}
                max={50}
                step={0.5}
                className="roll-input"
                value={draft.min_profit_pct_to_add}
                onChange={(e) =>
                  patch("min_profit_pct_to_add", Number(e.target.value))
                }
              />
            </Field>
            <Field label="加仓次数上限" hint="[1, 10]">
              <input
                type="number"
                min={1}
                max={10}
                step={1}
                className="roll-input"
                value={draft.max_add_times}
                onChange={(e) => patch("max_add_times", Math.floor(Number(e.target.value)) || 1)}
              />
            </Field>
          </div>

          <MultiSelect
            label="加仓触发器（默认至少勾 1）"
            options={Object.entries(ADD_TRIGGER_LABEL) as [AddTrigger, string][]}
            value={draft.default_add_triggers}
            onToggle={(v) =>
              patch("default_add_triggers", toggleFromSet(draft.default_add_triggers, v))
            }
          />
        </Card>

        {/* ── 减仓 & 止损 ── */}
        <Card title="减仓与止损">
          <MultiSelect
            label="减仓信号"
            options={Object.entries(REDUCE_SIGNAL_LABEL) as [ReduceSignal, string][]}
            value={draft.default_reduce_signals}
            onToggle={(v) =>
              patch("default_reduce_signals", toggleFromSet(draft.default_reduce_signals, v))
            }
          />

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Field label="每次减仓比例 (%)" hint="full 档">
              <input
                type="number"
                min={5}
                max={100}
                step={1}
                className="roll-input"
                value={(draft.reduce_step_size_pct * 100).toFixed(0)}
                onChange={(e) =>
                  patch("reduce_step_size_pct", Number(e.target.value) / 100)
                }
              />
            </Field>
            <Field
              label="trail_sl_after_add_n"
              hint={`[1, ${draft.max_add_times}] · N=1 即首次加仓后移保本`}
            >
              <input
                type="number"
                min={1}
                max={draft.max_add_times}
                step={1}
                className="roll-input"
                value={draft.trail_sl_after_add_n}
                onChange={(e) =>
                  patch("trail_sl_after_add_n", Math.floor(Number(e.target.value)) || 1)
                }
              />
            </Field>
            <Field label="trail_sl_atr_mult" hint="[0.5, 5]">
              <input
                type="number"
                min={0.5}
                max={5}
                step={0.1}
                className="roll-input"
                value={draft.trail_sl_atr_mult}
                onChange={(e) => patch("trail_sl_atr_mult", Number(e.target.value))}
              />
            </Field>
          </div>
        </Card>

        {/* ── 闸门 ── */}
        <Card title="三道安全闸门 + 间距约束">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Field label="A · 均价距现价 (%)" hint="[1, 8]">
              <input
                type="number"
                min={1}
                max={8}
                step={0.1}
                className="roll-input"
                value={draft.min_avg_distance_pct}
                onChange={(e) => patch("min_avg_distance_pct", Number(e.target.value))}
              />
            </Field>
            <Field label="B · 爆仓距现价 (%)" hint="[5, 30]">
              <input
                type="number"
                min={5}
                max={30}
                step={0.5}
                className="roll-input"
                value={draft.min_liq_distance_pct}
                onChange={(e) => patch("min_liq_distance_pct", Number(e.target.value))}
              />
            </Field>
            <Field label="C · 有效杠杆上限" hint="[2, 30]">
              <input
                type="number"
                min={2}
                max={30}
                step={0.5}
                className="roll-input"
                value={draft.max_eff_leverage}
                onChange={(e) => patch("max_eff_leverage", Number(e.target.value))}
              />
            </Field>
            <Field label="最小加仓量 (USD)" hint="[1, 1000]">
              <input
                type="number"
                min={1}
                max={1000}
                step={1}
                className="roll-input"
                value={draft.min_add_margin_usd}
                onChange={(e) => patch("min_add_margin_usd", Number(e.target.value))}
              />
            </Field>
            <Field label="加仓间距 (× ATR)" hint="距上次加仓至少 N×ATR">
              <input
                type="number"
                min={0}
                max={5}
                step={0.1}
                className="roll-input"
                value={draft.min_add_bar_distance_atr}
                onChange={(e) =>
                  patch("min_add_bar_distance_atr", Number(e.target.value))
                }
              />
            </Field>
          </div>
        </Card>

        {/* ── 置信度阈值 ── */}
        <Card title="置信度阈值（严格递减）">
          <div className="grid grid-cols-3 gap-3">
            <Field label="full_add" hint="[65, 85]">
              <input
                type="number"
                min={65}
                max={85}
                step={1}
                className="roll-input"
                value={draft.full_add}
                onChange={(e) => patch("full_add", Number(e.target.value))}
              />
            </Field>
            <Field label="half_add" hint="[45, 65]">
              <input
                type="number"
                min={45}
                max={65}
                step={1}
                className="roll-input"
                value={draft.half_add}
                onChange={(e) => patch("half_add", Number(e.target.value))}
              />
            </Field>
            <Field label="small_add" hint="[25, 45]">
              <input
                type="number"
                min={25}
                max={45}
                step={1}
                className="roll-input"
                value={draft.small_add}
                onChange={(e) => patch("small_add", Number(e.target.value))}
              />
            </Field>
            <Field label="full_reduce" hint="[50, 75]">
              <input
                type="number"
                min={50}
                max={75}
                step={1}
                className="roll-input"
                value={draft.full_reduce}
                onChange={(e) => patch("full_reduce", Number(e.target.value))}
              />
            </Field>
            <Field label="half_reduce" hint="[30, 55]">
              <input
                type="number"
                min={30}
                max={55}
                step={1}
                className="roll-input"
                value={draft.half_reduce}
                onChange={(e) => patch("half_reduce", Number(e.target.value))}
              />
            </Field>
          </div>
        </Card>
      </fieldset>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 通用子组件
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-3 rounded-lg border border-slate-800 bg-slate-900/40">
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

function MultiSelect<T extends string>({
  label,
  options,
  value,
  onToggle,
}: {
  label: string;
  options: [T, string][];
  value: T[];
  onToggle: (v: T) => void;
}) {
  const set = new Set(value);
  return (
    <div>
      <div className="mb-1 text-[11px] text-slate-400">{label}</div>
      <div className="flex flex-wrap gap-1.5">
        {options.map(([v, l]) => {
          const active = set.has(v);
          return (
            <button
              type="button"
              key={v}
              onClick={() => onToggle(v)}
              className={[
                "rounded border px-2 py-1 text-[11px] transition",
                active
                  ? "border-emerald-500 bg-emerald-900/30 text-emerald-100"
                  : "border-slate-700 bg-slate-900/60 text-slate-400 hover:border-slate-500",
              ].join(" ")}
            >
              {active ? "✓ " : ""}
              {l}
              <span className="ml-1 text-[10px] text-slate-500">({v})</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
