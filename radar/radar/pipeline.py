"""评估后置流水线：警报 → 追踪。

采集器负责"把数据变成评估结果"，本模块负责"把评估结果变成产出"。
拆开是为了让 Replay 引擎能复用同一条产出链路——
如果这段逻辑写死在采集器里，回测就必须另写一份，
而两份实现一旦分叉，回测结论就不再能代表线上行为，
这恰恰是回测最致命的失效方式。

顺序不能颠倒：先落库警报拿到 alert_id，再据此开启追踪。
反过来会得到一条没有信号现场的 Outcome 记录。
"""

from __future__ import annotations

import logging
from typing import Any

from .alerts import AlertManager
from .registry import Evaluation
from .tracker import OutcomeTracker

logger = logging.getLogger("radar.pipeline")


class EvaluationPipeline:
    def __init__(self, *, alerts: AlertManager, tracker: OutcomeTracker) -> None:
        self._alerts = alerts
        self._tracker = tracker
        self.stats = {"processed": 0, "alerts": 0, "errors": 0}

    async def __call__(self, ev: Evaluation) -> None:
        await self.process(ev)

    async def process(self, ev: Evaluation) -> None:
        self.stats["processed"] += 1

        # 追踪先行：即使本次评估不产生警报，已有警报的 Outcome
        # 也必须用这次观测更新。放到警报之后会漏掉整整一个周期的价格
        try:
            self._tracker.on_observation(ev.view, ev.evaluated_at)
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            logger.exception("追踪更新失败 | %s", ev.view.contract_address[:12])

        try:
            record = await self._alerts.handle(ev)
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            logger.exception("警报处理失败 | %s", ev.view.contract_address[:12])
            return

        if record is None or record.alert_id is None:
            return

        self.stats["alerts"] += 1
        # 用警报生成时刻而非当前时刻作为信号起点：
        # 两者相差的这几十毫秒会让 time_to_2x 之类的指标产生系统性偏移
        self._tracker.track(
            alert_id=record.alert_id,
            alert_kind=record.kind,
            view=ev.view,
            at_ms=record.created_at,
        )

    def snapshot(self) -> dict[str, Any]:
        return dict(self.stats)
