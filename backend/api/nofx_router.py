"""NOFX 外部 AI 决策接口路由（/api/nofx/*）。

设计原则（与 dev-constraints.mdc 对齐）：
  - 零侵入：纯读内存 Engine._states，不触发任何外部请求 / processor 重算
  - 防呆保护：30s 字节级响应缓存 + IP 令牌桶限频
  - Schema 稳定：字段只加不改，版本通过 nofx_builder.SCHEMA_VERSION 暴露
  - 失败隔离：任一字段缺失 → 值为 None / [] / 0，永不抛 500

端点：
  - GET /api/nofx/coins             · 支持的币种列表
  - GET /api/nofx/health            · 接口 + 上游数据源健康
  - GET /api/nofx/snapshot/{coin}   · 主接口（NOFX 3min/次）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from api.nofx_builder import SCHEMA_VERSION, build_nofx_snapshot
from config.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nofx")

_engine = None


def set_engine(engine) -> None:
    """由 main.py 注入 Engine 实例（与 api/routes.py 一致约定）。"""
    global _engine
    _engine = engine


# ── 响应字节缓存（按 coin 粒度） ──────────────────────────────
# 目的：NOFX 3 分钟/次，但同一时刻可能有多个消费方并发；缓存让重复请求
#       共享同一份 model_dump + json.dumps 结果，CPU 近零。
class _BytesCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # coin -> (expire_ts, bytes_payload, etag)
        self._store: dict[str, tuple[float, bytes, str]] = {}

    def get(self, key: str) -> Optional[tuple[bytes, str]]:
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expire_ts, payload, etag = item
            if expire_ts < time.time():
                self._store.pop(key, None)
                return None
            return payload, etag

    def set(self, key: str, payload: bytes, ttl_sec: int, etag: str) -> None:
        if ttl_sec <= 0:
            return
        with self._lock:
            self._store[key] = (time.time() + ttl_sec, payload, etag)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_CACHE = _BytesCache()


# ── IP 令牌桶限频 ──────────────────────────────────────────
class _RateLimiter:
    """滑动窗口：每 IP 每分钟最多 N 次请求。"""

    def __init__(self, window_sec: int = 60) -> None:
        self._window = window_sec
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, per_min: int) -> tuple[bool, int]:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= per_min:
                # retry_after 秒：队首消费后就能再请求
                retry_after = max(1, int(self._window - (now - dq[0])))
                return False, retry_after
            dq.append(now)
            return True, 0


_LIMITER = _RateLimiter()


# ── 工具 ──────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """优先从 X-Forwarded-For / X-Real-IP 解析（反向代理后常用），否则用 socket 地址。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip", "")
    if xri:
        return xri.strip()
    client = request.client
    return client.host if client else "unknown"


def _json_response(data: Any, ttl_sec: int, etag: Optional[str] = None) -> Response:
    """统一 JSON 响应：UTF-8 字节、禁止压缩破坏契约、带 ETag / Cache-Control。"""
    if isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    else:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "X-NOFX-Schema-Version": SCHEMA_VERSION,
    }
    if ttl_sec > 0:
        headers["Cache-Control"] = f"public, max-age={ttl_sec}"
    if etag:
        headers["ETag"] = etag
    return Response(
        content=body, media_type="application/json; charset=utf-8", headers=headers,
    )


def _check_enabled_and_rate_limit(request: Request) -> None:
    """统一的前置校验：接口开关 + IP 限频。"""
    cfg = get_settings().nofx
    if not cfg.enabled:
        raise HTTPException(503, "NOFX interface disabled")
    ip = _client_ip(request)
    ok, retry_after = _LIMITER.allow(ip, cfg.rate_limit_per_min)
    if not ok:
        logger.info("[NOFX] rate limit hit | ip=%s retry_after=%ds", ip, retry_after)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {cfg.rate_limit_per_min}/min",
            headers={"Retry-After": str(retry_after)},
        )


# ── 端点 ──────────────────────────────────────────────────

@router.get("/coins")
async def nofx_coins(request: Request):
    """支持的币种列表（NOFX 端启动时调一次即可）。"""
    _check_enabled_and_rate_limit(request)
    cfg = get_settings().nofx
    coins_conf = get_settings().coins
    items = []
    for ccy in cfg.allow_coins:
        if ccy not in coins_conf:
            continue
        c = coins_conf[ccy]
        items.append({
            "coin": ccy,
            "symbol": c.symbol_cg_pair,
            "exchange_primary": c.exchange_primary,
        })
    return _json_response(
        {
            "schema_version": SCHEMA_VERSION,
            "coins": items,
            "default_coin": get_settings().default_coin if get_settings().default_coin in cfg.allow_coins else (items[0]["coin"] if items else ""),
        },
        ttl_sec=300,
    )


@router.get("/health")
async def nofx_health(request: Request):
    """接口 + 上游数据源健康（供 NOFX 自检 / 告警）。"""
    _check_enabled_and_rate_limit(request)
    if _engine is None:
        return _json_response(
            {"schema_version": SCHEMA_VERSION, "status": "starting", "sources": []},
            ttl_sec=0,
        )
    try:
        sources = _engine.get_source_health()
    except Exception as e:
        logger.warning("[NOFX] health aggregate failed: %s", e)
        sources = []
    coins_ready = {}
    try:
        for ccy, st in _engine._states.items():
            coins_ready[ccy] = bool(getattr(st, "ticker", None))
    except Exception:
        pass
    return _json_response(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "running",
            "ts": int(time.time()),
            "coins_ready": coins_ready,
            "sources": sources,
        },
        ttl_sec=10,
    )


@router.get("/snapshot/{coin}")
async def nofx_snapshot(coin: str, request: Request):
    """主接口：返回指定币种的完整原始行情快照。

    - 请求哪个币种就返回哪个币种（不支持一次多币）
    - 30s 字节级缓存：并发请求共享同一份序列化结果
    - 失败返回 `{ready:false, reason:...}` 而非 500
    """
    _check_enabled_and_rate_limit(request)

    cfg = get_settings().nofx
    ccy = (coin or "").upper().strip()
    if ccy not in cfg.allow_coins:
        raise HTTPException(400, f"Unsupported coin: {ccy}. Allowed: {cfg.allow_coins}")
    if _engine is None:
        raise HTTPException(503, "Engine not ready")

    # 缓存命中直接返回字节
    cached = _CACHE.get(ccy)
    if cached is not None:
        payload_bytes, etag = cached
        return _json_response(payload_bytes, ttl_sec=cfg.cache_ttl_sec, etag=etag)

    # 组装快照
    state = _engine._states.get(ccy)
    if state is None:
        raise HTTPException(503, f"No state for {ccy}")

    coin_conf = get_settings().coins.get(ccy)
    symbol_pair = coin_conf.symbol_cg_pair if coin_conf else ccy

    try:
        source_health = _engine.get_source_health()
    except Exception:
        source_health = []

    try:
        data = build_nofx_snapshot(
            state=state,
            symbol_pair=symbol_pair,
            candle_limit=cfg.candle_limit,
            source_health=source_health,
        )
    except Exception as e:
        logger.exception("[NOFX] build snapshot failed | coin=%s err=%s", ccy, e)
        raise HTTPException(500, f"snapshot build failed: {type(e).__name__}")

    # 序列化 + 落缓存（字节级）
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    etag = f'W/"{ccy}-{data.get("ts", 0)}-{len(body)}"'
    _CACHE.set(ccy, body, cfg.cache_ttl_sec, etag)
    return _json_response(body, ttl_sec=cfg.cache_ttl_sec, etag=etag)
