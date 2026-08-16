"use client";

/**
 * 运行时配置页。
 *
 * 三个刻意的设计决定：
 *   1. **表单完全由后端注册表驱动**——分组、说明、控件类型、取值范围
 *      都来自 /admin/config。前端不硬编码任何参数名，后端加参数
 *      这里零改动。
 *   2. **令牌只存 localStorage**，随请求头发送。绝不进构建变量：
 *      NEXT_PUBLIC_* 会把令牌烧进对外公开的 JS 文件。
 *   3. **所有修改重启后才生效**。保存后页面顶部会出现"待重启"横幅，
 *      直到用户主动点"重启服务"——静默重启一个正在追踪代币的服务
 *      比多点一次按钮危险得多。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  getAdminConfig,
  getHealth,
  requestAdminRestart,
  saveAdminConfig,
} from "@/lib/radarApi";
import type {
  AdminConfigGroup,
  AdminConfigParam,
  AdminConfigResponse,
} from "@/lib/radarTypes";

import { Card, Empty } from "../_components/ui";

const TOKEN_STORAGE_KEY = "radar_admin_token";

// ═════════════════════════════════════════════════════════════════════════
// 值的展示与解析
// ═════════════════════════════════════════════════════════════════════════

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "开" : "关";
  if (Array.isArray(value) || typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

/** 编辑控件的初始文本：列表用逗号分隔，JSON 用格式化文本。 */
function editText(param: AdminConfigParam, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (param.kind === "num_list" || param.kind === "str_list") {
    return Array.isArray(value) ? value.join(", ") : String(value);
  }
  if (param.kind === "json") return JSON.stringify(value, null, 2);
  return String(value);
}

/** 把控件文本解析回参数值。返回 [值, 错误]。 */
function parseText(
  param: AdminConfigParam,
  text: string,
): [unknown, string | null] {
  const trimmed = text.trim();
  switch (param.kind) {
    case "int":
    case "float": {
      if (!trimmed) return [null, "不能为空"];
      const n = Number(trimmed);
      if (!Number.isFinite(n)) return [null, "必须是数字"];
      if (param.kind === "int" && !Number.isInteger(n))
        return [null, "必须是整数"];
      if (param.lo !== null && n < param.lo) return [null, `不能小于 ${param.lo}`];
      if (param.hi !== null && n > param.hi) return [null, `不能大于 ${param.hi}`];
      return [n, null];
    }
    case "str":
      if (!trimmed) return [null, "不能为空"];
      return [trimmed, null];
    case "num_list": {
      const parts = trimmed.split(/[,，\s]+/).filter(Boolean);
      if (!parts.length) return [null, "不能为空"];
      const nums = parts.map(Number);
      if (nums.some((n) => !Number.isFinite(n))) return [null, "必须全是数字"];
      if (param.ascending && nums.some((n, i) => i > 0 && n <= nums[i - 1]))
        return [null, "必须严格递增"];
      return [nums, null];
    }
    case "str_list": {
      const parts = trimmed.split(/[,，\s]+/).filter(Boolean);
      if (!parts.length) return [null, "不能为空"];
      return [parts, null];
    }
    case "json": {
      try {
        return [JSON.parse(trimmed), null];
      } catch {
        return [null, "JSON 格式错误"];
      }
    }
    default:
      return [trimmed, null];
  }
}

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

// ═════════════════════════════════════════════════════════════════════════
// 页面
// ═════════════════════════════════════════════════════════════════════════

