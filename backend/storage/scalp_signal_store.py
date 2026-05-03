"""短线预测合约持久化层 · 纯文件 IO

目录结构（默认位于 backend/data/scalp_signal/）：

    config.json                       运行时配置（PATCH /api/scalp/config 写入此处）
    signals_active.json               活跃信号池（重启时加载继续等结算）
    signals_history_YYYY-MM.jsonl     按月分片归档（一行一信号，append-only）
    stats_cache.json                  策略统计缓存（每次结算后由 calibrator 更新）
    calibration_cache.json            calibration 曲线缓存

设计原则（对齐 DEVELOPMENT §2 "无静默失败" + dev-constraints #3 复用决策）：
  - 复用决策：独立新写。复用 roll_storage 需要么改既有文件（违反保守修改）
    要么提取公共 utils（不必要的耦合），故按既有 _atomic_write_json 模式
    在本模块内自实现一份相同语义的工具函数（~10 行）
  - 所有读操作对文件不存在/JSON 损坏做显式容错，返回默认值并 warn 日志
  - 所有写操作走 tmp + os.replace 原子替换（避免写一半进程崩溃丢数据）
  - 任何反序列化异常都记录但不抛出，让上层有"空态可用"的兜底

线程/并发：
  - 目标使用者：engine 主事件循环（asyncio 单线程） + FastAPI 异步路由
  - 现阶段无锁；活跃池写入是 read-modify-write，
    依赖"asyncio 事件循环内不抢占"的语义保证安全
  - 若未来引入多进程/多线程，需在调用方加文件锁 / asyncio.Lock
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from models.scalp_signal import (
    CalibrationCurve,
    GlobalStats,
    ScalpConfig,
    ScalpDirection,
    ScalpSignal,
    StrategyName,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 路径计算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SCALP_SUBDIR = "scalp_signal"

CONFIG_FILE = "config.json"
ACTIVE_FILE = "signals_active.json"
HISTORY_FILE_TPL = "signals_history_{year_month}.jsonl"
STATS_CACHE_FILE = "stats_cache.json"
CALIBRATION_CACHE_FILE = "calibration_cache.json"


def _default_data_dir() -> str:
    """生产路径：<repo>/backend/data （从 __file__ 反推，无依赖配置）"""
    here = os.path.dirname(os.path.abspath(__file__))  # backend/storage
    return os.path.normpath(os.path.join(here, "..", "data"))


def _atomic_write_json(path: Path, payload: dict | list) -> None:
    """原子写入 JSON：先写 .tmp 再 os.replace，避免写一半导致文件损坏"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _safe_read_json(path: Path) -> Optional[dict | list]:
    """读 JSON · 文件不存在返回 None，损坏 warn 后返回 None（不抛错）"""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("ScalpSignalStore JSON load failed | path=%s err=%s", path, e)
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Store 主体
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ScalpSignalStore:
    """短线信号持久化 · 单实例服务（engine 启动时构造，注入到各模块）

    构造参数：
        data_dir: 数据根目录（默认走 backend/data；单测可传 tmp_path）

    暴露语义：
        - 配置：load_config / save_config（运行时可写）
        - 活跃池：add_active / update_active / get_active / remove_active
        - 历史归档：archive_signal / iter_history（按月分片）
        - 统计缓存：load/save_stats_cache / load/save_calibration_cache
        - cooldown 查询：get_last_signal_ts（用于策略 cooldown 判定）

    所有方法均为同步阻塞 IO（持久化数据量小，无需 aiofiles），
    在 asyncio 事件循环里调用单次写入 < 1ms，可接受。
    """

    def __init__(self, data_dir: Optional[str] = None) -> None:
        base = data_dir if data_dir else _default_data_dir()
        self._root = Path(base) / SCALP_SUBDIR
        self._root.mkdir(parents=True, exist_ok=True)

    # ── 路径辅助 ──────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    def _config_path(self) -> Path:
        return self._root / CONFIG_FILE

    def _active_path(self) -> Path:
        return self._root / ACTIVE_FILE

    def _history_path(self, year_month: str) -> Path:
        return self._root / HISTORY_FILE_TPL.format(year_month=year_month)

    def _stats_cache_path(self) -> Path:
        return self._root / STATS_CACHE_FILE

    def _calibration_cache_path(self) -> Path:
        return self._root / CALIBRATION_CACHE_FILE

    # ── 配置 ──────────────────────────────────────────────────

    def load_config(self) -> ScalpConfig:
        """读取配置 · 文件不存在 / 损坏返回默认 ScalpConfig（首次启动场景）"""
        raw = _safe_read_json(self._config_path())
        if raw is None or not isinstance(raw, dict):
            return ScalpConfig()
        try:
            return ScalpConfig.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "ScalpSignalStore config schema invalid | err=%s | fallback=default", e,
            )
            return ScalpConfig()

    def save_config(self, cfg: ScalpConfig) -> None:
        _atomic_write_json(self._config_path(), cfg.model_dump(mode="json"))

    # ── 活跃信号池（重启可恢复） ────────────────────────────────

    def get_active(self) -> list[ScalpSignal]:
        """返回所有活跃（未到期 / 未结算）信号"""
        raw = _safe_read_json(self._active_path())
        if not isinstance(raw, list):
            return []
        out: list[ScalpSignal] = []
        for item in raw:
            try:
                out.append(ScalpSignal.model_validate(item))
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "ScalpSignalStore active item schema invalid | item=%r err=%s",
                    item, e,
                )
        return out

    def get_active_by_id(self, signal_id: str) -> Optional[ScalpSignal]:
        for sig in self.get_active():
            if sig.signal_id == signal_id:
                return sig
        return None

    def add_active(self, signal: ScalpSignal) -> None:
        """加入活跃池 · 同 signal_id 已存在时报错（避免覆盖）"""
        items = self.get_active()
        if any(s.signal_id == signal.signal_id for s in items):
            raise ValueError(f"signal_id already exists in active pool: {signal.signal_id}")
        items.append(signal)
        self._write_active(items)

    def update_active(self, signal: ScalpSignal) -> None:
        """更新活跃池中的某条信号（按 signal_id 匹配） · 不存在时报错"""
        items = self.get_active()
        replaced = False
        for i, s in enumerate(items):
            if s.signal_id == signal.signal_id:
                items[i] = signal
                replaced = True
                break
        if not replaced:
            raise KeyError(f"signal_id not in active pool: {signal.signal_id}")
        self._write_active(items)

    def remove_active(self, signal_id: str) -> bool:
        """从活跃池删除 · 返回是否真的删了一条"""
        items = self.get_active()
        new_items = [s for s in items if s.signal_id != signal_id]
        if len(new_items) == len(items):
            return False
        self._write_active(new_items)
        return True

    def _write_active(self, items: list[ScalpSignal]) -> None:
        payload = [s.model_dump(mode="json") for s in items]
        _atomic_write_json(self._active_path(), payload)

    # ── 历史归档（按月分片 jsonl，append-only） ─────────────────

    @staticmethod
    def _year_month_of(ts: int) -> str:
        """将 unix 秒时间戳格式化为 YYYY-MM（UTC，按到期 / 创建时间归档均可）"""
        tm = time.gmtime(ts)
        return f"{tm.tm_year:04d}-{tm.tm_mon:02d}"

    def archive_signal(self, signal: ScalpSignal) -> None:
        """把已结算 / 取消的信号写入对应月份的 jsonl

        归档键：用 settled_at 优先（结算月），其次 created_at（创建月）
        """
        ts = signal.settled_at if signal.settled_at else signal.created_at
        path = self._history_path(self._year_month_of(ts))
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(signal.model_dump(mode="json"), ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def iter_history(
        self,
        *,
        limit: Optional[int] = None,
        strategy: Optional[StrategyName] = None,
        coin: Optional[str] = None,
        horizon_min: Optional[int] = None,
        since_ts: Optional[int] = None,
    ) -> list[ScalpSignal]:
        """读取历史信号（倒序：最新在前）

        Args:
            limit: 最多返回多少条（None=不限）
            strategy / coin / horizon_min: 过滤条件
            since_ts: 只返回 created_at >= since_ts 的（用于增量）
        """
        files = sorted(
            (p for p in self._root.glob("signals_history_*.jsonl") if p.is_file()),
            key=lambda p: p.name,
            reverse=True,  # 最新月份先读
        )
        out: list[ScalpSignal] = []
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError as e:
                logger.warning("ScalpSignalStore history read failed | path=%s err=%s", path, e)
                continue
            # 倒序（同文件内最新在末尾）
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = ScalpSignal.model_validate(json.loads(line))
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "ScalpSignalStore history line schema invalid | path=%s err=%s",
                        path, e,
                    )
                    continue
                if strategy is not None and item.strategy != strategy:
                    continue
                if coin is not None and item.coin != coin:
                    continue
                if horizon_min is not None and item.horizon_min != horizon_min:
                    continue
                if since_ts is not None and item.created_at < since_ts:
                    continue
                out.append(item)
                if limit is not None and len(out) >= limit:
                    return out
        return out

    # ── 统计缓存 ──────────────────────────────────────────────

    def load_stats_cache(self) -> Optional[GlobalStats]:
        raw = _safe_read_json(self._stats_cache_path())
        if raw is None:
            return None
        try:
            return GlobalStats.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("ScalpSignalStore stats cache schema invalid | err=%s", e)
            return None

    def save_stats_cache(self, stats: GlobalStats) -> None:
        _atomic_write_json(self._stats_cache_path(), stats.model_dump(mode="json"))

    def load_calibration_cache(self) -> Optional[CalibrationCurve]:
        raw = _safe_read_json(self._calibration_cache_path())
        if raw is None:
            return None
        try:
            return CalibrationCurve.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("ScalpSignalStore calibration cache schema invalid | err=%s", e)
            return None

    def save_calibration_cache(self, curve: CalibrationCurve) -> None:
        _atomic_write_json(self._calibration_cache_path(), curve.model_dump(mode="json"))

    # ── Cooldown 查询（active + history 合并扫最近一条） ──────

    def get_last_signal_ts(
        self,
        strategy: StrategyName,
        direction: ScalpDirection,
        *,
        coin: Optional[str] = None,
        within_seconds: int = 24 * 3600,
    ) -> Optional[int]:
        """查询同 strategy × 同 direction（× 同 coin）最近一次信号的 created_at

        优先扫活跃池（活跃信号 created_at 最新）；活跃无命中则扫最近一份历史月文件
        within_seconds 限制只看近 N 秒，避免历史文件过大时全量扫描

        Returns:
            最近一条匹配信号的 created_at（秒级 unix），未找到返回 None
        """
        now = int(time.time())
        cutoff = now - within_seconds

        # 1) 活跃池优先（小集合，扫描成本低）
        candidates: list[int] = []
        for s in self.get_active():
            if s.strategy != strategy or s.direction != direction:
                continue
            if coin is not None and s.coin != coin:
                continue
            if s.created_at >= cutoff:
                candidates.append(s.created_at)

        # 2) 当月 + 上月历史（事件合约 cooldown 通常 ≤ 几小时，无需翻更老月份）
        cur_ym = self._year_month_of(now)
        prev_ts = now - 31 * 24 * 3600
        prev_ym = self._year_month_of(prev_ts)
        for ym in {cur_ym, prev_ym}:
            path = self._history_path(ym)
            if not path.exists():
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in reversed(list(f)):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("strategy") != strategy.value:
                            continue
                        if obj.get("direction") != direction:
                            continue
                        if coin is not None and obj.get("coin") != coin:
                            continue
                        ts = int(obj.get("created_at") or 0)
                        if ts >= cutoff:
                            candidates.append(ts)
                        break  # 该月最新一条已找到，无需再翻
            except OSError:
                continue

        if not candidates:
            return None
        return max(candidates)
