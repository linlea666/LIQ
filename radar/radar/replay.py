"""Replay 引擎：用历史快照重跑策略。

存在的理由很直接：阈值都是拍脑袋定的初始值。
"把 S1 的机会分从 72 降到 68 会怎样"这个问题，
唯一诚实的回答方式是拿历史数据重跑一遍，而不是凭直觉调。

**复用而非重写**是这个模块最重要的约束。重放走的是与线上完全相同的
注册表、评分器、状态机和产出流水线，只是换一份配置和一个时间源。
如果为了方便另写一套简化版逻辑，回测结论就不再能代表线上行为——
而这种偏差不会报错，只会让人对着一份错的回测结果反复调参。

**三条不可违反的安全边界**：
  1. 绝不写生产库。重放输出进独立的数据库文件。
  2. 绝不发邮件。传输层用空实现，不是"把 enabled 改成 false"——
     后者只要哪天配置传错就会真的发出去。
  3. 绝不读墙上时钟。所有时间来自快照的 observed_at。
     任何一处漏掉，回测算出的代币年龄就会是"当时到今天"，
     年龄分档全部落到最宽松档，结论与线上完全对不上。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .alerts import AlertManager
from .domain.models import TokenObservation
from .notify import EmailRenderer
from .obs.events import EventBus
from .pipeline import EvaluationPipeline
from .registry import TokenRegistry
from .storage import repo
from .storage.db import Database
from .tracker import OutcomeTracker

logger = logging.getLogger("radar.replay")

# 快照列 → 观测字段。两者同名的直接映射，此处只列出需要还原的字段。
# 刻意显式枚举而不是"把整行塞进 TokenObservation"：
# 快照表里有 features_json / opportunity 这类**派生结果**，
# 一旦被当作输入喂回去，回测就变成了"用旧结论算新结论"的自证循环。
_SNAPSHOT_INPUT_COLUMNS: tuple[str, ...] = (
    "price", "market_cap", "fdv", "liquidity",
    "interval_high", "interval_low", "interval_volume",
    "price_high_24h", "price_low_24h",
    "bonding_progress", "migrate_status", "binance_score",
    "holders", "kyc_holders",
    "top10_percent", "dev_percent", "sniper_percent", "insider_percent",
    "bundler_percent", "new_wallet_percent", "smart_money_percent",
    "kol_percent", "pro_percent", "sniper_count", "dev_sell_percent",
    "volume_5m", "volume_1h", "volume_4h", "volume_24h",
    "volume_1h_buy", "volume_1h_sell",
    "count_5m", "count_1h", "count_1h_buy", "count_1h_sell",
    "unique_trader_5m", "unique_trader_1h", "unique_trader_24h",
    "pct_change_5m", "pct_change_1h", "pct_change_4h", "pct_change_24h",
    "volume_agg", "count_agg", "count_agg_buy", "count_agg_sell", "pct_change_agg",
    "smart_money_count", "smart_money_traders", "exit_rate", "max_gain",
    "alert_market_cap", "net_inflow", "signal_direction", "signal_type",
    "signal_status",
    "social_hype", "social_hype_cn", "social_hype_en", "kol_count",
    "search_count_24h", "sentiment", "twitter_followers",
    "audit_risk_level", "buy_tax_pct", "sell_tax_pct", "contract_verified",
)

_INT_COLUMNS = frozenset({
    "holders", "kyc_holders", "sniper_count", "count_5m", "count_1h",
    "count_1h_buy", "count_1h_sell", "unique_trader_5m", "unique_trader_1h",
    "unique_trader_24h", "count_agg", "count_agg_buy", "count_agg_sell",
    "smart_money_count", "smart_money_traders", "kol_count", "search_count_24h",
    "twitter_followers", "audit_risk_level", "migrate_status",
})


class NullTransport:
    """重放期间的邮件传输：什么都不做，且记录被拦下的数量。

    刻意用独立实现而不是"把 email.enabled 设成 false"：
    配置项可能被传错、被覆盖、被遗忘，而一个物理上不会连 SMTP 的对象
    在任何配置下都发不出邮件。回测误发几百封邮件是不可接受的。
    """

    def __init__(self) -> None:
        self.blocked = 0

    async def send(self, *, subject: str, html: str) -> None:
        self.blocked += 1


@dataclass
class ReplayReport:
    source_db: str
    output_db: str
    start_ms: int
    end_ms: int
    snapshots_read: int = 0
    tokens: int = 0
    evaluations: int = 0
    alerts: int = 0
    near_miss: int = 0
    alerts_by_kind: dict[str, int] = field(default_factory=dict)
    baseline_alerts: int = 0
    baseline_by_kind: dict[str, int] = field(default_factory=dict)
    downsampled_ratio: float = 0.0
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_db": self.source_db,
            "output_db": self.output_db,
            "window": {
                "start": _iso(self.start_ms),
                "end": _iso(self.end_ms),
            },
            "snapshots_read": self.snapshots_read,
            "tokens": self.tokens,
            "evaluations": self.evaluations,
            "alerts": self.alerts,
            "near_miss": self.near_miss,
            "alerts_by_kind": self.alerts_by_kind,
            "baseline_alerts": self.baseline_alerts,
            "baseline_by_kind": self.baseline_by_kind,
            "downsampled_ratio": round(self.downsampled_ratio, 4),
            "warnings": self.warnings,
            "duration_ms": self.duration_ms,
        }


class ReplayEngine:
    def __init__(self, *, source_db: Path, output_db: Path,
                 config: Mapping[str, Any], fingerprint: Mapping[str, str]) -> None:
        self._source_path = source_db
        self._output_path = output_db
        self._config = dict(config)
        self._fingerprint = dict(fingerprint)

    async def run(self, *, start_ms: int, end_ms: int,
                  batch_size: int = 2000) -> ReplayReport:
        report = ReplayReport(
            source_db=str(self._source_path), output_db=str(self._output_path),
            start_ms=start_ms, end_ms=end_ms,
        )
        started = _monotonic_ms()

        source = Database(self._source_path, read_only=True)
        await source.start()
        output = Database(self._output_path)
        await output.start()

        try:
            await self._check_fidelity(source, report, start_ms, end_ms)
            await self._replay(source, output, report, start_ms, end_ms, batch_size)
            await self._collect_baseline(source, report, start_ms, end_ms)
            await output.drain()
        finally:
            await source.stop()
            await output.stop()

        report.duration_ms = _monotonic_ms() - started
        return report

    # ═════════════════════════════════════════════════════════════════════
    # 保真度检查
    # ═════════════════════════════════════════════════════════════════════

    async def _check_fidelity(self, source: Database, report: ReplayReport,
                              start_ms: int, end_ms: int) -> None:
        """在跑之前先说清楚这次回测有多可信。

        快照在 48 小时后会被抽稀，抽稀后的数据里看不出短时暴涨暴跌。
        如果不主动告知，使用者会把一份基于稀疏数据的结论当成完整回测——
        这比不做回测更危险，因为它带着数字的权威感。
        """
        row = await source.fetch_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN keep_forever=1 THEN 1 ELSE 0 END) AS kept "
            "FROM snapshots WHERE observed_at >= ? AND observed_at < ?",
            (start_ms, end_ms),
        )
        total = int(row["total"] or 0) if row else 0
        if total == 0:
            report.warnings.append("窗口内没有任何快照，回测结果无意义")
            return

        downsample_after_ms = int(
            float((self._config.get("storage", {}) or {})
                  .get("downsample_after_hours", 48)) * 3_600_000
        )
        latest = await source.fetch_one("SELECT MAX(observed_at) AS m FROM snapshots")
        newest = int(latest["m"] or end_ms) if latest else end_ms
        if newest - start_ms > downsample_after_ms:
            affected = min(1.0, (newest - start_ms - downsample_after_ms)
                           / max(1, end_ms - start_ms))
            report.downsampled_ratio = affected
            report.warnings.append(
                f"窗口约 {affected:.0%} 落在降采样区间（超过 "
                f"{downsample_after_ms // 3_600_000} 小时），"
                "短时极值可能已被抽稀，MFE/ATH 类结论会偏保守"
            )

    # ═════════════════════════════════════════════════════════════════════
    # 重放主循环
    # ═════════════════════════════════════════════════════════════════════

    async def _replay(self, source: Database, output: Database,
                      report: ReplayReport, start_ms: int, end_ms: int,
                      batch_size: int) -> None:
        events = EventBus()
        events.configure_fingerprint(self._fingerprint)
        events.set_sink(repo.make_event_sink(output))

        registry = TokenRegistry(db=output, events=events, config=self._config,
                                 fingerprint=self._fingerprint)
        alerts = AlertManager(
            db=output, config=self._config, fingerprint=self._fingerprint,
            renderer=EmailRenderer(fingerprint=self._fingerprint),
        )
        tracker = OutcomeTracker(db=output, config=self._config,
                                 fingerprint=self._fingerprint)
        pipeline = EvaluationPipeline(alerts=alerts, tracker=tracker)

        last_id = 0
        while True:
            rows = await source.fetch_all(
                "SELECT s.*, t.chain_id, t.contract_address, t.symbol, t.name, "
                "t.decimals, t.launch_time_ms, t.creator_address, t.launch_platform, "
                "t.circulating_supply, t.total_supply, t.max_supply "
                "FROM snapshots s JOIN token_master t ON t.token_id = s.token_id "
                "WHERE s.observed_at >= ? AND s.observed_at < ? AND s.snapshot_id > ? "
                "ORDER BY s.observed_at ASC, s.snapshot_id ASC LIMIT ?",
                (start_ms, end_ms, last_id, batch_size),
            )
            if not rows:
                break

            for row in rows:
                last_id = max(last_id, int(row["snapshot_id"]))
                observation = _observation_from_snapshot(row)
                if observation is None:
                    continue
                report.snapshots_read += 1

                views = await registry.ingest([observation])
                if not views:
                    continue
                view = views[0]
                # 时间基准严格取快照的 observed_at：
                # 这里只要漏用一次墙上时钟，代币年龄就会变成"当时到今天"
                evaluation = await registry.evaluate(
                    view, observation.observed_at, endpoint="replay"
                )
                report.evaluations += 1
                await pipeline.process(evaluation)

            # 每批之间让出事件循环，避免长回测把写队列堵死
            await asyncio.sleep(0)

        tracker.sweep(end_ms)
        await output.drain()

        report.tokens = len(registry)
        report.alerts = alerts.stats.created
        report.near_miss = alerts.stats.near_miss
        report.alerts_by_kind = await _count_alerts(output, near_miss=False)

    # ═════════════════════════════════════════════════════════════════════
    # 与线上结果对照
    # ═════════════════════════════════════════════════════════════════════

    async def _collect_baseline(self, source: Database, report: ReplayReport,
                                start_ms: int, end_ms: int) -> None:
        """统计同一窗口内线上真实产生的警报，作为对照组。

        没有对照组的回测只能回答"新阈值会报多少个"，
        回答不了"比现在多了还是少了、多出来的是什么"——
        而后者才是调阈值时真正要看的东西。
        """
        rows = await source.fetch_all(
            "SELECT alert_kind, COUNT(*) AS n FROM alerts "
            "WHERE is_near_miss=0 AND created_at >= ? AND created_at < ? "
            "GROUP BY alert_kind",
            (start_ms, end_ms),
        )
        report.baseline_by_kind = {str(r["alert_kind"]): int(r["n"]) for r in rows}
        report.baseline_alerts = sum(report.baseline_by_kind.values())


async def _count_alerts(db: Database, *, near_miss: bool) -> dict[str, int]:
    rows = await db.fetch_all(
        "SELECT alert_kind, COUNT(*) AS n FROM alerts WHERE is_near_miss=? "
        "GROUP BY alert_kind",
        (1 if near_miss else 0,),
    )
    return {str(r["alert_kind"]): int(r["n"]) for r in rows}


# ═════════════════════════════════════════════════════════════════════════
# 快照 → 观测
# ═════════════════════════════════════════════════════════════════════════

def _observation_from_snapshot(row: Mapping[str, Any]) -> TokenObservation | None:
    chain_id = row["chain_id"]
    contract = row["contract_address"]
    if not chain_id or not contract:
        return None

    observation = TokenObservation(
        chain_id=str(chain_id),
        contract_address=str(contract),
        endpoint=str(row["endpoint"] or "replay"),
        observed_at=int(row["observed_at"]),
        source_at=_int(row["source_at"]),
        parser_version=str(row["parser_version"] or ""),
        symbol=row["symbol"],
        name=row["name"],
        decimals=_int(row["decimals"]),
        launch_time_ms=_int(row["launch_time_ms"]),
        creator_address=row["creator_address"],
        launch_platform=row["launch_platform"],
        circulating_supply=_float(row["circulating_supply"]),
        total_supply=_float(row["total_supply"]),
        max_supply=_float(row["max_supply"]),
    )

    keys = set(row.keys())
    for column in _SNAPSHOT_INPUT_COLUMNS:
        if column not in keys:
            continue
        value = row[column]
        if value is None:
            continue
        if column in _INT_COLUMNS:
            parsed: Any = _int(value)
        elif isinstance(value, (int, float)):
            parsed = _float(value)
        else:
            parsed = value
        if parsed is not None:
            setattr(observation, column, parsed)

    # audit_available 在快照里是 0/1，还原成布尔。
    # 它必须区分 False（币安明确说没有审计结果）与 None（我们没查过）——
    # 混淆这两者会让风险门在回测里放行一批线上被拦下的币
    audit_available = row["audit_available"] if "audit_available" in keys else None
    if audit_available is not None:
        observation.audit_available = bool(audit_available)

    return observation


# ═════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════

def _load_config(path: Path) -> tuple[dict[str, Any], str]:
    import hashlib

    raw_bytes = path.read_bytes()
    config = yaml.safe_load(raw_bytes) or {}
    return config, hashlib.sha256(raw_bytes).hexdigest()[:16]


def _parse_time(value: str) -> int:
    """接受 ISO 日期、毫秒时间戳，或 "7d" 这样的相对时长。"""
    text = value.strip()
    if text.endswith(("d", "h")) and text[:-1].replace(".", "").isdigit():
        amount = float(text[:-1])
        delta_ms = int(amount * (86_400_000 if text.endswith("d") else 3_600_000))
        return int(datetime.now(timezone.utc).timestamp() * 1000) - delta_ms
    if text.isdigit():
        return int(text)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return int(parsed.timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar-replay",
        description="用历史快照重跑策略，对比不同阈值下的产出",
    )
    parser.add_argument("--db", type=Path, required=True, help="源数据库（只读）")
    parser.add_argument("--config", type=Path, required=True,
                        help="用于回测的配置文件（可与线上不同）")
    parser.add_argument("--out", type=Path, required=True,
                        help="回测输出数据库路径（不得指向源库）")
    parser.add_argument("--start", default="7d",
                        help="起始时间：ISO 日期 / 毫秒时间戳 / 相对时长如 7d")
    parser.add_argument("--end", default=None, help="结束时间，默认现在")
    parser.add_argument("--batch", type=int, default=2000, help="每批读取的快照数")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    source = args.db.resolve()
    output = args.out.resolve()
    if source == output:
        print("拒绝执行：输出库不能是源库，回测绝不能写生产数据", file=sys.stderr)
        return 2
    if not source.exists():
        print(f"源数据库不存在: {source}", file=sys.stderr)
        return 2
    if output.exists():
        # 复用旧输出库会把两次回测的警报混在一起，而且不会有任何提示
        print(f"拒绝执行：输出库已存在，请换个路径或先删除: {output}", file=sys.stderr)
        return 2

    config, config_hash = _load_config(args.config)
    scoring = config.get("scoring", {}) or {}
    fingerprint = {
        "strategy_version": str(scoring.get("strategy_version", "replay")),
        "feature_version": str(scoring.get("feature_version", "replay")),
        "parser_version": "replay",
        "config_hash": config_hash,
        "code_commit": "replay",
    }

    start_ms = _parse_time(args.start)
    end_ms = _parse_time(args.end) if args.end else int(
        datetime.now(timezone.utc).timestamp() * 1000
    )
    if end_ms <= start_ms:
        print("拒绝执行：结束时间必须晚于起始时间", file=sys.stderr)
        return 2

    engine = ReplayEngine(source_db=source, output_db=output,
                          config=config, fingerprint=fingerprint)
    report = await engine.run(start_ms=start_ms, end_ms=end_ms,
                              batch_size=args.batch)

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0


def _print_report(report: ReplayReport) -> None:
    print(f"回测窗口  {_iso(report.start_ms)} → {_iso(report.end_ms)}")
    print(f"读取快照  {report.snapshots_read}（代币 {report.tokens}，"
          f"评估 {report.evaluations} 次）")
    print(f"耗时      {report.duration_ms / 1000:.1f}s")
    print()
    print(f"回测警报  {report.alerts}  {_fmt_kinds(report.alerts_by_kind)}")
    print(f"线上实际  {report.baseline_alerts}  {_fmt_kinds(report.baseline_by_kind)}")
    delta = report.alerts - report.baseline_alerts
    print(f"差异      {delta:+d}")
    print(f"Near-Miss {report.near_miss}")
    if report.warnings:
        print()
        for warning in report.warnings:
            print(f"[注意] {warning}")
    print()
    print(f"结果库    {report.output_db}")


def _fmt_kinds(counts: Mapping[str, int]) -> str:
    if not counts:
        return ""
    return "(" + " ".join(f"{k}:{v}" for k, v in sorted(counts.items())) + ")"


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return asyncio.run(_run_cli(args))


# ═════════════════════════════════════════════════════════════════════════
# 工具
# ═════════════════════════════════════════════════════════════════════════

def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(at_ms: int) -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(at_ms / 1000, tz).strftime("%Y-%m-%d %H:%M")


def _monotonic_ms() -> int:
    import time

    return int(time.perf_counter() * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
