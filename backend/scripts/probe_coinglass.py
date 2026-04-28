#!/usr/bin/env python3
"""Coinglass 接口探测脚本（流动性墙引擎调研专用）

独立调研工具 — **不进生产链路、不写 state、不参与轮询**。

用途：
    1. 用真实 API key 拉一次 Coinglass 样本，dump 完整 JSON 到本地
    2. 自动推导字段树（type / nullable / sample / depth）
    3. 汇总每个 endpoint 的响应延迟、数据点数、code/msg

用法：
    cd backend
    # 列出所有可探测 endpoint
    python3 scripts/probe_coinglass.py --list

    # 探测单个 endpoint
    python3 scripts/probe_coinglass.py --endpoint orderbook_history --coin BTC

    # 探测全套（流动性墙引擎相关）
    python3 scripts/probe_coinglass.py --all --coin BTC
    python3 scripts/probe_coinglass.py --all --coins BTC,ETH

    # 自定义参数
    python3 scripts/probe_coinglass.py --endpoint orderbook_history \
        --coin BTC --interval 5m --limit 24

输出：
    backend/scripts/coinglass_probe_samples/{ts}/
        BTC/{endpoint}.json          # 完整 raw 响应
        BTC/{endpoint}.schema.json   # 推导的字段结构
        summary.md                   # 汇总报告

设计约束：
    - 复用 CoinglassSource，但调用前清空内存 cache，确保 fresh
    - 受全局 FixedIntervalLimiter 约束（rate_per_min=10），自动间隔 7s
    - 探测前不读、探测后不写主 api_cache.json
    - 失败的 endpoint 不阻塞其他 endpoint（独立异常处理）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings  # noqa: E402
from sources.coinglass import CoinglassSource, create_coinglass_source  # noqa: E402

logger = logging.getLogger("probe_coinglass")


PROBES: dict[str, dict[str, Any]] = {
    # ── A. 订单簿核心（墙引擎 P0）──
    "orderbook_history": {
        "tag": "P0-墙(已用)",
        "method": "fetch_orderbook_heatmap",
        "default_kwargs": {"interval": "5m", "limit": 12},
        "needs": ("exchange", "symbol_pair"),
        "note": "5m 分价位深度热力图，OP 模块当前 limit=2，建议升级 limit=12",
    },
    "orderbook_aggregated_ask_bids": {
        "tag": "P0-墙(已 poll 但 OP 未用)",
        "method": "fetch_orderbook_aggregated_ask_bids",
        "default_kwargs": {"interval": "5m", "limit": 12, "range_pct": "5"},
        "needs": ("symbol",),
        "note": "多交易所聚合的买卖盘 USD 总额时间序列",
    },
    "orderbook_ask_bids_history_per_exchange": {
        "tag": "未接(单所版)",
        "method": "fetch_orderbook_ask_bids_history",
        "default_kwargs": {"interval": "5m", "limit": 12, "range_pct": "5"},
        "needs": ("exchange", "symbol_pair"),
        "note": "单所版本，聚合版已可用，一般不需要",
    },
    "large_orders_current": {
        "tag": "P0-墙(已用)",
        "method": "fetch_large_orders",
        "default_kwargs": {},
        "needs": ("exchange", "symbol_pair"),
        "note": "当前活跃的大额限价单",
    },
    "large_orders_history": {
        "tag": "P0-墙(已用)",
        "method": "fetch_large_orders_history",
        "default_kwargs": {},
        "needs": ("exchange", "symbol_pair"),
        "note": "大单生命周期历史（含 holding/ended 状态）",
    },

    # ── B. 主动成交 / CVD（行为评估 P0）──
    "aggregated_taker": {
        "tag": "P0-行为(已用)",
        "method": "fetch_aggregated_taker_bs_history",
        "default_kwargs": {"interval": "5m", "limit": 24},
        "needs": ("symbol",),
        "note": "聚合多所主动买卖成交量",
    },
    "aggregated_cvd": {
        "tag": "P0-行为(已用)",
        "method": "fetch_aggregated_cvd_history",
        "default_kwargs": {"interval": "5m", "limit": 100},
        "needs": ("symbol",),
        "note": "聚合 CVD 时间序列",
    },

    # ── C. 持仓拥挤度 ──
    "oi_aggregated_history_5m": {
        "tag": "P0-拥挤(已用)",
        "method": "fetch_oi_aggregated_history",
        "default_kwargs": {"interval": "5m", "limit": 50, "unit": "usd"},
        "needs": ("symbol",),
        "note": "OI 聚合历史（5m 颗粒，用于 oi_delta_5m）",
    },
    "oi_aggregated_history_1h": {
        "tag": "P0-拥挤(已用)",
        "method": "fetch_oi_aggregated_history",
        "default_kwargs": {"interval": "1h", "limit": 24, "unit": "usd"},
        "needs": ("symbol",),
        "note": "OI 聚合历史（1h 颗粒，用于 24h 累计 delta）",
    },
    "oi_exchange_list": {
        "tag": "P0-拥挤(已用)",
        "method": "fetch_oi_exchange_list",
        "default_kwargs": {},
        "needs": ("symbol",),
        "note": "各交易所 OI 排名（市场份额）",
    },
    "funding_oi_weight_history": {
        "tag": "P1-拥挤(已用)",
        "method": "fetch_fr_oi_weight_history",
        "default_kwargs": {"interval": "8h", "limit": 90},
        "needs": ("symbol",),
        "note": "OI 加权 Funding 历史（用于百分位）",
    },
    "global_ls_ratio": {
        "tag": "P1-拥挤(已用)",
        "method": "fetch_global_ls_ratio_history",
        "default_kwargs": {"interval": "1h", "limit": 24},
        "needs": ("exchange", "symbol_pair"),
        "note": "全网账户多空比（需要 exchange+pair）",
    },
    "top_ls_position_ratio": {
        "tag": "P1-拥挤(已用)",
        "method": "fetch_top_ls_position_ratio_history",
        "default_kwargs": {"interval": "1h", "limit": 24},
        "needs": ("exchange", "symbol_pair"),
        "note": "大户持仓多空比（需要 exchange+pair）",
    },
    "net_position_v2": {
        "tag": "P1-拥挤(已用)",
        "method": "fetch_net_position_v2_history",
        "default_kwargs": {"interval": "1h", "limit": 25},
        "needs": ("exchange", "symbol_pair"),
        "note": "净持仓 v2（需要 exchange+pair）",
    },

    # ── D. 清算磁铁（已接，墙引擎复用） ──
    "liquidation_aggregated_map_1d": {
        "tag": "P0-磁铁(已用)",
        "method": "fetch_liquidation_aggregated_map",
        "default_kwargs": {"range_": "1d"},
        "needs": ("symbol",),
        "note": "1d 清算地图（簇） · 使用 raw_response=True",
        "raw_response": True,
    },
    "liquidation_aggregated_heatmap_m1": {
        "tag": "P1-磁铁(已用)",
        "method": "fetch_liquidation_aggregated_heatmap",
        "default_kwargs": {"range_": "24h", "model": 1},
        "needs": ("symbol",),
        "note": "清算热力图 model 1 · 实测 range 必须为 24h/7d/30d",
    },
    "liquidation_aggregated_heatmap_m2": {
        "tag": "未接(GPT 提及)",
        "method": "fetch_liquidation_aggregated_heatmap",
        "default_kwargs": {"range_": "24h", "model": 2},
        "needs": ("symbol",),
        "note": "model 2 数据差异调研",
    },
    "liquidation_aggregated_heatmap_m3": {
        "tag": "未接(GPT 提及)",
        "method": "fetch_liquidation_aggregated_heatmap",
        "default_kwargs": {"range_": "24h", "model": 3},
        "needs": ("symbol",),
        "note": "model 3 数据差异调研",
    },
    "liquidation_max_pain": {
        "tag": "P0-磁铁(已用)",
        "method": "fetch_liquidation_max_pain",
        "default_kwargs": {"range_": "24h"},
        "needs": (),
        "note": "清算 max-pain · 全市场 list，无需 symbol",
    },

    # ── E. Footprint（吸收识别已用） ──
    "footprint_history_5m": {
        "tag": "P1-行为(已用)",
        "method": "fetch_futures_footprint_history",
        "default_kwargs": {"interval": "5m", "limit": 6},
        "needs": ("exchange", "symbol_pair"),
        "note": "合约足迹图（吸收 / stacked imbalance 用）",
    },

    # ── F. 现货（期现共振，部分未接） ──
    "spot_orderbook_history": {
        "tag": "未接(GPT M4 候选)",
        "method": "fetch_spot_orderbook_heatmap",
        "default_kwargs": {"interval": "5m", "limit": 6},
        "needs": ("exchange_spot", "symbol_pair"),
        "note": "现货 L2 深度（期现差异分析）",
    },
    "spot_aggregated_taker": {
        "tag": "P2-期现(已用)",
        "method": "fetch_spot_aggregated_taker_bs",
        "default_kwargs": {"interval": "5m", "limit": 24},
        "needs": ("symbol",),
        "note": "现货聚合主动成交",
    },
    "spot_aggregated_cvd": {
        "tag": "P2-期现(已用)",
        "method": "fetch_spot_aggregated_cvd",
        "default_kwargs": {"interval": "5m", "limit": 100},
        "needs": ("symbol",),
        "note": "现货聚合 CVD",
    },
    "spot_large_orders": {
        "tag": "未接(GPT M4 候选)",
        "method": "fetch_spot_large_orders",
        "default_kwargs": {},
        "needs": ("exchange_spot", "symbol_pair"),
        "note": "现货大单（看现货墙）",
    },

    # ── G. Hyperliquid 鲸鱼仓位 ──
    "hyperliquid_whale_alert": {
        "tag": "P3-鲸鱼(已用)",
        "method": "fetch_hyperliquid_whale_alert",
        "default_kwargs": {},
        "needs": (),
        "note": "Hyperliquid 鲸鱼成交告警",
    },
    "hyperliquid_whale_position": {
        "tag": "P3-鲸鱼(已用)",
        "method": "fetch_hyperliquid_whale_position",
        "default_kwargs": {},
        "needs": (),
        "note": "Hyperliquid 鲸鱼持仓快照",
    },
    "hyperliquid_position_per_coin": {
        "tag": "P3-鲸鱼(未接)",
        "method": "fetch_hyperliquid_position",
        "default_kwargs": {},
        "needs": ("symbol",),
        "note": "Hyperliquid 单币种鲸鱼持仓详情",
    },
}


# ──────────────────────────────────────────────────────────────
# Schema 推导
# ──────────────────────────────────────────────────────────────


def derive_schema(obj: Any, max_depth: int = 6, max_array_samples: int = 2) -> Any:
    """递归推导 JSON 字段结构。"""
    if obj is None:
        return {"type": "null"}
    if isinstance(obj, bool):
        return {"type": "bool", "sample": obj}
    if isinstance(obj, int):
        return {"type": "int", "sample": obj}
    if isinstance(obj, float):
        return {"type": "float", "sample": obj}
    if isinstance(obj, str):
        return {"type": "str", "len": len(obj),
                "sample": obj[:80] + "..." if len(obj) > 80 else obj}
    if isinstance(obj, list):
        if not obj:
            return {"type": "list", "len": 0}
        if max_depth <= 0:
            return {"type": "list", "len": len(obj), "items": "...truncated..."}
        sample_count = min(len(obj), max_array_samples)
        return {
            "type": "list",
            "len": len(obj),
            "items_sampled": sample_count,
            "items_schema": [
                derive_schema(obj[i], max_depth - 1, max_array_samples)
                for i in range(sample_count)
            ],
        }
    if isinstance(obj, dict):
        if max_depth <= 0:
            return {"type": "dict", "keys": list(obj.keys())[:20]}
        return {
            "type": "dict",
            "key_count": len(obj),
            "fields": {
                k: derive_schema(v, max_depth - 1, max_array_samples)
                for k, v in obj.items()
            },
        }
    return {"type": type(obj).__name__, "sample": str(obj)[:80]}


# ──────────────────────────────────────────────────────────────
# 参数解析
# ──────────────────────────────────────────────────────────────


def resolve_kwargs(probe: dict, coin: str, exchange: str, exchange_spot: str,
                   user_overrides: dict) -> dict:
    """根据 probe 配置 + 用户覆盖，组装最终 kwargs。"""
    kwargs = dict(probe["default_kwargs"])
    needs = probe.get("needs", ())

    if "symbol" in needs:
        kwargs["symbol"] = coin
    if "symbol_pair" in needs:
        kwargs["symbol"] = f"{coin}USDT"
    if "exchange" in needs:
        kwargs["exchange"] = exchange
    if "exchange_spot" in needs:
        kwargs["exchange"] = exchange_spot

    for k, v in user_overrides.items():
        if v is not None:
            kwargs[k] = v
    return kwargs


# ──────────────────────────────────────────────────────────────
# 单次 probe
# ──────────────────────────────────────────────────────────────


async def probe_one(cg, name: str, probe: dict, kwargs: dict,
                    out_dir: Path, raw_response_default: bool = False) -> dict:
    """探测单个 endpoint。返回 summary entry。"""
    cg._cache.clear()  # 强制 fresh fetch（不污染主 cache：这是新建实例）

    method_name = probe["method"]
    method = getattr(cg, method_name, None)
    if method is None:
        return {
            "name": name,
            "tag": probe["tag"],
            "ok": False,
            "error": f"method {method_name!r} not found on CoinglassSource",
        }

    started = time.time()
    err: Optional[str] = None
    data: Any = None
    try:
        data = await method(**kwargs)
    except TypeError as e:
        err = f"TypeError: {e}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    elapsed_ms = round((time.time() - started) * 1000, 1)

    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"{name}.json"
    schema_path = out_dir / f"{name}.schema.json"

    summary: dict[str, Any] = {
        "name": name,
        "tag": probe["tag"],
        "method": method_name,
        "kwargs": {k: v for k, v in kwargs.items() if k != "self"},
        "elapsed_ms": elapsed_ms,
        "ok": err is None,
        "note": probe.get("note", ""),
    }

    if err is not None:
        summary["error"] = err
        return summary

    if data is None:
        summary["empty"] = True
        summary["data_len"] = 0
        try:
            raw_path.write_text("null\n")
        except OSError:
            pass
        return summary

    try:
        raw_path.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                       default=str) + "\n")
    except OSError as e:
        summary["dump_error"] = str(e)

    schema = derive_schema(data, max_depth=6, max_array_samples=2)
    try:
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        pass

    if isinstance(data, list):
        summary["data_type"] = "list"
        summary["data_len"] = len(data)
        summary["first_item_type"] = (
            type(data[0]).__name__ if data else "empty"
        )
    elif isinstance(data, dict):
        summary["data_type"] = "dict"
        summary["top_keys"] = list(data.keys())[:20]
    else:
        summary["data_type"] = type(data).__name__

    return summary


# ──────────────────────────────────────────────────────────────
# 汇总报告
# ──────────────────────────────────────────────────────────────


def write_summary(results: list[dict], out_root: Path) -> None:
    md_path = out_root / "summary.md"
    lines: list[str] = []
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines.append(f"# Coinglass Probe Summary · {ts}")
    lines.append("")
    lines.append("> 本报告由 `backend/scripts/probe_coinglass.py` 自动生成。")
    lines.append("> 用途：流动性墙引擎设计调研，不进生产链路。")
    lines.append("")
    lines.append("## 总体")
    total = len(results)
    ok = sum(1 for r in results if r.get("ok"))
    fail = total - ok
    lines.append(f"- 探测条数：{total}")
    lines.append(f"- 成功：{ok}")
    lines.append(f"- 失败：{fail}")
    lines.append("")

    by_tag: dict[str, list[dict]] = {}
    for r in results:
        by_tag.setdefault(r.get("tag", "unknown"), []).append(r)

    for tag in sorted(by_tag.keys()):
        lines.append(f"## {tag}")
        lines.append("")
        lines.append("| coin | endpoint | ok | latency | data | note |")
        lines.append("|------|----------|----|---------|------|------|")
        for r in by_tag[tag]:
            coin = r.get("coin", "-")
            ok_s = "✅" if r.get("ok") else "❌"
            lat = f"{r.get('elapsed_ms', '-')}ms"
            if r.get("empty"):
                data_s = "**空**"
            elif r.get("data_type") == "list":
                data_s = f"list × {r.get('data_len')}"
            elif r.get("data_type") == "dict":
                keys = r.get("top_keys", [])
                data_s = f"dict (keys: {', '.join(keys[:6])}{'...' if len(keys) > 6 else ''})"
            else:
                data_s = r.get("data_type", "?")
            note = r.get("error") or r.get("note", "")
            lines.append(f"| {coin} | `{r['name']}` | {ok_s} | {lat} | {data_s} | {note} |")
        lines.append("")

    md_path.write_text("\n".join(lines) + "\n")
    logger.info("Summary written → %s", md_path)


# ──────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> None:
    if args.rate_per_min and args.rate_per_min > 0:
        cfg = get_settings().coinglass
        api_key = os.getenv(cfg.api_key_env, cfg.api_key_default)
        cg = CoinglassSource(
            base_url=cfg.base_url, api_key=api_key,
            timeout_sec=cfg.timeout_sec, rate_per_min=args.rate_per_min,
        )
        logger.info("Using slow mode rate_per_min=%d", args.rate_per_min)
    else:
        cg = create_coinglass_source()

    out_root = Path(args.out_dir).resolve()
    if not out_root.is_absolute():
        out_root = Path(__file__).resolve().parent / "coinglass_probe_samples" / out_root.name
    ts_dir = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = out_root / ts_dir
    out_root.mkdir(parents=True, exist_ok=True)
    logger.info("Output dir: %s", out_root)

    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    if not coins:
        coins = [args.coin.upper()]

    if args.endpoints:
        target_names = [s.strip() for s in args.endpoints.split(",") if s.strip()]
    elif args.endpoint:
        target_names = [args.endpoint]
    elif args.all:
        target_names = list(PROBES.keys())
    else:
        raise SystemExit("请用 --endpoint / --endpoints / --all 指定探测目标")

    user_overrides: dict[str, Any] = {}
    if args.interval:
        user_overrides["interval"] = args.interval
    if args.limit:
        user_overrides["limit"] = args.limit
    if args.range_pct:
        user_overrides["range_pct"] = args.range_pct
    if args.range_:
        user_overrides["range_"] = args.range_

    results: list[dict] = []
    for coin in coins:
        coin_dir = out_root / coin
        for name in target_names:
            probe = PROBES.get(name)
            if probe is None:
                logger.warning("Unknown endpoint: %s", name)
                continue
            kwargs = resolve_kwargs(probe, coin, args.exchange, args.exchange_spot,
                                    user_overrides)
            logger.info("[%s] %s → %s(%s)", coin, name, probe["method"],
                        ", ".join(f"{k}={v}" for k, v in kwargs.items()))
            try:
                summary = await probe_one(cg, name, probe, kwargs, coin_dir)
            except Exception as e:  # noqa: BLE001
                summary = {
                    "name": name,
                    "tag": probe["tag"],
                    "ok": False,
                    "error": f"unexpected: {type(e).__name__}: {e}",
                }
            summary["coin"] = coin
            results.append(summary)
            logger.info("  → ok=%s, %s",
                        summary.get("ok"),
                        summary.get("error") or
                        f"{summary.get('data_type')} "
                        f"len={summary.get('data_len', '-')}, "
                        f"{summary.get('elapsed_ms', '?')}ms")

    write_summary(results, out_root)

    print()
    print(f"=== Probe done: {len([r for r in results if r.get('ok')])}/{len(results)} OK ===")
    print(f"Report: {out_root / 'summary.md'}")
    print(f"Daily Coinglass requests so far: {cg.daily_request_count}")

    try:
        await cg.close()
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="列出所有可探测的 endpoint")
    p.add_argument("--all", action="store_true", help="探测所有 endpoint")
    p.add_argument("--endpoint", type=str, default=None, help="单个 endpoint 名")
    p.add_argument("--endpoints", type=str, default=None,
                   help="多个 endpoint 名，逗号分隔（覆盖 --endpoint）")
    p.add_argument("--coin", type=str, default="BTC", help="单币种（默认 BTC）")
    p.add_argument("--coins", type=str, default="", help="多币种用逗号分隔（覆盖 --coin）")
    p.add_argument("--exchange", type=str, default="Binance", help="期货交易所（默认 Binance）")
    p.add_argument("--exchange-spot", dest="exchange_spot", type=str, default="Binance",
                   help="现货交易所（默认 Binance）")
    p.add_argument("--interval", type=str, default=None,
                   help="覆盖 default interval（如 5m / 15m / 1h / 8h / 1d）")
    p.add_argument("--limit", type=int, default=None, help="覆盖 default limit")
    p.add_argument("--range-pct", dest="range_pct", type=str, default=None,
                   help="覆盖 range（订单簿用，如 1 / 5）")
    p.add_argument("--range", dest="range_", type=str, default=None,
                   help="覆盖 range（清算用，如 1d / 7d / 30d）")
    p.add_argument("--out-dir", type=str,
                   default=str(Path(__file__).resolve().parent / "coinglass_probe_samples"),
                   help="输出根目录（默认 backend/scripts/coinglass_probe_samples/）")
    p.add_argument("--rate-per-min", dest="rate_per_min", type=int, default=0,
                   help="覆盖默认限流（默认 0=用 settings 的 10）；429 频繁时改 5-6")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    return p


def list_probes() -> None:
    print(f"{'name':<45} {'tag':<28} method")
    print("-" * 100)
    for name, probe in PROBES.items():
        print(f"{name:<45} {probe['tag']:<28} {probe['method']}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    if args.list:
        list_probes()
        return

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
