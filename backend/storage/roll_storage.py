"""滚仓模块本地持久化层（纯文件 IO）

目录结构（默认位于 backend/data/roll/）：
    positions.json     所有持仓 + 计划（JSON 字典，key=position_id）
    templates.json     策略模板（由 processors/roll_templates.py 管理）
    events.jsonl       事件追加日志（一行一个 RollEvent，便于回放与审计）
    settings.json      全局设置（RollGlobalSettings）

设计约束（对齐 DEVELOPMENT §2 "无静默失败"）：
  - 所有读操作对文件不存在/损坏做显式容错，并在日志里说明
  - 所有写操作用临时文件 + os.replace 原子替换，避免写一半崩溃导致丢失
  - 任何反序列化失败都记录但不阻塞调用方（返回空结构，调用方自行处理空态）

线程/并发：
  - 目标使用者是 engine._run_loop（asyncio 单事件循环），无需锁
  - 若未来多线程访问，需在调用方外层加文件锁
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from models.roll_position import (
    RollEvent,
    RollGlobalSettings,
    RollPlan,
    UserPosition,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 路径计算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ROLL_SUBDIR = "roll"


def roll_dir(data_dir: str) -> Path:
    return Path(data_dir) / ROLL_SUBDIR


def positions_path(data_dir: str) -> Path:
    return roll_dir(data_dir) / "positions.json"


def events_path(data_dir: str) -> Path:
    return roll_dir(data_dir) / "events.jsonl"


def settings_path(data_dir: str) -> Path:
    return roll_dir(data_dir) / "settings.json"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 通用：原子写入
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _atomic_write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Positions + Plans（一个 position 绑一个 plan，一起存）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RollStoreData:
    """In-memory 的 positions + plans 集合，便于批量操作。

    持久化格式（positions.json）：
        {
          "version": 1,
          "updated_at": <ts>,
          "positions": { "<id>": {...UserPosition...}, ... },
          "plans":     { "<id>": {...RollPlan...}, ... },
        }
    """

    def __init__(self):
        self.positions: dict[str, UserPosition] = {}
        self.plans: dict[str, RollPlan] = {}

    def get_position(self, position_id: str) -> Optional[UserPosition]:
        return self.positions.get(position_id)

    def get_plan(self, plan_id: str) -> Optional[RollPlan]:
        return self.plans.get(plan_id)

    def plan_for_position(self, position_id: str) -> Optional[RollPlan]:
        pos = self.positions.get(position_id)
        if pos is None:
            return None
        return self.plans.get(pos.plan_id)

    def active_positions(self) -> list[UserPosition]:
        return [p for p in self.positions.values() if p.status == "active"]

    def upsert_position(self, pos: UserPosition) -> None:
        self.positions[pos.id] = pos

    def upsert_plan(self, plan: RollPlan) -> None:
        self.plans[plan.id] = plan

    def delete_position(self, position_id: str) -> None:
        pos = self.positions.pop(position_id, None)
        if pos is not None:
            self.plans.pop(pos.plan_id, None)


def load_store(data_dir: str) -> RollStoreData:
    """从磁盘加载（文件不存在 / 损坏 → 返回空 store）。"""
    path = positions_path(data_dir)
    store = RollStoreData()
    if not path.exists():
        return store

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("roll_storage: positions.json 损坏，降级为空 store：%s", e)
        return store

    if not isinstance(raw, dict):
        return store

    for pid, data in (raw.get("positions") or {}).items():
        try:
            store.positions[pid] = UserPosition(**data)
        except Exception as e:   # noqa: BLE001
            logger.warning("roll_storage: 跳过损坏的 position=%s: %s", pid, e)

    for plan_id, data in (raw.get("plans") or {}).items():
        try:
            store.plans[plan_id] = RollPlan(**data)
        except Exception as e:
            logger.warning("roll_storage: 跳过损坏的 plan=%s: %s", plan_id, e)

    return store


def save_store(data_dir: str, store: RollStoreData) -> None:
    """原子写入整个 store。"""
    payload = {
        "version": 1,
        "updated_at": int(time.time()),
        "positions": {pid: p.model_dump() for pid, p in store.positions.items()},
        "plans": {plan_id: p.model_dump() for plan_id, p in store.plans.items()},
    }
    _atomic_write_json(positions_path(data_dir), payload)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Events（追加写 JSONL，便于回放与审计）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def append_event(data_dir: str, position_id: str, event: RollEvent) -> None:
    """追加写入一条事件。

    JSONL 格式：每行 = {"position_id": "...", "event": {...RollEvent...}}
    """
    path = events_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"position_id": position_id, "event": event.model_dump()},
        ensure_ascii=False,
    )
    # 追加写入；即便中途失败也只会丢失最后一行（JSONL 天然容错）
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_events(
    data_dir: str,
    position_id: Optional[str] = None,
) -> list[tuple[str, RollEvent]]:
    """读取事件历史。

    Args:
        position_id: 指定时只返回该 position 的事件；None 返回全部

    Returns:
        [(position_id, RollEvent), ...] 按文件顺序（时间升序）
    """
    path = events_path(data_dir)
    if not path.exists():
        return []

    results: list[tuple[str, RollEvent]] = []
    with open(path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                pid = entry["position_id"]
                if position_id is not None and pid != position_id:
                    continue
                event = RollEvent(**entry["event"])
                results.append((pid, event))
            except Exception as e:
                logger.warning("roll_storage: events.jsonl 第 %d 行损坏：%s", idx, e)

    return results


def rebuild_position_events(data_dir: str, store: RollStoreData) -> None:
    """根据 events.jsonl 回填 each UserPosition.events（冷启动用）。

    调用时机：main.py 启动时先 load_store，再调用本函数补全内存中 events 字段。
    """
    all_events = load_events(data_dir)
    by_pos: dict[str, list[RollEvent]] = {}
    for pid, ev in all_events:
        by_pos.setdefault(pid, []).append(ev)

    for pid, events in by_pos.items():
        pos = store.positions.get(pid)
        if pos is None:
            continue
        pos.events = events


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Global Settings
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_settings(data_dir: str) -> RollGlobalSettings:
    path = settings_path(data_dir)
    if not path.exists():
        return RollGlobalSettings()

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return RollGlobalSettings(**raw)
    except (json.JSONDecodeError, OSError, Exception) as e:
        logger.warning("roll_storage: settings.json 损坏，使用默认配置：%s", e)
        return RollGlobalSettings()


def save_settings(data_dir: str, settings: RollGlobalSettings) -> None:
    settings_with_ts = settings.model_copy(update={"updated_at": int(time.time())})
    _atomic_write_json(settings_path(data_dir), settings_with_ts.model_dump())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 便捷工具：统一初始化（主引擎启动时调用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def bootstrap(data_dir: str) -> tuple[RollStoreData, RollGlobalSettings]:
    """启动时调用：
      - 创建 roll/ 目录（若不存在）
      - 加载 positions + plans，回填 events
      - 加载 global settings（若无则默认）
    """
    roll_dir(data_dir).mkdir(parents=True, exist_ok=True)
    store = load_store(data_dir)
    rebuild_position_events(data_dir, store)
    settings = load_settings(data_dir)
    return store, settings
