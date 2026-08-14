/**
 * 扫单堆积带（B 语义：识别全市场扫单/爆仓堆积位置）
 *
 * 与已有模块的视角差异：
 *   - SweepWatch 代表区：只看每侧最近 1 个 + 5 态机过程
 *   - PriceAxisMap：按价格轴空间分布，不排序
 *   - ZoneDetailCard：单 zone 多维详情（要先点选）
 *   - OpportunityBoard：决策建议（A 语义：自己开仓的防守位）
 *   - 本面板：全局按 SA 排序的双向扫单堆积带（B 语义）
 *
 * 设计决策（与用户对齐）：
 *   - 主排序键：sweep_attractiveness（SA） DESC，不掺距离/不掺 trust
 *   - 不过滤角色：SR 低的纯防守自然沉底，但保留可见（让用户看到完整图景）
 *   - 不设距离上限 / 不设数量上限（容器滚动）
 *   - 噪声门槛：SA ≥ 0.05（与 rankings 同口径）
 *   - 辅助列：SR（反弹力）+ BTR（继续杀风险）+ fragility（脆性），让用户
 *     既看"哪里最招扫"，又能立刻判断"扫到会反弹还是继续杀"
 *   - 后端 0 改动，纯前端从 snap.zones 派生
 */
"use client";

import type { BrainPriceZone } from "@/lib/types";
import { ROLE_COLORS } from "./types";
import { formatPrice } from "@/lib/format";

interface Props {
  coin: string;
  zones: BrainPriceZone[];
  /** 当前选中 zone（用于高亮，与 PriceAxisMap / ZoneDetailCard 联动） */
  selectedId?: string | null;
  /** 点击行 → 联动 PriceAxisMap / ZoneDetailCard */
  onSelectZone?: (zoneId: string) => void;
}

// ─────────────────────────────────────────────────────────────────────
// 阈值常量（与代码库其它模块同口径）
// ─────────────────────────────────────────────────────────────────────
const SA_THRESHOLD = 0.05;
/** SA 噪声门槛，与 trading_brain_builder._rankings 的 trust ≥ 0.05 同语义。 */

const SA_HOT_LEVEL = 0.7;
/** SA ≥ 0.7 标 ★肉（与 ZoneDetailCard tooltip "≥0.7 高（容易被扫）" 对齐）。 */

const BTR_WARN_LEVEL = 0.6;
/** BTR ≥ 0.6 标 ⚠延续（接近 ZoneDetailCard "≥0.7 高 / 0.4-0.7 中" 的高警示线，
 *  这里取 0.6 提前提示"扫到大概率继续杀"，避免用户只看 SA 高就抢底）。 */

// ─────────────────────────────────────────────────────────────────────
// 配色（与 SweepWatchPanel 同语义：trust 高=绿、risk 高=红）
//   局部复制而非提取共享，是为了避免触动 SweepWatchPanel 的现有签名。
// ─────────────────────────────────────────────────────────────────────
function trustColor(v: number) {
  if (v >= 0.7) return { bar: "bg-emerald-500/80", text: "text-emerald-300" };
  if (v >= 0.4) return { bar: "bg-amber-500/80", text: "text-amber-300" };
  return { bar: "bg-rose-500/70", text: "text-rose-300" };
}

function riskColor(v: number) {
  if (v >= 0.7) return { bar: "bg-rose-500/80", text: "text-rose-300" };
  if (v >= 0.4) return { bar: "bg-amber-500/80", text: "text-amber-300" };
  return { bar: "bg-emerald-500/70", text: "text-emerald-300" };
}

// ─────────────────────────────────────────────────────────────────────
// 过滤 + 排序（纯 SA，距离不参与）
// ─────────────────────────────────────────────────────────────────────
function pickStack(
  zones: BrainPriceZone[],
  side: "below" | "above",
): BrainPriceZone[] {
  return zones
    .filter((z) => {
      if ((z.sweep_attractiveness ?? 0) < SA_THRESHOLD) return false;
      return side === "below" ? z.distance_pct < 0 : z.distance_pct > 0;
    })
    .sort(
      (a, b) =>
        (b.sweep_attractiveness ?? 0) - (a.sweep_attractiveness ?? 0),
    );
}

