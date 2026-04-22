"""滚仓服务层 —— 持久化 + 引擎调用 的粘合层

职责：
  - 维护内存中的 RollStoreData（positions+plans）与 RollGlobalSettings
  - 维护 per-position 的 IntensityStabilizer（跨评估保持状态）
  - 维护一个共享 ForwardScanner（跨 position 共享频控表）
  - 协调：evaluate → 执行动作 → 落盘事件 → WS 推送
  - 被 engine._run_loop 定时调度，也被 api/roll_position 直接调用

设计约束：
  - RollService 不持有 FastAPI 或 SocketIO 的引用（保持可独立测试）
  - 通过 on_signal 回调把新 RollSignal 抛给外层（由外层决定如何 WS 推送）
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Optional

from models.roll_position import (
    RollEvent,
    RollGlobalSettings,
    RollPlan,
    RollTemplate,
    UserPosition,
)
from models.roll_signal import RollSignal
from processors.roll_forward import ForwardScanner
from processors.roll_position_engine import (
    IntensityStabilizer,
    MarketContext,
    evaluate,
)
from processors.roll_risk import (
    count_user_override_events,
    effective_leverage,
    estimate_liq_price,
    unrealized_pnl_usd,
    weighted_avg_entry,
)
from processors.roll_templates import (
    bootstrap_templates,
    find_template,
    plan_from_template,
)
from storage.roll_storage import (
    RollStoreData,
    append_event,
    bootstrap,
    save_settings,
    save_store,
)


logger = logging.getLogger(__name__)


# on_signal callback：外层可以订阅，拿到每次评估后的 RollSignal
SignalCallback = Callable[[RollSignal], None]


class RollServiceError(ValueError):
    """服务层显式抛出的业务错误（API 层会转成 HTTPException 4xx）。"""


class RollService:
    """滚仓服务主入口。

    每个后端进程持有单例；由 engine.py 创建并注入给 api/roll_position。
    """

    def __init__(
        self,
        data_dir: str,
        on_signal: Optional[SignalCallback] = None,
    ):
        self.data_dir = data_dir
        self.on_signal = on_signal

        self.store: RollStoreData
        self.settings: RollGlobalSettings
        self.templates: list[RollTemplate]

        self.stabilizer = IntensityStabilizer()
        self.forward_scanner = ForwardScanner()

        # 每 position 最近一次评估结果的缓存（API 层直接读取）
        self.last_signals: dict[str, RollSignal] = {}

        # 懒初始化（避免测试场景下未准备 data_dir 就调用）
        self._initialized = False

    # ─── 初始化与保存 ──────────────────────────────────

    def bootstrap(self) -> None:
        """启动时调用一次：加载磁盘状态到内存。"""
        self.store, self.settings = bootstrap(self.data_dir)
        self.templates = bootstrap_templates(self.data_dir)

        # 前瞻扫描器的冷却时长从 settings 映射（分钟 → 秒）
        self.forward_scanner.default_cooldown_sec = self.settings.forward_alert_cooldown_min * 60

        self._initialized = True
        logger.info(
            "RollService bootstrap ok | positions=%d plans=%d templates=%d",
            len(self.store.positions), len(self.store.plans), len(self.templates),
        )

    def _ensure_ready(self) -> None:
        if not self._initialized:
            raise RollServiceError("RollService 未初始化")

    def persist_store(self) -> None:
        save_store(self.data_dir, self.store)

    def persist_settings(self) -> None:
        save_settings(self.data_dir, self.settings)

    # ─── 计划 & 持仓创建 ────────────────────────────────

    def create_position(
        self,
        coin: str,
        side: str,
        margin_mode: str,
        leverage: int,
        entry_price: float,
        margin_usd: float,
        total_account_usd: float,
        template_id: str,
        name: str = "",
        note: str = "",
        stop_loss: Optional[float] = None,
        plan_overrides: Optional[dict] = None,
    ) -> tuple[UserPosition, RollPlan]:
        """创建新持仓 + 配套计划。

        - position_size 根据 margin × leverage / entry_price 自动计算
        - 同时写入一条 init 事件（落盘 events.jsonl）
        """
        self._ensure_ready()

        tpl = find_template(self.templates, template_id)
        if tpl is None:
            raise RollServiceError(f"模板不存在: {template_id}")

        if side not in ("long", "short"):
            raise RollServiceError(f"side 必须为 long/short: {side}")
        if margin_mode not in ("isolated", "cross"):
            raise RollServiceError(f"margin_mode 必须为 isolated/cross: {margin_mode}")
        if leverage < 1 or leverage > 125:
            raise RollServiceError(f"leverage 越界: {leverage}")
        if entry_price <= 0 or margin_usd <= 0 or total_account_usd <= 0:
            raise RollServiceError("entry_price/margin/account 必须 > 0")

        position_id = f"pos-{uuid.uuid4().hex[:12]}"
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        size = margin_usd * leverage / entry_price
        now = int(time.time())

        # 资金硬约束：account 占比
        if margin_usd / total_account_usd > 0.50:
            raise RollServiceError(
                f"初始保证金占账户 {margin_usd/total_account_usd:.1%}，超过 50%"
            )

        # 每币种合计占比
        same_coin_margin = sum(
            p.margin_used_usd for p in self.store.active_positions()
            if p.coin == coin.upper()
        )
        if (same_coin_margin + margin_usd) / total_account_usd > self.settings.per_coin_margin_pct_cap:
            raise RollServiceError(
                f"同币种合计占比超上限 {self.settings.per_coin_margin_pct_cap:.0%}"
            )

        # 全账户合计占比
        total_margin = sum(p.margin_used_usd for p in self.store.active_positions())
        if (total_margin + margin_usd) / total_account_usd > self.settings.account_margin_pct_cap:
            raise RollServiceError(
                f"全账户合计占比超上限 {self.settings.account_margin_pct_cap:.0%}"
            )

        liq = estimate_liq_price(
            side=side, margin_mode=margin_mode, entry_price=entry_price,
            leverage=leverage, position_size=size, margin_used_usd=margin_usd,
            total_account_usd=total_account_usd,
        )

        position = UserPosition(
            id=position_id,
            coin=coin.upper(),
            side=side,
            margin_mode=margin_mode,
            leverage=leverage,
            entry_price=entry_price,
            position_size=size,
            position_value_usd=size * entry_price,
            margin_used_usd=margin_usd,
            total_account_usd=total_account_usd,
            stop_loss=stop_loss,
            initial_stop_loss=stop_loss,
            liq_price=liq,
            plan_id=plan_id,
            created_at=now,
            updated_at=now,
            note=note,
        )
        plan = plan_from_template(tpl, plan_id, position_id, name, plan_overrides)

        self.store.upsert_position(position)
        self.store.upsert_plan(plan)
        self.persist_store()

        init_event = RollEvent(
            ts=now, kind="init", price=entry_price,
            margin_delta_usd=margin_usd, size_delta=size,
            avg_price_after=entry_price, leverage_after=float(leverage),
            liq_price_after=liq or 0.0, sl_after=stop_loss,
            reason=f"init with template={template_id}",
        )
        position.events.append(init_event)
        append_event(self.data_dir, position_id, init_event)

        logger.info(
            "[Roll] create_position | id=%s coin=%s side=%s tpl=%s lev=%dx "
            "entry=%.4f margin=%.2f size=%.6f liq=%s sl=%s",
            position_id, coin, side, template_id, leverage,
            entry_price, margin_usd, size,
            f"{liq:.4f}" if liq else "—",
            f"{stop_loss:.4f}" if stop_loss else "—",
        )
        return position, plan

    def delete_position(self, position_id: str) -> None:
        self._ensure_ready()
        if position_id not in self.store.positions:
            raise RollServiceError(f"持仓不存在: {position_id}")
        self.store.delete_position(position_id)
        self.persist_store()
        logger.info("[Roll] delete_position | id=%s", position_id)

    # ─── 事件执行（用户确认后调用） ──────────────────────

    def execute_add(
        self,
        position_id: str,
        margin_delta_usd: float,
        price: float,
        reason: str = "",
        system_confidence: float = 0.0,
        system_action: str = "",
        user_override: bool = False,
    ) -> UserPosition:
        """执行一次加仓：更新 position 状态 + 追加事件。"""
        pos = self._require_active(position_id)
        plan = self.store.plan_for_position(position_id)
        assert plan is not None

        if margin_delta_usd <= 0 or price <= 0:
            raise RollServiceError("margin_delta / price 必须 > 0")

        # 覆盖熔断检查
        if user_override:
            self._check_override_cooldown(pos)

        size_delta = margin_delta_usd * pos.leverage / price
        new_avg = weighted_avg_entry(pos.position_size, pos.entry_price, size_delta, price)
        new_size = pos.position_size + size_delta
        new_margin = pos.margin_used_usd + margin_delta_usd

        pnl_after = unrealized_pnl_usd(pos.side, new_avg, price, new_size)
        new_eff_lev = effective_leverage(new_size, price, new_margin, pnl_after)
        new_liq = estimate_liq_price(
            side=pos.side, margin_mode=pos.margin_mode, entry_price=new_avg,
            leverage=pos.leverage, position_size=new_size,
            margin_used_usd=new_margin, total_account_usd=pos.total_account_usd,
        )

        event_kind = "user_override_add" if user_override else "add"
        event = RollEvent(
            ts=int(time.time()), kind=event_kind, price=price,
            margin_delta_usd=margin_delta_usd, size_delta=size_delta,
            avg_price_after=new_avg, leverage_after=new_eff_lev,
            liq_price_after=new_liq or 0.0, sl_after=pos.stop_loss,
            reason=reason, system_confidence=system_confidence,
            system_action=system_action, user_override=user_override,
        )

        pos.entry_price = new_avg
        pos.position_size = new_size
        pos.position_value_usd = new_size * price
        pos.margin_used_usd = new_margin
        pos.liq_price = new_liq
        pos.updated_at = event.ts
        pos.events.append(event)

        append_event(self.data_dir, position_id, event)
        self.persist_store()
        logger.info(
            "[Roll] execute_add | id=%s kind=%s price=%.4f +margin=%.2f +size=%.6f "
            "avg=%.4f eff_lev=%.2fx liq=%s sys=%s conf=%.0f override=%s",
            position_id, event_kind, price, margin_delta_usd, size_delta,
            new_avg, new_eff_lev, f"{new_liq:.4f}" if new_liq else "—",
            system_action or "—", system_confidence, user_override,
        )
        return pos

    def execute_reduce(
        self,
        position_id: str,
        reduce_pct: float,
        price: float,
        reason: str = "",
    ) -> UserPosition:
        """减仓：按比例缩仓位 + 释放保证金。reduce_pct ∈ (0, 1]。"""
        pos = self._require_active(position_id)
        if reduce_pct <= 0 or reduce_pct > 1:
            raise RollServiceError(f"reduce_pct 必须 ∈ (0, 1]: {reduce_pct}")
        if price <= 0:
            raise RollServiceError("price 必须 > 0")

        size_delta = -pos.position_size * reduce_pct
        margin_delta = -pos.margin_used_usd * reduce_pct
        new_size = pos.position_size + size_delta
        new_margin = pos.margin_used_usd + margin_delta

        # 均价减仓时不变（减的是同一均价的仓位）
        new_avg = pos.entry_price
        new_liq = estimate_liq_price(
            side=pos.side, margin_mode=pos.margin_mode, entry_price=new_avg,
            leverage=pos.leverage, position_size=new_size,
            margin_used_usd=new_margin, total_account_usd=pos.total_account_usd,
        ) if new_size > 0 else None
        pnl_after = unrealized_pnl_usd(pos.side, new_avg, price, new_size)
        new_eff_lev = effective_leverage(new_size, price, new_margin, pnl_after) if new_margin > 0 else 0.0

        event = RollEvent(
            ts=int(time.time()), kind="reduce", price=price,
            margin_delta_usd=margin_delta, size_delta=size_delta,
            avg_price_after=new_avg, leverage_after=new_eff_lev,
            liq_price_after=new_liq or 0.0, sl_after=pos.stop_loss,
            reason=reason,
        )

        pos.position_size = new_size
        pos.position_value_usd = new_size * price
        pos.margin_used_usd = new_margin
        pos.liq_price = new_liq
        pos.updated_at = event.ts
        pos.events.append(event)

        # 若减到接近 0（< 1% 剩余）视为平仓
        if pos.position_size <= pos.events[0].size_delta * 0.01:
            pos.status = "closed"
            pos.closed_at = event.ts

        append_event(self.data_dir, position_id, event)
        self.persist_store()
        logger.info(
            "[Roll] execute_reduce | id=%s price=%.4f pct=%.1f%% -margin=%.2f "
            "size_left=%.6f eff_lev=%.2fx status=%s",
            position_id, price, reduce_pct * 100, -margin_delta,
            new_size, new_eff_lev, pos.status,
        )
        return pos

    def execute_close(
        self,
        position_id: str,
        price: float,
        reason: str = "",
        kind: str = "close_manual",
    ) -> UserPosition:
        """平仓：position 状态置为 closed。"""
        if kind not in ("close_manual", "close_sl_hit", "close_tp_hit"):
            raise RollServiceError(f"close kind 非法: {kind}")
        pos = self._require_active(position_id)
        if price <= 0:
            raise RollServiceError("price 必须 > 0")

        event = RollEvent(
            ts=int(time.time()), kind=kind, price=price,  # type: ignore[arg-type]
            margin_delta_usd=-pos.margin_used_usd,
            size_delta=-pos.position_size,
            avg_price_after=pos.entry_price,
            leverage_after=0.0,
            liq_price_after=0.0,
            sl_after=pos.stop_loss,
            reason=reason,
        )
        pos.status = "closed"
        pos.closed_at = event.ts
        pos.updated_at = event.ts
        pos.position_size = 0.0
        pos.position_value_usd = 0.0
        pos.margin_used_usd = 0.0
        pos.events.append(event)

        append_event(self.data_dir, position_id, event)
        self.persist_store()

        # 清理稳定器 & 前瞻频控 & signal 缓存
        self.stabilizer.reset(position_id)
        self.forward_scanner.reset(position_id)
        self.last_signals.pop(position_id, None)
        logger.info(
            "[Roll] execute_close | id=%s kind=%s price=%.4f reason=%s",
            position_id, kind, price, reason or "—",
        )
        return pos

    def execute_move_sl(
        self,
        position_id: str,
        new_sl: float,
        price: float,
        reason: str = "",
    ) -> UserPosition:
        pos = self._require_active(position_id)
        if new_sl <= 0:
            raise RollServiceError("new_sl 必须 > 0")
        # 方向检查：long sl 只能上移，short sl 只能下移
        if pos.stop_loss is not None:
            if pos.side == "long" and new_sl < pos.stop_loss:
                raise RollServiceError("long 止损只能上移")
            if pos.side == "short" and new_sl > pos.stop_loss:
                raise RollServiceError("short 止损只能下移")

        event = RollEvent(
            ts=int(time.time()), kind="sl_move", price=price,
            margin_delta_usd=0.0, size_delta=0.0,
            avg_price_after=pos.entry_price,
            leverage_after=effective_leverage(
                pos.position_size, price, pos.margin_used_usd,
                unrealized_pnl_usd(pos.side, pos.entry_price, price, pos.position_size),
            ),
            liq_price_after=pos.liq_price or 0.0,
            sl_after=new_sl,
            reason=reason,
        )
        old_sl = pos.stop_loss
        pos.stop_loss = new_sl
        pos.updated_at = event.ts
        pos.events.append(event)
        append_event(self.data_dir, position_id, event)
        self.persist_store()
        logger.info(
            "[Roll] execute_move_sl | id=%s old_sl=%s new_sl=%.4f price=%.4f reason=%s",
            position_id,
            f"{old_sl:.4f}" if old_sl else "—",
            new_sl, price, reason or "—",
        )
        return pos

    # ─── 引擎评估 ───────────────────────────────────────

    def evaluate_position(
        self,
        position_id: str,
        market: MarketContext,
    ) -> Optional[RollSignal]:
        """对指定持仓执行一次引擎评估。若持仓不存在或非 active 返回 None。"""
        self._ensure_ready()
        pos = self.store.get_position(position_id)
        if pos is None or pos.status != "active":
            return None
        plan = self.store.get_plan(pos.plan_id)
        if plan is None:
            return None

        signal = evaluate(
            pos, plan, market,
            stabilizer=self.stabilizer,
            forward_scanner=self.forward_scanner,
        )

        # 缓存供 API 读取
        self.last_signals[position_id] = signal

        # 重要事件落盘（仅 alert_* / gate_blocked）
        self._archive_alert_event(pos, signal)

        if self.on_signal is not None:
            try:
                self.on_signal(signal)
            except Exception as e:   # noqa: BLE001
                logger.warning("on_signal callback 失败：%s", e)

        return signal

    def evaluate_all(
        self,
        market_provider: Callable[[str], Optional[MarketContext]],
    ) -> list[RollSignal]:
        """批量评估所有 active positions。

        market_provider: coin → MarketContext 的函数（由 engine.py 从实时数据组装）
        """
        self._ensure_ready()
        results: list[RollSignal] = []
        for pos in self.store.active_positions():
            market = market_provider(pos.coin)
            if market is None:
                continue
            signal = self.evaluate_position(pos.id, market)
            if signal is not None:
                results.append(signal)
        return results

    # ─── 内部工具 ──────────────────────────────────────

    def _require_active(self, position_id: str) -> UserPosition:
        pos = self.store.get_position(position_id)
        if pos is None:
            raise RollServiceError(f"持仓不存在: {position_id}")
        if pos.status != "active":
            raise RollServiceError(f"持仓已关闭: {position_id}")
        return pos

    def _archive_alert_event(self, pos: UserPosition, signal: RollSignal) -> None:
        """把引擎输出的重要建议写入 events.jsonl 作为 alert 记录。

        hold 不落盘（噪声）。其他动作都落盘便于审计与覆盖率统计。
        """
        kind_map = {
            "add": "alert_add",
            "reduce": "alert_reduce",
            "close": "alert_close",
            "move_sl": "alert_move_sl",
        }
        if signal.action == "hold":
            # 除非被闸门拦截（blocking 里有 safety_gates），不落盘
            if not any(b.source == "safety_gates" for b in signal.blocking):
                return
            alert_kind: str = "gate_blocked"
        else:
            alert_kind = kind_map.get(signal.action, "")
            if not alert_kind:
                return

        event = RollEvent(
            ts=signal.ts, kind=alert_kind,  # type: ignore[arg-type]
            price=signal.current_price,
            margin_delta_usd=(
                signal.add_preview.final_margin_usd if signal.add_preview else 0.0
            ),
            size_delta=(
                signal.add_preview.add_size_delta if signal.add_preview else 0.0
            ),
            avg_price_after=(
                signal.add_preview.after.avg_price if signal.add_preview else pos.entry_price
            ),
            leverage_after=(
                signal.add_preview.after.effective_leverage if signal.add_preview else 0.0
            ),
            liq_price_after=(
                signal.add_preview.after.liq_price or 0.0
            ) if signal.add_preview else 0.0,
            sl_after=signal.suggested_new_sl or pos.stop_loss,
            reason=signal.headline_cn,
            system_confidence=signal.confidence_score,
            system_action=signal.action,
        )
        # 内嵌到 position（便于查询）
        pos.events.append(event)
        append_event(self.data_dir, pos.id, event)

    def _check_override_cooldown(self, pos: UserPosition) -> None:
        """用户连续覆盖导致亏损时触发冷却熔断。

        简化判定：近 N 次 override 事件中亏损次数 ≥ 阈值 → 拒绝。
        （亏损判定：override 后 24h 内 position 浮亏出现）
        完整实现留给 Step 10 结合实际数据，这里先保留关键骨架 + 数量级拦截。
        """
        if not self.settings.override_cooldown_enabled:
            return
        total_overrides = count_user_override_events(pos)
        # 简化：总覆盖次数 ≥ warn_threshold → 警告（第一阶段）
        if total_overrides >= self.settings.override_warn_threshold:
            logger.warning(
                "roll_service: position=%s 累计手动覆盖 %d 次，建议复盘",
                pos.id, total_overrides,
            )
