/**
 * structureBrief — 市场结构 / 反弹质量 / 突破三步确认的中文短语工具
 *
 * 后端已经通过三条渠道把结构数据回传到前端：
 *   - RangeSignalData.ms_*            → 顶部徽章使用
 *   - RangeSignalData.ms_alignment    → 箱体卡面徽章使用
 *   - KeyLevelV2.bounce_quality       → 强位卡片使用
 *   - KeyLevelV2.breakout_stage       → 强位卡片使用
 *
 * 本工具只做"枚举 → 中文短语 + 颜色 + tooltip"的纯映射，
 * 不做任何数据抓取或业务判断，保证 UI 组件可复用一致口径。
 */

export interface BriefTag {
  label: string;
  color: string;          // tailwind text-* 类名
  bg?: string;            // tailwind bg-* 类名（可选）
  hint: string;           // tooltip 详细说明
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 市场结构方向
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function directionBrief(
  direction: string | null | undefined,
): BriefTag | null {
  if (!direction) return null;
  switch (direction) {
    case "bullish":
      return {
        label: "🟢 上升结构",
        color: "text-emerald-400",
        bg: "bg-emerald-500/10",
        hint: "1h 级别 HH + HL：价格高点与低点同步上移，顺势做多占优",
      };
    case "bearish":
      return {
        label: "🔴 下降结构",
        color: "text-red-400",
        bg: "bg-red-500/10",
        hint: "1h 级别 LH + LL：价格高点与低点同步下移，顺势做空占优",
      };
    case "ranging":
      return {
        label: "⚪ 震荡结构",
        color: "text-slate-300",
        bg: "bg-slate-500/15",
        hint: "1h 级别高点低点无明显趋势，区间震荡中，等箱体边沿再操作",
      };
    case "transitioning":
      return {
        label: "🟡 结构转换中",
        color: "text-yellow-300",
        bg: "bg-yellow-500/10",
        hint: "1h 级别出现 HH+LL 或 LH+HL 混合信号，方向尚不确定，建议观望",
      };
    default:
      return null;
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 结构事件（BOS / CHoCH）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function eventBrief(
  event: string | null | undefined,
): BriefTag | null {
  if (!event) return null;
  switch (event) {
    case "BOS_up":
      return {
        label: "BOS↑破前高",
        color: "text-emerald-400",
        bg: "bg-emerald-500/10",
        hint: "Break of Structure · 向上延续：价格突破最近 swing high，多头结构延续",
      };
    case "BOS_down":
      return {
        label: "BOS↓破前低",
        color: "text-red-400",
        bg: "bg-red-500/10",
        hint: "Break of Structure · 向下延续：价格跌破最近 swing low，空头结构延续",
      };
    case "CHoCH_up":
      return {
        label: "CHoCH↑反转",
        color: "text-amber-400",
        bg: "bg-amber-500/10",
        hint: "Change of Character · 向上反转：下降结构中价格破前高，可能转多",
      };
    case "CHoCH_down":
      return {
        label: "CHoCH↓反转",
        color: "text-orange-400",
        bg: "bg-orange-500/10",
        hint: "Change of Character · 向下反转：上升结构中价格跌破前低，可能转空",
      };
    default:
      return null;
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 操作偏置
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function biasBrief(
  bias: string | null | undefined,
): BriefTag | null {
  if (!bias) return null;
  switch (bias) {
    case "long_only":
      return {
        label: "📈 顺势做多",
        color: "text-emerald-400",
        bg: "bg-emerald-500/10",
        hint: "结构明确向上，仅推荐顺势做多交易；逆向操作胜率较低",
      };
    case "short_only":
      return {
        label: "📉 顺势做空",
        color: "text-red-400",
        bg: "bg-red-500/10",
        hint: "结构明确向下，仅推荐顺势做空交易；逆向操作胜率较低",
      };
    case "both_ok":
      return {
        label: "🔁 双向可做",
        color: "text-slate-300",
        bg: "bg-slate-500/10",
        hint: "结构偏震荡，多空双向都可以在边沿等反转信号",
      };
    case "stand_aside":
      return {
        label: "⏸️ 观望为宜",
        color: "text-yellow-300",
        bg: "bg-yellow-500/10",
        hint: "结构不清或信号冲突，建议空仓等待更明确的方向",
      };
    default:
      return null;
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// RangeSignal 与结构方向的对齐度
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function alignmentBrief(
  alignment: string | null | undefined,
): BriefTag | null {
  if (!alignment || alignment === "unknown" || alignment === "neutral") {
    return null;
  }
  if (alignment === "aligned") {
    return {
      label: "✅ 结构一致",
      color: "text-emerald-400",
      bg: "bg-emerald-500/10",
      hint: "箱体信号方向与 1h 市场结构一致，顺势开仓胜率更高",
    };
  }
  if (alignment === "conflict") {
    return {
      label: "⚠️ 结构冲突",
      color: "text-orange-400",
      bg: "bg-orange-500/10",
      hint: "箱体信号方向与 1h 市场结构相反，或结构要求观望，谨慎开仓",
    };
  }
  return null;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 反弹质量（bounce_quality）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function bounceQualityBrief(
  quality: string | null | undefined,
): BriefTag | null {
  if (!quality) return null;
  if (quality === "proactive") {
    return {
      label: "🔥 主动吸筹",
      color: "text-orange-300",
      bg: "bg-orange-500/10",
      hint: "反弹/拒绝伴随大成交量（≥ 均量 1.5×）+ 方向一致的 K 线 —— 真实主力在此承接/抛压",
    };
  }
  if (quality === "passive") {
    return {
      label: "🌫️ 被动触发",
      color: "text-slate-400",
      bg: "bg-slate-500/10",
      hint: "反弹时成交清淡（< 均量 0.8×） —— 可能只是止损/止盈触发，信号强度弱",
    };
  }
  return null;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 中长期基调（BullBearLine.current_regime · 日/周线维度）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function regimeBrief(
  regime: string | null | undefined,
): BriefTag | null {
  if (!regime) return null;
  switch (regime) {
    case "bull":
      return {
        label: "🌞 中长期偏多",
        color: "text-emerald-300",
        bg: "bg-emerald-500/10",
        hint: "日/周线牛熊分界线（200SMA / 牛市支撑带 / 一目云层）判定为多头区间",
      };
    case "bear":
      return {
        label: "🌑 中长期偏空",
        color: "text-red-300",
        bg: "bg-red-500/10",
        hint: "日/周线牛熊分界线（200SMA / 牛市支撑带 / 一目云层）判定为空头区间",
      };
    case "neutral":
      return {
        label: "⚖️ 多空胶着",
        color: "text-slate-300",
        bg: "bg-slate-500/15",
        hint: "日/周线中长期基调多空胶着，方向需看短期结构主导",
      };
    default:
      return null;
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// MACD 动能状态（日线 · 零轴 + 柱状图方向）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function macdMomentumBrief(
  aboveZero: boolean | null | undefined,
  histRising: boolean | null | undefined,
): BriefTag | null {
  if (aboveZero == null || histRising == null) return null;
  if (aboveZero && histRising) {
    return {
      label: "💪 强势多头",
      color: "text-emerald-400",
      bg: "bg-emerald-500/10",
      hint: "日线 MACD 在零轴上方且柱状图持续增长 —— 多头动能增强，顺势做多胜率高",
    };
  }
  if (aboveZero && !histRising) {
    return {
      label: "⚡ 多头动能衰减",
      color: "text-yellow-300",
      bg: "bg-yellow-500/10",
      hint: "日线 MACD 仍在零轴上方但柱状图缩短 —— 多头动能正在衰减，警惕回调",
    };
  }
  if (!aboveZero && !histRising) {
    return {
      label: "🧊 强势空头",
      color: "text-red-400",
      bg: "bg-red-500/10",
      hint: "日线 MACD 在零轴下方且柱状图持续增长（绝对值）—— 空头动能增强，顺势做空胜率高",
    };
  }
  return {
    label: "🌙 空头动能衰减",
    color: "text-sky-300",
    bg: "bg-sky-500/10",
    hint: "日线 MACD 仍在零轴下方但柱状图收缩 —— 空头动能衰减，可能出现反弹",
  };
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// MTF 一致性徽章（1w / 1d / 1h 三周期结构对齐度）
//
// 与 backend/ai/prompts.py `_mtf_alignment_line` 同口径。
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

type MTFDirectionInput = string | null | undefined;

interface MtfSnapshotLike {
  direction?: MTFDirectionInput;
  confidence?: number | null;
}

const MTF_CONF_MIN = 0.5;

function _normalizeDir(
  ms: MtfSnapshotLike | null | undefined,
): "bullish" | "bearish" | "unclear" | "missing" {
  if (!ms) return "missing";
  const conf = ms.confidence ?? 0;
  const d = ms.direction;
  if ((d === "bullish" || d === "bearish") && conf >= MTF_CONF_MIN) return d;
  return "unclear";
}

/**
 * MTF 一致性徽章输入：三个 TF 的原始结构快照（任意一个可缺失）。
 * 返回：
 *   - resonance=true  → 三周期同向共振（加码信号）
 *   - divergence_big  → 周/日方向相反（降仓信号）
 *   - partial_split   → 日周同向 + 1h 相反（1h 回调/反弹）
 *   - neutral         → 无明确共振（次要参考）
 */
export function mtfAlignmentBrief(
  ms_1w: MtfSnapshotLike | null | undefined,
  ms_1d: MtfSnapshotLike | null | undefined,
  ms_1h: MtfSnapshotLike | null | undefined,
): BriefTag | null {
  if (!ms_1w && !ms_1d && !ms_1h) return null;

  const w = _normalizeDir(ms_1w);
  const d = _normalizeDir(ms_1d);
  const h = _normalizeDir(ms_1h);

  // 三周期同向共振
  if (w === d && d === h && (w === "bullish" || w === "bearish")) {
    const isLong = w === "bullish";
    return {
      label: isLong ? "🎯 MTF 三周期共振·多" : "🎯 MTF 三周期共振·空",
      color: isLong ? "text-emerald-300" : "text-red-300",
      bg: isLong ? "bg-emerald-500/15" : "bg-red-500/15",
      hint: `1w / 1d / 1h 三周期同向${isLong ? "多头" : "空头"} — 高胜率窗口，可考虑加大仓位或延长持有`,
    };
  }

  // 日周冲突
  if ((w === "bullish" || w === "bearish") && (d === "bullish" || d === "bearish") && w !== d) {
    return {
      label: "⚠ MTF 周日冲突",
      color: "text-orange-400",
      bg: "bg-orange-500/15",
      hint: "周线与日线方向相反 — 结构转换期，宜降仓 / 只做短线，避免逆大周期重仓",
    };
  }

  // 日周同向 + 1h 反向 → 回调/反弹
  if (w === d && (w === "bullish" || w === "bearish") && (h === "bullish" || h === "bearish") && h !== w) {
    const bigIsLong = w === "bullish";
    return {
      label: `🔄 日周${bigIsLong ? "多" : "空"} vs 1h ${h === "bullish" ? "多" : "空"}`,
      color: "text-amber-300",
      bg: "bg-amber-500/15",
      hint: `日周主导方向=${bigIsLong ? "多头" : "空头"}，1h 视为${bigIsLong ? "回调" : "反弹"}执行层 — 短线可做 1h 方向，中远线以日周为准`,
    };
  }

  return {
    label: "ℹ MTF 未共振",
    color: "text-slate-400",
    bg: "bg-slate-500/15",
    hint: "周/日/小时结构未形成明确共振（含震荡或置信度不足），MTF 对齐度作为次要参考",
  };
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 突破三步确认（breakout_stage）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function breakoutStageBrief(
  stage: number | null | undefined,
): BriefTag | null {
  if (!stage || stage < 1 || stage > 3) return null;
  if (stage === 1) {
    return {
      label: "🔨 第1步·破位",
      color: "text-amber-400",
      bg: "bg-amber-500/10",
      hint: "价格刚突破该位（15 分钟内）。博主方法论：等待回踩再入场，首根破位不可追",
    };
  }
  if (stage === 2) {
    return {
      label: "🔄 第2步·回踩中",
      color: "text-yellow-300",
      bg: "bg-yellow-500/10",
      hint: "破位后出现回踩（90 分钟内触达原位 ±0.5×ATR）。等下一根确认再操作",
    };
  }
  return {
    label: "✅ 第3步·确认",
    color: "text-emerald-400",
    bg: "bg-emerald-500/10",
    hint: "回踩后再次反向延续 ≥ 0.3×ATR → 突破三步确认完成，信号最强",
  };
}
