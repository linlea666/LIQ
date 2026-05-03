"use client";

/**
 * 策略配置面板 · 完整版
 *
 * 模块：
 *   - 全局开关：enabled / coin / horizon
 *   - 通知：browser / email + 各自 min_confidence
 *   - 三策略 grid：每张卡可单独 enable / 改 confidence_threshold / cooldown_min / notes
 *   - auto_disabled 策略明显标识 + 自动禁用原因
 *   - 仅在用户点击"保存"时 PATCH（避免每次拖动都打 API）
 *
 * 使用：包裹在 strategies/page 中，自管 form state，并通过 store.patchConfig 提交
 */

import { useEffect, useMemo, useState } from "react";

import { useScalpStore } from "@/stores/scalpStore";
import {
  STRATEGY_META,
  type ScalpConfig,
  type ScalpConfigPatch,
  type StrategyName,
} from "@/lib/scalpTypes";

interface FormState {
  enabled: boolean;
  coin: string;
  horizon_min: 10 | 30 | 60;
  strategies: Record<
    StrategyName,
    { enabled: boolean; confidence_threshold: number; cooldown_min: number; notes: string }
  >;
  notification: {
    browser_enabled: boolean;
    browser_min_confidence: number;
    email_enabled: boolean;
    email_min_confidence: number;
  };
}

function configToForm(cfg: ScalpConfig): FormState {
  const strategies = {} as FormState["strategies"];
  for (const [name, sc] of Object.entries(cfg.strategies)) {
    strategies[name as StrategyName] = {
      enabled: sc.enabled,
      confidence_threshold: sc.confidence_threshold,
      cooldown_min: sc.cooldown_min,
      notes: sc.notes,
    };
  }
  return {
    enabled: cfg.enabled,
    coin: cfg.coin,
    horizon_min: cfg.horizon_min,
    strategies,
    notification: {
      browser_enabled: cfg.notification.browser_enabled,
      browser_min_confidence: cfg.notification.browser_min_confidence,
      email_enabled: cfg.notification.email_enabled,
      email_min_confidence: cfg.notification.email_min_confidence,
    },
  };
}

function diffPatch(orig: ScalpConfig, form: FormState): ScalpConfigPatch {
  const patch: ScalpConfigPatch = {};
  if (form.enabled !== orig.enabled) patch.enabled = form.enabled;
  if (form.coin !== orig.coin) patch.coin = form.coin;
  if (form.horizon_min !== orig.horizon_min) patch.horizon_min = form.horizon_min;

  const stratPatch: ScalpConfigPatch["strategies"] = {};
  let stratDirty = false;
  for (const [name, sc] of Object.entries(form.strategies) as [StrategyName, FormState["strategies"][StrategyName]][]) {
    const origSc = orig.strategies[name];
    if (!origSc) continue;
    const sub: NonNullable<NonNullable<ScalpConfigPatch["strategies"]>[StrategyName]> = {};
    if (sc.enabled !== origSc.enabled) sub.enabled = sc.enabled;
    if (sc.confidence_threshold !== origSc.confidence_threshold) sub.confidence_threshold = sc.confidence_threshold;
    if (sc.cooldown_min !== origSc.cooldown_min) sub.cooldown_min = sc.cooldown_min;
    if (sc.notes !== origSc.notes) sub.notes = sc.notes;
    if (Object.keys(sub).length > 0) {
      stratPatch[name] = sub;
      stratDirty = true;
    }
  }
  if (stratDirty) patch.strategies = stratPatch;

  const notifPatch: NonNullable<ScalpConfigPatch["notification"]> = {};
  let notifDirty = false;
  if (form.notification.browser_enabled !== orig.notification.browser_enabled) {
    notifPatch.browser_enabled = form.notification.browser_enabled;
    notifDirty = true;
  }
  if (form.notification.browser_min_confidence !== orig.notification.browser_min_confidence) {
    notifPatch.browser_min_confidence = form.notification.browser_min_confidence;
    notifDirty = true;
  }
  if (form.notification.email_enabled !== orig.notification.email_enabled) {
    notifPatch.email_enabled = form.notification.email_enabled;
    notifDirty = true;
  }
  if (form.notification.email_min_confidence !== orig.notification.email_min_confidence) {
    notifPatch.email_min_confidence = form.notification.email_min_confidence;
    notifDirty = true;
  }
  if (notifDirty) patch.notification = notifPatch;

  return patch;
}

