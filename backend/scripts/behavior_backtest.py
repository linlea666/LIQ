"""V1 vs V2 关键位行为对比回测 · CLI（V3-M3 · 2026-04）

用法：
    python3 -m scripts.behavior_backtest [选项]

选项：
    --coin BTC                  指定币种（默认 BTC）；逗号分隔多币：BTC,ETH
    --window-hours 4            事后真相判定窗口（小时，默认 4）
    --tier S,A                  仅统计指定强度（默认全部）
    --truth-atr 1.0             真相阈值（× ATR，默认 1.0）
    --v2-threshold 0.5          V2 0-1 二分类阈值（默认 0.5）
    --stage-threshold 3         突破阶段二分类阈值（默认 3）
    --format markdown           输出格式：markdown / json（默认 markdown）
    --output -                  输出文件（默认 stdout）
    --history PATH              指定 kl_history.json 路径（默认 backend/data/kl_history.json）

输出：
    - markdown：可读报告，含三维度对比表 + 显著性 + 参数
    - json：    机器可读结果，结构与 run_full_comparison 一致

设计纪律：
    1. 离线工具，不修改任何运行时状态
    2. 默认输出到 stdout，方便管道使用
    3. 失败优雅返回（无数据 / 文件不存在均给出清晰提示）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.key_level import KeyLevelSnapshotV2
from processors.behavior_backtest_engine import run_full_comparison

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "kl_history.json"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 历史快照加载
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_history(
    path: Path, *, coins: Optional[list[str]] = None,
) -> dict[str, list[KeyLevelSnapshotV2]]:
    """从磁盘加载 kl_history.json 并转成 {coin: [snapshots]}.

    跳过解析失败的条目；缺 level_id 的旧样本由下游引擎过滤。
    """
    if not path.exists():
        logger.error("History file not found: %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw: dict = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to read history: %s", e)
        return {}

    out: dict[str, list[KeyLevelSnapshotV2]] = {}
    for ccy, items in raw.items():
        if coins and ccy.upper() not in {c.upper() for c in coins}:
            continue
        snaps: list[KeyLevelSnapshotV2] = []
        for item in items:
            try:
                snaps.append(KeyLevelSnapshotV2(**item))
            except Exception:
                continue
        out[ccy.upper()] = snaps
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 报告渲染
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_dimension_table(stats: dict) -> str:
    """单维度对比表（markdown）。"""
    v1 = stats["v1"]
    v2 = stats["v2"]
    lines = [
        "| 指标 | V1 | V2 | Δ (V2-V1) |",
        "|---|---:|---:|---:|",
        f"| Accuracy | {v1['accuracy']:.4f} | {v2['accuracy']:.4f} | {stats['delta_accuracy']:+.4f} |",
        f"| Precision | {v1['precision']:.4f} | {v2['precision']:.4f} | — |",
        f"| Recall | {v1['recall']:.4f} | {v2['recall']:.4f} | — |",
        f"| F1 | {v1['f1']:.4f} | {v2['f1']:.4f} | {stats['delta_f1']:+.4f} |",
        "",
        f"- 样本数：**{stats['sample_size']}**（剔除 ambiguous {stats['ambiguous_count']} 条）",
        f"- 卡方统计量：χ² = {stats['chi_square_stat']:.4f}，p = {stats['chi_square_p_value']:.4f}",
        f"- V2 显著优于 V1：**{'是 ✅' if stats['is_v2_significantly_better'] else '否'}**"
        f"（判定标准：Δacc ≥ 0.05 且 p < 0.05）",
        "",
        f"- 混淆矩阵 V1：TP={v1['tp']} FP={v1['fp']} TN={v1['tn']} FN={v1['fn']}",
        f"- 混淆矩阵 V2：TP={v2['tp']} FP={v2['fp']} TN={v2['tn']} FN={v2['fn']}",
    ]
    return "\n".join(lines)


def render_markdown(result: dict) -> str:
    """渲染单币种 markdown 报告。"""
    coin = result["coin"]
    p = result["params"]
    tier_str = ",".join(result["tier_filter"]) or "全部"
    out = [
        f"# V1 vs V2 关键位行为回测报告 · {coin}",
        "",
        "## 参数",
        "",
        f"- 事后窗口：**{p['future_window_sec'] // 3600}h**（容差 {p['tolerance_sec']}s）",
        f"- 真相阈值：≥ {p['truth_atr_mult']:.1f}×ATR；模糊带：±{p['ambiguous_band']:.2f}×ATR",
        f"- V2 二分类阈值：{p['v2_threshold']:.2f}",
        f"- 突破阶段阈值：≥ {p['breakout_stage_threshold']}",
        f"- 强度过滤：{tier_str}",
        f"- 总配对样本：**{result['total_records']}** 条",
        "",
        "---",
        "",
        "## 维度 1：反弹质量（V1 proactive/passive vs V2 0-1 连续）",
        "",
        _render_dimension_table(result["stats"]["bounce_quality"]),
        "",
        "## 维度 2：突破阶段（V1 时间窗 vs V2 自适应窗口）",
        "",
        _render_dimension_table(result["stats"]["breakout_stage"]),
        "",
        "## 维度 3：假破回收（V1 布尔事件 vs V2 0-1 连续）",
        "",
        _render_dimension_table(result["stats"]["fake_break"]),
        "",
        "---",
        "",
        "## 决策建议",
        "",
    ]

    # 综合建议
    sigs: list[str] = []
    for dim_key, dim_cn in [
        ("bounce_quality", "反弹质量"),
        ("breakout_stage", "突破阶段"),
        ("fake_break", "假破回收"),
    ]:
        st = result["stats"][dim_key]
        if st["sample_size"] < 30:
            sigs.append(f"- ⏳ **{dim_cn}**：样本不足（n={st['sample_size']}<30），结论不可信")
        elif st["is_v2_significantly_better"]:
            sigs.append(f"- ✅ **{dim_cn}**：V2 显著优于 V1，可考虑切换")
        elif st["delta_accuracy"] < -0.02:
            sigs.append(f"- ❌ **{dim_cn}**：V2 反而劣于 V1（Δacc={st['delta_accuracy']:+.3f}），保留 V1")
        else:
            sigs.append(f"- ➖ **{dim_cn}**：V1/V2 无显著差异（Δacc={st['delta_accuracy']:+.3f}），建议继续观察")
    out.extend(sigs)
    out.append("")
    return "\n".join(out)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI 入口
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="V1 vs V2 关键位行为对比回测 CLI",
    )
    parser.add_argument("--coin", default="BTC", help="逗号分隔，例如 BTC,ETH（默认 BTC）")
    parser.add_argument("--window-hours", type=float, default=4.0, help="事后真相窗口（小时）")
    parser.add_argument("--tolerance-sec", type=int, default=600, help="配对容差（秒）")
    parser.add_argument("--tier", default="", help="逗号分隔；留空=全部")
    parser.add_argument("--truth-atr", type=float, default=1.0, help="真相阈值（×ATR）")
    parser.add_argument("--ambiguous-band", type=float, default=0.3, help="模糊带（×ATR）")
    parser.add_argument("--v2-threshold", type=float, default=0.5, help="V2 0-1 二分类阈值")
    parser.add_argument("--stage-threshold", type=int, default=3, help="突破阶段二分类阈值")
    parser.add_argument(
        "--format", default="markdown", choices=["markdown", "json"], help="输出格式",
    )
    parser.add_argument(
        "--output", default="-",
        help="输出文件路径，'-' 表示 stdout（默认 -）",
    )
    parser.add_argument(
        "--history", default=str(DEFAULT_HISTORY_PATH),
        help=f"kl_history.json 路径（默认 {DEFAULT_HISTORY_PATH}）",
    )
    args = parser.parse_args(argv)

    coins = [c.strip().upper() for c in args.coin.split(",") if c.strip()]
    tier_filter: Optional[list[str]] = None
    if args.tier.strip():
        tier_filter = [t.strip().upper() for t in args.tier.split(",") if t.strip()]

    history_path = Path(args.history).expanduser().resolve()
    history_map = load_history(history_path, coins=coins)
    if not history_map:
        logger.error("无可用历史快照，退出。")
        return 1

    results: list[dict] = []
    for coin, snaps in history_map.items():
        if not snaps:
            logger.warning("[%s] 0 个快照，跳过", coin)
            continue
        logger.info("[%s] 加载 %d 个快照，执行回测...", coin, len(snaps))
        result = run_full_comparison(
            snaps,
            coin=coin,
            future_window_sec=int(args.window_hours * 3600),
            tolerance_sec=args.tolerance_sec,
            truth_atr_mult=args.truth_atr,
            ambiguous_band=args.ambiguous_band,
            v2_threshold=args.v2_threshold,
            breakout_stage_threshold=args.stage_threshold,
            tier_filter=tier_filter,
        )
        results.append(result)

    if not results:
        logger.error("所有币种均无数据，退出。")
        return 1

    if args.format == "json":
        payload = json.dumps({"results": results}, ensure_ascii=False, indent=2)
    else:
        sections = [render_markdown(r) for r in results]
        payload = "\n\n=====\n\n".join(sections)

    if args.output == "-":
        print(payload)
    else:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        logger.info("报告已写入 %s", out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