export default function RadarConfigPage() {
  const [token, setToken] = useState("");
  const [tokenDraft, setTokenDraft] = useState("");
  const [data, setData] = useState<AdminConfigResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // 草稿：path → 已解析的新值；恢复默认集合；控件级错误
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  const [removals, setRemovals] = useState<Record<string, true>>({});
  const [inputErrors, setInputErrors] = useState<Record<string, string>>({});

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveNote, setSaveNote] = useState<string | null>(null);

  const [restarting, setRestarting] = useState(false);
  const [restartNote, setRestartNote] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});

  // localStorage 只能在客户端读，首次挂载时取回令牌
  useEffect(() => {
    const saved = window.localStorage.getItem(TOKEN_STORAGE_KEY) || "";
    if (saved) {
      setToken(saved);
      setTokenDraft(saved);
    }
  }, []);

  const load = useCallback(
    async (tk: string) => {
      if (!tk) return;
      setLoading(true);
      setLoadError(null);
      try {
        const res = await getAdminConfig(tk);
        setData(res);
        setEdits({});
        setRemovals({});
        setInputErrors({});
        setOpenGroups((prev) =>
          Object.keys(prev).length
            ? prev
            : Object.fromEntries(
                res.groups.map((g, i) => [g.id, i === 0]),
              ),
        );
      } catch (e) {
        setData(null);
        setLoadError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (token) void load(token);
  }, [token, load]);

  const connect = () => {
    const tk = tokenDraft.trim();
    window.localStorage.setItem(TOKEN_STORAGE_KEY, tk);
    setToken(tk);
    if (tk === token) void load(tk);
  };

  // ── 草稿操作 ─────────────────────────────────────────────────────────
  const paramByPath = useMemo(() => {
    const map = new Map<string, AdminConfigParam>();
    for (const g of data?.groups ?? [])
      for (const p of g.params) map.set(p.path, p);
    return map;
  }, [data]);

  const setEdit = useCallback(
    (path: string, value: unknown | undefined, error: string | null) => {
      setInputErrors((prev) => {
        const next = { ...prev };
        if (error) next[path] = error;
        else delete next[path];
        return next;
      });
      setEdits((prev) => {
        const next = { ...prev };
        const param = paramByPath.get(path);
        if (
          error ||
          value === undefined ||
          (param && sameValue(value, param.value))
        ) {
          delete next[path];
        } else {
          next[path] = value;
        }
        return next;
      });
      // 手动编辑与"恢复默认"互斥
      setRemovals((prev) => {
        if (!prev[path]) return prev;
        const next = { ...prev };
        delete next[path];
        return next;
      });
    },
    [paramByPath],
  );

  const toggleRemoval = useCallback((path: string) => {
    setRemovals((prev) => {
      const next = { ...prev };
      if (next[path]) delete next[path];
      else next[path] = true;
      return next;
    });
    setEdits((prev) => {
      if (!(path in prev)) return prev;
      const next = { ...prev };
      delete next[path];
      return next;
    });
    setInputErrors((prev) => {
      if (!(path in prev)) return prev;
      const next = { ...prev };
      delete next[path];
      return next;
    });
  }, []);

  const dirtyCount = Object.keys(edits).length + Object.keys(removals).length;
  const errorCount = Object.keys(inputErrors).length;

  const save = async () => {
    if (!data || dirtyCount === 0 || errorCount > 0) return;
    setSaving(true);
    setSaveError(null);
    setSaveNote(null);
    try {
      const res = await saveAdminConfig(token, edits, Object.keys(removals));
      if (res.saved) {
        const changedCount = Object.keys(res.changed ?? {}).length;
        setSaveNote(
          `已保存：修改 ${changedCount} 项、恢复默认 ${res.removed?.length ?? 0} 项。重启服务后生效。`,
        );
      } else {
        setSaveNote(res.reason ?? "没有实际变化");
      }
      await load(token);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  // ── 重启与恢复检测 ────────────────────────────────────────────────────
  const pollRef = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current);
    },
    [],
  );

  const restart = async () => {
    if (!window.confirm("确认重启雷达服务？预计中断约 20 秒，写队列会先排空，不丢数据。"))
      return;
    setRestarting(true);
    setRestartNote("已发送重启指令，等待服务停机…");
    try {
      await requestAdminRestart(token);
    } catch (e) {
      setRestarting(false);
      setRestartNote(`重启指令失败：${e instanceof Error ? e.message : String(e)}`);
      return;
    }
    const startedAt = Date.now();
    let sawDown = false;
    pollRef.current = window.setInterval(async () => {
      const waited = Math.round((Date.now() - startedAt) / 1000);
      try {
        const health = await getHealth();
        if (sawDown || health.uptime_sec < waited + 5) {
          // 服务已带着新配置回来了
          if (pollRef.current !== null) window.clearInterval(pollRef.current);
          pollRef.current = null;
          setRestarting(false);
          setRestartNote(
            `服务已恢复（新配置指纹 ${health.version.config_hash.slice(0, 8)}）`,
          );
          await load(token);
        } else {
          setRestartNote(`等待服务停机…（${waited}s）`);
        }
      } catch {
        sawDown = true;
        setRestartNote(`服务重启中…（${waited}s）`);
      }
      if (Date.now() - startedAt > 120_000 && pollRef.current !== null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
        setRestarting(false);
        setRestartNote(
          "等待超时（120s）。请到运维页或服务器上检查容器状态：docker logs liq-radar",
        );
      }
    }, 3000);
  };

  // ── 搜索过滤 ─────────────────────────────────────────────────────────
  const filteredGroups: AdminConfigGroup[] = useMemo(() => {
    if (!data) return [];
    const q = search.trim().toLowerCase();
    if (!q) return data.groups;
    return data.groups
      .map((g) => ({
        ...g,
        params: g.params.filter(
          (p) =>
            p.label.toLowerCase().includes(q) ||
            p.path.toLowerCase().includes(q) ||
            p.desc.toLowerCase().includes(q),
        ),
      }))
      .filter((g) => g.params.length > 0);
  }, [data, search]);

  // ═══════════════════════════════════════════════════════════════════
  // 渲染
  // ═══════════════════════════════════════════════════════════════════

  return (
    <div className="space-y-3">
      {/* 令牌 */}
      <Card title="管理令牌">
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="password"
            value={tokenDraft}
            onChange={(e) => setTokenDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && connect()}
            placeholder="radar/.env 中的 RADAR_ADMIN_TOKEN"
            className="w-80 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[12px] text-slate-200 placeholder:text-slate-600"
          />
          <button
            onClick={connect}
            disabled={!tokenDraft.trim() || loading}
            className="rounded bg-sky-800 px-3 py-1 text-[12px] text-white hover:bg-sky-700 disabled:opacity-40"
          >
            {loading ? "连接中…" : "连接"}
          </button>
          <span className="text-[11px] text-slate-500">
            令牌只存于本浏览器 localStorage，随请求头发送，不进代码仓库
          </span>
        </div>
        {loadError && (
          <div className="mt-2 rounded border border-rose-700/40 bg-rose-950/40 px-2 py-1.5 text-[12px] text-rose-200">
            {loadError}
          </div>
        )}
      </Card>

      {!data && !loadError && !loading && (
        <Empty text="输入管理令牌后加载配置" />
      )}

      {data && (
        <>
          {/* 状态横幅 */}
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <span className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-400">
              运行中指纹 {data.running.config_hash.slice(0, 8)}
            </span>
            <span className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-400">
              已保存指纹 {data.saved_config_hash.slice(0, 8)}
            </span>
            <span className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-400">
              覆盖 {data.override_count} 项
            </span>
            {data.restart_pending && (
              <span className="rounded border border-amber-700 bg-amber-950/50 px-2 py-1 text-amber-300">
                有已保存但未生效的修改——重启服务后生效
              </span>
            )}
            <button
              onClick={restart}
              disabled={restarting}
              className="rounded border border-rose-700/60 bg-rose-950/40 px-3 py-1 text-rose-200 hover:bg-rose-900/50 disabled:opacity-40"
              title="优雅停机（写队列排空后退出），由容器编排自动拉起，预计中断约 20 秒"
            >
              {restarting ? "重启中…" : "重启服务"}
            </button>
            {restartNote && <span className="text-slate-400">{restartNote}</span>}
          </div>

          {/* 搜索 */}
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索参数（名称 / 路径 / 说明）…"
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-[12px] text-slate-200 placeholder:text-slate-600"
          />

          {/* 分组 */}
          {filteredGroups.map((group) => {
            const open = search.trim() ? true : (openGroups[group.id] ?? false);
            const groupDirty = group.params.filter(
              (p) => p.path in edits || removals[p.path],
            ).length;
            return (
              <Card
                key={group.id}
                title={
                  <button
                    onClick={() =>
                      setOpenGroups((prev) => ({
                        ...prev,
                        [group.id]: !open,
                      }))
                    }
                    className="flex items-center gap-2 text-left"
                  >
                    <span className="text-slate-500">{open ? "▾" : "▸"}</span>
                    {group.label}
                    <span className="text-[10px] font-normal text-slate-500">
                      {group.desc} · {group.params.length} 项
                    </span>
                    {groupDirty > 0 && (
                      <span className="rounded bg-amber-900/50 px-1.5 py-px text-[10px] font-normal text-amber-300">
                        {groupDirty} 项待保存
                      </span>
                    )}
                  </button>
                }
              >
                {open && (
                  <div className="divide-y divide-slate-800/60">
                    {group.params.map((param) => (
                      <ParamRow
                        key={param.path}
                        param={param}
                        edited={edits[param.path]}
                        isEdited={param.path in edits}
                        markedForRemoval={!!removals[param.path]}
                        error={inputErrors[param.path]}
                        onEdit={setEdit}
                        onToggleRemoval={toggleRemoval}
                      />
                    ))}
                  </div>
                )}
              </Card>
            );
          })}

          {/* 保存栏 */}
          <div className="sticky bottom-3 flex flex-wrap items-center gap-3 rounded-lg border border-slate-700 bg-slate-900/95 px-4 py-2.5 shadow-lg backdrop-blur">
            <span className="text-[12px] text-slate-300">
              {dirtyCount > 0
                ? `${Object.keys(edits).length} 项修改、${Object.keys(removals).length} 项恢复默认待保存`
                : "无未保存的修改"}
            </span>
            {errorCount > 0 && (
              <span className="text-[12px] text-rose-300">
                {errorCount} 项输入有误，修正后才能保存
              </span>
            )}
            <div className="ml-auto flex items-center gap-2">
              {dirtyCount > 0 && (
                <button
                  onClick={() => {
                    setEdits({});
                    setRemovals({});
                    setInputErrors({});
                  }}
                  className="rounded border border-slate-600 px-3 py-1 text-[12px] text-slate-300 hover:bg-slate-800"
                >
                  放弃修改
                </button>
              )}
              <button
                onClick={save}
                disabled={dirtyCount === 0 || errorCount > 0 || saving}
                className="rounded bg-emerald-700 px-4 py-1 text-[12px] font-medium text-white hover:bg-emerald-600 disabled:opacity-40"
              >
                {saving ? "保存中…" : "保存（重启后生效）"}
              </button>
            </div>
            {saveError && (
              <div className="w-full text-[12px] text-rose-300">
                保存失败：{saveError}
              </div>
            )}
            {saveNote && !saveError && (
              <div className="w-full text-[12px] text-emerald-300">{saveNote}</div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ═════════════════════════════════════════════════════════════════════════
// 单个参数行
// ═════════════════════════════════════════════════════════════════════════

function ParamRow({
  param,
  edited,
  isEdited,
  markedForRemoval,
  error,
  onEdit,
  onToggleRemoval,
}: {
  param: AdminConfigParam;
  edited: unknown;
  isEdited: boolean;
  markedForRemoval: boolean;
  error: string | undefined;
  onEdit: (path: string, value: unknown | undefined, error: string | null) => void;
  onToggleRemoval: (path: string) => void;
}) {
  // 文本类控件保留原始输入，避免"输到一半被格式化打断"。
  // 保存/重载后服务器值变化时在渲染期间同步（React 推荐的
  // "adjust state during render"模式，不用 effect）
  const [text, setText] = useState(() => editText(param, param.value));
  const [syncedValue, setSyncedValue] = useState(param.value);
  if (!sameValue(syncedValue, param.value)) {
    setSyncedValue(param.value);
    if (!isEdited) setText(editText(param, param.value));
  }

  const handleText = (raw: string) => {
    setText(raw);
    const [value, err] = parseText(param, raw);
    onEdit(param.path, err ? undefined : value, err);
  };

  const rangeHint = [
    param.lo !== null || param.hi !== null
      ? `范围 ${param.lo ?? "-∞"} ~ ${param.hi ?? "+∞"}`
      : "",
    param.unit,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      className={`flex flex-wrap items-start gap-x-4 gap-y-1 py-2 ${
        markedForRemoval ? "opacity-60" : ""
      }`}
    >
      <div className="w-72 min-w-56">
        <div className="flex items-center gap-1.5 text-[12px] text-slate-200">
          {param.label}
          {param.overridden && (
            <span
              className="rounded bg-sky-900/60 px-1 py-px text-[9px] text-sky-300"
              title={`出厂默认: ${displayValue(param.default)}`}
            >
              已覆盖
            </span>
          )}
          {(isEdited || markedForRemoval) && (
            <span className="rounded bg-amber-900/50 px-1 py-px text-[9px] text-amber-300">
              {markedForRemoval ? "将恢复默认" : "待保存"}
            </span>
          )}
        </div>
        <div className="mt-0.5 font-mono text-[9px] text-slate-600">{param.path}</div>
        {param.desc && (
          <div className="mt-0.5 text-[10px] leading-snug text-slate-500">
            {param.desc}
          </div>
        )}
      </div>

      <div className="flex-1">
        <ParamControl
          param={param}
          text={text}
          edited={edited}
          isEdited={isEdited}
          disabled={markedForRemoval}
          onText={handleText}
          onValue={(v) => onEdit(param.path, v, null)}
        />
        <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[10px]">
          {error ? (
            <span className="text-rose-300">{error}</span>
          ) : (
            rangeHint && <span className="text-slate-600">{rangeHint}</span>
          )}
          <span className="text-slate-600">
            默认 {displayValue(param.default)}
          </span>
          {param.overridden && (
            <button
              onClick={() => onToggleRemoval(param.path)}
              className={`rounded border px-1.5 py-px ${
                markedForRemoval
                  ? "border-amber-600 text-amber-300"
                  : "border-slate-700 text-slate-400 hover:text-slate-200"
              }`}
            >
              {markedForRemoval ? "取消恢复" : "恢复默认"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function ParamControl({
  param,
  text,
  edited,
  isEdited,
  disabled,
  onText,
  onValue,
}: {
  param: AdminConfigParam;
  text: string;
  edited: unknown;
  isEdited: boolean;
  disabled: boolean;
  onText: (raw: string) => void;
  onValue: (value: unknown) => void;
}) {
  const inputCls =
    "rounded border border-slate-700 bg-slate-950 px-2 py-1 text-[12px] " +
    "text-slate-200 disabled:opacity-50";

  if (param.kind === "bool") {
    const current = isEdited ? Boolean(edited) : Boolean(param.value);
    return (
      <button
        disabled={disabled}
        onClick={() => onValue(!current)}
        className={`rounded border px-3 py-1 text-[12px] ${
          current
            ? "border-emerald-700 bg-emerald-950/50 text-emerald-300"
            : "border-slate-700 bg-slate-950 text-slate-400"
        } disabled:opacity-50`}
      >
        {current ? "已开启" : "已关闭"}
      </button>
    );
  }

  if (param.kind === "choice" && param.choices) {
    const current = isEdited ? edited : param.value;
    return (
      <select
        disabled={disabled}
        value={String(current)}
        onChange={(e) => {
          const match = param.choices!.find(
            (c) => String(c) === e.target.value,
          );
          onValue(match ?? e.target.value);
        }}
        className={inputCls}
      >
        {param.choices.map((c) => (
          <option key={String(c)} value={String(c)}>
            {String(c)}
          </option>
        ))}
      </select>
    );
  }

  if (param.kind === "json") {
    return (
      <textarea
        disabled={disabled}
        value={text}
        onChange={(e) => onText(e.target.value)}
        rows={Math.min(8, Math.max(3, text.split("\n").length))}
        spellCheck={false}
        className={`${inputCls} w-full max-w-xl font-mono text-[11px]`}
      />
    );
  }

  const wide =
    param.kind === "num_list" || param.kind === "str_list" || param.kind === "str";
  return (
    <input
      disabled={disabled}
      type="text"
      value={text}
      onChange={(e) => onText(e.target.value)}
      spellCheck={false}
      className={`${inputCls} ${wide ? "w-full max-w-xl" : "w-40"} font-mono`}
    />
  );
}
