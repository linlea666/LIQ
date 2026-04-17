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
