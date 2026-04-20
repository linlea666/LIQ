"""新闻源注册表实现（D07）

职责：
  - 启动时由 config/news_sources.yml 驱动注册所有 NewsSource 实例
  - 向上层（news_agent_loop）提供 `fetch_all(since_ts)` 聚合接口
  - 跨源去重（按 dedupe_key）

落实日志锚点：
  - D.D07_NEWS_SOURCES：
      * 注册成功打点 status=ok 上报 sources_registered
      * 每次 fetch_all 完成打点 items=total dedupe_dropped=x errors=y
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Optional

import yaml

from models.news_event import RawNewsItem
from sources.news.base import NewsSource
from sources.news.okx import (
    OkxTimelineSource,
    create_industry_source,
    create_kol_source,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部状态（单例字典 + 锁）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_sources: dict[str, NewsSource] = {}
_lock = threading.RLock()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 注册表 API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def register(name: str, source: NewsSource) -> None:
    """注册一个新闻源实例。重名覆盖（允许 hot-reload）"""
    if not name:
        raise ValueError("news source name must be non-empty")
    with _lock:
        _sources[name] = source
    logger.info("[D07] registered news source | name=%s type=%s", name, source.source_type)


def unregister(name: str) -> None:
    """注销一个新闻源"""
    with _lock:
        _sources.pop(name, None)


def get(name: str) -> Optional[NewsSource]:
    """按名字获取单个源"""
    with _lock:
        return _sources.get(name)


def get_all() -> list[NewsSource]:
    """获取所有已注册的源（只读快照）"""
    with _lock:
        return list(_sources.values())


def clear() -> None:
    """清空注册表（测试用）"""
    with _lock:
        _sources.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 聚合拉取
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def fetch_all(since_ts: Optional[int] = None) -> list[RawNewsItem]:
    """并发拉取所有已注册源，聚合+去重后返回按时间升序的条目。

    流程：
      1. asyncio.gather(*source.fetch(since_ts))  — 单源失败不影响其他
      2. 单源内存去重：调用各源的 filter_new()
      3. 跨源去重：按 (title 归一化 + publish_time 分钟级) 识别多源报道同一事件
      4. 排序（按 publish_time 升序）
      5. 通过 DecisionTracker 上报 D07 metrics

    错误处理：
      - 单源异常被捕获并记 error_count，不抛
      - 所有源都失败 → 返回 []
    """
    sources = get_all()
    if not sources:
        _mark_d07(status="warn", items=0, dedupe_dropped=0, errors=0,
                  sources_registered=0, reason="no_sources_registered", log=False)
        return []

    tasks = [asyncio.create_task(_safe_fetch(s, since_ts)) for s in sources]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    all_items: list[RawNewsItem] = []
    errors = 0
    per_source: dict[str, int] = {}
    for src, (items, err) in zip(sources, results):
        if err:
            errors += 1
            per_source[src.name] = 0
            continue
        fresh = src.filter_new(items)
        per_source[src.name] = len(fresh)
        all_items.extend(fresh)

    unique, dropped = deduplicate_cross_source(all_items)
    unique.sort(key=lambda it: it.publish_time)

    _mark_d07(
        status="ok" if errors == 0 else ("warn" if errors < len(sources) else "failed"),
        items=len(unique),
        dedupe_dropped=dropped,
        errors=errors,
        sources_registered=len(sources),
        per_source=per_source,
        log=True,
    )
    return unique


async def _safe_fetch(
    source: NewsSource,
    since_ts: Optional[int],
) -> tuple[list[RawNewsItem], bool]:
    """(items, has_error)。捕获单源异常避免影响其他源"""
    try:
        items = await source.fetch(since_ts=since_ts)
        if not isinstance(items, list):
            logger.debug("[D07] %s returned non-list: %s", source.name, type(items))
            return [], True
        return items, False
    except Exception:  # noqa: BLE001
        logger.warning("[D07] source %s fetch raised", source.name, exc_info=True)
        return [], True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 跨源去重
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_NORM_RE = re.compile(r"[\s\-_—·•\.,。，；：!?!?()（）\[\]【】\"'“”‘’]+")


def _normalize_title(title: str) -> str:
    """标题归一化：去空白/标点/大小写，用于跨源同一事件识别"""
    if not title:
        return ""
    t = title.strip().lower()
    t = _NORM_RE.sub("", t)
    return t


def deduplicate_cross_source(
    items: list[RawNewsItem],
) -> tuple[list[RawNewsItem], int]:
    """跨源去重。

    规则：
      - key = (normalized_title, minute_bucket) — 同一分钟同一标题视为重复
      - 同一事件保留 source_reliability 最高的一条；相同则保留 publish_time 最早的
    返回：
      (unique_items, dropped_count)
    """
    if not items:
        return [], 0

    # key -> 选中条目
    chosen: dict[tuple[str, int], RawNewsItem] = {}
    dropped = 0

    for it in items:
        key_title = _normalize_title(it.title)
        if not key_title:
            # 无标题不做跨源去重（防止空串大批误判）
            chosen[(f"__id:{it.source_type}:{it.external_id}", 0)] = it
            continue
        bucket = (it.publish_time // 60_000) if it.publish_time > 0 else 0
        key = (key_title, int(bucket))

        prev = chosen.get(key)
        if prev is None:
            chosen[key] = it
            continue

        # 冲突 → 比较 source_reliability
        if it.source_reliability > prev.source_reliability:
            chosen[key] = it
        elif (
            it.source_reliability == prev.source_reliability
            and it.publish_time > 0
            and prev.publish_time > 0
            and it.publish_time < prev.publish_time
        ):
            chosen[key] = it
        dropped += 1

    return list(chosen.values()), dropped


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配置加载
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_from_yaml(path: Optional[str] = None) -> int:
    """从 config/news_sources.yml 读取并注册所有源。

    YAML 示例：
      sources:
        - type: okx
          name: okx_industry
          query_name: "822539543374725120"
          reliability: 0.85
          poll_interval_min: 15
        - type: okx
          name: okx_kol
          query_name: "789451205264277506"
          reliability: 0.75
          poll_interval_min: 30

    若 path 为 None 或文件不存在：回退到硬编码两个默认源（行业 + 博主）。
    返回：成功注册条数
    """
    sources_cfg: list[dict] = []
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            raw_sources = data.get("sources") or []
            if isinstance(raw_sources, list):
                sources_cfg = [s for s in raw_sources if isinstance(s, dict)]
        except Exception as e:  # noqa: BLE001
            logger.warning("[D07] load_from_yaml parse failed %s: %s", path, e)

    count = 0
    if sources_cfg:
        for cfg in sources_cfg:
            stype = str(cfg.get("type") or "").lower()
            name = str(cfg.get("name") or "").strip()
            if stype == "okx":
                try:
                    src = OkxTimelineSource(
                        name=name or "okx",
                        query_name=str(cfg.get("query_name") or "").strip(),
                        size=int(cfg.get("size") or 50),
                        source_author=str(cfg.get("source_author") or "OKX"),
                        source_reliability=float(cfg.get("reliability") or 0.8),
                        poll_interval_min=int(cfg.get("poll_interval_min") or 15),
                        timeout_sec=int(cfg.get("timeout_sec") or 10),
                    )
                    register(src.name, src)
                    count += 1
                except Exception:  # noqa: BLE001
                    logger.warning("[D07] bad yaml entry %s", cfg, exc_info=True)
            else:
                logger.warning("[D07] unknown news source type=%s (skipped)", stype)
    else:
        # 回退：默认两个 OKX 源
        register("okx_industry", create_industry_source())
        register("okx_kol", create_kol_source())
        count = 2

    _mark_d07(
        status="ok",
        items=0,
        dedupe_dropped=0,
        errors=0,
        sources_registered=count,
        log=True,
    )
    return count


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Decision Tracker 封装
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _mark_d07(
    *,
    status: str,
    items: int,
    dedupe_dropped: int,
    errors: int,
    sources_registered: int,
    per_source: Optional[dict] = None,
    reason: str = "",
    log: bool = False,
) -> None:
    try:
        from utils.decision_tracker import D, get_tracker
        get_tracker().mark(
            D.D07_NEWS_SOURCES,
            status=status,
            log=log,
            items=items,
            dedupe_dropped=dedupe_dropped,
            errors=errors,
            sources_registered=sources_registered,
            per_source=per_source or {},
            reason=reason,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[D07] tracker mark failed", exc_info=True)