export default function StrategyConfigPanel() {
  const config = useScalpStore((s) => s.config);
  const patchConfig = useScalpStore((s) => s.patchConfig);
  const loadConfig = useScalpStore((s) => s.loadConfig);

  const [form, setForm] = useState<FormState | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  // config 变化 → reset form
  useEffect(() => {
    if (config) setForm(configToForm(config));
  }, [config]);

  const dirty = useMemo(() => {
    if (!config || !form) return false;
    return Object.keys(diffPatch(config, form)).length > 0;
  }, [config, form]);

  if (!config || !form) {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-6 text-center text-[12px] text-slate-500">
        加载配置中...
      </div>
    );
  }

  const updateStrategy = (
    name: StrategyName,
    patch: Partial<FormState["strategies"][StrategyName]>,
  ) => {
    setForm((prev) =>
      prev
        ? {
            ...prev,
            strategies: { ...prev.strategies, [name]: { ...prev.strategies[name], ...patch } },
          }
        : prev,
    );
  };

  const handleSave = async () => {
    if (!config || !form) return;
    const patch = diffPatch(config, form);
    if (Object.keys(patch).length === 0) return;
    setSaving(true);
    setErrMsg(null);
    try {
      await patchConfig(patch);
      setSavedAt(Date.now());
    } catch (e) {
      setErrMsg((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setForm(configToForm(config));
    setErrMsg(null);
  };

  const enabledCount = Object.values(form.strategies).filter((s) => s.enabled).length;

  return (
    <div className="space-y-4">
      {/* Action bar */}
      <div className="sticky top-0 z-10 flex items-center justify-between rounded-lg border border-slate-700 bg-slate-900/95 px-4 py-2.5 backdrop-blur">
        <div className="flex items-center gap-3 text-[12px]">
          <span className="font-semibold text-slate-200">策略配置</span>
          <span className="text-slate-500">|</span>
          <span className="text-slate-400">
            启用 <span className="font-mono text-slate-200">{enabledCount}</span> / 3 策略
          </span>
          {dirty && <span className="rounded bg-amber-900/40 px-2 py-0.5 text-[10px] text-amber-300">未保存</span>}
          {savedAt && !dirty && (
            <span className="rounded bg-emerald-900/40 px-2 py-0.5 text-[10px] text-emerald-300">
              ✓ 已保存 {timeAgo(savedAt)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => loadConfig()}
            className="rounded border border-slate-700 px-3 py-1 text-[11px] text-slate-400 hover:bg-slate-800"
          >
            重新加载
          </button>
          <button
            onClick={handleReset}
            disabled={!dirty || saving}
            className="rounded border border-slate-700 px-3 py-1 text-[11px] text-slate-400 hover:bg-slate-800 disabled:opacity-50"
          >
            撤销修改
          </button>
          <button
            onClick={handleSave}
            disabled={!dirty || saving}
            className="rounded bg-amber-600 px-3 py-1 text-[11px] font-medium text-white hover:bg-amber-500 disabled:opacity-50"
          >
            {saving ? "保存中..." : "保存修改"}
          </button>
        </div>
      </div>

      {errMsg && (
        <div className="rounded-md border border-rose-700/40 bg-rose-950/40 px-3 py-2 text-[12px] text-rose-200">
          ⚠ {errMsg}
        </div>
      )}

      {/* Global switch */}
      <Section title="① 全局开关">
        <Toggle
          label="启用引擎"
          desc="关闭后引擎跳过 tick（不取消已活跃信号），仅作整体停摆"
          checked={form.enabled}
          onChange={(v) => setForm((p) => (p ? { ...p, enabled: v } : p))}
        />
        <div className="grid grid-cols-2 gap-3">
          <Field label="币种">
            <select
              value={form.coin}
              onChange={(e) => setForm((p) => (p ? { ...p, coin: e.target.value } : p))}
              className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-[12px] text-slate-100"
            >
              {["BTC", "ETH", "SOL"].map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </Field>
          <Field label="预测周期 (horizon_min)">
            <select
              value={form.horizon_min}
              onChange={(e) =>
                setForm((p) =>
                  p ? { ...p, horizon_min: Number(e.target.value) as 10 | 30 | 60 } : p,
                )
              }
              className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-[12px] text-slate-100"
            >
              <option value={10}>10 分钟</option>
              <option value={30}>30 分钟</option>
              <option value={60}>60 分钟</option>
            </select>
          </Field>
        </div>
      </Section>

      {/* Strategies */}
      <Section title={`② 策略（${enabledCount}/3 启用）`}>
        <p className="text-[11px] text-slate-500">
          建议进度：先单独启用一个策略观察 24-48h，待累计 ≥30 单后启用第二个；
          冷却期是为避免同方向密集出信号造成统计偏置（同币同向在 cooldown 内不再产生）
        </p>
        <div className="grid gap-3 lg:grid-cols-3">
          {(Object.keys(form.strategies) as StrategyName[]).map((name) => (
            <StrategyCard
              key={name}
              name={name}
              cfg={config.strategies[name]}
              form={form.strategies[name]}
              onChange={(patch) => updateStrategy(name, patch)}
            />
          ))}
        </div>
      </Section>

      {/* Notifications */}
      <Section title="③ 通知">
        <div className="grid gap-3 lg:grid-cols-2">
          {/* Browser */}
          <div className="space-y-3 rounded-lg border border-slate-800 bg-slate-900/40 p-3">
            <Toggle
              label="浏览器通知"
              desc="本浏览器需先授予权限（看板顶部按钮）"
              checked={form.notification.browser_enabled}
              onChange={(v) =>
                setForm((p) =>
                  p ? { ...p, notification: { ...p.notification, browser_enabled: v } } : p,
                )
              }
            />
            <Field label={`置信阈值: ${form.notification.browser_min_confidence}`}>
              <input
                type="range"
                min={0}
                max={100}
                step={1}
                value={form.notification.browser_min_confidence}
                onChange={(e) =>
                  setForm((p) =>
                    p
                      ? {
                          ...p,
                          notification: {
                            ...p.notification,
                            browser_min_confidence: Number(e.target.value),
                          },
                        }
                      : p,
                  )
                }
                disabled={!form.notification.browser_enabled}
                className="w-full"
              />
            </Field>
          </div>
          {/* Email */}
          <div className="space-y-3 rounded-lg border border-slate-800 bg-slate-900/40 p-3">
            <Toggle
              label="邮件通知"
              desc="复用 LIQ 现有 SMTP 配置（config.yaml notifications.email）"
              checked={form.notification.email_enabled}
              onChange={(v) =>
                setForm((p) =>
                  p ? { ...p, notification: { ...p.notification, email_enabled: v } } : p,
                )
              }
            />
            <Field label={`置信阈值: ${form.notification.email_min_confidence}`}>
              <input
                type="range"
                min={0}
                max={100}
                step={1}
                value={form.notification.email_min_confidence}
                onChange={(e) =>
                  setForm((p) =>
                    p
                      ? {
                          ...p,
                          notification: {
                            ...p.notification,
                            email_min_confidence: Number(e.target.value),
                          },
                        }
                      : p,
                  )
                }
                disabled={!form.notification.email_enabled}
                className="w-full"
              />
            </Field>
          </div>
        </div>
      </Section>
    </div>
  );
}

function StrategyCard({
  name,
  cfg,
  form,
  onChange,
}: {
  name: StrategyName;
  cfg: ScalpConfig["strategies"][StrategyName];
  form: FormState["strategies"][StrategyName];
  onChange: (patch: Partial<FormState["strategies"][StrategyName]>) => void;
}) {
  const meta = STRATEGY_META[name];
  return (
    <div
      className="space-y-2 rounded-lg border border-slate-800 bg-slate-900/50 p-3"
      style={{ borderLeft: `3px solid ${meta.color}` }}
    >
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span>{meta.emoji}</span>
            <span className="text-[12px] font-semibold text-slate-200">{meta.shortCn}</span>
          </div>
          <div className="mt-0.5 text-[10px] text-slate-500">{cfg.display_name}</div>
        </div>
        {cfg.auto_disabled && (
          <span className="rounded bg-rose-900/40 px-1.5 py-0.5 text-[10px] text-rose-300">
            自动禁用
          </span>
        )}
      </div>

      {cfg.description && (
        <div className="rounded bg-slate-950/40 p-2 text-[10px] text-slate-400">{cfg.description}</div>
      )}

      {cfg.auto_disabled && cfg.auto_disabled_reason && (
        <div className="rounded border border-rose-800/40 bg-rose-950/40 p-2 text-[10px] text-rose-300">
          原因：{cfg.auto_disabled_reason}
        </div>
      )}

      <Toggle
        label="启用"
        checked={form.enabled}
        onChange={(v) => onChange({ enabled: v })}
        disabled={cfg.auto_disabled}
      />
      <Field label={`置信阈值: ${form.confidence_threshold}`}>
        <input
          type="range"
          min={50}
          max={100}
          step={1}
          value={form.confidence_threshold}
          onChange={(e) => onChange({ confidence_threshold: Number(e.target.value) })}
          className="w-full"
        />
      </Field>
      <Field label={`同币同向冷却: ${form.cooldown_min} 分钟`}>
        <input
          type="range"
          min={5}
          max={240}
          step={5}
          value={form.cooldown_min}
          onChange={(e) => onChange({ cooldown_min: Number(e.target.value) })}
          className="w-full"
        />
      </Field>
      <Field label="备注（仅本地展示）">
        <textarea
          rows={2}
          value={form.notes}
          onChange={(e) => onChange({ notes: e.target.value })}
          className="w-full rounded border border-slate-700 bg-slate-900 px-2 py-1 text-[11px] text-slate-200"
          placeholder="如：今日观察 / 待 30 单后调阈值..."
        />
      </Field>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/30 p-4">
      <h3 className="mb-3 text-[13px] font-semibold text-slate-300">{title}</h3>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      {children}
    </label>
  );
}

function Toggle({
  label,
  desc,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  desc?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className={`flex items-start gap-3 ${disabled ? "opacity-50" : ""}`}>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => !disabled && onChange(e.target.checked)}
        disabled={disabled}
        className="mt-0.5 h-4 w-4 cursor-pointer accent-amber-500"
      />
      <div className="flex-1">
        <div className="text-[12px] text-slate-200">{label}</div>
        {desc && <div className="text-[10px] text-slate-500">{desc}</div>}
      </div>
    </label>
  );
}

function timeAgo(ts: number): string {
  const sec = Math.floor((Date.now() - ts) / 1000);
  if (sec < 60) return `${sec}s 前`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m 前`;
  return `${Math.floor(sec / 3600)}h 前`;
}
