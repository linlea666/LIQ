"""滚仓服务层 (processors/roll_service.py) 单元测试

覆盖要点：
  1. bootstrap：首次启动创建文件 + 加载模板
  2. create_position：
      - 成功创建（position + plan + init event）
      - 模板不存在 → RollServiceError
      - 资金占用硬约束：初始保证金 > 账户 50%
      - 同币种/账户占比上限
      - 参数非法（side / mode / leverage / price / margin）
  3. execute_add / execute_reduce / execute_close / execute_move_sl：
      - 正常更新 position + 落盘 event
      - 参数非法拒绝
      - move_sl 方向合法性（long 上移 / short 下移）
      - close 后再执行抛错
      - close 清理 stabilizer / forward_scanner / last_signals
  4. delete_position：
      - 成功 + 不存在抛错
  5. evaluate_position：
      - 非 active 或不存在 → None
      - 返回 signal 并缓存到 last_signals
  6. on_signal 回调：评估后被调用
  7. persist：save_store 写入后重新加载数据一致
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pytest

from models.roll_signal import RollSignal
from processors.roll_position_engine import MarketContext
from processors.roll_service import RollService, RollServiceError


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def service(tmp_path: Path) -> RollService:
    svc = RollService(data_dir=str(tmp_path))
    svc.bootstrap()
    return svc


def _create_default_position(svc: RollService, margin: float = 600.0):
    return svc.create_position(
        coin="BTC",
        side="long",
        margin_mode="isolated",
        leverage=10,
        entry_price=60000.0,
        margin_usd=margin,
        total_account_usd=svc.settings.total_account_usd,
        template_id="fatzhai",
        stop_loss=55000.0,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. bootstrap
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBootstrap:
    def test_first_bootstrap(self, tmp_path: Path):
        svc = RollService(data_dir=str(tmp_path))
        svc.bootstrap()
        assert svc._initialized is True
        # 4 个预置模板
        assert len(svc.templates) >= 4
        ids = {t.id for t in svc.templates}
        assert {"fatzhai", "li_fashi", "pyramid", "conservative"}.issubset(ids)

    def test_ensure_ready_before_use(self, tmp_path: Path):
        svc = RollService(data_dir=str(tmp_path))
        with pytest.raises(RollServiceError, match="未初始化"):
            svc.create_position(
                coin="BTC", side="long", margin_mode="isolated", leverage=10,
                entry_price=60000.0, margin_usd=100.0, total_account_usd=10000.0,
                template_id="fatzhai",
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. create_position
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCreatePosition:
    def test_create_success(self, service: RollService):
        pos, plan = _create_default_position(service)
        assert pos.id.startswith("pos-")
        assert plan.id.startswith("plan-")
        assert plan.position_id == pos.id
        assert pos.plan_id == plan.id
        assert pos.coin == "BTC"
        # size = 600 * 10 / 60000 = 0.1
        assert abs(pos.position_size - 0.1) < 1e-9
        # init event 落盘
        assert len(pos.events) == 1
        assert pos.events[0].kind == "init"
        # 持久化后能再加载
        assert pos.id in service.store.positions

    def test_unknown_template_rejected(self, service: RollService):
        with pytest.raises(RollServiceError, match="模板不存在"):
            service.create_position(
                coin="BTC", side="long", margin_mode="isolated", leverage=10,
                entry_price=60000.0, margin_usd=100.0,
                total_account_usd=service.settings.total_account_usd,
                template_id="does-not-exist",
            )

    def test_invalid_side(self, service: RollService):
        with pytest.raises(RollServiceError, match="side"):
            service.create_position(
                coin="BTC", side="xx", margin_mode="isolated", leverage=10,  # type: ignore[arg-type]
                entry_price=60000.0, margin_usd=100.0,
                total_account_usd=service.settings.total_account_usd,
                template_id="fatzhai",
            )

    def test_invalid_margin_mode(self, service: RollService):
        with pytest.raises(RollServiceError, match="margin_mode"):
            service.create_position(
                coin="BTC", side="long", margin_mode="portfolio", leverage=10,  # type: ignore[arg-type]
                entry_price=60000.0, margin_usd=100.0,
                total_account_usd=service.settings.total_account_usd,
                template_id="fatzhai",
            )

    def test_margin_over_half_account_rejected(self, service: RollService):
        with pytest.raises(RollServiceError, match="50%"):
            service.create_position(
                coin="BTC", side="long", margin_mode="isolated", leverage=10,
                entry_price=60000.0, margin_usd=6000.0,  # 60% of 10k
                total_account_usd=service.settings.total_account_usd,
                template_id="fatzhai",
            )

    def test_per_coin_cap_respected(self, service: RollService):
        # per_coin_margin_pct_cap 默认 0.5
        _create_default_position(service, margin=4000.0)   # 40%
        with pytest.raises(RollServiceError, match="占比超上限"):
            _create_default_position(service, margin=2000.0)   # 累计 60% > 50%

    def test_li_fashi_requires_stop_loss(self, service: RollService):
        """C2：李法师派模板在建仓时必须设置 stop_loss（模板硬约定）。"""
        with pytest.raises(RollServiceError, match="李法师派"):
            service.create_position(
                coin="BTC", side="short", margin_mode="isolated", leverage=10,
                entry_price=60000.0, margin_usd=500.0,
                total_account_usd=service.settings.total_account_usd,
                template_id="li_fashi",
                stop_loss=None,
            )

    def test_li_fashi_with_stop_loss_succeeds(self, service: RollService):
        pos, plan = service.create_position(
            coin="BTC", side="short", margin_mode="isolated", leverage=10,
            entry_price=60000.0, margin_usd=500.0,
            total_account_usd=service.settings.total_account_usd,
            template_id="li_fashi",
            stop_loss=61200.0,
        )
        assert pos.stop_loss == 61200.0
        assert plan.template_id == "li_fashi"

    def test_other_templates_allow_no_stop_loss(self, service: RollService):
        """非 li_fashi 模板不应被此硬约束拦住。"""
        pos, _ = service.create_position(
            coin="BTC", side="long", margin_mode="isolated", leverage=10,
            entry_price=60000.0, margin_usd=500.0,
            total_account_usd=service.settings.total_account_usd,
            template_id="fatzhai",
            stop_loss=None,
        )
        assert pos.stop_loss is None

    def test_account_cap_respected(self, service: RollService):
        # account_margin_pct_cap 默认 0.8
        # per_coin_cap 默认 0.5，必须先放宽才能测 account cap
        service.settings.per_coin_margin_pct_cap = 0.9
        service.create_position(
            coin="BTC", side="long", margin_mode="isolated", leverage=10,
            entry_price=60000.0, margin_usd=4500.0,
            total_account_usd=service.settings.total_account_usd,
            template_id="fatzhai",
        )
        service.create_position(
            coin="ETH", side="long", margin_mode="isolated", leverage=10,
            entry_price=3000.0, margin_usd=3000.0,
            total_account_usd=service.settings.total_account_usd,
            template_id="fatzhai",
        )
        with pytest.raises(RollServiceError, match="合计占比超上限"):
            service.create_position(
                coin="SOL", side="long", margin_mode="isolated", leverage=10,
                entry_price=150.0, margin_usd=1000.0,  # 累计 85% > 80%
                total_account_usd=service.settings.total_account_usd,
                template_id="fatzhai",
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. execute_add / reduce / close / move_sl
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestExecuteEvents:
    def test_execute_add(self, service: RollService):
        pos, _ = _create_default_position(service)
        updated = service.execute_add(
            position_id=pos.id,
            margin_delta_usd=200.0,
            price=61000.0,
            reason="test add",
        )
        assert updated.margin_used_usd == pytest.approx(800.0)
        # size 增加：200 * 10 / 61000 ≈ 0.0328
        assert updated.position_size > 0.1
        # 均价被拉高
        assert updated.entry_price > 60000.0
        # 事件：init + add = 2
        kinds = [e.kind for e in updated.events]
        assert kinds == ["init", "add"]

    def test_execute_add_invalid_margin(self, service: RollService):
        pos, _ = _create_default_position(service)
        with pytest.raises(RollServiceError):
            service.execute_add(
                position_id=pos.id, margin_delta_usd=-10.0, price=61000.0,
            )

    def test_execute_reduce(self, service: RollService):
        pos, _ = _create_default_position(service)
        updated = service.execute_reduce(
            position_id=pos.id, reduce_pct=0.5, price=62000.0,
        )
        assert updated.position_size == pytest.approx(0.05)
        assert updated.margin_used_usd == pytest.approx(300.0)
        assert updated.entry_price == 60000.0  # 减仓不改均价

    def test_execute_reduce_invalid_pct(self, service: RollService):
        pos, _ = _create_default_position(service)
        with pytest.raises(RollServiceError):
            service.execute_reduce(position_id=pos.id, reduce_pct=1.5, price=60000.0)
        with pytest.raises(RollServiceError):
            service.execute_reduce(position_id=pos.id, reduce_pct=0.0, price=60000.0)

    def test_execute_close(self, service: RollService):
        pos, _ = _create_default_position(service)
        closed = service.execute_close(position_id=pos.id, price=62000.0, reason="stopped")
        assert closed.status == "closed"
        assert closed.position_size == 0.0
        assert closed.margin_used_usd == 0.0

    def test_execute_close_bad_kind(self, service: RollService):
        pos, _ = _create_default_position(service)
        with pytest.raises(RollServiceError):
            service.execute_close(position_id=pos.id, price=60000.0, kind="bogus")

    def test_actions_on_closed_position_fail(self, service: RollService):
        pos, _ = _create_default_position(service)
        service.execute_close(position_id=pos.id, price=60000.0)
        with pytest.raises(RollServiceError, match="已关闭"):
            service.execute_add(position_id=pos.id, margin_delta_usd=100.0, price=60000.0)

    def test_close_clears_caches(self, service: RollService):
        pos, _ = _create_default_position(service)
        # 手动种一个 signal 缓存
        dummy_signal = self._dummy_signal(pos.id)
        service.last_signals[pos.id] = dummy_signal

        service.execute_close(position_id=pos.id, price=60000.0)
        assert pos.id not in service.last_signals

    def test_move_sl_long_only_up(self, service: RollService):
        pos, _ = _create_default_position(service)
        updated = service.execute_move_sl(
            position_id=pos.id, new_sl=57000.0, price=60000.0,
        )
        assert updated.stop_loss == 57000.0

        # 再往下移应拒绝
        with pytest.raises(RollServiceError, match="只能上移"):
            service.execute_move_sl(
                position_id=pos.id, new_sl=56000.0, price=60000.0,
            )

    def test_move_sl_short_only_down(self, service: RollService):
        # 用 service 直接创建一个 short 持仓（避开 per-coin 限制）
        pos, _ = service.create_position(
            coin="ETH", side="short", margin_mode="isolated", leverage=10,
            entry_price=3000.0, margin_usd=300.0,
            total_account_usd=service.settings.total_account_usd,
            template_id="fatzhai",
            stop_loss=3200.0,
        )
        updated = service.execute_move_sl(position_id=pos.id, new_sl=3100.0, price=3000.0)
        assert updated.stop_loss == 3100.0

        with pytest.raises(RollServiceError, match="只能下移"):
            service.execute_move_sl(position_id=pos.id, new_sl=3300.0, price=3000.0)

    @staticmethod
    def _dummy_signal(position_id: str) -> RollSignal:
        return RollSignal(
            position_id=position_id, plan_id="plan-x", coin="BTC",
            ts=int(time.time()),
            action="hold", urgency="info",
            confidence_score=0.0, reduce_confidence_score=0.0,
            intensity="reject",
            headline_cn="test",
            current_price=60000.0,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. delete_position
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDeletePosition:
    def test_delete_success(self, service: RollService):
        pos, _ = _create_default_position(service)
        service.delete_position(pos.id)
        assert pos.id not in service.store.positions
        assert pos.plan_id not in service.store.plans

    def test_delete_nonexistent(self, service: RollService):
        with pytest.raises(RollServiceError, match="持仓不存在"):
            service.delete_position("pos-nothing")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. evaluate_position
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_market(ts: Optional[int] = None, current_price: float = 60200.0) -> MarketContext:
    return MarketContext(
        ts=ts or int(time.time()),
        current_price=current_price,
        atr=500.0,
    )


class TestEvaluate:
    def test_returns_none_for_missing_position(self, service: RollService):
        signal = service.evaluate_position("pos-missing", _make_market())
        assert signal is None

    def test_returns_none_for_closed_position(self, service: RollService):
        pos, _ = _create_default_position(service)
        service.execute_close(position_id=pos.id, price=60000.0)
        signal = service.evaluate_position(pos.id, _make_market())
        assert signal is None

    def test_caches_signal(self, service: RollService):
        pos, _ = _create_default_position(service)
        signal = service.evaluate_position(pos.id, _make_market())
        assert signal is not None
        assert service.last_signals[pos.id] is signal

    def test_signal_history_ring_buffer(self, service: RollService):
        """C4：每次评估都应追加到 ring buffer，且遵守容量上限。"""
        pos, _ = _create_default_position(service)
        # 缩小容量方便测试
        service.signal_history_capacity = 3
        for i in range(5):
            sig = service.evaluate_position(
                pos.id, _make_market(ts=1_000_000 + i * 10),
            )
            assert sig is not None
        buf = service.signal_history[pos.id]
        assert len(buf) == 3
        # 最后一次 ts 必须是最新的（ring 语义：旧的被挤出）
        assert buf[-1].ts == 1_000_000 + 40
        assert buf[0].ts == 1_000_000 + 20

    def test_signal_history_cleared_on_close(self, service: RollService):
        pos, _ = _create_default_position(service)
        service.evaluate_position(pos.id, _make_market())
        assert pos.id in service.signal_history
        service.execute_close(position_id=pos.id, price=60000.0)
        assert pos.id not in service.signal_history

    def test_signal_history_cleared_on_delete(self, service: RollService):
        pos, _ = _create_default_position(service)
        service.evaluate_position(pos.id, _make_market())
        assert pos.id in service.signal_history
        service.delete_position(pos.id)
        assert pos.id not in service.signal_history

    def test_on_signal_callback_fires(self, tmp_path: Path):
        captured: list[RollSignal] = []
        svc = RollService(data_dir=str(tmp_path), on_signal=captured.append)
        svc.bootstrap()
        _create_default_position(svc)
        pos = list(svc.store.positions.values())[0]
        signal = svc.evaluate_position(pos.id, _make_market())
        assert signal is not None
        assert len(captured) == 1
        assert captured[0].position_id == pos.id

    def test_callback_exception_does_not_raise(self, tmp_path: Path):
        def raising(_sig: RollSignal) -> None:
            raise RuntimeError("boom")
        svc = RollService(data_dir=str(tmp_path), on_signal=raising)
        svc.bootstrap()
        _create_default_position(svc)
        pos = list(svc.store.positions.values())[0]
        # 不应抛
        signal = svc.evaluate_position(pos.id, _make_market())
        assert signal is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. 持久化：新服务重新加载数据
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPersistence:
    def test_data_survives_restart(self, tmp_path: Path):
        svc1 = RollService(data_dir=str(tmp_path))
        svc1.bootstrap()
        pos, _ = _create_default_position(svc1)
        svc1.execute_add(position_id=pos.id, margin_delta_usd=200.0, price=61000.0)

        # 模拟重启
        svc2 = RollService(data_dir=str(tmp_path))
        svc2.bootstrap()
        loaded = svc2.store.get_position(pos.id)
        assert loaded is not None
        assert loaded.margin_used_usd == pytest.approx(800.0)
        # events 从 events.jsonl 回填
        assert len(loaded.events) >= 2
        kinds = [e.kind for e in loaded.events]
        assert "init" in kinds and "add" in kinds
