"""F-01.1 回归测试：_build_strategic_snapshot 字段访问 smoke

修这个 bug 时根因：PR-2 实现 _build_strategic_snapshot 把 TickerData 字段
`high_24h` / `low_24h` 写成了不存在的 `high24` / `low24`，Pydantic v2 严格
模式直接 raise AttributeError，导致 Strategic 100% 装配失败、自动循环空跑。

bug 隐藏 6 周才被线上 strategic_error 推送暴露，根本原因是这条路径完全
没有任何测试覆盖。本文件作为 smoke test：用最小 CoinState 触发完整字段
访问链路，任何字段名 typo 立刻在 pytest 里失败。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_minimal_state(coin: str = "BTC", price: float = 70000.0):
    """构造刚好够 _build_strategic_snapshot 跑通的最小 CoinState。

    只塞 ticker（必填，否则函数直接 return None），其它字段保持默认
    None / 空容器。这样断言"装配不抛 AttributeError"就足以捕获字段
    访问 typo。
    """
    from engine import CoinState
    from models.market import TickerData

    state = CoinState(coin)
    state.ticker = TickerData(
        coin=coin,
        ts=int(time.time()),
        last=price,
        high_24h=price * 1.02,
        low_24h=price * 0.98,
        vol_24h=1.0,
        change_24h=0.0,
        change_pct_24h=0.0,
    )
    return state


def _make_engine_with_state(state):
    """绕过完整 Engine.__init__ 构造一个仅持有 _states 的实例。

    _build_strategic_snapshot 只读 self._states，不需要其它 settings /
    polls / arbiter 依赖。
    """
    from engine import Engine

    eng = Engine.__new__(Engine)
    eng._states = {state.coin: state}
    return eng


class TestBuildStrategicSnapshotSmoke:
    def test_minimal_state_returns_snapshot(self):
        """最小 state（只有 ticker）应能成功装配，断言无 AttributeError。"""
        state = _make_minimal_state("BTC", 70000.0)
        eng = _make_engine_with_state(state)

        snap = eng._build_strategic_snapshot("BTC")

        assert snap is not None, (
            "字段访问异常会进 except 返回 None；如果是 typo 会先触发 "
            "AttributeError 被 logger.error 吞掉再返回 None"
        )
        assert snap.coin == "BTC"
        assert snap.price == 70000.0
        assert snap.high_24h > snap.price
        assert snap.low_24h < snap.price

    def test_no_state_returns_none(self):
        """不支持的币种返回 None，不抛。"""
        eng = _make_engine_with_state(_make_minimal_state("BTC"))
        assert eng._build_strategic_snapshot("DOGE") is None

    def test_no_ticker_returns_none(self):
        """state 存在但 ticker 未就绪 → 返回 None（不抛 AttributeError）。"""
        from engine import CoinState

        state = CoinState("BTC")
        # ticker 留 None
        eng = _make_engine_with_state(state)
        assert eng._build_strategic_snapshot("BTC") is None

    def test_ticker_field_access_uses_correct_names(self):
        """显式断言字段名：防止再被 typo 打回。

        如果未来有人把 high_24h 写成 high24（或类似 typo），即使主 smoke
        测试因 try/except 兜底而通过，本测试也会失败。
        """
        from models.market import TickerData

        # TickerData 的真实字段在 PR 中被 typo 过，这里把契约钉死
        fields = TickerData.model_fields
        assert "high_24h" in fields, "TickerData.high_24h 字段不能改名/删除"
        assert "low_24h" in fields, "TickerData.low_24h 字段不能改名/删除"
        assert "high24" not in fields, "防止再次出现下划线 typo"
        assert "low24" not in fields, "防止再次出现下划线 typo"
