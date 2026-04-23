"use client";

/**
 * /roll/settings · 全局设置（完整版）
 *
 * 分区：
 *   1. 账户与资金占用
 *   2. 静默时段（UTC，跨日支持）
 *   3. 通知与声音
 *   4. 前瞻窗口频控
 *   5. 覆盖行为熔断（参数，熔断实际触发在 Step 9 统计中接入）
 *
 * 保存策略：
 *   - 改动后端 PUT /api/roll/settings，patch 为修改过的字段
 *   - 为避免 set-state-in-effect 警告，表单草稿以 key={settings.updated_at} 重挂载同步
 */

import { useMemo, useState } from "react";

import { useRollStore } from "@/stores/rollStore";
import type { RollGlobalSettings } from "@/lib/rollTypes";

export default function RollSettingsPage() {
  const settings = useRollStore((s) => s.settings);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">全局设置</h1>
        <p className="mt-1 text-[12px] text-slate-500">
          这些配置作用于所有活跃计划。修改后立即落盘。
        </p>
      </div>

      {settings ? (
        <SettingsForm key={settings.updated_at} initial={settings} />
      ) : (
        <div className="py-10 text-center text-[12px] text-slate-500">
          加载中…
        </div>
      )}
    </div>
  );
}

interface Draft {
  total_account_usd: number;
  per_coin_margin_pct_cap: number;
  account_margin_pct_cap: number;

  quiet_hours_enabled: boolean;
  quiet_start_utc: number;
  quiet_end_utc: number;
  quiet_allow_urgent: boolean;

  notification_enabled: boolean;
  notification_sound_for_urgent: boolean;

  forward_alert_cooldown_min: number;

  override_cooldown_enabled: boolean;
  override_warn_threshold: number;
  override_warn_window: number;
  override_cooldown_hours: number;

  liq_emergency_pct: number;
}

function fromSettings(s: RollGlobalSettings): Draft {
  return {
    total_account_usd: s.total_account_usd,
    per_coin_margin_pct_cap: s.per_coin_margin_pct_cap,
    account_margin_pct_cap: s.account_margin_pct_cap,
    quiet_hours_enabled: s.quiet_hours_enabled,
    quiet_start_utc: s.quiet_start_utc,
    quiet_end_utc: s.quiet_end_utc,
    quiet_allow_urgent: s.quiet_allow_urgent,
    notification_enabled: s.notification_enabled,
    notification_sound_for_urgent: s.notification_sound_for_urgent,
    forward_alert_cooldown_min: s.forward_alert_cooldown_min,
    override_cooldown_enabled: s.override_cooldown_enabled,
    override_warn_threshold: s.override_warn_threshold,
    override_warn_window: s.override_warn_window,
    override_cooldown_hours: s.override_cooldown_hours,
    liq_emergency_pct: s.liq_emergency_pct,
  };
}

function validateDraft(d: Draft): string | null {
  if (d.total_account_usd <= 0) return "账户总额必须 > 0";
  if (d.per_coin_margin_pct_cap < 0 || d.per_coin_margin_pct_cap > 1)
    return "单币种上限应 ∈ [0%, 100%]";
  if (d.account_margin_pct_cap < 0 || d.account_margin_pct_cap > 1)
    return "全账户上限应 ∈ [0%, 100%]";

  if (d.quiet_start_utc < 0 || d.quiet_start_utc > 23)
    return "quiet_start_utc 必须 ∈ [0, 23]";
  if (d.quiet_end_utc < 0 || d.quiet_end_utc > 23)
    return "quiet_end_utc 必须 ∈ [0, 23]";

  if (d.forward_alert_cooldown_min < 1 || d.forward_alert_cooldown_min > 240)
    return "前瞻窗口冷却应 ∈ [1, 240] 分钟";

  if (d.override_warn_window < 1) return "覆盖统计窗口必须 ≥ 1";
  if (d.override_warn_threshold < 0 || d.override_warn_threshold > d.override_warn_window)
    return "覆盖警告阈值应 ∈ [0, 统计窗口]";
  if (d.override_cooldown_hours < 1 || d.override_cooldown_hours > 168)
    return "覆盖冷却时长应 ∈ [1, 168] 小时";

  if (d.liq_emergency_pct < 1 || d.liq_emergency_pct > 15)
    return "紧急离场距爆仓阈值应 ∈ [1, 15]%";
  return null;
}

