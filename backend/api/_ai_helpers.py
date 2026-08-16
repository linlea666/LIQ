"""TE · AI 解读的辅助收集器（routes.py + ws.py 共享）。

放这里的目的：**保证指纹计算两端一致**。
- routes.py 里触发 AI → 算指纹 → 落缓存
- ws.py 里 on_subscribe → 算指纹 → 尝试 replay
两处用同一份 `_collect_extras`，否则指纹不一致会导致 replay 永远 miss。

严格容错：任意字段 / 数据源异常都单独 try/except，降级为 None 字段，
绝不让一次数据问题阻断整条 AI 链路。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from engine import CoinState

logger = logging.getLogger(__name__)


def collect_extras(state: "CoinState") -> Optional[dict]:
    """从 CoinState 收集 AI 审计需要的"上下文扩展数据"。

    收集项：
      - market_structure × 3 周期（1h / 1d / 1w）
      - funding + multi_funding
      - oi + oi_history（截断最近 60 条=5h，够算 4h%）+ oi_change_24h_pct
      - ls_ratio × 3 维度（散户 + 大户账户 + 大户持仓）
      - liq_map_1d（含 clusters_above/below + vacuum_zones + imbalance_ratio）

    返回 None 仅当整体失败或所有字段都缺失；否则返回 dict（字段可部分缺失）。
    """
    try:
        extras: dict = {}

        # ── Market Structure × 3 周期 ──
        try:
            if getattr(state, "market_structure", None):
                extras["ms_1h"] = state.market_structure.model_dump()
        except Exception:
            pass
        try:
            if getattr(state, "market_structure_1d", None):
                extras["ms_1d"] = state.market_structure_1d.model_dump()
        except Exception:
            pass
        try:
            if getattr(state, "market_structure_1w", None):
                extras["ms_1w"] = state.market_structure_1w.model_dump()
        except Exception:
            pass

        # ── Funding ──
        try:
            if getattr(state, "funding", None):
                extras["funding"] = state.funding.model_dump()
        except Exception:
            pass
        try:
            if getattr(state, "multi_funding", None):
                extras["multi_funding"] = state.multi_funding.model_dump()
        except Exception:
            pass

        # ── OI + history（截断最近 60 条=5h） ──
        try:
            if getattr(state, "oi", None):
                extras["oi"] = state.oi.model_dump()
        except Exception:
            pass
        try:
            history = getattr(state, "oi_history", None)
            if history:
                # deque → list 切片，避免 deque 引用在异步间传递
                recent = list(history)[-60:]
                extras["oi_history"] = [s.model_dump() for s in recent]
        except Exception:
            pass
        try:
            val = getattr(state, "oi_change_24h_pct", None)
            if val is not None:
                extras["oi_change_24h_pct"] = float(val)
        except Exception:
            pass

        # ── LS Ratio × 3 维度 ──
        try:
            if getattr(state, "ls_ratio", None):
                extras["ls_ratio"] = state.ls_ratio.model_dump()
        except Exception:
            pass
        try:
            if getattr(state, "ls_ratio_top_account", None):
                extras["ls_top_account"] = state.ls_ratio_top_account.model_dump()
        except Exception:
            pass
        try:
            if getattr(state, "ls_ratio_top_position", None):
                extras["ls_top_position"] = state.ls_ratio_top_position.model_dump()
        except Exception:
            pass

        # ── Liquidation Map（统一降级链 1d→7d→30d） ──
        try:
            from models.liquidation import pick_primary_liq_map
            liq_map = pick_primary_liq_map(getattr(state, "liq_maps", None))
            if liq_map:
                extras["liq_map_1d"] = liq_map.model_dump()
        except Exception:
            pass

        return extras if extras else None
    except Exception as e:  # 兜底，任何上游 state 访问异常都不阻断
        logger.warning("[TE-AI] collect_extras failed: %s", e)
        return None
