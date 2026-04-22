r"""数据时效元信息（DataMeta）基础设施 · P1.1 骨架版

## 背景
生产 AI 报告里经常出现 "ETF 当日流入: 2026-04-21 \$0" 这种陷阱——
看似"流入为 0 = 资金面转空"，实际只是**当日美股未收盘 ETF 数据尚未更新**。
类似问题在 CVD 日线 / OI 小时线 / 清算窗口等多处都有，但共同根因都是：
数据点没有统一的"**as_of / staleness / status**"元信息，渲染层只能看到值，
无法区分"真实 0" vs "尚未更新"。

## 本轮定位（P1.1）
本文件提供**轻量 DataMeta 模型**作为未来注入基础设施的骨架，
当前只在 ETF 当日明细处落地消费（P1.1 最高价值切片）。
CVD / OI / 清算等批量注入留作下一轮专项（见 P1.1-extend TODO）。

## 字段语义
- `as_of`: 数据快照代表的时间（秒级 UTC），不是采集时间也不是计算时间
- `staleness_sec`: 距当前时刻的秒数（>0）
- `status`: "fresh" / "stale" / "pending" / "missing"
  - fresh: 在预期更新周期内
  - stale: 超过预期周期但仍可参考
  - pending: 当前周期尚未收盘/结算（如 ETF 当日、CVD 日线当根）
  - missing: 上游采集失败
- `pending_reason`: 仅在 status=pending 时填 "尚未收盘" / "日线当根未收"
- `source`: "coinglass-v4" / "bbx" / "binance-fapi" 等

## 未来集成路径
1. 每个 poll/processor 产出数据时附带 DataMeta（新字段，可选）
2. prompts.py 渲染层优先读 DataMeta.status 决定是否展示"真值"还是"pending"标签
3. 前端 AI 详情页同步展示 staleness 角标
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


DataStatus = Literal["fresh", "stale", "pending", "missing"]


class DataMeta(BaseModel):
    """数据点的时效/状态元信息（可选字段，向后兼容）。"""

    as_of: int = 0                         # 数据代表的时间（秒级 UTC）
    staleness_sec: int = 0                 # 距当前时刻的秒数
    status: DataStatus = "fresh"
    pending_reason: str = ""               # 仅在 status=pending 时填
    source: str = ""                       # "coinglass-v4" / "bbx" / ...

    def describe_cn(self) -> str:
        """人类可读的简要状态描述（供 prompt 渲染）。"""
        if self.status == "fresh":
            return ""
        if self.status == "pending":
            return f"pending（{self.pending_reason or '尚未收盘/结算'}）"
        if self.status == "stale":
            mins = max(1, self.staleness_sec // 60)
            return f"stale（数据延迟 {mins}min）"
        if self.status == "missing":
            return "数据缺失"
        return ""


def infer_etf_daily_status(date_str: str, total_net: float,
                           now_ts: int, as_of_ts: Optional[int] = None) -> DataMeta:
    """为 ETF 当日明细推断 DataMeta。

    规则（美股 ETF 结算约在美东 16:30 ≈ 次日 UTC 20:30 前后完成聚合）：
    1. date == 今日 UTC → **无条件 pending**（无论金额多少，当日均未收盘）
       - 之所以不再用 "total_net ≈ 0 才标 pending" 的老条件，是因为：
         · ETF 当日盘中会有预估流入快照（如 $1千万 / $3千万），不是真实终值
         · 交易员看到非零数字会以为是当日收盘终值而据此调整方向 / 仓位
         · 实盘中"美股未收盘 → 当日所有 ETF 数据都不可靠"是铁律
       - 金额大小不是判定依据，时间上是"今日"就足以 pending
    2. date < 今日 UTC → fresh（历史日，已收盘终值）

    本方法由 P0.8 HIGH-2 改造，覆盖 P1.1 的判定条件过严 bug。
    """
    from datetime import datetime, timezone

    today = datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    if date_str == today:
        amount_hint = ""
        try:
            net_f = float(total_net or 0)
            if abs(net_f) >= 1.0:
                amount_hint = "，盘中快照非终值"
            else:
                amount_hint = "，0 可能非真实流入"
        except (TypeError, ValueError):
            amount_hint = ""
        return DataMeta(
            as_of=as_of_ts or now_ts,
            staleness_sec=0,
            status="pending",
            pending_reason=f"今日美股 ETF 尚未收盘{amount_hint}",
            source="coinglass-v4",
        )
    # 其余情况保持 fresh
    return DataMeta(
        as_of=as_of_ts or now_ts,
        staleness_sec=max(0, now_ts - (as_of_ts or now_ts)),
        status="fresh",
        source="coinglass-v4",
    )
