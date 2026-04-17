/**
 * 关键位 source 标签归一化 + 白话解释 + 权重
 *
 * 后端 `KeyLevelV2.sources` 存的是中文长标签（如 "日线EMA20"、"200日SMA(多空分界线)"、
 * "买墙$12M"、"2x多头清算$5M"）。本模块通过关键词匹配归一到统一分类，并提供：
 *   - label: 白话短标签（用于卡片一行展示）
 *   - hint:  小白解释（用于 title/tooltip）
 *   - weight: 可信度权重（用于"为什么强" TOP-K 排序）
 *   - category: 归类（macro / liq / capital / structure）
 *
 * 新增数据源只需在 RULES 追加一行；命中即返回，无需改调用方。
 */

export type SourceCategory = "macro" | "liq" | "capital" | "structure";

export interface SourceBrief {
  label: string;
  hint: string;
  weight: number; // 0-10
  category: SourceCategory;
}

type Matcher = RegExp | ((s: string) => boolean);

// 匹配规则按优先级从上到下，第一个命中即返回
const RULES: Array<[Matcher, SourceBrief]> = [
  // 周期级强支撑
  [
    /200周均线|200W|sma[_\-]?200w/i,
    { label: "200周均线", hint: "周期级强支撑，历史上熊市底部位", weight: 10, category: "structure" },
  ],
  [
    /CVDD/i,
    { label: "CVDD", hint: "已销毁币天价值，长期周期底线", weight: 10, category: "macro" },
  ],
  [
    /200日SMA|200日均线|sma[_\-]?200/i,
    { label: "200日均线", hint: "全球交易员都看的牛熊分水岭", weight: 9, category: "structure" },
  ],
  [
    /牛市支撑带|bmsa/i,
    { label: "牛市支撑带", hint: "20W SMA + 21W EMA 组成的强趋势区", weight: 9, category: "structure" },
  ],

  // 订单簿 / 清算
  [
    /买墙|卖墙|orderbook[_\-]?(bid|ask)[_\-]?wall/i,
    { label: "订单簿大墙", hint: "主力真金白银挂的限价防线", weight: 10, category: "capital" },
  ],
  [
    /清算|liq[_\-]?cluster|x(多头|空头)?清算/i,
    { label: "清算密集区", hint: "杠杆强平堆积区，真实资金博弈点", weight: 9, category: "liq" },
  ],

  // 经典技术位
  [
    /Fib|黄金分割|fib[_\-]?\d/i,
    { label: "黄金分割位", hint: "Fibonacci 回调位，最经典的技术支撑/阻力", weight: 8, category: "structure" },
  ],
  [
    /均线共振|ma[_\-]?cluster/i,
    { label: "均线共振", hint: "多条均线聚集，中期强位", weight: 8, category: "structure" },
  ],
  [
    /VP.*(POC|VAL|VAH)|成交密集|HVN/i,
    { label: "成交密集区", hint: "真实换手密集区（量价支撑）", weight: 8, category: "capital" },
  ],

  // 中等权重
  [
    /VWAP/i,
    { label: "VWAP", hint: "机构成本中轴（成交量加权平均价）", weight: 7, category: "capital" },
  ],
  [
    /Pivot|pivot[_\-]?(R|S)\d?/i,
    { label: "枢轴位", hint: "日内交易者看的经典支撑/阻力", weight: 7, category: "structure" },
  ],
  [
    /影线|未回补/,
    { label: "未回补影线", hint: "价格缺口，有磁吸回归倾向", weight: 7, category: "structure" },
  ],
  [
    /前[高低]|swing[_\-]?(high|low)/i,
    { label: "前高/前低", hint: "历史摆动极值点", weight: 7, category: "structure" },
  ],

  // 偏弱技术位
  [
    /一目|ichimoku/i,
    { label: "一目均衡", hint: "日式综合技术指标（云层/基准线）", weight: 6, category: "structure" },
  ],
  [
    /EMA|ema[_\-]?\d/i,
    { label: "指数均线", hint: "近期价格均衡位", weight: 6, category: "structure" },
  ],
  [
    /OI骤[增减]|oi[_\-]?surge/i,
    { label: "OI 骤变区", hint: "杠杆持仓剧烈变化的价位", weight: 6, category: "capital" },
  ],
  [
    /STH|短期盈亏/i,
    { label: "STH 成本", hint: "短期持有者盈亏平衡线", weight: 6, category: "macro" },
  ],

  // 心理位 / 整数
  [
    /心理关口|round[_\-]?\$|整数关口/i,
    { label: "心理关口", hint: "散户挂单和止损密集的整数位", weight: 5, category: "structure" },
  ],
  [
    /SMA|sma[_\-]?\d/i,
    { label: "简单均线", hint: "历史价格均衡位", weight: 5, category: "structure" },
  ],
  [
    /Pi周期|pi[_\-]?/i,
    { label: "Pi 周期", hint: "周期顶底参考指标", weight: 5, category: "macro" },
  ],
];

const FALLBACK: SourceBrief = {
  label: "",
  hint: "",
  weight: 3,
  category: "structure",
};

/** 单条 source → 归一化 brief */
export function getSourceBrief(source: string): SourceBrief {
  if (!source) return { ...FALLBACK };
  for (const [matcher, brief] of RULES) {
    const hit = matcher instanceof RegExp ? matcher.test(source) : matcher(source);
    if (hit) return brief;
  }
  // 未命中：用原字符串作 label，避免显示空字符串；hint 留空不误导
  return { ...FALLBACK, label: source };
}

/**
 * 多条 sources → 按 label 去重 + 按权重降序 + 取 TOP-K。
 * 用于"为什么强"一行拼接。
 */
export function summarizeSources(sources: string[], topK = 3): SourceBrief[] {
  if (!sources?.length) return [];
  const best = new Map<string, SourceBrief>();
  for (const s of sources) {
    const b = getSourceBrief(s);
    if (!b.label) continue;
    const cur = best.get(b.label);
    if (!cur || b.weight > cur.weight) best.set(b.label, b);
  }
  return [...best.values()].sort((a, b) => b.weight - a.weight).slice(0, topK);
}
