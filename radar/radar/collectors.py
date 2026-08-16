"""数据采集器。

采集策略的核心是**用列表接口做被动批量更新**，而不是给每枚币单独轮询。

一次 trending 请求返回 50 枚币的完整数据；如果改成给 50 枚币各发一次详情请求，
成本是 50 倍而信息量几乎相同。因此：

  - 列表接口（trending / meme_rush / meme_rank / inflow / signal / social）
    以固定频率轮询，一次刷新几十上百枚币——这是数据的主要来源。
  - 详情接口只给"值得单独花配额"的币：S0 以上、警报爆发窗口内的币，
    以及少量按抽样比例追踪的观察池与拒绝样本。
  - 审计接口最省着用：只在晋升交易型状态前补查，且流动性太薄的币不查。

三个容易被忽略但必须处理的问题：

1. **冷启动涌入**。首次启动时列表里全是没见过的币，几百个新币同时建档
   会瞬间打爆写队列和请求预算。因此新币入库有每分钟上限，
   超出的部分直接丢弃——列表下一轮还会再返回它们，不会真的错过。

2. **一轮一次评估**。同一枚币可能同时出现在 trending 和 inflow 里。
   必须等本轮所有观测合并完再评估一次，否则会用只有一半字段的视图打分，
   产生虚假的状态抖动。

3. **单个接口失败不能中断整轮**。某个接口挂掉时，其余接口的数据照常入库，
   只是相关字段组会因为过期而自动降低 DataQuality——
   这正是分组新鲜度机制存在的意义。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .domain.models import TokenObservation, TokenState, TokenView
from .obs.events import EventType, Severity, bus
from .obs.logging_setup import CorrelationScope, now_ms
from .obs.metrics import metrics
from .registry import Evaluation, TokenRegistry
from .scheduler import RequestScheduler
from .sources import endpoints as ep
from .sources import parsers
from .sources.client import BinanceClient, FetchError, FetchResult
from .storage import repo
from .storage.db import Database

logger = logging.getLogger("radar.collectors")

# 各状态对应的调度层。决定刷新频率与配额归属。
_STATE_TIER: dict[TokenState, str] = {
    TokenState.S2: "s2",
    TokenState.MOMENTUM: "s2",
    TokenState.S1: "s1",
    TokenState.DISTRIBUTION: "s1",
    TokenState.S0: "s0",
    TokenState.WATCHING: "watching",
    TokenState.DISCOVERED: "watching",
}


@dataclass
class ListTask:
    """一次列表采集任务。"""

    endpoint: ep.Endpoint
    chain_id: str
    params: dict[str, Any] | None = None
    body: dict[str, Any] | None = None
    stage: int | None = None
    label: str = ""

    def describe(self) -> str:
        return self.label or f"{self.endpoint.name}:{self.chain_id}"


@dataclass
class CycleReport:
    """一轮采集的结果摘要，用于事件与诊断。"""

    started_at: int
    correlation_id: str = ""
    tasks: int = 0
    succeeded: int = 0
    failed: int = 0
    observations: int = 0
    tokens_touched: int = 0
    new_tokens: int = 0
    throttled_new: int = 0
    evicted: int = 0
    evaluations: int = 0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)


class OnboardingThrottle:
    """新币入库限速。

    冷启动时列表里几乎全是没见过的币。若不限速，一轮就会产生几百次
    建档写入 + 几百次评估，把写队列和 CPU 同时打满，
    而这些币里绝大多数根本不值得追踪。

    被丢弃的观测不会真的丢失信息：列表接口每分钟都会再返回它们，
    只是入库时间推后。而"最值得先收的币"用流动性排序近似——
    这个字段所有列表接口都有，且与后续能否通过风险门高度相关。
    """

    def __init__(self, max_per_min: int) -> None:
        self._max_per_min = max(1, max_per_min)
        self._admitted: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        while self._admitted and self._admitted[0] < cutoff:
            self._admitted.popleft()

    def capacity(self) -> int:
        now = time.monotonic()
        self._prune(now)
        return max(0, self._max_per_min - len(self._admitted))

    def admit(self, count: int) -> int:
        """申请配额，返回实际获批数量。"""
        available = min(count, self.capacity())
        now = time.monotonic()
        for _ in range(available):
            self._admitted.append(now)
        return available


class CollectorService:
    """采集编排：发现循环 + 详情刷新循环 + 审计补查。"""

    def __init__(
        self,
        *,
        client: BinanceClient,
        scheduler: RequestScheduler,
        registry: TokenRegistry,
        db: Database,
        settings: Any,
        on_evaluation: Callable[[Evaluation], Any] | None = None,
    ) -> None:
        self._client = client
        self._scheduler = scheduler
        self._registry = registry
        self._db = db
        self._settings = settings
        self._on_evaluation = on_evaluation

        cfg = settings.collectors
        self._page_size = int(cfg.get("list_page_size", 50))
        self._rush_stages: list[int] = list(cfg.get("meme_rush_stages", [10, 20, 30]))
        self._trending_period = int(cfg.get("trending_period", 30))
        self._inflow_period = str(cfg.get("inflow_period", "1h"))
        self._social_language = str(cfg.get("social_language", "zh-CN"))
        self._extract_chart = bool(cfg.get("extract_chart_extremes", True))

        sched_cfg = settings.scheduler
        self._onboarding = OnboardingThrottle(
            int(sched_cfg.get("onboarding_max_per_min", 90))
        )

        storage_cfg = settings.storage
        self._raw_list_ttl_ms = int(
            float(storage_cfg.get("raw_list_retention_hours", 72)) * 3_600_000
        )
        self._raw_detail_ttl_ms = int(
            float(storage_cfg.get("raw_detail_retention_days", 400)) * 86_400_000
        )
        self._reject_sample_ratio = float(storage_cfg.get("reject_sample_ratio", 0.08))

        self._chains: list[str] = [c.id for c in settings.chains if c.enabled]
        # 每枚币下一次可以做详情刷新的时刻
        self._next_refresh: dict[tuple[str, str], int] = {}
        # 待补查审计的币（有序去重，先进先出）
        self._audit_queue: deque[tuple[str, str]] = deque()
        self._audit_pending: set[tuple[str, str]] = set()

        self._running = False
        self._tasks: list[asyncio.Task[None]] = []
        self.last_cycle: CycleReport | None = None

    # ═════════════════════════════════════════════════════════════════════
    # 生命周期
    # ═════════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._social_loop(), name="social"),
            asyncio.create_task(self._refresh_loop(), name="refresh"),
            asyncio.create_task(self._audit_loop(), name="audit"),
        ]

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        # 等待所有循环真正退出，避免停机后仍有请求在途导致连接池报错
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # ═════════════════════════════════════════════════════════════════════
    # 发现循环
    # ═════════════════════════════════════════════════════════════════════

    async def _discovery_loop(self) -> None:
        # 启动时立刻跑一轮，不等第一个间隔——否则重启后前 60 秒完全没有数据
        while self._running:
            try:
                await self.run_discovery_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("发现循环异常，将在下个周期重试")
            await self._sleep(self._scheduler.interval_with_jitter("discovery"))

    async def run_discovery_cycle(self) -> CycleReport:
        """跑一轮完整的列表采集。可被 CLI / 测试直接调用。"""
        report = CycleReport(started_at=now_ms())
        started = time.perf_counter()

        # 整轮共用一个链路 ID：事后要把"这条 S1 警报"回溯到
        # "是哪一轮采集、哪个接口的哪次响应触发的"，靠的就是它
        with CorrelationScope("scan") as correlation_id:
            report.correlation_id = correlation_id
            tasks = self._build_discovery_tasks()
            report.tasks = len(tasks)
            observations = await self._run_list_tasks(tasks, report)
            await self._ingest_and_evaluate(observations, report)

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        self.last_cycle = report
        metrics.gauge("discovery_cycle_ms", report.duration_ms)

        if report.failed:
            logger.warning(
                "发现循环完成 | 任务 %d 成功 %d 失败 %d | 观测 %d | 代币 %d | %dms",
                report.tasks, report.succeeded, report.failed,
                report.observations, report.tokens_touched, report.duration_ms,
            )
        else:
            logger.info(
                "发现循环完成 | 观测 %d | 触及代币 %d | 新增 %d | %dms",
                report.observations, report.tokens_touched,
                report.new_tokens, report.duration_ms,
            )
        return report

    def _build_discovery_tasks(self) -> list[ListTask]:
        tasks: list[ListTask] = []
        for chain_id in self._chains:
            tasks.append(ListTask(
                endpoint=ep.EP_TRENDING, chain_id=chain_id,
                body=ep.trending_body(chain_id, period=self._trending_period,
                                      limit=self._page_size),
            ))
            for stage in self._rush_stages:
                tasks.append(ListTask(
                    endpoint=ep.EP_MEME_RUSH, chain_id=chain_id, stage=stage,
                    body=ep.meme_rush_body(chain_id, stage, limit=20),
                    label=f"meme_rush:{ep.STAGE_NAMES.get(stage, stage)}:{chain_id}",
                ))
            if ep.EP_MEME_RANK.supports(chain_id):
                tasks.append(ListTask(
                    endpoint=ep.EP_MEME_RANK, chain_id=chain_id,
                    params=ep.meme_rank_params(chain_id, limit=self._page_size),
                ))
            tasks.append(ListTask(
                endpoint=ep.EP_INFLOW, chain_id=chain_id,
                body=ep.inflow_body(chain_id, period=self._inflow_period,
                                    limit=self._page_size),
            ))
            tasks.append(ListTask(
                endpoint=ep.EP_SIGNAL, chain_id=chain_id,
                body=ep.signal_body(chain_id, limit=self._page_size),
            ))
        return tasks

    async def _social_loop(self) -> None:
        """社交榜单独一个循环。

        它的更新频率远低于其他榜（社交热度本身是慢变量），
        且 Meme 币覆盖率很低。混在发现循环里会白白占用高优先级配额。
        """
        while self._running:
            await self._sleep(self._scheduler.interval_with_jitter("social"))
            if not self._running:
                return
            try:
                report = CycleReport(started_at=now_ms())
                with CorrelationScope("social"):
                    tasks = [
                        ListTask(
                            endpoint=ep.EP_SOCIAL, chain_id=chain_id,
                            params=ep.social_params(
                                chain_id, language=self._social_language, limit=30
                            ),
                        )
                        for chain_id in self._chains
                    ]
                    report.tasks = len(tasks)
                    observations = await self._run_list_tasks(tasks, report)
                    # 社交数据只做合并，不单独触发评估：
                    # 它变化很慢，没必要为它多跑一遍全量评分
                    await self._registry.ingest(observations)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("社交循环异常")

    async def _run_list_tasks(self, tasks: Sequence[ListTask],
                              report: CycleReport) -> list[TokenObservation]:
        """并发执行列表任务。

        单个任务失败只影响它自己：其余接口的数据照常入库，
        相关字段组会因过期自动降低 DataQuality。
        整轮中断反而是更坏的结果——那会让所有维度同时失去更新。
        """
        results = await asyncio.gather(
            *(self._run_list_task(task) for task in tasks),
            return_exceptions=True,
        )
        observations: list[TokenObservation] = []
        for task, result in zip(tasks, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                report.failed += 1
                report.errors.append(f"{task.describe()}: {result}")
                continue
            report.succeeded += 1
            observations.extend(result)
        report.observations = len(observations)
        return observations

    async def _run_list_task(self, task: ListTask) -> list[TokenObservation]:
        result = await self._client.fetch(
            task.endpoint,
            chain_id=task.chain_id,
            params=task.params,
            body=task.body,
            tier=task.endpoint.tier,
            # 列表任务不值得为配额等太久：等超过一个采集周期就说明
            # 预算已经紧张到该跳过本轮，而不是把整轮拖长
            budget_timeout_sec=45.0,
        )
        self._archive(result, kind="list", retention_class="short")
        return self._parse_list(task, result)

    def _parse_list(self, task: ListTask, result: FetchResult) -> list[TokenObservation]:
        rows = parsers.extract_rows(task.endpoint.name, result.data)
        if not rows:
            return []

        self._check_schema_drift(task, rows)

        observations: list[TokenObservation] = []
        for row in rows:
            try:
                obs = self._parse_row(task, row, result.observed_at)
            except Exception:  # noqa: BLE001
                # 单行解析失败绝不能拖垮整批：币安偶尔会返回结构异常的条目
                logger.debug("解析行失败 | %s", task.describe(), exc_info=True)
                continue
            if obs is None:
                continue
            obs.latency_ms = result.latency_ms
            obs.response_hash = result.response_hash
            observations.append(obs)
        return observations

    def _parse_row(self, task: ListTask, row: dict[str, Any],
                   observed_at: int) -> TokenObservation | None:
        name = task.endpoint.name
        chain_id = task.chain_id
        if name == "trending":
            obs = parsers.parse_trending_row(chain_id, row, observed_at)
            if obs is not None and not self._extract_chart:
                # 关闭区间极值提取时明确清空，而不是让解析器分支——
                # 保证无论开关如何，解析器行为都只有一种，fixtures 才有意义
                obs.interval_high = obs.interval_low = obs.interval_volume = None
            return obs
        if name == "meme_rush":
            return parsers.parse_meme_rush_row(
                chain_id, row, observed_at, stage=task.stage or 0
            )
        if name == "meme_rank":
            return parsers.parse_meme_rank_row(chain_id, row, observed_at)
        if name == "inflow":
            return parsers.parse_inflow_row(
                chain_id, row, observed_at, period=self._inflow_period
            )
        if name == "signal":
            return parsers.parse_signal_row(chain_id, row, observed_at)
        if name == "social":
            return parsers.parse_social_row(chain_id, row, observed_at)
        return None

    def _check_schema_drift(self, task: ListTask, rows: list[dict[str, Any]]) -> None:
        missing = parsers.detect_missing_keys(task.endpoint.name, rows)
        if not missing:
            return
        # 接口改版是这个系统最危险的静默失效来源：字段一改，
        # 解析器安静地返回 None，评分照常输出，只是全部基于空数据。
        # 因此必须显式告警，且走错误聚合避免刷屏。
        should_emit, occurrences, _ = bus.errors.record_failure(
            f"drift:{task.endpoint.name}"
        )
        if should_emit:
            bus.emit(
                EventType.API_SCHEMA_CHANGED,
                module="collector",
                chain_id=task.chain_id,
                summary=f"{task.endpoint.name} 缺失预期字段: {', '.join(missing[:6])}",
                payload={
                    "endpoint": task.endpoint.name,
                    "missing_keys": list(missing),
                    "occurrences": occurrences,
                },
            )

    # ═════════════════════════════════════════════════════════════════════
    # 合并 + 评估
    # ═════════════════════════════════════════════════════════════════════

    async def _ingest_and_evaluate(self, observations: list[TokenObservation],
                                   report: CycleReport) -> None:
        admitted = self._apply_onboarding_throttle(observations, report)
        if not admitted:
            return

        touched = await self._registry.ingest(admitted)
        # 合并过程中可能触发内存淘汰。被淘汰的币已经决定不再追踪，
        # 继续为它们评估并写快照，等于在内存最紧张的时刻做最无用的写入
        views = [v for v in touched if self._registry.get(*v.key) is v]
        report.tokens_touched = len(views)
        report.evicted = len(touched) - len(views)

        # 同一枚币本轮可能来自多个接口，此处所有观测已合并完毕，
        # 因此每枚币只评估一次，用的是字段最完整的视图
        evaluated_at = now_ms()
        for view in views:
            try:
                evaluation = await self._registry.evaluate(
                    view, evaluated_at, endpoint="list_merge"
                )
            except Exception:  # noqa: BLE001
                logger.exception("评估失败 | %s", view.contract_address[:12])
                continue
            report.evaluations += 1
            self._post_evaluation(evaluation)
            if self._on_evaluation is not None:
                maybe = self._on_evaluation(evaluation)
                if asyncio.iscoroutine(maybe):
                    await maybe

    def _apply_onboarding_throttle(self, observations: list[TokenObservation],
                                   report: CycleReport) -> list[TokenObservation]:
        """限制本轮新建档的代币数量。"""
        known: list[TokenObservation] = []
        unknown: dict[tuple[str, str], list[TokenObservation]] = {}
        for obs in observations:
            if self._registry.get(obs.chain_id, obs.contract_address) is not None:
                known.append(obs)
            else:
                unknown.setdefault(obs.key, []).append(obs)

        if not unknown:
            return known

        capacity = self._onboarding.capacity()
        if capacity >= len(unknown):
            self._onboarding.admit(len(unknown))
            report.new_tokens = len(unknown)
            for group in unknown.values():
                known.extend(group)
            return known

        # 配额不足时按流动性优先：所有列表接口都有这个字段，
        # 且它与"能否通过风险门"高度相关，是最省事的优先级近似
        ranked = sorted(
            unknown.items(),
            key=lambda item: max(
                (o.liquidity or 0.0) for o in item[1]
            ),
            reverse=True,
        )
        granted = self._onboarding.admit(capacity)
        for _, group in ranked[:granted]:
            known.extend(group)

        report.new_tokens = granted
        report.throttled_new = len(unknown) - granted
        if report.throttled_new > 0:
            bus.emit(
                EventType.ONBOARDING_THROTTLED,
                module="collector",
                summary=(
                    f"新币入库限速：本轮放行 {granted}，推迟 {report.throttled_new}"
                ),
                payload={"admitted": granted, "deferred": report.throttled_new},
            )
        return known

    def _post_evaluation(self, ev: Evaluation) -> None:
        """评估之后的调度副作用：安排刷新、补查审计、抽样拒绝样本。"""
        view = ev.view
        key = view.key

        if ev.risk.needs_audit and key not in self._audit_pending:
            self._audit_pending.add(key)
            self._audit_queue.append(key)

        if ev.promoted and ev.state.new_state.rank >= TokenState.S1.rank:
            # 晋升交易型状态后立刻开高频采样窗口：
            # 报警后前几分钟的价格轨迹事后无法补采，
            # 而延迟入场收益、纸面成交价全都依赖这段数据
            self._scheduler.open_burst(key, reason=ev.state.new_state.value)

        self._maybe_mark_reject_sample(ev)

        # 状态变化会改变所属调度层，重新安排下次刷新时间
        if ev.state.changed:
            self._next_refresh.pop(key, None)

    def _maybe_mark_reject_sample(self, ev: Evaluation) -> None:
        """按比例抽样保留被研究门拒绝的币。

        全部保留会撑爆内存和数据库，全部丢弃则三个月后无法回答
        "我们的阈值到底错杀了多少赢家"——这是整个策略迭代的基础。
        所以按固定比例随机抽样，且抽中后永久追踪。
        """
        view = ev.view
        if view.is_reject_sample or not ev.risk.gate_blocked:
            return
        if ev.risk.blocked:
            # 硬拒的币（蜜罐等）没有反事实价值，不占样本名额
            return
        if random.random() < self._reject_sample_ratio:
            view.is_reject_sample = True
            bus.emit_token(
                EventType.TOKEN_REJECTED,
                token=view,
                module="collector",
                summary=f"纳入拒绝样本池: {ev.risk.primary_reason()}",
                payload={"violations": [v.rule for v in ev.risk.research_violations]},
            )

    # ═════════════════════════════════════════════════════════════════════
    # 详情刷新循环
    # ═════════════════════════════════════════════════════════════════════

    async def _refresh_loop(self) -> None:
        while self._running:
            try:
                await self._refresh_due_tokens()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("刷新循环异常")
            await self._sleep(5.0)

    async def _refresh_due_tokens(self) -> None:
        now = now_ms()
        due = self._collect_due(now)
        if not due:
            return

        # 每轮只处理一小批：调度器会按配额阻塞，一次塞太多会让
        # 高优先级的币排在低优先级后面白等
        for view, tier in due[:8]:
            if not self._running:
                return
            await self._refresh_token(view, tier, now)

    def _collect_due(self, now: int) -> list[tuple[TokenView, str]]:
        """挑出到期需要详情刷新的币，按调度层优先级排序。

        观察池和拒绝样本的数量远超配额能覆盖的范围，
        它们主要靠列表接口被动更新，这里只做少量补充采样。
        """
        due: list[tuple[int, TokenView, str]] = []
        for view in self._registry.all_views():
            if view.state in (TokenState.DEAD, TokenState.BLOCKED):
                continue
            tier = self._tier_for(view)
            if tier is None:
                continue
            next_at = self._next_refresh.get(view.key, 0)
            if next_at > now:
                continue
            due.append((_TIER_ORDER.get(tier, 9), view, tier))

        due.sort(key=lambda item: item[0])
        return [(view, tier) for _, view, tier in due]

    def _tier_for(self, view: TokenView) -> str | None:
        if self._scheduler.in_burst(view.key):
            return "burst"
        if view.is_reject_sample and view.state.rank < TokenState.S0.rank:
            return "reject"
        return _STATE_TIER.get(view.state)

    async def _refresh_token(self, view: TokenView, tier: str, now: int) -> None:
        # 先占住下次刷新时间：即使本次请求失败也不要立刻重试，
        # 否则接口故障时会对同一枚币疯狂重试并吃光配额
        interval = self._scheduler.interval_with_jitter(tier)
        self._next_refresh[view.key] = now + int(interval * 1000)

        try:
            with CorrelationScope("detail"):
                result = await self._client.fetch(
                    ep.EP_DETAIL,
                    chain_id=view.chain_id,
                    params=ep.detail_params(view.chain_id, view.contract_address),
                    tier=tier,
                    budget_timeout_sec=min(60.0, interval),
                )
        except FetchError as exc:
            logger.debug("详情刷新失败 | %s | %s", view.contract_address[:12], exc)
            return

        retention = "long" if view.state.rank >= TokenState.S1.rank else "short"
        self._archive(result, kind="detail", retention_class=retention,
                      token_id=view.token_id)

        obs = parsers.parse_detail(
            view.chain_id, view.contract_address, result.data, result.observed_at
        )
        if obs is None:
            return
        obs.latency_ms = result.latency_ms
        obs.response_hash = result.response_hash

        await self._registry.ingest([obs])
        try:
            evaluation = await self._registry.evaluate(
                view, now_ms(), endpoint=ep.EP_DETAIL.name
            )
        except Exception:  # noqa: BLE001
            logger.exception("详情评估失败 | %s", view.contract_address[:12])
            return

        self._post_evaluation(evaluation)
        if self._on_evaluation is not None:
            maybe = self._on_evaluation(evaluation)
            if asyncio.iscoroutine(maybe):
                await maybe

    # ═════════════════════════════════════════════════════════════════════
    # 审计补查
    # ═════════════════════════════════════════════════════════════════════

    async def _audit_loop(self) -> None:
        """审计是最贵的请求，单开一个低频循环按需消费队列。

        队列由评估流水线填充：只有"分数够高、就差审计结果"的币才会进来。
        这样审计配额永远花在真正影响决策的地方。
        """
        while self._running:
            await self._sleep(3.0)
            if not self._audit_queue or not self._running:
                continue
            key = self._audit_queue.popleft()
            self._audit_pending.discard(key)
            view = self._registry.get(*key)
            if view is None or view.state in (TokenState.DEAD, TokenState.BLOCKED):
                continue
            try:
                await self._fetch_audit(view)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("审计补查异常 | %s", view.contract_address[:12])

    async def _fetch_audit(self, view: TokenView) -> None:
        try:
            with CorrelationScope("audit"):
                result = await self._client.fetch(
                    ep.EP_AUDIT,
                    chain_id=view.chain_id,
                    body=ep.audit_body(view.chain_id, view.contract_address),
                    tier="audit",
                    budget_timeout_sec=90.0,
                )
        except FetchError as exc:
            # 审计查不到不等于安全：这里只记录，风险门会继续把它当作 UNKNOWN，
            # 从而阻止它晋升到交易型状态
            logger.debug("审计请求失败 | %s | %s", view.contract_address[:12], exc)
            return

        self._archive(result, kind="audit", retention_class="long",
                      token_id=view.token_id)
        obs = parsers.parse_audit(
            view.chain_id, view.contract_address, result.data, result.observed_at
        )
        if obs is None:
            return
        obs.latency_ms = result.latency_ms
        obs.response_hash = result.response_hash

        await self._registry.ingest([obs])
        evaluation = await self._registry.evaluate(
            view, now_ms(), endpoint=ep.EP_AUDIT.name
        )
        self._post_evaluation(evaluation)
        if evaluation.risk.blocked:
            bus.emit_token(
                EventType.AUDIT_FAILED,
                token=view,
                module="collector",
                severity=Severity.NOTICE,
                summary=f"审计判定高危: {view.block_reason}",
            )
        if self._on_evaluation is not None:
            maybe = self._on_evaluation(evaluation)
            if asyncio.iscoroutine(maybe):
                await maybe

    # ═════════════════════════════════════════════════════════════════════
    # 归档与工具
    # ═════════════════════════════════════════════════════════════════════

    def _archive(self, result: FetchResult, *, kind: str, retention_class: str,
                 token_id: int | None = None) -> None:
        """归档原始响应。

        归档的唯一目的是"接口改版后能重放旧数据验证新解析器"。
        因此列表响应短留存即可，而重要币的详情/审计响应长期保留。
        压缩失败（响应过大）时放弃归档而不是阻塞采集。
        """
        payload = result.compressed_payload()
        ttl = self._raw_detail_ttl_ms if retention_class == "long" else self._raw_list_ttl_ms
        repo.insert_raw_archive(
            self._db,
            fetched_at=result.observed_at,
            endpoint=result.endpoint.name,
            chain_id=result.chain_id,
            token_id=token_id,
            kind=kind,
            http_status=result.http_status,
            latency_ms=result.latency_ms,
            response_hash=result.response_hash,
            item_count=result.item_count,
            payload_gz=payload,
            retention_class=retention_class,
            expires_at=result.observed_at + ttl,
        )

    async def _sleep(self, seconds: float) -> None:
        """可被停机打断的休眠。

        直接 asyncio.sleep(900) 会让停机最多卡 15 分钟；
        分片休眠使得 stop() 之后最迟 1 秒退出。
        """
        remaining = max(0.0, seconds)
        while remaining > 0 and self._running:
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step

    # ── 诊断 ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        report = self.last_cycle
        return {
            "chains": self._chains,
            "onboarding_capacity": self._onboarding.capacity(),
            "audit_queue": len(self._audit_queue),
            "refresh_tracked": len(self._next_refresh),
            "last_cycle": None if report is None else {
                "started_at": report.started_at,
                "tasks": report.tasks,
                "succeeded": report.succeeded,
                "failed": report.failed,
                "observations": report.observations,
                "tokens_touched": report.tokens_touched,
                "new_tokens": report.new_tokens,
                "throttled_new": report.throttled_new,
                "evicted": report.evicted,
                "evaluations": report.evaluations,
                "duration_ms": report.duration_ms,
                "errors": report.errors[:5],
            },
        }


# 调度层的处理顺序（数字越小越先被刷新）
_TIER_ORDER: dict[str, int] = {
    "burst": 0,
    "s2": 1,
    "s1": 2,
    "s0": 3,
    "watching": 4,
    "reject": 5,
}
