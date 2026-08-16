"""代币注册表：内存中的代币集合 + 评估流水线。

这是整个雷达的中枢，把采集结果变成状态与决策：

    观测 → 合并进视图 → 数据质量 → 特征 → 风险门 → 五维评分 → 状态机 → 落库

三个非功能性约束在这里落地：

1. **内存有界**。容器只有 512MB。代币数量是无界增长的（每天有几万个新
   Meme 币），所以必须有淘汰策略。淘汰的只是内存视图，
   数据库里的历史完整保留，代币再次出现时会自动复活。
   永不淘汰 S0 及以上的代币和拒绝样本。

2. **重启可恢复**。进程重启后如果从零开始，所有代币的状态、
   历史、退出确认计数全部丢失，会导致重启后一次性重发一批警报。
   因此启动时从数据库恢复活跃代币。

3. **决策可追溯**。每次状态变更都写一条包含完整判定依据的快照，
   事后能回答"当时为什么升的、依据哪几个数"。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .domain.features import FeatureEngine
from .domain.models import (
    FieldGroup,
    QualityReport,
    ScoreResult,
    TokenObservation,
    TokenState,
    TokenView,
)
from .domain.quality import QualityEvaluator, best_market_cap
from .domain.risk_gate import GATE_EXECUTION, RISK_PARSER_VERSION, RiskDecision, RiskGate
from .domain.scoring import Scorer
from .domain.states import StateDecision, StateMachine
from .obs.events import EventBus, EventType, Severity
from .storage import repo
from .storage.db import PRIORITY_CRITICAL, Database, json_dump

logger = logging.getLogger("radar.registry")

# 永不因内存压力淘汰的状态
_PROTECTED_STATES = frozenset({
    TokenState.S0, TokenState.S1, TokenState.S2,
    TokenState.MOMENTUM, TokenState.DISTRIBUTION,
})


@dataclass(slots=True)
class Evaluation:
    """一次完整评估的结果。上层（警报调度、追踪器）消费这个对象。"""

    view: TokenView
    quality: QualityReport
    scores: ScoreResult
    risk: RiskDecision
    state: StateDecision
    features_json: str
    evaluated_at: int
    snapshot_id: int | None = None
    market_cap: float | None = None
    mc_source: str = "unknown"

    @property
    def promoted(self) -> bool:
        return self.state.changed and self.state.new_state.rank > self.state.old_state.rank


@dataclass
class RegistryStats:
    observations: int = 0
    evaluations: int = 0
    promotions: int = 0
    demotions: int = 0
    evicted: int = 0
    revived: int = 0
    by_state: dict[str, int] = field(default_factory=dict)


class TokenRegistry:
    def __init__(
        self,
        *,
        db: Database,
        events: EventBus,
        config: Mapping[str, Any],
        fingerprint: Mapping[str, str],
    ) -> None:
        self._db = db
        self._events = events
        self._fingerprint = dict(fingerprint)

        registry_cfg = config.get("registry", {}) or {}
        self._max_tokens = int(registry_cfg.get("max_tokens_in_memory", 4000))
        self._evict_batch = int(registry_cfg.get("evict_batch", 200))
        self._restore_limit = int(registry_cfg.get("restore_limit", 1500))
        self._restore_max_age_hours = int(registry_cfg.get("restore_max_age_hours", 48))

        alerts_cfg = config.get("alerts", {}) or {}
        near_miss_margin = float(alerts_cfg.get("near_miss_margin", 5.0))

        features_cfg = config.get("features", {}) or {}
        self._snapshot_min_interval_ms = int(
            float(features_cfg.get("snapshot_min_interval_sec", 55)) * 1000
        )
        # 按状态放宽的间隔。低状态的币占绝大多数，若与 S1 同频写快照，
        # 磁盘增长会在抽稀启动之前就把机器压垮
        self._snapshot_interval_by_state: dict[str, int] = {
            str(state).upper(): int(float(seconds) * 1000)
            for state, seconds in (
                features_cfg.get("snapshot_min_interval_by_state", {}) or {}
            ).items()
        }

        self._quality = QualityEvaluator(config.get("quality", {}) or {})
        self._features = FeatureEngine(config.get("features", {}) or {})
        self._risk = RiskGate(config.get("risk", {}) or {})
        self._scorer = Scorer(config.get("scoring", {}) or {})
        self._states = StateMachine(
            config.get("state_machine", {}) or {}, near_miss_margin=near_miss_margin
        )

        self._views: dict[tuple[str, str], TokenView] = {}
        # 建档中的代币：避免同一轮里对同一个新币并发建档
        self._onboarding: set[tuple[str, str]] = set()
        self.stats = RegistryStats()

    # ═════════════════════════════════════════════════════════════════════
    # 访问
    # ═════════════════════════════════════════════════════════════════════

    def __len__(self) -> int:
        return len(self._views)

    def get(self, chain_id: str, contract_address: str) -> TokenView | None:
        return self._views.get((chain_id, contract_address))

    def all_views(self) -> Iterable[TokenView]:
        return self._views.values()

    def views_by_min_rank(self, min_rank: int) -> list[TokenView]:
        return [v for v in self._views.values() if v.state.rank >= min_rank]

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for view in self._views.values():
            counts[view.state.value] = counts.get(view.state.value, 0) + 1
        return counts

    # ═════════════════════════════════════════════════════════════════════
    # 观测入口
    # ═════════════════════════════════════════════════════════════════════

    async def ingest(self, observations: Iterable[TokenObservation]) -> list[TokenView]:
        """合并一批观测，返回被触及的视图。

        只做合并，不做评估：一轮采集里同一枚币可能被多个接口触及，
        评估应该在所有观测合并完之后进行一次，而不是每个接口触发一次。
        否则不仅浪费 CPU，还会因为"用只有一半字段的视图评分"
        产生虚假的状态抖动。
        """
        batch = [o for o in observations if o.contract_address]

        # 每枚新币取本批次里最早的那条观测作为建档种子，
        # 保证 first_seen_market_cap 记录的是"我们最早看到的那一刻"
        seeds: dict[tuple[str, str], TokenObservation] = {}
        for obs in batch:
            if obs.key not in self._views and obs.key not in seeds:
                seeds[obs.key] = obs

        # 建档必须并发：每次建档都要 await 数据库返回自增主键，
        # 串行执行时一轮发现 50 个新币就是 50 次往返，
        # 足以把一个采集周期拖到分钟级
        if seeds:
            await asyncio.gather(
                *(self._create_view(o) for o in seeds.values()),
                return_exceptions=True,
            )

        touched: dict[tuple[str, str], TokenView] = {}
        for obs in batch:
            view = self._views.get(obs.key)
            if view is None:
                # 建档失败（数据库异常）：跳过本轮，下一轮采集会再试
                continue
            if seeds.get(obs.key) is obs:
                # 种子观测已在建档时合并过，重复 apply 会把 observation_count
                # 记成 2，让新币在第一次评估时就绕过"观测过少"的质量扣分
                touched[obs.key] = view
                continue
            groups = view.apply(obs)
            self.stats.observations += 1
            if FieldGroup.AUDIT.value in groups:
                view.audit_checked_at = obs.observed_at
            touched[obs.key] = view

        if len(self._views) > self._max_tokens:
            self._evict()
        return list(touched.values())

    async def _create_view(self, obs: TokenObservation) -> TokenView | None:
        key = obs.key
        if key in self._onboarding:
            return None
        self._onboarding.add(key)
        try:
            view = TokenView(chain_id=obs.chain_id, contract_address=obs.contract_address)
            view.first_seen_ms = obs.observed_at
            view.last_observed_ms = obs.observed_at
            view.state = TokenState.DISCOVERED
            view.state_since_ms = obs.observed_at
            # 先合并一次再建档：first_seen_market_cap 等字段必须带上真实值，
            # 否则"我们在多少市值时发现它"这个核心研究指标会全是 NULL
            groups = view.apply(obs)
            self.stats.observations += 1
            if FieldGroup.AUDIT.value in groups:
                view.audit_checked_at = obs.observed_at
            row = await repo.upsert_token(self._db, view, source=obs.endpoint)
            revived = _restore_persisted_state(view, row)
            if view.state.rank >= TokenState.S1.rank:
                # 复活为 S1+ 的币必须带回锚点，否则 S2 确认永远无法通过
                await self._load_s1_anchors([view])
            self._views[key] = view
            if revived:
                self.stats.revived += 1
            self._events.emit_token(
                EventType.TOKEN_REACTIVATED if revived else EventType.TOKEN_DISCOVERED,
                token=view,
                module="registry",
                summary=(
                    f"代币重新进入内存 {view.symbol or obs.contract_address[:10]}"
                    f"（{view.state.value}）"
                    if revived else
                    f"发现新代币 {view.symbol or obs.contract_address[:10]}"
                ),
                payload={"source": obs.endpoint},
            )
            return view
        except Exception as exc:  # noqa: BLE001
            self._events.emit(
                EventType.DB_WRITE_FAILED,
                module="registry",
                summary=f"建档失败: {exc}",
                chain_id=obs.chain_id,
                contract_address=obs.contract_address,
            )
            return None
        finally:
            self._onboarding.discard(key)

    # ═════════════════════════════════════════════════════════════════════
    # 评估流水线
    # ═════════════════════════════════════════════════════════════════════

    async def evaluate(self, view: TokenView, now_ms: int, *,
                       endpoint: str = "merged",
                       force_snapshot: bool = False) -> Evaluation:
        """跑完整流水线并落库。

        顺序不可调换：风险门要用到年龄（来自视图），
        评分要用到风险门结论与数据质量，状态机要用到评分。
        """
        quality = self._quality.evaluate(view, now_ms)
        features = self._features.compute(view, now_ms)
        risk = self._risk.evaluate(view, now_ms)
        scores = self._scorer.score(view, features, quality, risk)

        # "因数据不可信而未晋升"与"因分数不够而未晋升"必须区分开：
        # 前者是需要人介入的运维问题，后者是正常业务结果。
        self._track_quality_degradation(view, quality, scores)

        state = self._states.evaluate(view, scores, risk, now_ms)
        market_cap, mc_source = best_market_cap(view, quality)

        features_json = json_dump(features.as_dict())
        evaluation = Evaluation(
            view=view, quality=quality, scores=scores, risk=risk, state=state,
            features_json=features_json, evaluated_at=now_ms,
            market_cap=market_cap, mc_source=mc_source,
        )

        await self._persist(evaluation, endpoint=endpoint, force_snapshot=force_snapshot)
        self._apply_state_change(evaluation, now_ms)

        # 历史必须在评估之后压入：否则本轮特征会把"当前值"当成"历史值"，
        # 所有速度类特征恒等于 0
        view.push_history(now_ms)
        view.last_scores = scores.as_scores_dict()
        view.last_features = features.as_dict()
        self.stats.evaluations += 1
        return evaluation

    def _track_quality_degradation(self, view: TokenView, quality: QualityReport,
                                   scores: ScoreResult) -> None:
        """只在"进入/离开降级状态"时发事件，而不是每轮都发。

        每个采集周期都为同一枚币发一条 WARNING 的话，一天就是几万条噪声，
        真正需要人看的告警会被彻底埋掉——这正是可观测设计里
        错误聚合降噪要解决的问题，不能在这里又破一个口子。

        另外只关心"分数够高、只被数据质量挡住"的币：
        本来就够不上晋升的垃圾币缺数据完全正常，不值得告警。
        """
        degraded = quality.block_s1 and scores.opportunity >= 50
        if degraded == view.quality_degraded:
            return
        view.quality_degraded = degraded

        if degraded:
            self._events.emit_token(
                EventType.DATA_QUALITY_DEGRADED,
                token=view,
                module="registry",
                summary=(
                    f"数据质量 {quality.score:.0f} 不足，机会分 "
                    f"{scores.opportunity:.0f} 仍被阻止晋升"
                ),
                payload={
                    "stale_groups": list(quality.stale_groups),
                    "missing_groups": list(quality.missing_groups),
                    "conflicts": list(quality.conflicts),
                    "penalties": quality.penalties,
                },
            )
        else:
            self._events.emit_token(
                EventType.DATA_QUALITY_RECOVERED,
                token=view,
                module="registry",
                summary=f"数据质量恢复至 {quality.score:.0f}",
            )

    async def _persist(self, ev: Evaluation, *, endpoint: str,
                       force_snapshot: bool) -> None:
        """落库。

        快照分两条路径：需要 snapshot_id 的（状态变更、警报关联）走
        await 版本；常规刷新走 fire-and-forget 版本，
        避免为每个快照创建 Future——那是高频路径上最大的一笔固定开销。
        """
        view = ev.view
        if view.token_id is None:
            return

        keep_forever = (
            ev.state.changed
            or ev.state.new_state.rank >= TokenState.S1.rank
            or view.is_reject_sample
        )
        needs_id = ev.state.changed or force_snapshot or ev.state.near_miss

        # 写放大控制：爆发窗口里同一枚币每 25 秒就会被刷新一次，
        # 但快照的分析价值并不随写入频率线性增长。
        # 状态变更与 Near-Miss 例外——那些是不可再生的决策现场，必须落盘。
        if not needs_id and view.last_snapshot_ms:
            since_last = ev.evaluated_at - view.last_snapshot_ms
            if since_last < self._snapshot_interval_ms(ev.state.new_state):
                self._record_rejections(ev)
                repo.update_token_runtime(self._db, view)
                return

        row = repo.build_snapshot_row(
            view,
            observed_at=view.last_observed_ms or ev.evaluated_at,
            stored_at=ev.evaluated_at,
            endpoint=endpoint,
            latency_ms=None,
            response_hash=None,
            parser_version=self._fingerprint.get("parser_version", ""),
            cohort=self._cohort(view, ev.evaluated_at),
            features_json=ev.features_json,
            scores=ev.scores,
            quality=ev.quality,
            risk_flags_json=json_dump(ev.risk.as_dict()),
            risk_parser_version=RISK_PARSER_VERSION,
            keep_forever=keep_forever,
        )

        if needs_id:
            try:
                ev.snapshot_id = await repo.insert_snapshot(self._db, row)
            except Exception as exc:  # noqa: BLE001
                # 快照写失败不能中断状态推进：状态在内存里是正确的，
                # 丢一条快照只影响事后追溯，而抛异常会让整轮采集中断
                logger.warning("快照写入失败 token_id=%s: %s", view.token_id, exc)
        else:
            repo.insert_snapshot_nowait(self._db, row)

        view.last_snapshot_ms = ev.evaluated_at
        view.last_snapshot_id = ev.snapshot_id
        self._record_rejections(ev)
        repo.update_token_runtime(self._db, view)

    def _snapshot_interval_ms(self, state: TokenState) -> int:
        """该状态下两帧快照之间至少要隔多久。

        没有配置的状态回落到基准值（S1/S2/MOMENTUM 等高价值状态），
        因此新增状态时默认是"密集记录"而不是"悄悄不记录"——
        默认值选错的代价必须是多花磁盘，而不是丢失决策历史。
        """
        return self._snapshot_interval_by_state.get(
            state.value, self._snapshot_min_interval_ms
        )

    def _record_rejections(self, ev: Evaluation) -> None:
        """把风险门的拒绝写成结构化记录。

        只在状态变更或首次命中时写，否则每个采集周期都会为同一枚币
        重复写入同样的拒绝记录，几小时就能把表灌满。
        """
        violations = ev.risk.violations
        if not violations:
            ev.view.gate_reasons = ()
            return
        current = tuple(sorted(v.rule for v in violations))
        if current == ev.view.gate_reasons:
            return
        ev.view.gate_reasons = current

        for violation in violations:
            repo.insert_rejection(
                self._db,
                view=ev.view,
                occurred_at=ev.evaluated_at,
                gate=violation.gate,
                rule=violation.rule,
                actual_value=violation.actual_value,
                threshold_value=violation.threshold_value,
                actual_text=violation.actual_text or violation.detail or None,
                data_quality=ev.scores.data_quality,
                snapshot_id=ev.snapshot_id,
                correlation_id="",
                fingerprint=self._fingerprint,
            )

    def _apply_state_change(self, ev: Evaluation, now_ms: int) -> None:
        view = ev.view
        decision = ev.state
        view.blocked = ev.risk.blocked
        view.gate_blocked = ev.risk.gate_blocked
        view.block_reason = (
            "; ".join(v.detail or v.rule for v in ev.risk.execution_violations)
            if ev.risk.blocked else ""
        )

        if not decision.changed:
            return

        old, new = decision.old_state, decision.new_state
        view.state = new
        view.state_since_ms = now_ms
        # 换状态后旧状态的退出确认计数必须清零，
        # 否则将来回到该状态时会带着陈旧计数，一次抖动就直接降级
        view.exit_streak.clear()

        # S1 锚点生命周期：进入 S1 记录现场，跌回 S1 以下（DISTRIBUTION
        # 除外——派发观察期间锚点仍有对照价值）时清除。
        # S2 确认的全部行为条件（回撤、LP 抽离、dev 减仓）都以它为基准
        if new == TokenState.S1 and old.rank < TokenState.S1.rank:
            price = view.getf("price")
            view.s1_anchor = {
                "price": price,
                "market_cap": ev.market_cap or view.getf("market_cap"),
                "liquidity": view.getf("liquidity"),
                "top10_percent": view.getf("top10_percent"),
                "dev_percent": view.getf("dev_percent"),
                "dev_sell_percent": view.getf("dev_sell_percent"),
                "holders": view.geti("holders"),
                "at": now_ms,
            }
            view.s1_peak_price = price
            view.s1_inflow_dipped = False
        elif new.rank < TokenState.S1.rank and new != TokenState.DISTRIBUTION:
            view.s1_anchor = None
            view.s1_peak_price = None
            view.s1_inflow_dipped = False

        # 状态变更必须立刻补写数据库：_persist 在状态机结论应用之前执行，
        # 它写入的是变更前的状态。若不补写，"终局评估"（如 S1→DEAD 之后
        # 再无任何观测）会让 token_master 永远停在旧状态，
        # 重启恢复时把已死亡的币复活成 S1。
        # 用 CRITICAL 优先级：这次写入不可再生，队列紧张时也不允许丢弃。
        if view.token_id is not None:
            repo.update_token_runtime(self._db, view, priority=PRIORITY_CRITICAL)

        if new.rank > old.rank:
            self.stats.promotions += 1
        elif new.rank < old.rank:
            self.stats.demotions += 1

        # 用分类法里的具体事件类型（S1_ENTER 等），severity/importance 由
        # 事件规格表自动给出，避免在每个调用点手写一遍从而出现不一致
        self._events.emit_token(
            _state_event_type(old, new),
            token=view,
            module="registry",
            old_state=old.value,
            new_state=new.value,
            snapshot_id=ev.snapshot_id,
            summary=f"{view.symbol or view.contract_address[:10]}: {old.value} → {new.value}｜{decision.reason}",
            payload={
                "reason": decision.reason,
                "scores": ev.scores.as_scores_dict(),
                "prev_scores": view.last_scores or None,
                "requirements": decision.as_dict()["requirements"],
                "factors": ev.scores.factors_as_list(),
                "risk": ev.risk.as_dict(),
                "quality": ev.quality.as_dict(),
                "market_cap": ev.market_cap,
                "mc_source": ev.mc_source,
            },
        )

        if new == TokenState.BLOCKED and _has_honeypot(ev.risk):
            # 蜜罐单独发一条：这是最值得沉淀的黑样本类型，
            # 混在 EXECUTION_BLOCKED 里会被税率、审计等原因淹没
            self._events.emit_token(
                EventType.HONEYPOT_DETECTED,
                token=view,
                module="registry",
                summary=f"检测到蜜罐: {view.block_reason}",
                payload={"violations": [
                    v.as_dict() for v in ev.risk.violations if v.gate == GATE_EXECUTION
                ]},
            )

    def _cohort(self, view: TokenView, now_ms: int) -> dict[str, str | None]:
        """分组标签。

        Outcome 分析必须按同类比较：把上线 5 分钟的 3 万市值币
        和上线 3 天的 200 万市值币放在一起算命中率毫无意义。
        """
        age_sec = view.age_sec(now_ms)
        market_cap = view.getf("market_cap")
        return {
            "chain": view.chain_id,
            "age_bucket": _age_bucket(age_sec),
            "mc_bucket": _mc_bucket(market_cap),
            "stage": view.stage,
        }

    # ═════════════════════════════════════════════════════════════════════
    # 内存淘汰
    # ═════════════════════════════════════════════════════════════════════

    def _evict(self) -> int:
        """按"价值最低优先"淘汰内存视图。

        只淘汰内存，数据库历史完整保留；被淘汰的币再次出现在榜单上时
        会重新建视图（token_master 里 token_id 不变，历史自动接续）。

        永不淘汰：S0 及以上、派发中、拒绝样本。
        这些恰恰是长期研究价值最高的部分，宁可少收几个新币也不能丢它们。
        """
        candidates: list[tuple[float, tuple[str, str]]] = []
        for key, view in self._views.items():
            if view.state in _PROTECTED_STATES or view.is_reject_sample:
                continue
            candidates.append((self._eviction_priority(view), key))

        if not candidates:
            self._events.emit(
                EventType.MEMORY_WARNING,
                module="registry",
                summary=(
                    f"内存中 {len(self._views)} 个代币全部受保护，无法淘汰。"
                    "请下调 max_tokens_in_memory 或收紧晋升阈值"
                ),
            )
            return 0

        candidates.sort(key=lambda item: item[0])
        target = max(self._evict_batch, len(self._views) - self._max_tokens)
        removed = 0
        for _, key in candidates[:target]:
            self._views.pop(key, None)
            removed += 1

        self.stats.evicted += removed
        self._events.emit(
            EventType.MEMORY_WARNING,
            module="registry",
            severity=Severity.INFO,
            summary=f"淘汰 {removed} 个低价值代币视图，剩余 {len(self._views)}",
            payload={"remaining": len(self._views), "state_counts": self.state_counts()},
        )
        return removed

    @staticmethod
    def _eviction_priority(view: TokenView) -> float:
        """淘汰优先级，越小越先被淘汰。

        DEAD/BLOCKED 最先（已有明确结论，留在内存没有意义），
        然后是 DORMANT，最后按"最近机会分 + 观测新鲜度"排序。
        """
        base = {
            TokenState.DEAD: 0.0,
            TokenState.BLOCKED: 10.0,
            TokenState.DORMANT: 20.0,
            TokenState.DISCOVERED: 100.0,
            TokenState.WATCHING: 200.0,
        }.get(view.state, 500.0)
        opportunity = view.last_scores.get("opportunity", 0.0)
        # 最近有观测的略微加权，避免刚发现就被淘汰导致反复建档
        freshness = min(50.0, view.observation_count * 5.0)
        return base + opportunity + freshness

    # ═════════════════════════════════════════════════════════════════════
    # 重启恢复
    # ═════════════════════════════════════════════════════════════════════

    async def restore(self, now_ms: int) -> int:
        """从数据库恢复活跃代币。

        不恢复会有一个很隐蔽的后果：所有代币回到 DISCOVERED，
        于是那些本来早已是 S1 的币会在重启后**重新晋升一次**，
        用户收到一批重复警报，而且 Outcome 的起点被重置。
        """
        cutoff = now_ms - self._restore_max_age_hours * 3_600_000
        try:
            rows = await self._db.fetch_all(
                "SELECT token_id, chain_id, contract_address, symbol, name, decimals, "
                "launch_time_ms, creator_address, launch_platform, first_seen_ms, "
                "state, state_since_ms, last_observed_ms, last_snapshot_ms, "
                "is_reject_sample, circulating_supply, total_supply, max_supply "
                "FROM token_master "
                "WHERE (last_observed_ms >= ? AND state NOT IN ('DEAD','BLOCKED')) "
                "   OR is_reject_sample = 1 "
                "ORDER BY CASE state "
                "  WHEN 'S2' THEN 0 WHEN 'MOMENTUM' THEN 0 WHEN 'S1' THEN 1 "
                "  WHEN 'DISTRIBUTION' THEN 1 WHEN 'S0' THEN 2 ELSE 3 END, "
                "last_observed_ms DESC LIMIT ?",
                (cutoff, self._restore_limit),
            )
        except Exception as exc:  # noqa: BLE001
            self._events.emit(
                EventType.DB_READ_FAILED,
                module="registry",
                summary=f"恢复代币失败，将以空注册表启动: {exc}",
            )
            return 0

        restored = 0
        for row in rows:
            view = _view_from_row(row)
            self._views[view.key] = view
            restored += 1

        await self._restore_history(now_ms)
        await self._load_s1_anchors(list(self._views.values()))
        self.stats.revived = restored
        self._events.emit(
            EventType.REGISTRY_RESTORED,
            module="registry",
            summary=f"从数据库恢复 {restored} 个代币",
            payload={"state_counts": self.state_counts()},
        )
        return restored

    async def _load_s1_anchors(self, views: list[TokenView]) -> None:
        """从最近一条非 near-miss S1 警报恢复 S1 锚点。

        警报行只有价格/市值/流动性/持有人（无 top10/dev），
        缺的字段在 S2 确认里会自动跳过对比——诚实的少判，
        优先于用错误的基准判。
        """
        targets = {
            v.token_id: v for v in views
            if (v.token_id is not None and v.s1_anchor is None
                and v.state.rank >= TokenState.S1.rank)
        }
        if not targets:
            return
        placeholders = ",".join("?" * len(targets))
        try:
            rows = await self._db.fetch_all(
                "SELECT a.token_id, a.price, a.market_cap, a.liquidity, "
                "a.holders, a.created_at FROM alerts a "
                "JOIN (SELECT token_id, MAX(created_at) AS mc FROM alerts "
                f"      WHERE alert_kind='S1' AND is_near_miss=0 "
                f"      AND token_id IN ({placeholders}) GROUP BY token_id) m "
                "ON m.token_id = a.token_id AND m.mc = a.created_at "
                "WHERE a.alert_kind='S1' AND a.is_near_miss=0",
                tuple(targets.keys()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复 S1 锚点失败，S2 确认将等待下次晋升: %s", exc)
            return
        for row in rows:
            view = targets.get(row["token_id"])
            if view is None:
                continue
            view.s1_anchor = {
                "price": row["price"],
                "market_cap": row["market_cap"],
                "liquidity": row["liquidity"],
                "top10_percent": None,
                "dev_percent": None,
                "dev_sell_percent": None,
                "holders": row["holders"],
                "at": int(row["created_at"]),
            }
            view.s1_peak_price = row["price"]

    async def _restore_history(self, now_ms: int) -> None:
        """为已恢复的代币补回近期历史点。

        只恢复 S0 及以上代币的历史：速度类特征只在这些币上才影响决策，
        而给几千个 WATCHING 币各查一次历史会让启动时间变得不可接受。
        """
        targets = {
            v.token_id: v for v in self._views.values()
            if v.token_id is not None and v.state.rank >= TokenState.S0.rank
        }
        if not targets:
            return
        cutoff = now_ms - 7_200_000
        placeholders = ",".join("?" * len(targets))
        try:
            rows = await self._db.fetch_all(
                "SELECT token_id, observed_at, price, market_cap, holders, liquidity, "
                "net_inflow, smart_money_count, social_hype, top10_percent, "
                "unique_trader_1h FROM snapshots "
                f"WHERE token_id IN ({placeholders}) AND observed_at >= ? "
                "ORDER BY observed_at ASC",
                (*targets.keys(), cutoff),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复历史失败，速度类特征将在几个周期后自愈: %s", exc)
            return

        for row in rows:
            view = targets.get(row["token_id"])
            if view is None:
                continue
            # 借用 push_history 的构造逻辑：先把值填进视图再压入，
            # 保证内存历史与线上路径产生的历史点结构完全一致
            snapshot = dict(view.values)
            for name in ("price", "market_cap", "holders", "liquidity", "net_inflow",
                         "smart_money_count", "social_hype", "top10_percent",
                         "unique_trader_1h"):
                view.values[name] = row[name]
            view.push_history(int(row["observed_at"]))
            view.values = snapshot


# ═════════════════════════════════════════════════════════════════════════
# 辅助
# ═════════════════════════════════════════════════════════════════════════

def _restore_persisted_state(view: TokenView, row: Mapping[str, Any]) -> bool:
    """把数据库里的状态回填到新建的视图，返回是否是"旧币重新入内存"。

    代币被内存淘汰后再次出现在榜单上时会走建档路径。若不回填，
    它会被当成全新的币重新从 DISCOVERED 开始，
    同时把 token_master 里真实的状态与首次发现时间覆盖掉。
    """
    view.token_id = int(row["token_id"])
    first_seen = int(row["first_seen_ms"] or 0)
    revived = bool(first_seen and first_seen < view.first_seen_ms)
    if first_seen:
        view.first_seen_ms = first_seen
    view.is_reject_sample = bool(row["is_reject_sample"])
    try:
        state = TokenState(str(row["state"]))
    except ValueError:
        state = TokenState.DISCOVERED
    if state != TokenState.DISCOVERED:
        view.state = state
        view.state_since_ms = int(row["state_since_ms"] or view.first_seen_ms)
    return revived


def _view_from_row(row: Mapping[str, Any]) -> TokenView:
    view = TokenView(
        chain_id=str(row["chain_id"]),
        contract_address=str(row["contract_address"]),
        token_id=int(row["token_id"]),
        symbol=row["symbol"],
        name=row["name"],
        decimals=row["decimals"],
        launch_time_ms=row["launch_time_ms"],
        creator_address=row["creator_address"],
        launch_platform=row["launch_platform"],
    )
    view.first_seen_ms = int(row["first_seen_ms"] or 0)
    view.last_observed_ms = int(row["last_observed_ms"] or 0)
    view.last_snapshot_ms = int(row["last_snapshot_ms"] or 0)
    view.is_reject_sample = bool(row["is_reject_sample"])
    try:
        view.state = TokenState(str(row["state"]))
    except ValueError:
        # 状态枚举在版本间可能变化，遇到无法识别的值退回观察而不是崩溃
        view.state = TokenState.WATCHING
    view.state_since_ms = int(row["state_since_ms"] or view.first_seen_ms)
    for name in ("circulating_supply", "total_supply", "max_supply"):
        if row[name] is not None:
            view.values[name] = row[name]
    return view


def _has_honeypot(risk: RiskDecision) -> bool:
    return any(v.rule == "honeypot" for v in risk.execution_violations)


def _state_event_type(old: TokenState, new: TokenState) -> EventType:
    """把状态迁移映射到事件分类法里的具体类型。

    刻意不用一个笼统的 STATE_CHANGED：日后要按事件类型统计
    "本周 S2 进入次数" 或 "派发恢复率"，笼统类型只能靠解析文本，
    而文本格式一改统计脚本就全废。
    """
    specific = {
        TokenState.S0: EventType.S0_ENTER,
        TokenState.S1: EventType.S1_ENTER,
        TokenState.S2: EventType.S2_ENTER,
        TokenState.MOMENTUM: EventType.S2_ENTER,
        TokenState.DISTRIBUTION: EventType.DISTRIBUTION_ENTER,
        TokenState.DORMANT: EventType.DORMANT_ENTER,
        TokenState.DEAD: EventType.DEAD_ENTER,
        TokenState.BLOCKED: EventType.EXECUTION_BLOCKED,
    }
    if new in specific:
        return specific[new]
    if old == TokenState.DISTRIBUTION:
        return EventType.DISTRIBUTION_RECOVERY
    if new.rank < old.rank:
        return EventType.STATE_DOWNGRADE
    return EventType.STATE_TRANSITION


def _age_bucket(age_sec: int | None) -> str | None:
    if age_sec is None:
        return None
    minutes = age_sec / 60.0
    if minutes < 10:
        return "0-10m"
    if minutes < 60:
        return "10-60m"
    if minutes < 360:
        return "1-6h"
    if minutes < 1440:
        return "6-24h"
    if minutes < 10080:
        return "1-7d"
    return "7d+"


def _mc_bucket(market_cap: float | None) -> str | None:
    if market_cap is None or market_cap <= 0:
        return None
    if market_cap < 50_000:
        return "<50K"
    if market_cap < 200_000:
        return "50-200K"
    if market_cap < 1_000_000:
        return "200K-1M"
    if market_cap < 5_000_000:
        return "1-5M"
    return "5M+"