// ─────────────────────────────────────────────────────────────────────
// 紧凑行内进度条
// ─────────────────────────────────────────────────────────────────────
function MiniBar({ value, kind }: { value: number; kind: "trust" | "risk" }) {
  const v = Math.max(0, Math.min(1, value));
  const c = kind === "trust" ? trustColor(v) : riskColor(v);
  return (
    <div className="flex items-center gap-1">
      <div className="h-1 w-12 overflow-hidden rounded bg-slate-800">
        <div className={`h-full ${c.bar}`} style={{ width: `${v * 100}%` }} />
      </div>
      <span className={`tabular-nums text-[10px] ${c.text}`}>
        {v.toFixed(2)}
      </span>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 单条 zone 行（紧凑 4 行布局：角色+距离 / 价格 / SA+SR / BTR+脆性）
// ─────────────────────────────────────────────────────────────────────
function ZoneRow({
  coin,
  zone,
  side,
  selected,
  onSelect,
}: {
  coin: string;
  zone: BrainPriceZone;
  side: "below" | "above";
  selected: boolean;
  onSelect?: (id: string) => void;
}) {
  const role = ROLE_COLORS[zone.dominant_role];
  const sa = zone.sweep_attractiveness ?? 0;
  const sr = side === "below" ? zone.support_trust : zone.resistance_trust;
  const btr = zone.break_through_risk ?? 0;
  const frag =
    (side === "below" ? zone.support_fragility : zone.resistance_fragility) ?? 0;

  const isHot = sa >= SA_HOT_LEVEL;
  const isContinuationRisk = btr >= BTR_WARN_LEVEL;

  // selected 状态优先于 role 配色，确保点击后视觉一致
  const cardCls = selected
    ? "border-sky-500 bg-sky-950/30"
    : `${role.border} ${role.bg}`;

  return (
    <button
      type="button"
      onClick={() => onSelect?.(zone.zone_id)}
      className={`w-full rounded border ${cardCls} px-2 py-1.5 text-left transition hover:border-sky-500 hover:bg-sky-950/20`}
      title={`点击在 PriceAxisMap / ZoneDetailCard 中高亮：${zone.dominant_label || role.label}`}
    >
      {/* 第一行：角色色块 + 标签 + 标记 + 距离 */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <span
            className="inline-block h-2 w-2 shrink-0 rounded"
            style={{ backgroundColor: role.hex }}
          />
          <span className={`truncate text-[11px] font-medium ${role.text}`}>
            {zone.dominant_label || role.label}
          </span>
          {isHot && (
            <span
              className="shrink-0 text-[9px] text-amber-300"
              title="SA ≥ 0.7：高扫单吸引（哪里最招扫）"
            >
              ★肉
            </span>
          )}
          {isContinuationRisk && (
            <span
              className="shrink-0 text-[9px] text-rose-300"
              title="BTR ≥ 0.6：扫到大概率继续杀"
            >
              ⚠延续
            </span>
          )}
        </div>
        <span className="shrink-0 tabular-nums text-[10px] text-slate-400">
          {zone.distance_pct >= 0 ? "+" : ""}
          {zone.distance_pct.toFixed(2)}%
        </span>
      </div>

      {/* 第二行：价格区间 */}
      <div className="mt-0.5 tabular-nums text-[10px] text-slate-500">
        {formatPrice(zone.price_low, coin)} – {formatPrice(zone.price_high, coin)}
      </div>

      {/* 第三行：SA + SR 双进度条 */}
      <div className="mt-1 grid grid-cols-2 gap-2">
        <div
          className="flex items-center gap-1"
          title="SA = 扫单吸引（杠杆拥挤+磁铁邻近+主动攻击+撤墙风险+变薄综合分）"
        >
          <span className="text-[9px] text-slate-500">扫单吸引(SA)</span>
          <MiniBar value={sa} kind="risk" />
        </div>
        <div
          className="flex items-center gap-1"
          title={
            side === "below"
              ? "SR = 支撑信任（已校准）：扫到这里能不能反弹"
              : "SR = 阻力信任（已校准）：突破后会不会被压回"
          }
        >
          <span className="text-[9px] text-slate-500">防守可信(SR)</span>
          <MiniBar value={sr ?? 0} kind="trust" />
        </div>
      </div>

      {/* 第四行：BTR / 脆性 数字（无条形，节省空间） */}
      <div className="mt-0.5 flex items-center gap-3 text-[9px] text-slate-500">
        <span title="BTR = 打穿风险（≥0.7 高：不建议提前接，等扫单反应）">
          继续打穿风险(BTR) <span className="tabular-nums text-slate-400">{btr.toFixed(2)}</span>
        </span>
        <span title="脆性（同区合约层 active_attack / wall_removal_risk 综合，0=无攻击 / 1=正在被攻击）">
          脆 <span className="tabular-nums text-slate-400">{frag.toFixed(2)}</span>
        </span>
      </div>
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 单侧栏（标题 + 计数 + 滚动列表）
// ─────────────────────────────────────────────────────────────────────
function SideColumn({
  coin,
  side,
  zones,
  selectedId,
  onSelect,
}: {
  coin: string;
  side: "below" | "above";
  zones: BrainPriceZone[];
  selectedId?: string | null;
  onSelect?: (id: string) => void;
}) {
  const list = pickStack(zones, side);
  const title = side === "below" ? "下方多头爆仓堆积带" : "上方空头爆仓堆积带";
  const subtitle = side === "below" ? "做多止损 / 多头爆仓" : "做空止损 / 空头爆仓";

  return (
    <div className="flex flex-col rounded-md border border-slate-800 bg-slate-950/40">
      <div className="flex items-center justify-between border-b border-slate-800 px-2.5 py-1.5">
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] font-medium text-slate-200">{title}</span>
          <span className="text-[10px] text-slate-500">· {subtitle}</span>
        </div>
        <span className="text-[10px] text-slate-500">{list.length} 条</span>
      </div>
      {list.length === 0 ? (
        <div className="px-2.5 py-3 text-center text-[10px] text-slate-500">
          该侧暂无 SA ≥ {SA_THRESHOLD.toFixed(2)} 的扫单堆积
        </div>
      ) : (
        <div className="max-h-[260px] space-y-1 overflow-auto p-1.5">
          {list.map((z) => (
            <ZoneRow
              key={z.zone_id}
              coin={coin}
              zone={z}
              side={side}
              selected={selectedId === z.zone_id}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 主面板
// ─────────────────────────────────────────────────────────────────────
export default function SweepStackPanel({
  coin,
  zones,
  selectedId,
  onSelectZone,
}: Props) {
  if (!zones || zones.length === 0) {
    return (
      <div className="rounded border border-slate-800 bg-slate-900/40 p-3 text-[11px] text-slate-500">
        ⚪ 扫单堆积带 · 数据未就绪
      </div>
    );
  }

  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/30 p-3">
      {/* 顶部标题栏：标题 + 视角说明 */}
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-semibold text-slate-200">
            扫单堆积带
          </span>
          <span className="text-[10px] text-slate-500">
            按扫单吸引力排序 · 不限距离 · 全局视图
          </span>
        </div>
        <div className="text-[10px] text-slate-500">
          扫单吸引高 = 更招扫 ｜ 防守可信高 = 更可能反弹/压回 ｜ 打穿风险高 = 更可能继续穿越
        </div>
      </div>

      <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
        <SideColumn
          coin={coin}
          side="below"
          zones={zones}
          selectedId={selectedId}
          onSelect={onSelectZone}
        />
        <SideColumn
          coin={coin}
          side="above"
          zones={zones}
          selectedId={selectedId}
          onSelect={onSelectZone}
        />
      </div>
    </div>
  );
}
