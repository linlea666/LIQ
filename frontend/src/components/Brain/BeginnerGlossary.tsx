const TERMS = [
  ["偏多 / 偏空", "只是当前证据更偏向上涨或下跌，不代表现在应该买卖。"],
  ["市场状态", "上涨趋势、下降趋势、震荡、挤压或高波动乱震，是市场所处环境。"],
  ["平均波动（ATR）", "近期一根K线通常波动多大；只表示波动，不表示方向。"],
  ["主动资金流（CVD）", "主动买入减主动卖出的累计变化；上升是买方更主动，下降是卖方更主动。"],
  ["持仓（OI）", "尚未平掉的合约总量；增加是参与/杠杆升温，减少是平仓或去杠杆，本身没有方向。"],
  ["资金费（Funding）", "多空双方定期支付的持仓成本；极端正值说明多头拥挤，极端负值说明空头拥挤。"],
  ["扫单吸引（SA）", "这个区域有多容易吸引价格去触发止损或清算；高不等于会反弹。"],
  ["防守可信（SR）", "价格到达后作为支撑反弹或阻力压回的证据强度。"],
  ["继续打穿风险（BTR）", "价格扫到这里后继续快速穿过去的风险；高时不宜把它当硬支撑/阻力。"],
  ["清算磁铁 / 止损带", "杠杆仓位集中的潜在扫单区域，可能吸引价格，但不是必达目标。"],
  ["结构确认", "只表示观察条件已形成，不等于系统建议入场。"],
] as const;

export default function BeginnerGlossary() {
  return (
    <details className="shrink-0 border-b border-slate-800 bg-slate-950/60 px-3 py-1.5 text-[11px] text-slate-400">
      <summary className="cursor-pointer select-none font-medium text-slate-300">
        新手词典：偏多、OI、资金费、磁铁这些词是什么意思？
      </summary>
      <div className="mt-2 grid gap-2 pb-1 sm:grid-cols-2 xl:grid-cols-3">
        {TERMS.map(([term, meaning]) => (
          <div key={term} className="rounded border border-slate-800/80 bg-slate-900/50 px-2 py-1.5">
            <div className="font-medium text-slate-200">{term}</div>
            <div className="mt-0.5 leading-relaxed text-slate-500">{meaning}</div>
          </div>
        ))}
      </div>
    </details>
  );
}
