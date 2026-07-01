"""趋势/资金流事件生成与邮件渲染。无任何交易建议字段。"""

from __future__ import annotations

import html
from typing import Optional

from models.trend_monitor import TrendEvent, TrendSnapshot
from storage.trend_store import TrendStore


def _event(
    snapshot: TrendSnapshot, event_type: str, title: str, message: str,
    suffix: str, severity: str = "warning",
) -> TrendEvent:
    direction = snapshot.direction if snapshot.direction in ("bullish", "bearish") else None
    return TrendEvent(
        ts=snapshot.ts, event_type=event_type, severity=severity,
        direction=direction, title=title, message=message,
        dedup_key=f"BTC:{event_type}:{suffix}",
        payload={"state": snapshot.state, "core_score": snapshot.core_score,
                 "confidence": snapshot.confidence, "closed_5m_ts": snapshot.closed_5m_ts},
    )


def build_events(snapshot: TrendSnapshot, previous: Optional[TrendSnapshot],
                 store: TrendStore, config=None, looknode_config=None) -> list[TrendEvent]:
    events: list[TrendEvent] = []
    state_changed = previous is None or snapshot.state != previous.state
    recovered = bool(previous and previous.state == "data_invalid" and snapshot.state != "data_invalid")
    confirmation_bars = int(getattr(config, "confirmation_bars", 3))
    # range 不是邮件事件；从 data_invalid 恢复到 range 仍必须发恢复通知。
    if state_changed and (snapshot.state != "range" or recovered):
        names = {
            "data_invalid": ("数据异常", "核心原生数据未通过质量门，趋势判断已停用。"),
            "range": ("趋势回到区间", "核心方向分低于有效趋势阈值。"),
            "bullish_watch": ("多头趋势预警", "出现多头倾向，但尚未满足确认条件。"),
            "bearish_watch": ("空头趋势预警", "出现空头倾向，但尚未满足确认条件。"),
            "bullish_candidate": ("多头候选趋势", f"核心条件成立，等待连续{confirmation_bars}个闭合5m周期。"),
            "bearish_candidate": ("空头候选趋势", f"核心条件成立，等待连续{confirmation_bars}个闭合5m周期。"),
            "bullish_confirmed": ("多头趋势确认", "4h主趋势、1h确认、1d过滤与现货CVD均通过。"),
            "bearish_confirmed": ("空头趋势确认", "4h主趋势、1h确认、1d过滤与现货CVD均通过。"),
            "weakening": ("趋势减弱", "此前确认趋势的核心条件已不再完整成立。"),
            "reversal_watch": ("反转预警", "核心方向已与此前确认趋势相反，尚未完成三周期确认。"),
            "reversal_confirmed": ("反转确认", f"相反方向已连续{confirmation_bars}个闭合5m周期满足核心确认条件。"),
        }
        title, message = names[snapshot.state]
        event_type = "data_recovered" if recovered else f"trend_{snapshot.state}"
        if recovered:
            title = "核心趋势数据恢复"
            message = f"核心原生数据重新通过质量门，当前状态：{names[snapshot.state][0]}。"
        events.append(_event(
            snapshot, event_type, title, message,
            f"{snapshot.closed_5m_ts}:{snapshot.state}",
            "critical" if "confirmed" in snapshot.state else "warning",
        ))

    extremes: dict[tuple[str, str], tuple[float, float, Optional[float], int]] = {}
    for market, flow in snapshot.active_flows.items():
        for window in flow.windows:
            if window.window not in ("1h", "24h"):
                continue
            window_sec = 3600 if window.window == "1h" else 86400
            if not flow.quality.valid:
                continue
            # NetFlow端点是滚动窗口，不能写入闭合历史基线。窗口结束键只用于去重。
            window_end_ts = (snapshot.ts // window_sec) * window_sec
            lookback = 30 if window.window == "1h" else 180
            percentile = store.flow_abs_percentile(
                market, window.window, window.net_usd, lookback,
                min_samples=720 if window.window == "1h" else 180,
                as_of_ts=window_end_ts,
            )
            window.historical_percentile = percentile
            threshold = (
                float(getattr(config, "active_flow_1h_percentile", 99.0))
                if window.window == "1h" else
                float(getattr(config, "active_flow_24h_percentile", 97.5))
            )
            min_ratio = float(getattr(config, "active_flow_1h_min_ratio", 0.10))
            ratio_ok = window.window != "1h" or abs(window.net_ratio) >= min_ratio
            if percentile is not None and percentile >= threshold and ratio_ok:
                direction = "净买" if window.net_usd > 0 else "净卖"
                event_type = f"{market}_active_flow_extreme"
                events.append(_event(
                    snapshot, event_type,
                    f"{market.upper()} {window.window}异常{direction}",
                    f"主动成交净流 ${window.net_usd:,.0f}，历史分位 {percentile:.1f}%。",
                    f"{window.window}:{'buy' if window.net_usd > 0 else 'sell'}:{window_end_ts}",
                ))
                extremes[(market, window.window)] = (
                    window.net_usd, window.net_ratio, percentile, window_end_ts,
                )
    for window_name in ("1h", "24h"):
        spot = extremes.get(("spot", window_name))
        futures = extremes.get(("futures", window_name))
        if spot and futures and spot[0] * futures[0] > 0 and spot[3] == futures[3]:
            direction = "净买" if spot[0] > 0 else "净卖"
            events.append(_event(
                snapshot, "cross_market_flow_resonance",
                f"现货/合约{window_name}资金共振{direction}",
                f"现货与合约{window_name}主动成交净流同时达到动态极端阈值。",
                f"{window_name}:{direction}:{spot[3]}", "critical",
            ))

    wallet = snapshot.wallet_flow
    daily_history = [
        p.net_change_btc for p in wallet.chart[:-1] if p.net_change_btc is not None
    ][-365:]
    if wallet.quality.valid and len(daily_history) >= 365 and wallet.change_1d_btc is not None:
        rank = 100 * sum(abs(v) <= abs(wallet.change_1d_btc) for v in daily_history) / len(daily_history)
        if rank >= 99:
            word = "流入" if wallet.change_1d_btc > 0 else "流出"
            events.append(_event(
                snapshot, "wallet_large_flow", f"交易所钱包大额{word}",
                f"日级余额变化 {wallet.change_1d_btc:+,.2f} BTC，365日绝对值分位 {rank:.1f}%。余额增加代表潜在卖压，不等于已经卖出。",
                f"1d:{word}:{wallet.chart[-1].ts}", "critical",
            ))
        for days, persistence_required in ((3, 2), (7, 5)):
            current_points = wallet.chart[-days:]
            if len(current_points) < days or any(p.net_change_btc is None for p in current_points):
                continue
            current = sum(float(p.net_change_btc) for p in current_points)
            historical = []
            for end in range(days - 1, len(wallet.chart) - days):
                sample = wallet.chart[end - days + 1:end + 1]
                if all(point.net_change_btc is not None for point in sample):
                    historical.append(sum(float(point.net_change_btc) for point in sample))
            historical = historical[-365:]
            if len(historical) < 365:
                continue
            percentile = (
                100 * sum(abs(value) <= abs(current) for value in historical) / len(historical)
                if historical else 0.0
            )
            sign_count = sum(
                float(point.net_change_btc) * current > 0 for point in current_points
            ) if current else 0
            if percentile >= 98 and sign_count >= persistence_required:
                word = "流入" if current > 0 else "流出"
                events.append(_event(
                    snapshot, f"wallet_large_flow_{days}d",
                    f"交易所钱包{days}日大额{word}",
                    f"{days}日累计 {current:+,.2f} BTC，历史绝对值分位 {percentile:.1f}%，"
                    f"其中 {sign_count}/{days} 天同向。余额增加代表潜在卖压，不等于已经卖出。",
                    f"{days}d:{word}:{wallet.chart[-1].ts}", "critical",
                ))

        # 单一交易所异常与全市场事件分开，避免把地址迁移误报成全市场变化。
        for exchange, series in wallet.exchange_charts.items():
            exchange_deltas = [p.net_change_btc for p in series[:-1] if p.net_change_btc is not None][-365:]
            if len(exchange_deltas) < 365 or series[-1].net_change_btc is None:
                continue
            current = float(series[-1].net_change_btc)
            percentile = 100 * sum(
                abs(value) <= abs(current) for value in exchange_deltas[-365:]
            ) / 365
            if percentile >= 99:
                word = "流入" if current > 0 else "流出"
                events.append(_event(
                    snapshot, "wallet_exchange_large_flow",
                    f"{exchange} 钱包大额{word}",
                    f"单交易所日级余额变化 {current:+,.2f} BTC，365日分位 {percentile:.1f}%。"
                    "可能包含内部钱包迁移，不等于已经买卖。",
                    f"{exchange}:1d:{word}:{series[-1].ts}",
                ))
    if wallet.quality.valid and abs(wallet.consecutive_direction_days) >= 3:
        days = abs(wallet.consecutive_direction_days)
        bucket = 7 if days >= 7 else 3
        word = "流入" if wallet.consecutive_direction_days > 0 else "流出"
        latest_wallet_ts = wallet.chart[-1].ts if wallet.chart else 0
        streak_start_ts = (
            wallet.chart[-days].ts if wallet.chart and len(wallet.chart) >= days else latest_wallet_ts
        )
        events.append(_event(
            snapshot, f"wallet_persistent_{bucket}d", f"交易所钱包连续{bucket}日{word}",
            f"当前连续同向 {days} 日。余额增加代表潜在卖压，不等于已经卖出。",
            f"{bucket}d:{word}:{streak_start_ts}",
        ))

    transfer = snapshot.exchange_transfer_flow
    if transfer.quality.valid and transfer.quality.points >= 180 and transfer.latest_date_ts:
        daily_threshold = float(getattr(looknode_config, "alert_daily_percentile", 99.0))
        multiday_threshold = float(
            getattr(looknode_config, "alert_multiday_percentile", 98.0)
        )
        one_day = next((item for item in transfer.windows if item.window == "1d"), None)
        if one_day:
            inflow_extreme = (one_day.inflow_percentile_365d or 0) >= daily_threshold
            outflow_extreme = (one_day.outflow_percentile_365d or 0) >= daily_threshold
            if inflow_extreme and outflow_extreme and abs(one_day.net_ratio) < 0.10:
                events.append(_event(
                    snapshot, "looknode_exchange_high_turnover",
                    "七家交易所BTC双向高周转",
                    f"日级流入 {one_day.inflow_btc:,.2f} BTC、流出 "
                    f"{one_day.outflow_btc:,.2f} BTC 同时达到历史极端，但净流比例仅 "
                    f"{one_day.net_ratio * 100:.2f}%。可能是内部钱包迁移，不作方向解读。",
                    f"1d:turnover:{transfer.latest_date_ts}",
                ))
            elif inflow_extreme and one_day.netflow_btc > 0:
                events.append(_event(
                    snapshot, "looknode_exchange_inflow_extreme",
                    "七家交易所BTC大额流入",
                    f"日级流入 {one_day.inflow_btc:,.2f} BTC，达到365日"
                    f"{one_day.inflow_percentile_365d:.1f}分位。充值增加潜在卖压，不等于已经卖出。",
                    f"1d:in:{transfer.latest_date_ts}", "critical",
                ))
            elif outflow_extreme and one_day.netflow_btc < 0:
                events.append(_event(
                    snapshot, "looknode_exchange_outflow_extreme",
                    "七家交易所BTC大额流出",
                    f"日级流出 {one_day.outflow_btc:,.2f} BTC，达到365日"
                    f"{one_day.outflow_percentile_365d:.1f}分位。提现减少潜在卖压，不等于已经买入。",
                    f"1d:out:{transfer.latest_date_ts}", "critical",
                ))
        for window in transfer.windows:
            if window.window not in ("3d", "7d"):
                continue
            required = 2 if window.window == "3d" else 5
            if ((window.abs_net_percentile_365d or 0) < multiday_threshold
                    or window.same_sign_days < required):
                continue
            word = "净流入" if window.netflow_btc > 0 else "净流出"
            events.append(_event(
                snapshot, f"looknode_exchange_net_{window.window}_extreme",
                f"七家交易所BTC持续{word}",
                f"{window.window}累计 {window.netflow_btc:+,.2f} BTC，绝对净流达到365日"
                f"{window.abs_net_percentile_365d:.1f}分位，{window.same_sign_days}/"
                f"{window.window[:-1]}天同向。链上转账不等于实际买卖。",
                f"{window.window}:{'in' if window.netflow_btc > 0 else 'out'}:"
                f"{transfer.latest_date_ts}",
            ))
        seven_day = next((item for item in transfer.windows if item.window == "7d"), None)
        if (transfer.cross_source_status == "conflict" and seven_day
                and (seven_day.abs_net_percentile_365d or 0) >= 95
                and (transfer.coinglass_7d_abs_percentile or 0) >= 95):
            events.append(_event(
                snapshot, "wallet_cross_source_conflict",
                "交易所钱包数据源强冲突",
                "Looknode七家交易所净流与CoinGlass钱包余额7日方向相反，"
                "且双方均达到历史高分位；本轮钱包可信度修正已归零。",
                f"7d:conflict:{transfer.latest_date_ts}", "warning",
            ))

    # 辅助源质量变化单独通知；Footprint首版零权重，不进入邮件。
    if previous is not None:
        current_sources = {
            "spot_netflow": snapshot.active_flows.get("spot").quality if snapshot.active_flows.get("spot") else None,
            "futures_netflow": snapshot.active_flows.get("futures").quality if snapshot.active_flows.get("futures") else None,
            "wallet": snapshot.wallet_flow.quality,
            "funding": snapshot.funding.quality,
            "etf": snapshot.etf_flow.quality,
            "looknode_exchange_flow": snapshot.exchange_transfer_flow.quality,
        }
        previous_sources = {
            "spot_netflow": previous.active_flows.get("spot").quality if previous.active_flows.get("spot") else None,
            "futures_netflow": previous.active_flows.get("futures").quality if previous.active_flows.get("futures") else None,
            "wallet": previous.wallet_flow.quality,
            "funding": previous.funding.quality,
            "etf": previous.etf_flow.quality,
            "looknode_exchange_flow": previous.exchange_transfer_flow.quality,
        }
        for source, quality in current_sources.items():
            old = previous_sources.get(source)
            if quality is None or old is None or quality.valid == old.valid:
                continue
            if quality.status == "pending" or old.status == "pending":
                continue
            recovered_source = quality.valid
            if recovered_source and not store.has_unrecovered_source_failure(source):
                continue
            events.append(_event(
                snapshot,
                "data_source_recovered" if recovered_source else "data_source_invalid",
                f"{source}数据{'恢复' if recovered_source else '异常'}",
                "数据重新通过时效与完整性质量门。" if recovered_source else quality.reason,
                f"{source}:{'up' if recovered_source else 'down'}:{snapshot.closed_5m_ts}",
                "info" if recovered_source else "warning",
            ))
    return events


def render_email(event: TrendEvent, snapshot: TrendSnapshot) -> tuple[str, str]:
    subject = f"[LIQ BTC监控] {event.title}"
    color = "#ef4444" if event.severity == "critical" else "#f59e0b"
    exhaustion = snapshot.flow_exhaustion_watch
    exhaustion_html = ""
    if exhaustion.quality.valid:
        evidence = "".join(
            f"<li>{html.escape(item)}</li>" for item in exhaustion.evidence[:3]
        ) or "<li>暂无额外证据</li>"
        risks = "".join(
            f"<li>{html.escape(item)}</li>" for item in exhaustion.risks[:3]
        ) or "<li>暂无额外风险证据</li>"
        missing = "".join(
            f"<li>{html.escape(item)}</li>"
            for item in exhaustion.missing_confirmations[:3]
        ) or "<li>当前分类无需额外解释条件</li>"
        exhaustion_html = f"""
        <div style="margin-top:18px;padding:14px;border:1px solid #334155;border-radius:8px;background:#0f172a">
          <div style="font-weight:700">资金衰竭诊断（零权重）</div>
          <div style="margin-top:6px">{html.escape(exhaustion.headline)}</div>
          <div style="color:#94a3b8;font-size:12px">{html.escape(exhaustion.detail)}</div>
          <div style="margin-top:10px;font-size:12px"><b>证据</b><ul>{evidence}</ul></div>
          <div style="font-size:12px"><b>风险</b><ul>{risks}</ul></div>
          <div style="font-size:12px"><b>仍缺确认</b><ul>{missing}</ul></div>
          <div style="color:#64748b;font-size:11px">只解释资金行为，不单独触发邮件，不改变趋势方向或确认状态。</div>
        </div>"""
    body = f"""<!doctype html><html><body style="font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;padding:24px">
    <div style="max-width:680px;margin:auto;background:#111827;border:1px solid #334155;border-radius:12px;overflow:hidden">
      <div style="background:{color};padding:14px 20px;color:white;font-weight:700">{html.escape(event.title)}</div>
      <div style="padding:20px;line-height:1.7">
        <p>{html.escape(event.message)}</p>
        <p>状态：{html.escape(snapshot.state)} · 核心方向分：{snapshot.core_score:.1f} · 信号强度：{snapshot.confidence:.1f}/100（不是胜率）</p>
        {exhaustion_html}
        <p style="color:#94a3b8;font-size:12px">仅用于趋势与资金状态监控，不构成交易、开仓、止盈止损或仓位建议。</p>
      </div>
    </div></body></html>"""
    return subject, body
