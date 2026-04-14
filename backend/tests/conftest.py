"""
共享 fixtures：mock CoinglassSource 和 CoinState 工厂。
"""
from __future__ import annotations

import sys
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import CoinState


class FakeCG:
    """Mock CoinglassSource — 每个 fetch_* 方法为 AsyncMock，由测试动态设置返回值。"""

    def __getattr__(self, name):
        if name.startswith("fetch_"):
            mock = AsyncMock(return_value=None)
            setattr(self, name, mock)
            return mock
        raise AttributeError(name)


@pytest.fixture
def cg():
    return FakeCG()


@pytest.fixture
def btc_state():
    return CoinState("BTC")


@pytest.fixture
def eth_state():
    return CoinState("ETH")


@pytest.fixture
def states(btc_state, eth_state):
    return {"BTC": btc_state, "ETH": eth_state}
