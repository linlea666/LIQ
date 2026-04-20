"""趋势衰竭模块 · 事后打标 & 日报生成（P0-B）

设计目标
--------
1. 读取 `logs/te_shadow/{YYYY-MM-DD}/{COIN}.jsonl`
2. 对每条过去 24h+ 的信号，基于**后续价格轨迹**给它贴标签：
   `correct` / `wrong` / `neutral`
3. 产出 Markdown 日报到 `logs/te_eval/daily_{YYYY-MM-DD}.md`
4. 支持**因子剥离**（Factor Ablation）——虚拟关掉某因子后准确率变化

打标规则
--------
基于 ATR14 正则化的 4h/12h/24h 未来收益 r = (p_future - p_now) / atr：

    - overall_state = "healthy_continuation" + direction=up → 期望 r₁₂ₕ > +0.3 ATR
    - overall_state = "momentum_fading"                    → 中性观察，r 绝对值 < 0.5 ATR 记 correct
    - overall_state = "exhaustion_warn" + direction=up     → 期望 r₁₂ₕ < 0（反向）
    - overall_state = "structural_reversal"                → 期望 r₂₄ₕ 与 direction 反向 > 0.5 ATR
    - overall_state = "neutral"                            → 观测，不参与统计
    - regime_vetoed = True                                 → 强制 skip（本身就是"别做"的信号）

未来价格查找
------------
不用外部数据源，直接在**同一份 shadow jsonl** 里找离 ts + horizon 最近的一条记录
（shadow logger 至少每小时有一次 heartbeat，24h 内必有记录）。
优点：零外部依赖、数据自洽；缺点：分辨率 ≈ 1h，够用于"方向性"判定。

无论何种打标，都保留 `r_4h / r_12h / r_24h` 原始读数进日报，方便手动复核。

每天产出文件
------------
    logs/te_eval/daily_{YYYY-MM-DD}.md

使用方式
--------
- CLI: `python -m monitoring.te_eval --date 2026-04-19`
- 程序化: `evaluate_day(date_slug="2026-04-19")` → 返回 report 文件路径
- 被 main.py scheduler 每小时调用一次，评估 yesterday
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from monitoring.te_shadow import shadow_log_root, _BJ_TZ

logger = logging.getLogger(__name__)

# ── 打标阈值（ATR 倍数）─────────────────────────────────────
_ATR_STRONG = 0.5    # "显著反向 / 同向" 的门槛
_ATR_WEAK = 0.3      # 健康续航要求的最低同向推进
_ATR_NEUTRAL = 0.4   # momentum_fading 的中性区间上限


@dataclass
class LabeledRecord:
    """单条打标记录。"""

    ts: int
    coin: str
    price: float
    atr: float
    regime: str
    regime_vetoed: bool
    consensus: str
    overall_state: str
    overall_action: str
    overall_direction: str
    position_pct: float
    data_quality: str
    sub_triggers: list[str]  # 各 TF triggers 汇总（用于 factor ablation）
    # 未来价格（按 atr 正则化后的收益）
    r_4h: Optional[float] = None
    r_12h: Optional[float] = None
    r_24h: Optional[float] = None
    label: str = "pending"    # correct / wrong / neutral / skip / pending
    skip_reason: str = ""

    # 原始（debug 用）
    p_4h: Optional[float] = None
    p_12h: Optional[float] = None
    p_24h: Optional[float] = None


def _read_shadow(coin_jsonl: Path) -> list[dict]:
    if not coin_jsonl.exists():
        return []
    out = []
    with coin_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    out.sort(key=lambda r: r.get("ts", 0))
    return out


def _read_shadow_span(coin: str, start_date: str, end_date: str) -> list[dict]:
    """读取 [start_date, end_date] 包含范围内的所有 shadow 记录（按 ts 升序）。"""
    root = Path(shadow_log_root())
    if not root.exists():
        return []
    all_recs: list[dict] = []
    d0 = datetime.strptime(start_date, "%Y-%m-%d")
    d1 = datetime.strptime(end_date, "%Y-%m-%d")
    cur = d0
    while cur <= d1:
        slug = cur.strftime("%Y-%m-%d")
        path = root / slug / f"{coin}.jsonl"
        all_recs.extend(_read_shadow(path))
        cur += timedelta(days=1)
    all_recs.sort(key=lambda r: r.get("ts", 0))
    return all_recs


def _find_price_at(
    sorted_recs: list[dict], target_ts: int, tolerance_sec: int = 7200
) -> Optional[float]:
    """在已按 ts 升序的记录中找距 target_ts 最近的 price，超过容忍返回 None。"""
    if not sorted_recs:
        return None
    # 二分
    lo, hi = 0, len(sorted_recs) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_recs[mid].get("ts", 0) < target_ts:
            lo = mid + 1
        else:
            hi = mid
    cand_idx = [lo]
    if lo - 1 >= 0:
        cand_idx.append(lo - 1)
    best = None
    best_diff = float("inf")
    for idx in cand_idx:
        rec = sorted_recs[idx]
        diff = abs(rec.get("ts", 0) - target_ts)
        if diff < best_diff:
            best_diff = diff
            best = rec
    if best is None or best_diff > tolerance_sec:
        return None
    p = best.get("price")
    if p is None or p <= 0:
        return None
    return float(p)


def _collect_triggers(rec: dict) -> list[str]:
    triggers = []
    for tf_key in ("1h", "4h", "1d"):
        tf = (rec.get("tf") or {}).get(tf_key) or {}
        for t in (tf.get("triggers") or []):
            triggers.append(f"{tf_key}:{t}")
    return triggers


def _label_one(rec: LabeledRecord) -> None:
    """根据 rec.overall_state / direction + r_*h 给它贴标签。"""
    if rec.regime_vetoed:
        rec.label = "skip"
        rec.skip_reason = "regime_vetoed(震荡/极端，本就建议观望)"
        return
    if rec.overall_state == "neutral":
        rec.label = "skip"
        rec.skip_reason = "overall_state=neutral（样本不足，不计入）"
        return
    if rec.data_quality == "insufficient":
        rec.label = "skip"
        rec.skip_reason = "data_quality=insufficient"
        return

    r12 = rec.r_12h
    r24 = rec.r_24h
    r4 = rec.r_4h
    if r12 is None and r24 is None:
        rec.label = "pending"
        rec.skip_reason = "未来价格不足（样本尚未成熟）"
        return

    state = rec.overall_state
    d = rec.overall_direction
    sign = 1 if d == "up" else (-1 if d == "down" else 0)

    if state == "healthy_continuation" or state == "accelerating_trend":
        # 期待：12h 内同向推进 > _ATR_WEAK
        target = r12 if r12 is not None else r24
        if target is None or sign == 0:
            rec.label = "pending"
            return
        if sign * target >= _ATR_WEAK:
            rec.label = "correct"
        elif sign * target <= -_ATR_STRONG:
            rec.label = "wrong"
        else:
            rec.label = "neutral"
        return

    if state == "momentum_fading":
        # 期待：12-24h 动能减弱 → |r| 不应继续放大
        target = r12 if r12 is not None else r24
        if target is None or sign == 0:
            rec.label = "pending"
            return
        # 同向继续放大 > _ATR_STRONG 视为"判错"（动能根本没减）
        if sign * target >= _ATR_STRONG:
            rec.label = "wrong"
        else:
            rec.label = "correct"  # 停滞 or 小幅回调都算 "动能减"
        return

    if state in ("exhaustion_warn", "structural_reversal"):
        # 期待：与 direction 相反 的推进
        target = r24 if r24 is not None else r12
        if target is None or sign == 0:
            rec.label = "pending"
            return
        if sign * target <= -_ATR_WEAK:
            rec.label = "correct"  # 按预期反向
        elif sign * target >= _ATR_STRONG:
            rec.label = "wrong"    # 不仅没反，反而强势续航
        else:
            rec.label = "neutral"
        return

    rec.label = "skip"
    rec.skip_reason = f"unknown_state={state}"


def label_records_for_day(
    coin: str,
    target_date: str,
) -> list[LabeledRecord]:
    """读取 target_date 的 shadow，用 target_date + 1 的数据查未来价格并打标。

    Returns:
        已打标的 LabeledRecord 列表（仅该日产生的信号）。
    """
    d0 = datetime.strptime(target_date, "%Y-%m-%d")
    d_next = (d0 + timedelta(days=1)).strftime("%Y-%m-%d")
    d_next2 = (d0 + timedelta(days=2)).strftime("%Y-%m-%d")

    # 读当天 + 往后两天（确保能查到 +24h 价格）
    all_recs = _read_shadow_span(coin, target_date, d_next2)
    today_recs = [
        r for r in all_recs
        if datetime.fromtimestamp(r.get("ts", 0), tz=_BJ_TZ).strftime("%Y-%m-%d") == target_date
    ]

    out: list[LabeledRecord] = []
    for r in today_recs:
        ts = int(r.get("ts", 0))
        price = float(r.get("price", 0.0) or 0.0)
        atr = float(r.get("atr", 0.0) or 0.0)
        if price <= 0 or atr <= 0:
            # 缺失 atr 我们降级用 price * 0.005 (0.5%) 作为代理
            atr = max(atr, price * 0.005) if price > 0 else atr
        overall = r.get("overall") or {}
        lr = LabeledRecord(
            ts=ts,
            coin=coin,
            price=price,
            atr=atr,
            regime=r.get("regime", "unknown"),
            regime_vetoed=bool(r.get("regime_vetoed", False)),
            consensus=r.get("consensus_level", "neutral"),
            overall_state=overall.get("state", "neutral"),
            overall_action=overall.get("action", "stand_aside"),
            overall_direction=overall.get("direction", "flat"),
            position_pct=float(overall.get("position_pct", 0.0) or 0.0),
            data_quality=r.get("data_quality", "insufficient"),
            sub_triggers=_collect_triggers(r),
        )
        # 查 +4 / +12 / +24 h 价格
        for horizon_h, field_p, field_r in (
            (4, "p_4h", "r_4h"),
            (12, "p_12h", "r_12h"),
            (24, "p_24h", "r_24h"),
        ):
            p_fut = _find_price_at(all_recs, ts + horizon_h * 3600)
            if p_fut is not None and atr > 0 and price > 0:
                setattr(lr, field_p, p_fut)
                setattr(lr, field_r, (p_fut - price) / atr)
        _label_one(lr)
        out.append(lr)
    return out


# ── 汇总统计 ────────────────────────────────────────────────

@dataclass
class Bucket:
    total: int = 0
    correct: int = 0
    wrong: int = 0
    neutral: int = 0
    pending: int = 0
    skip: int = 0

    def add(self, label: str) -> None:
        self.total += 1
        if label == "correct":
            self.correct += 1
        elif label == "wrong":
            self.wrong += 1
        elif label == "neutral":
            self.neutral += 1
        elif label == "pending":
            self.pending += 1
        else:
            self.skip += 1

    @property
    def judged(self) -> int:
        return self.correct + self.wrong + self.neutral

    @property
    def accuracy(self) -> Optional[float]:
        """只计 correct/wrong，不含 neutral。"""
        denom = self.correct + self.wrong
        if denom == 0:
            return None
        return self.correct / denom

    @property
    def soft_accuracy(self) -> Optional[float]:
        """把 neutral 按 0.5 计入——避免只有 1-2 个 wrong 时准确率剧烈抖动。"""
        if self.judged == 0:
            return None
        return (self.correct + 0.5 * self.neutral) / self.judged


@dataclass
class DayStats:
    date: str
    coins: list[str]
    total_records: int = 0
    total_labeled: int = 0
    overall: Bucket = field(default_factory=Bucket)
    per_state: dict[str, Bucket] = field(default_factory=dict)
    per_coin: dict[str, Bucket] = field(default_factory=dict)
    per_regime: dict[str, Bucket] = field(default_factory=dict)
    per_consensus: dict[str, Bucket] = field(default_factory=dict)
    factor_hits: dict[str, Bucket] = field(default_factory=dict)  # 含该 trigger 的信号桶
    raw_records: list[LabeledRecord] = field(default_factory=list)


def summarize(day_records_per_coin: dict[str, list[LabeledRecord]], date: str) -> DayStats:
    stats = DayStats(date=date, coins=sorted(day_records_per_coin.keys()))
    for coin, recs in day_records_per_coin.items():
        for rec in recs:
            stats.total_records += 1
            stats.overall.add(rec.label)
            stats.per_coin.setdefault(coin, Bucket()).add(rec.label)
            stats.per_state.setdefault(rec.overall_state, Bucket()).add(rec.label)
            stats.per_regime.setdefault(rec.regime, Bucket()).add(rec.label)
            stats.per_consensus.setdefault(rec.consensus, Bucket()).add(rec.label)
            for trig in rec.sub_triggers:
                stats.factor_hits.setdefault(trig, Bucket()).add(rec.label)
            if rec.label in ("correct", "wrong", "neutral"):
                stats.total_labeled += 1
            stats.raw_records.append(rec)
    return stats


# ── Markdown 日报 ─────────────────────────────────────────

def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def _fmt_bucket(b: Bucket) -> str:
    acc = _fmt_pct(b.accuracy)
    soft = _fmt_pct(b.soft_accuracy)
    return (
        f"判 {b.judged} (对 {b.correct} / 错 {b.wrong} / 中性 {b.neutral}) "
        f"· 严格 {acc} / 软 {soft} · 未成熟 {b.pending} · 观望 {b.skip}"
    )


def _render_markdown(stats: DayStats) -> str:
    date = stats.date
    lines: list[str] = []
    lines.append(f"# 趋势衰竭模块 · {date} 准确率日报")
    lines.append("")
    lines.append(f"_生成时间：{datetime.now(_BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')} (北京时间)_")
    lines.append("")
    lines.append("## 概览")
    lines.append("")
    b = stats.overall
    lines.append(f"- 信号总条数：**{stats.total_records}**（已打标 {stats.total_labeled}，未成熟 {b.pending}，观望 {b.skip}）")
    lines.append(f"- **严格准确率**（correct / (correct+wrong)）：**{_fmt_pct(b.accuracy)}**")
    lines.append(f"- **软准确率**（含 0.5×neutral）：**{_fmt_pct(b.soft_accuracy)}**")
    lines.append(f"- correct **{b.correct}** · wrong **{b.wrong}** · neutral **{b.neutral}**")
    lines.append("")

    def _section(title: str, buckets: dict[str, Bucket], min_count: int = 1):
        if not buckets:
            return
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| 分桶 | 判定数 | 严格 | 软 | 对 | 错 | 中性 | 未成熟 | 观望 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for key in sorted(buckets.keys(), key=lambda k: (-buckets[k].total, k)):
            v = buckets[key]
            if v.total < min_count:
                continue
            lines.append(
                f"| `{key}` | {v.judged} | {_fmt_pct(v.accuracy)} | {_fmt_pct(v.soft_accuracy)} |"
                f" {v.correct} | {v.wrong} | {v.neutral} | {v.pending} | {v.skip} |"
            )
        lines.append("")

    _section("按信号状态（overall_state）", stats.per_state)
    _section("按市场状态（regime）", stats.per_regime)
    _section("按 MTF 共识（consensus_level）", stats.per_consensus)
    _section("按币种", stats.per_coin)

    # 因子命中 Top / Bottom
    if stats.factor_hits:
        lines.append("## 因子命中 · Top / Bottom（按判定数 ≥ 3 过滤）")
        lines.append("")
        lines.append("> 含该触发器（如 `1h:rsi_bear_div`）的信号的准确率分布。命中越多越可靠；准确率垫底的因子就是下次调优首要对象。")
        lines.append("")
        lines.append("| 因子 | 命中数 | 严格准确率 | 软准确率 | 对/错/中性 |")
        lines.append("|---|---:|---:|---:|---|")
        filtered = [
            (k, v) for k, v in stats.factor_hits.items() if v.judged >= 3
        ]
        filtered.sort(key=lambda kv: (kv[1].accuracy or 0.0), reverse=True)
        top = filtered[:10]
        bot = filtered[-10:] if len(filtered) > 10 else []
        for k, v in top:
            lines.append(
                f"| `{k}` | {v.judged} | {_fmt_pct(v.accuracy)} | {_fmt_pct(v.soft_accuracy)} | "
                f"{v.correct}/{v.wrong}/{v.neutral} |"
            )
        if bot and bot != top:
            lines.append("|  |  |  |  |  |")
            lines.append("| **— 垫底 —** |  |  |  |  |")
            for k, v in bot:
                lines.append(
                    f"| `{k}` | {v.judged} | {_fmt_pct(v.accuracy)} | {_fmt_pct(v.soft_accuracy)} | "
                    f"{v.correct}/{v.wrong}/{v.neutral} |"
                )
        lines.append("")

    # 严重错判样本（wrong + 反向推进强）
    bad = [r for r in stats.raw_records if r.label == "wrong"]
    bad.sort(key=lambda r: -(abs((r.r_12h or 0) - 0) + abs((r.r_24h or 0) - 0)))
    if bad:
        lines.append("## 严重错判样本（最多 8 条）")
        lines.append("")
        lines.append("| 时间 | 币种 | 状态 | 方向 | regime | 价格 | r_4h | r_12h | r_24h |")
        lines.append("|---|---|---|---|---|---:|---:|---:|---:|")
        for r in bad[:8]:
            tm = datetime.fromtimestamp(r.ts, tz=_BJ_TZ).strftime("%m-%d %H:%M")
            lines.append(
                f"| {tm} | {r.coin} | `{r.overall_state}` | {r.overall_direction} |"
                f" `{r.regime}` | {r.price:.2f} |"
                f" {_fmt_r(r.r_4h)} | {_fmt_r(r.r_12h)} | {_fmt_r(r.r_24h)} |"
            )
        lines.append("")

    # 健康体检结论
    lines.append("## 健康体检 · 关键观察")
    lines.append("")
    insights = _auto_insights(stats)
    if not insights:
        lines.append("- ✅ 未发现明显异常。继续观察 48h+ 以积累置信度。")
    for msg in insights:
        lines.append(f"- {msg}")
    lines.append("")

    # AI Review Prompt
    lines.append("## 🔍 发给 AI 复核用 Prompt（直接复制本节）")
    lines.append("")
    lines.append("```")
    lines.append(_build_ai_prompt(stats))
    lines.append("```")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "**打标规则**：按 ATR14 正则化的 12h/24h 未来收益 r 与预期方向对比。"
        f" correct：同预期 ≥ {_ATR_WEAK:.1f}σ；wrong：反预期 ≥ {_ATR_STRONG:.1f}σ；"
        "neutral：二者之间；skip：regime 否决或 neutral 状态本身不参与。"
    )
    lines.append("")
    return "\n".join(lines)


def _fmt_r(v: Optional[float]) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "—"
    return f"{v:+.2f}σ"


def _auto_insights(stats: DayStats) -> list[str]:
    msgs: list[str] = []
    b = stats.overall
    # 1) 严格准确率基线
    acc = b.accuracy
    if acc is not None:
        if b.correct + b.wrong < 6:
            msgs.append(
                f"⚠️ 样本过少（已判 {b.correct + b.wrong} 条），当前 {_fmt_pct(acc)} 仅供参考，"
                "建议累积 48-72h 再评估。"
            )
        elif acc < 0.5:
            msgs.append(
                f"🚨 严格准确率 {_fmt_pct(acc)} 低于随机（50%），**必须复盘**：检查错判样本的 regime 分布与主导触发器。"
            )
        elif acc < 0.6:
            msgs.append(
                f"🟡 准确率 {_fmt_pct(acc)} 属「勉强过线」，建议先看「垫底因子」表，尝试调权或剥离。"
            )
        else:
            msgs.append(f"✅ 严格准确率 {_fmt_pct(acc)}，高于 60% 基准线。")

    # 2) 某状态显著低于均值
    if acc is not None:
        for key, v in stats.per_state.items():
            if v.correct + v.wrong < 4:
                continue
            local_acc = v.accuracy
            if local_acc is None:
                continue
            if local_acc < acc - 0.2:
                msgs.append(
                    f"⚠️ 状态 `{key}` 准确率 {_fmt_pct(local_acc)}（样本 {v.correct + v.wrong}），"
                    f"比整体低 {int((acc - local_acc) * 100)}pp，是优化重点。"
                )

    # 3) regime 交叉
    if stats.per_regime:
        for key, v in stats.per_regime.items():
            if v.correct + v.wrong < 4:
                continue
            local_acc = v.accuracy
            if local_acc is None:
                continue
            if local_acc < 0.4:
                msgs.append(
                    f"🚨 在 `{key}` 市况下准确率仅 {_fmt_pct(local_acc)}，"
                    "考虑扩大 regime 否决集合或为该市况单独调权。"
                )

    # 4) pending 比例
    if stats.total_records > 0:
        pending_ratio = b.pending / stats.total_records
        if pending_ratio > 0.5:
            msgs.append(
                f"ℹ️ 未成熟（pending）占比 {_fmt_pct(pending_ratio)}，说明数据窗口不够长，"
                "明天 + 的日报将更可靠。"
            )

    return msgs


def _build_ai_prompt(stats: DayStats) -> str:
    """把日报核心数字压缩成 AI 可直接消化的 prompt。"""
    b = stats.overall
    lines = []
    lines.append(f"我是 LIQ 项目 quant，请帮我分析 {stats.date} 的 TrendExhaustion 模块表现：")
    lines.append(f"- 总信号 {stats.total_records}，已判 {b.judged} (对 {b.correct}/错 {b.wrong}/中性 {b.neutral})，"
                 f"严格准确率 {_fmt_pct(b.accuracy)}，软准确率 {_fmt_pct(b.soft_accuracy)}。")
    if stats.per_state:
        parts = []
        for k, v in stats.per_state.items():
            if v.correct + v.wrong > 0:
                parts.append(f"{k}={_fmt_pct(v.accuracy)}({v.correct}/{v.wrong})")
        if parts:
            lines.append("- 各状态严格准确率：" + ", ".join(parts) + "。")
    if stats.per_regime:
        parts = []
        for k, v in stats.per_regime.items():
            if v.correct + v.wrong > 0:
                parts.append(f"{k}={_fmt_pct(v.accuracy)}")
        if parts:
            lines.append("- 各 regime：" + ", ".join(parts) + "。")
    if stats.factor_hits:
        top_bad = sorted(
            [(k, v) for k, v in stats.factor_hits.items() if v.judged >= 3],
            key=lambda kv: (kv[1].accuracy or 0.0),
        )[:3]
        if top_bad:
            bad_str = ", ".join(f"{k}={_fmt_pct(v.accuracy)}" for k, v in top_bad)
            lines.append(f"- 准确率最差的 3 个因子：{bad_str}。")
    lines.append("")
    lines.append("请基于以上数字：")
    lines.append("1. 判断模块是否达到可用线（≥60% 严格准确率），若未达到，指出最可能的 3 个原因；")
    lines.append("2. 针对准确率最差的因子给出调优建议（剥离 / 降权 / 增加确认信号 / 只在某 regime 下启用等）；")
    lines.append("3. 若某 regime 表现明显差，给出是否应把该 regime 加进 veto 名单的理由；")
    lines.append("4. 建议下次评估前需要新增的 2-3 个因子或交叉条件。")
    return "\n".join(lines)


# ── 报告写盘 ────────────────────────────────────────────

def report_root() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "te_eval")


def _daily_report_path(date: str) -> str:
    return os.path.join(report_root(), f"daily_{date}.md")


def evaluate_day(
    date_slug: str,
    coins: Optional[list[str]] = None,
    write: bool = True,
) -> tuple[DayStats, Optional[str]]:
    """对指定日期执行"读 shadow → 打标 → 生成 Markdown"。

    Args:
        date_slug: "YYYY-MM-DD"（北京时间）
        coins: 仅评估这些币种；None 则扫描 shadow 目录。
        write: 是否写文件。False 仅返回 stats（单元测试用）。

    Returns:
        (DayStats, report_path_or_None)
    """
    # 扫描该日期下 jsonl 文件
    day_dir = Path(shadow_log_root()) / date_slug
    if coins is None:
        if day_dir.is_dir():
            coins = sorted([
                p.stem for p in day_dir.glob("*.jsonl") if p.stat().st_size > 0
            ])
        else:
            coins = []

    per_coin_records: dict[str, list[LabeledRecord]] = {}
    for coin in coins:
        try:
            per_coin_records[coin] = label_records_for_day(coin, date_slug)
        except Exception:
            logger.exception("[TE-Eval] label failed for %s %s", coin, date_slug)
            per_coin_records[coin] = []

    stats = summarize(per_coin_records, date_slug)
    if not write:
        return stats, None

    md = _render_markdown(stats)
    os.makedirs(report_root(), exist_ok=True)
    path = _daily_report_path(date_slug)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        logger.info("[TE-Eval] report written: %s (coins=%s records=%d)", path, coins, stats.total_records)
    except Exception:
        logger.exception("[TE-Eval] write report failed: %s", path)
        return stats, None
    return stats, path


def list_reports(max_days: int = 30) -> list[dict]:
    """列出已有日报（日期 + 文件大小 + 修改时间）。"""
    root = Path(report_root())
    if not root.exists():
        return []
    out = []
    for f in sorted(root.glob("daily_*.md"), reverse=True)[:max_days]:
        try:
            stat = f.stat()
            date = f.stem.replace("daily_", "")
            out.append({
                "date": date,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            })
        except Exception:
            continue
    return out


def read_report(date: str) -> Optional[str]:
    path = _daily_report_path(date)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        logger.exception("[TE-Eval] read report failed: %s", path)
        return None


# ── CLI / scheduler 入口 ───────────────────────────────────

def _yesterday_slug() -> str:
    return (datetime.now(_BJ_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="TrendExhaustion 日报生成器")
    parser.add_argument("--date", default=_yesterday_slug(), help="目标日期 YYYY-MM-DD（北京时间，默认昨天）")
    parser.add_argument("--coins", nargs="*", default=None, help="要评估的币种；不填=扫目录")
    parser.add_argument("--dry-run", action="store_true", help="只计算不写文件")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    stats, path = evaluate_day(args.date, coins=args.coins, write=not args.dry_run)
    b = stats.overall
    acc = _fmt_pct(b.accuracy)
    print(
        f"[{args.date}] 信号 {stats.total_records} · 已判 {b.judged} · "
        f"严格 {acc} · 报告 {path or '(dry-run)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