function SettingsForm({ initial }: { initial: RollGlobalSettings }) {
  const updateSettings = useRollStore((s) => s.updateSettings);
  const [draft, setDraft] = useState<Draft>(() => fromSettings(initial));
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const preflightErr = useMemo(() => validateDraft(draft), [draft]);
  const dirty = useMemo(() => {
    const cur = fromSettings(initial);
    return JSON.stringify(cur) !== JSON.stringify(draft);
  }, [initial, draft]);

  const patch = <K extends keyof Draft>(k: K, v: Draft[K]) =>
    setDraft((d) => ({ ...d, [k]: v }));

  const onSave = async () => {
    if (preflightErr) {
      setErr(preflightErr);
      setMsg(null);
      return;
    }
    setErr(null);
    setMsg(null);
    setSaving(true);
    try {
      await updateSettings(draft);
      setMsg("已保存");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const onReset = () => {
    setDraft(fromSettings(initial));
    setMsg(null);
    setErr(null);
  };

  return (
    <div className="space-y-4">
      {/* ── 1. 账户与资金占用 ── */}
      <Card title="账户与资金占用">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="账户总额 (USD)" hint="驱动新建持仓的占比校验">
            <input
              type="number"
              min={1}
              step={10}
              className="roll-input"
              value={draft.total_account_usd}
              onChange={(e) => patch("total_account_usd", Number(e.target.value) || 0)}
            />
          </Field>
          <Field label="单币种上限 (%)" hint="同币种所有活跃计划保证金 ÷ 账户">
            <input
              type="number"
              min={0}
              max={100}
              step={1}
              className="roll-input"
              value={(draft.per_coin_margin_pct_cap * 100).toFixed(1)}
              onChange={(e) =>
                patch("per_coin_margin_pct_cap", Number(e.target.value) / 100)
              }
            />
          </Field>
          <Field label="全账户上限 (%)" hint="所有活跃计划保证金 ÷ 账户">
            <input
              type="number"
              min={0}
              max={100}
              step={1}
              className="roll-input"
              value={(draft.account_margin_pct_cap * 100).toFixed(1)}
              onChange={(e) =>
                patch("account_margin_pct_cap", Number(e.target.value) / 100)
              }
            />
          </Field>
        </div>
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field
            label="紧急离场阈值 (%)"
            hint="距爆仓百分比小于此值时强制 close + urgent；范围 [1, 15]，激进可设 3，保守 5+"
          >
            <input
              type="number"
              min={1}
              max={15}
              step={0.5}
              className="roll-input"
              value={draft.liq_emergency_pct}
              onChange={(e) =>
                patch("liq_emergency_pct", Number(e.target.value) || 5)
              }
            />
          </Field>
        </div>
      </Card>

      {/* ── 2. 静默时段 ── */}
      <Card title="静默时段（UTC）">
        <Toggle
          checked={draft.quiet_hours_enabled}
          onChange={(v) => patch("quiet_hours_enabled", v)}
          label="启用静默时段"
          hint="在指定 UTC 小时段内抑制非 urgent 提醒"
        />
        <fieldset
          disabled={!draft.quiet_hours_enabled}
          className={draft.quiet_hours_enabled ? "" : "opacity-50"}
        >
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="开始 (UTC 小时)" hint="[0, 23]">
              <input
                type="number"
                min={0}
                max={23}
                step={1}
                className="roll-input"
                value={draft.quiet_start_utc}
                onChange={(e) =>
                  patch("quiet_start_utc", Math.floor(Number(e.target.value)) || 0)
                }
              />
            </Field>
            <Field label="结束 (UTC 小时)" hint="start > end 支持跨日（如 23 → 7）">
              <input
                type="number"
                min={0}
                max={23}
                step={1}
                className="roll-input"
                value={draft.quiet_end_utc}
                onChange={(e) =>
                  patch("quiet_end_utc", Math.floor(Number(e.target.value)) || 0)
                }
              />
            </Field>
            <div className="self-end">
              <Toggle
                checked={draft.quiet_allow_urgent}
                onChange={(v) => patch("quiet_allow_urgent", v)}
                label="静默期内仍推紧急提醒"
              />
            </div>
          </div>
          <div className="mt-2 rounded border border-slate-800 bg-slate-900/60 px-3 py-2 text-[11px] text-slate-400">
            当前本地时间 UTC：{" "}
            <span className="font-mono text-slate-200">
              {new Date().toISOString().slice(11, 16)}
            </span>
            {" · "}
            <span>
              {isInQuiet(draft) ? "⏸ 处于静默期" : "▶ 不处于静默期"}
            </span>
          </div>
        </fieldset>
      </Card>

      {/* ── 3. 通知与声音 ── */}
      <Card title="通知与声音">
        <Toggle
          checked={draft.notification_enabled}
          onChange={(v) => patch("notification_enabled", v)}
          label="启用桌面 Notification"
          hint="首次使用会请求浏览器权限；Mac 需在系统 通知 中允许浏览器"
        />
        <Toggle
          checked={draft.notification_sound_for_urgent}
          onChange={(v) => patch("notification_sound_for_urgent", v)}
          label="urgent 级别播放提示音"
          hint="使用 WebAudio 生成短蜂鸣，不依赖外部音频资源"
        />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <PermissionPill />
        </div>
      </Card>

      {/* ── 4. 前瞻窗口频控 ── */}
      <Card title="前瞻窗口提醒频控">
        <Field
          label="同计划同类型冷却 (分钟)"
          hint="[1, 240]；例：kind=key_level_approaching 的前瞻在 N 分钟内最多 1 次"
        >
          <input
            type="number"
            min={1}
            max={240}
            step={1}
            className="roll-input"
            value={draft.forward_alert_cooldown_min}
            onChange={(e) =>
              patch(
                "forward_alert_cooldown_min",
                Math.floor(Number(e.target.value)) || 1,
              )
            }
          />
        </Field>
      </Card>

      {/* ── 5. 覆盖行为熔断 ── */}
      <Card title="用户覆盖行为熔断">
        <Toggle
          checked={draft.override_cooldown_enabled}
          onChange={(v) => patch("override_cooldown_enabled", v)}
          label="启用覆盖熔断"
          hint="当用户连续多次强行覆盖系统建议且亏损时，冷却一段时间禁止新的覆盖加仓"
        />
        <fieldset
          disabled={!draft.override_cooldown_enabled}
          className={draft.override_cooldown_enabled ? "" : "opacity-50"}
        >
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="统计窗口 (近 N 次覆盖)" hint="≥1">
              <input
                type="number"
                min={1}
                step={1}
                className="roll-input"
                value={draft.override_warn_window}
                onChange={(e) =>
                  patch("override_warn_window", Math.floor(Number(e.target.value)) || 1)
                }
              />
            </Field>
            <Field label="警告阈值 (亏 ≥ 此数触发)" hint="∈ [0, 统计窗口]">
              <input
                type="number"
                min={0}
                step={1}
                className="roll-input"
                value={draft.override_warn_threshold}
                onChange={(e) =>
                  patch(
                    "override_warn_threshold",
                    Math.floor(Number(e.target.value)) || 0,
                  )
                }
              />
            </Field>
            <Field label="冷却时长 (小时)" hint="[1, 168]">
              <input
                type="number"
                min={1}
                max={168}
                step={1}
                className="roll-input"
                value={draft.override_cooldown_hours}
                onChange={(e) =>
                  patch(
                    "override_cooldown_hours",
                    Math.floor(Number(e.target.value)) || 1,
                  )
                }
              />
            </Field>
          </div>
        </fieldset>
      </Card>

      {/* ── 反馈 + 操作 ── */}
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

      <div className="flex items-center gap-3">
        <button
          onClick={onSave}
          disabled={saving || !dirty || !!preflightErr}
          className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-50"
        >
          {saving ? "保存中…" : "保存"}
        </button>
        <button
          onClick={onReset}
          disabled={saving || !dirty}
          className="rounded-md border border-slate-700 px-3 py-2 text-[12px] text-slate-300 transition hover:bg-slate-800 disabled:opacity-50"
        >
          重置
        </button>
        {dirty && (
          <span className="text-[11px] text-slate-500">未保存的改动</span>
        )}
        <span className="ml-auto text-[10px] text-slate-500">
          上次更新：
          {initial.updated_at
            ? new Date(initial.updated_at * 1000).toLocaleString("zh-CN", { hour12: false })
            : "—"}
        </span>
      </div>
    </div>
  );
}

function isInQuiet(d: Draft): boolean {
  if (!d.quiet_hours_enabled) return false;
  const h = new Date().getUTCHours();
  if (d.quiet_start_utc === d.quiet_end_utc) return false;
  if (d.quiet_start_utc < d.quiet_end_utc) {
    return h >= d.quiet_start_utc && h < d.quiet_end_utc;
  }
  return h >= d.quiet_start_utc || h < d.quiet_end_utc;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 子组件
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function Card({ title, children }: { title: string; children: React.ReactNode }) {
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

function Toggle({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: string;
}) {
  return (
    <label className="flex items-start gap-3">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={[
          "mt-0.5 inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition",
          checked
            ? "border-emerald-500 bg-emerald-600/70"
            : "border-slate-700 bg-slate-800",
        ].join(" ")}
      >
        <span
          className={[
            "h-4 w-4 rounded-full bg-white transition-transform",
            checked ? "translate-x-4" : "translate-x-0.5",
          ].join(" ")}
        />
      </button>
      <span className="flex flex-col">
        <span className="text-[12px] text-slate-200">{label}</span>
        {hint && <span className="text-[10px] text-slate-500">{hint}</span>}
      </span>
    </label>
  );
}

function PermissionPill() {
  // 浏览器权限状态。注意：denied 后 requestPermission 会立即返回 denied 且不弹原生对话框，
  // 这是浏览器防骚扰策略，JS 无法绕过。此时只能引导用户去地址栏锁图标手动开启。
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">(
    () =>
      typeof window !== "undefined" && "Notification" in window
        ? Notification.permission
        : "unsupported",
  );
  const [showHelp, setShowHelp] = useState(false);

  const requestPerm = async () => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    // 已 denied：直接展示引导，不浪费一次无效调用
    if (Notification.permission === "denied") {
      setShowHelp(true);
      return;
    }
    try {
      const result = await Notification.requestPermission();
      setPermission(result);
      if (result === "denied") setShowHelp(true);
    } catch {
      setPermission(Notification.permission);
    }
  };

  const tone =
    permission === "granted"
      ? "border-emerald-700/40 bg-emerald-950/40 text-emerald-200"
      : permission === "denied"
      ? "border-rose-700/40 bg-rose-950/40 text-rose-200"
      : "border-amber-700/40 bg-amber-950/40 text-amber-200";

  const label =
    permission === "granted"
      ? "✓ 浏览器通知权限已授予"
      : permission === "denied"
      ? "✗ 已拒绝 · 浏览器已记住此选择，需手动开启"
      : permission === "default"
      ? "⚠ 尚未授予通知权限"
      : "⚠ 当前浏览器不支持通知 API";

  const btnLabel = permission === "denied" ? "查看开启方法" : "申请权限";

  return (
    <div className="space-y-2">
      <div
        className={[
          "flex items-center justify-between gap-3 rounded border px-3 py-2 text-[12px]",
          tone,
        ].join(" ")}
      >
        <span>{label}</span>
        {permission !== "granted" && permission !== "unsupported" && (
          <button
            type="button"
            onClick={requestPerm}
            className="rounded border border-current px-2 py-0.5 text-[11px] transition hover:bg-slate-900/30"
          >
            {btnLabel}
          </button>
        )}
      </div>
      {showHelp && permission === "denied" && (
        <div className="rounded border border-slate-700/60 bg-slate-900/40 p-2.5 text-[11px] leading-relaxed text-slate-300">
          <div className="mb-1 font-medium text-slate-200">手动开启浏览器通知</div>
          <ol className="list-decimal space-y-1 pl-5">
            <li>点击地址栏左侧的 🔒 / 🛡 / ⓘ 图标</li>
            <li>找到「通知 / Notifications」一项，将其改为「允许 / Allow」</li>
            <li>刷新本页面，再回到此处确认状态变绿</li>
          </ol>
          <div className="mt-2 text-[10px] text-slate-500">
            说明：被拒绝后浏览器会永久记住此决定，JavaScript 无法再次唤起原生授权弹窗（这是
            Chrome/Edge/Safari 统一的反骚扰策略，不是本应用的 bug）。
          </div>
          <button
            type="button"
            onClick={() => setShowHelp(false)}
            className="mt-2 text-[10px] text-slate-400 underline hover:text-slate-200"
          >
            收起
          </button>
        </div>
      )}
    </div>
  );
}
