"""D04 扩展 · 分歧回测闭环（Divergence Backfill Loop）

职责：
    1. 消费 Signal Fusion Layer 产出的 FinalDecision（consensus=conflict 时）
    2. 记录 math/ai 方向对 + 记录时刻价格，去重同一 divergence 60min 内
    3. 每次 recompute tick 推进时间窗口（1h / 2h / 24h 取价）
    4. 24h 窗口到达后结算，按 divergence_type（math_action × ai_action）
       聚合为 DivergenceStats（math_win_rate / ai_win_rate / avg_delta_pct_24h）
    5. 持久化到 backend/data/divergence_backfill.json，进程重启不丢
    6. 向 D04_BACKTEST_LOOP 上报扩展 metrics（divergence_samples 等）

设计说明：
    - 独立仓（参考 plan_backtest.py 形态，但结构不同：双引擎方向对 + 多时间窗）
    - 胜方判定（24h 主结算）：
        * math_win: math_bias=bullish & delta_24h >= +WIN_THRESH，或 bearish & <= -WIN_THRESH
        * ai_win:   同理
        * 双 win / 双 lose 都允许（中短期反弹 vs 尾部方向，记录即反映真实)
    - 阈值 WIN_THRESH=0.5% 避开噪声；窗口 1h/2h/24h 分别记录，用于诊断
    - 去重 key：(coin, math_action, ai_action, round(price, decimals))
    - 已结算记录保留 14 天，pending 超过 24h + grace 也强制结算
    - 样本 <10 时仍返回，但 `winner_hint_cn` 会标注"样本不足"
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from models.fused_decision import DivergenceStats

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DEDUP_WINDOW_SEC = 60 * 60                  # 同一方向对 60min 内不重复记录
_WINDOW_1H_SEC = 60 * 60
_WINDOW_2H_SEC = 2 * 60 * 60
_WINDOW_24H_SEC = 24 * 60 * 60
_SETTLE_GRACE_SEC = 10 * 60                  # 24h 窗口后 10min 宽限内若没推进则强制结算
_RESOLVED_KEEP_SEC = 14 * 24 * 60 * 60       # 结算后保留 14 天
_PERSIST_EVERY_N = 25                        # 每 N 次 advance/track 落盘一次
_WIN_THRESH_PCT = 0.5                        # ±0.5% 作为方向命中阈值
_DATA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "divergence_backfill.json",
)

_PRICE_DECIMALS = {"BTC": 0, "ETH": 1, "SOL": 2}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部数据结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _DivergenceSample:
    sample_id: str
    coin: str
    divergence_type: str                     # "math_long_ai_short" 等
    math_action: str                         # "long" / "short" / "wait" / "avoid"
    math_bias: str                           # "bullish" / "bearish" / "neutral"
    ai_action: str
    ai_bias: str
    price_at_record: float
    created_ts: int

    # 时间窗价格快照（未到窗口则 None）
    price_1h: Optional[float] = None
    price_2h: Optional[float] = None
    price_24h: Optional[float] = None
    ts_1h: Optional[int] = None
    ts_2h: Optional[int] = None
    ts_24h: Optional[int] = None

    # 结算结果（24h 窗口达到后填充）
    resolved_ts: Optional[int] = None
    delta_pct_1h: Optional[float] = None
    delta_pct_2h: Optional[float] = None
    delta_pct_24h: Optional[float] = None
    math_win: Optional[bool] = None
    ai_win: Optional[bool] = None
    outcome: str = "pending"                 # pending / resolved / expired

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "coin": self.coin,
            "divergence_type": self.divergence_type,
            "math_action": self.math_action,
            "math_bias": self.math_bias,
            "ai_action": self.ai_action,
            "ai_bias": self.ai_bias,
            "price_at_record": self.price_at_record,
            "created_ts": self.created_ts,
            "price_1h": self.price_1h,
            "price_2h": self.price_2h,
            "price_24h": self.price_24h,
            "ts_1h": self.ts_1h,
            "ts_2h": self.ts_2h,
            "ts_24h": self.ts_24h,
            "resolved_ts": self.resolved_ts,
            "delta_pct_1h": self.delta_pct_1h,
            "delta_pct_2h": self.delta_pct_2h,
            "delta_pct_24h": self.delta_pct_24h,
            "math_win": self.math_win,
            "ai_win": self.ai_win,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_DivergenceSample":
        return cls(
            sample_id=str(d.get("sample_id", "")),
            coin=str(d.get("coin", "")),
            divergence_type=str(d.get("divergence_type", "")),
            math_action=str(d.get("math_action", "")),
            math_bias=str(d.get("math_bias", "")),
            ai_action=str(d.get("ai_action", "")),
            ai_bias=str(d.get("ai_bias", "")),
            price_at_record=float(d.get("price_at_record", 0) or 0),
            created_ts=int(d.get("created_ts", 0) or 0),
            price_1h=d.get("price_1h"),
            price_2h=d.get("price_2h"),
            price_24h=d.get("price_24h"),
            ts_1h=d.get("ts_1h"),
            ts_2h=d.get("ts_2h"),
            ts_24h=d.get("ts_24h"),
            resolved_ts=d.get("resolved_ts"),
            delta_pct_1h=d.get("delta_pct_1h"),
            delta_pct_2h=d.get("delta_pct_2h"),
            delta_pct_24h=d.get("delta_pct_24h"),
            math_win=d.get("math_win"),
            ai_win=d.get("ai_win"),
            outcome=str(d.get("outcome", "pending")),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _bias_win(bias: str, delta_pct: float, thresh: float = _WIN_THRESH_PCT) -> bool:
    """给定方向 bias 和价格变化百分比，判断该方向是否命中。"""
    if bias == "bullish":
        return delta_pct >= thresh
    if bias == "bearish":
        return delta_pct <= -thresh
    # neutral / potential_reversal / wait / avoid：中性方向，只要绝对幅度小就算"命中"
    # 但为了安全，中性不计 win（避免和有方向的引擎错误对比）
    return False


def _divergence_type_of(math_action: str, ai_action: str) -> str:
    return f"math_{math_action}_ai_{ai_action}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Store
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DivergenceBackfillStore:
    """分歧回测闭环仓"""

    def __init__(self, data_file: str = _DATA_FILE) -> None:
        self._lock = threading.RLock()
        self._data_file = data_file
        # coin -> list[_DivergenceSample]
        self._samples: dict[str, list[_DivergenceSample]] = {}
        self._tick_counter = 0
        self._load_from_disk()

    # ── 持久化 ───────────────────────────────────────────

    def _load_from_disk(self) -> None:
        if not os.path.exists(self._data_file):
            return
        try:
            with open(self._data_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for coin, lst in (raw.get("samples") or {}).items():
                self._samples[coin] = [
                    _DivergenceSample.from_dict(d) for d in lst if isinstance(d, dict)
                ]
            logger.info(
                "[D04.div] loaded | coins=%s total=%d",
                list(self._samples.keys()),
                sum(len(v) for v in self._samples.values()),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[D04.div] load failed: %s", e)

    def _persist_to_disk(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
            payload = {
                "samples": {
                    coin: [s.to_dict() for s in samples]
                    for coin, samples in self._samples.items()
                },
                "ts": int(time.time()),
            }
            tmp = self._data_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self._data_file)
        except (OSError, TypeError) as e:
            logger.debug("[D04.div] persist failed: %s", e)

    # ── 去重 id ──────────────────────────────────────────

    @staticmethod
    def _make_sample_id(
        coin: str, math_action: str, ai_action: str, price: float
    ) -> str:
        decimals = _PRICE_DECIMALS.get(coin.upper(), 2)
        rounded = round(price, decimals) if decimals >= 0 else int(price)
        return f"{coin.upper()}|{math_action}|{ai_action}|{rounded}"

    # ── 核心：记录分歧 ─────────────────────────────────

    def track(self, decision, *, current_price: float) -> Optional[str]:
        """消费一次 FinalDecision：若为 conflict 则记录（含去重）

        Returns:
            sample_id（新记录或去重命中的现有 id），非 conflict 或失败返回 None
        """
        try:
            if decision is None:
                return None
            consensus = getattr(decision, "consensus_level", "")
            if consensus != "conflict":
                return None
            math_b = getattr(decision, "math_brief", None)
            ai_b = getattr(decision, "ai_brief", None)
            if math_b is None or ai_b is None:
                return None

            math_action = str(getattr(math_b, "action", "wait"))
            math_bias = str(getattr(math_b, "bias", "neutral"))
            ai_action = str(getattr(ai_b, "action", "wait"))
            ai_bias = str(getattr(ai_b, "bias", "neutral"))

            # 只记录真的有分歧方向的样本（双方都 wait/avoid 就不是有意义的分歧）
            if math_action in ("wait", "avoid") and ai_action in ("wait", "avoid"):
                return None

            coin = str(getattr(decision, "coin", "")).upper()
            price = float(current_price) if current_price else float(
                getattr(decision, "current_price", 0) or 0
            )
            if not coin or price <= 0:
                return None

            with self._lock:
                sample_id = self._make_sample_id(coin, math_action, ai_action, price)
                bucket = self._samples.setdefault(coin, [])
                now = int(time.time())

                # 去重：同 id 且仍在 dedup 窗口内的活跃样本
                for s in bucket:
                    if (
                        s.sample_id == sample_id
                        and s.outcome == "pending"
                        and now - s.created_ts <= _DEDUP_WINDOW_SEC
                    ):
                        return s.sample_id

                div_type = _divergence_type_of(math_action, ai_action)
                sample = _DivergenceSample(
                    sample_id=sample_id,
                    coin=coin,
                    divergence_type=div_type,
                    math_action=math_action,
                    math_bias=math_bias,
                    ai_action=ai_action,
                    ai_bias=ai_bias,
                    price_at_record=price,
                    created_ts=now,
                )
                bucket.append(sample)
                logger.info(
                    "[D04.div] new divergence logged | %s @ %.2f | type=%s",
                    sample_id, price, div_type,
                )

                self._tick_counter += 1
                self._maybe_persist_locked()
                return sample_id
        except Exception:
            logger.debug("[D04.div] track failed", exc_info=True)
            return None

    # ── 推进时间窗 ─────────────────────────────────────

    def advance(self, coin: str, current_price: float) -> int:
        """每次 recompute 时推进：填充 1h/2h/24h 价格、完成结算。

        Returns:
            本次推进影响的样本数量
        """
        try:
            coin = coin.upper()
            if current_price <= 0:
                return 0
            with self._lock:
                bucket = self._samples.get(coin, [])
                if not bucket:
                    return 0
                now = int(time.time())
                touched = 0
                for s in bucket:
                    if s.outcome != "pending":
                        continue
                    age = now - s.created_ts
                    moved = False
                    if s.price_1h is None and age >= _WINDOW_1H_SEC:
                        s.price_1h = float(current_price)
                        s.ts_1h = now
                        s.delta_pct_1h = _pct(s.price_at_record, current_price)
                        moved = True
                    if s.price_2h is None and age >= _WINDOW_2H_SEC:
                        s.price_2h = float(current_price)
                        s.ts_2h = now
                        s.delta_pct_2h = _pct(s.price_at_record, current_price)
                        moved = True
                    if s.price_24h is None and age >= _WINDOW_24H_SEC:
                        s.price_24h = float(current_price)
                        s.ts_24h = now
                        s.delta_pct_24h = _pct(s.price_at_record, current_price)
                        # 24h 窗到 → 结算
                        s.math_win = _bias_win(s.math_bias, s.delta_pct_24h)
                        s.ai_win = _bias_win(s.ai_bias, s.delta_pct_24h)
                        s.resolved_ts = now
                        s.outcome = "resolved"
                        logger.info(
                            "[D04.div] resolved | %s | Δ24h=%.2f%% | math_win=%s ai_win=%s",
                            s.sample_id, s.delta_pct_24h, s.math_win, s.ai_win,
                        )
                        moved = True
                    # 超时宽限：24h + grace 仍 pending 则 expire
                    if (
                        s.outcome == "pending"
                        and age >= _WINDOW_24H_SEC + _SETTLE_GRACE_SEC
                        and s.price_24h is None
                    ):
                        s.outcome = "expired"
                        s.resolved_ts = now
                        moved = True
                    if moved:
                        touched += 1

                if touched:
                    self._tick_counter += 1
                    self._maybe_persist_locked()
                    self._mark_tracker_locked()
                return touched
        except Exception:
            logger.debug("[D04.div] advance failed", exc_info=True)
            return 0

    # ── 查询：产出 DivergenceStats 列表 ────────────────

    def get_stats_list(self, coin: str) -> list[DivergenceStats]:
        """按 divergence_type 聚合样本 → list[DivergenceStats]"""
        coin = coin.upper()
        with self._lock:
            bucket = list(self._samples.get(coin, []))

        # 按 divergence_type 分桶
        groups: dict[str, list[_DivergenceSample]] = {}
        for s in bucket:
            if s.outcome != "resolved":
                continue
            groups.setdefault(s.divergence_type, []).append(s)

        out: list[DivergenceStats] = []
        for div_type, samples in groups.items():
            n = len(samples)
            if n == 0:
                continue
            math_wins = sum(1 for s in samples if s.math_win)
            ai_wins = sum(1 for s in samples if s.ai_win)
            deltas = [s.delta_pct_24h or 0.0 for s in samples]
            math_rate = math_wins / n if n else 0.0
            ai_rate = ai_wins / n if n else 0.0
            avg_delta = sum(deltas) / n if n else 0.0

            # winner hint
            if n < 10:
                hint = f"样本不足（n={n}）· 参考性低"
            elif abs(math_rate - ai_rate) < 0.1:
                hint = f"双方胜率接近（math {math_rate:.0%} vs AI {ai_rate:.0%}）· 等待观望"
            elif math_rate > ai_rate:
                hint = f"历史此类分歧数学引擎胜率 {math_rate:.0%}（n={n}）"
            else:
                hint = f"历史此类分歧 AI 胜率 {ai_rate:.0%}（n={n}）"

            out.append(
                DivergenceStats(
                    divergence_type=div_type,
                    sample_size=n,
                    math_win_rate=round(math_rate, 4),
                    ai_win_rate=round(ai_rate, 4),
                    avg_delta_pct_24h=round(avg_delta, 4),
                    winner_hint_cn=hint,
                )
            )
        # 样本多的在前
        out.sort(key=lambda x: x.sample_size, reverse=True)
        return out

    # ── 维护 ─────────────────────────────────────────

    def _gc_locked(self) -> None:
        """清理结算 14 天前的样本（已持锁）"""
        cutoff = int(time.time()) - _RESOLVED_KEEP_SEC
        for coin, bucket in list(self._samples.items()):
            self._samples[coin] = [
                s for s in bucket
                if s.outcome == "pending" or (s.resolved_ts or 0) >= cutoff
            ]

    def _maybe_persist_locked(self) -> None:
        if self._tick_counter % _PERSIST_EVERY_N == 0:
            self._gc_locked()
            self._persist_to_disk()

    def _mark_tracker_locked(self) -> None:
        """向 DecisionTracker 上报 D04 扩展 metrics（已持锁）"""
        try:
            from utils.decision_tracker import D, get_tracker
            total = sum(len(v) for v in self._samples.values())
            resolved = sum(
                1 for v in self._samples.values() for s in v if s.outcome == "resolved"
            )
            math_wins = sum(
                1 for v in self._samples.values() for s in v
                if s.outcome == "resolved" and s.math_win
            )
            ai_wins = sum(
                1 for v in self._samples.values() for s in v
                if s.outcome == "resolved" and s.ai_win
            )
            math_rate = (math_wins / resolved) if resolved else 0.0
            ai_rate = (ai_wins / resolved) if resolved else 0.0
            get_tracker().mark(
                D.D04_BACKTEST_LOOP,
                status="ok" if resolved >= 10 else "warn",
                log=False,
                divergence_samples_total=total,
                divergence_resolved=resolved,
                divergence_math_win_rate=round(math_rate, 4),
                divergence_ai_win_rate=round(ai_rate, 4),
            )
        except Exception:
            logger.debug("[D04.div] mark tracker failed", exc_info=True)

    # ── 调试 snapshot ────────────────────────────────

    def snapshot_dict(self, coin: Optional[str] = None) -> dict:
        with self._lock:
            if coin:
                coin_u = coin.upper()
                return {coin_u: [s.to_dict() for s in self._samples.get(coin_u, [])]}
            return {
                c: [s.to_dict() for s in samples]
                for c, samples in self._samples.items()
            }


def _pct(p_from: float, p_to: float) -> float:
    if p_from <= 0:
        return 0.0
    return (p_to - p_from) / p_from * 100.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_store: Optional[DivergenceBackfillStore] = None
_store_lock = threading.Lock()


def get_divergence_store() -> DivergenceBackfillStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DivergenceBackfillStore()
    return _store


def reset_divergence_store_for_test() -> None:
    """仅单元测试用"""
    global _store
    with _store_lock:
        _store = None
