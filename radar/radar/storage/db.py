"""数据库连接与单写入协程。

严格纪律（代码 Review 必查项）：
  - **全库只有一个写入者**。所有 collector / scorer / tracker 的写入
    都必须经过有界队列交给 writer 协程串行执行，否则高频并发写
    SQLite 迟早出现 "database is locked"。
  - **读走独立连接**。WAL 模式下读写互不阻塞；读操作在线程池执行，
    不占用事件循环。
  - 队列满时丢弃可丢弃写入（如普通快照）并发出 DB_QUEUE_HIGH，
    绝不阻塞采集主循环；不可丢弃写入（警报/事件/Outcome）优先保留。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..obs.events import EventType, bus
from ..obs.metrics import metrics
from .schema import PRAGMA_SQL, SCHEMA_SQL, SCHEMA_VERSION

logger = logging.getLogger("radar.db")

# 写入优先级：0 = 绝不丢弃，1 = 队列紧张时可丢弃
PRIORITY_CRITICAL = 0
PRIORITY_DROPPABLE = 1


@dataclass
class WriteOp:
    sql: str
    params: tuple[Any, ...] = ()
    priority: int = PRIORITY_CRITICAL
    future: asyncio.Future[int] | None = None
    label: str = ""


class Database:
    """SQLite 封装。写入串行化，读取并发。"""

    def __init__(self, path: Path, *, queue_size: int = 5000,
                 batch_size: int = 200, flush_interval_sec: float = 2.0,
                 busy_timeout_ms: int = 8000, read_only: bool = False) -> None:
        self.path = path
        # 只读模式给回测和诊断工具用：连接以 mode=ro 打开，
        # 任何写入在 SQLite 层就会被拒绝。靠"约定不写"是不够的——
        # 一次误写生产库造成的污染无法回滚
        self.read_only = read_only
        self._queue: asyncio.Queue[WriteOp | None] = asyncio.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._flush_interval = flush_interval_sec
        self._busy_timeout_ms = busy_timeout_ms
        self._write_conn: sqlite3.Connection | None = None
        self._local = threading.local()
        self._writer_task: asyncio.Task[None] | None = None
        self._running = False
        self._dropped = 0

    # ── 生命周期 ────────────────────────────────────────────────────────
    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect(readonly=False)
        for pragma in PRAGMA_SQL:
            conn.execute(pragma)
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        self._write_conn = conn
        logger.info("数据库就绪 | %s | schema v%s", self.path, SCHEMA_VERSION)

    def _connect(self, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            uri = f"file:{self.path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
        else:
            conn = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return conn

    def _read_conn(self) -> sqlite3.Connection:
        """每线程一个只读连接（sqlite 连接不宜跨线程共享）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect(readonly=True)
            for pragma in ("PRAGMA temp_store=MEMORY", "PRAGMA cache_size=-4000"):
                conn.execute(pragma)
            self._local.conn = conn
        return conn

    async def start(self) -> None:
        if self.read_only:
            if not self.path.exists():
                raise FileNotFoundError(f"只读数据库不存在: {self.path}")
            self._running = True
            return
        if self._write_conn is None:
            self.init_schema()
        self._running = True
        self._writer_task = asyncio.create_task(self._writer_loop(), name="db_writer")

    async def stop(self) -> None:
        """优雅停机：让 writer 把队列排空后再关闭连接。"""
        if not self._running:
            return
        if self.read_only:
            self._running = False
            self._close_read_conn()
            return
        self._running = False
        await self._queue.put(None)
        if self._writer_task is not None:
            try:
                await asyncio.wait_for(self._writer_task, timeout=20)
            except asyncio.TimeoutError:
                logger.warning("writer 停机超时，仍有未落盘写入")
                self._writer_task.cancel()
        if self._write_conn is not None:
            try:
                self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._write_conn.commit()
            except sqlite3.Error:
                logger.warning("停机 checkpoint 失败", exc_info=True)
            self._write_conn.close()
            self._write_conn = None

    def _close_read_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── 写入 API ────────────────────────────────────────────────────────
    def submit(self, sql: str, params: Sequence[Any] = (), *,
               priority: int = PRIORITY_CRITICAL, label: str = "") -> bool:
        """投递一次写入（不等待落盘）。返回是否成功入队。"""
        if self.read_only:
            raise RuntimeError(f"只读数据库拒绝写入: {label or sql[:40]}")
        op = WriteOp(sql=sql, params=tuple(params), priority=priority, label=label)
        try:
            self._queue.put_nowait(op)
        except asyncio.QueueFull:
            self._dropped += 1
            metrics.incr("db_writes_dropped")
            if priority == PRIORITY_CRITICAL:
                # 关键写入被丢弃属于严重问题，必须显式暴露
                logger.error("写队列已满，关键写入被丢弃 | %s", label or sql[:60])
                bus.emit(
                    EventType.DB_WRITE_FAILED,
                    module="storage",
                    summary=f"写队列满，关键写入丢弃: {label or sql[:40]}",
                    payload={"dropped_total": self._dropped, "label": label},
                )
            else:
                bus.emit(
                    EventType.DB_QUEUE_HIGH,
                    module="storage",
                    summary=f"写队列满，丢弃可丢弃写入: {label}",
                    payload={"dropped_total": self._dropped},
                )
            return False
        metrics.gauge("db_queue_depth", self._queue.qsize())
        return True

    async def submit_returning(self, sql: str, params: Sequence[Any] = (),
                               *, label: str = "") -> int:
        """投递写入并等待返回自增主键（用于需要 id 的插入）。"""
        if self.read_only:
            raise RuntimeError(f"只读数据库拒绝写入: {label or sql[:40]}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[int] = loop.create_future()
        op = WriteOp(
            sql=sql, params=tuple(params), priority=PRIORITY_CRITICAL,
            future=future, label=label,
        )
        await self._queue.put(op)
        metrics.gauge("db_queue_depth", self._queue.qsize())
        return await future

    async def drain(self) -> None:
        """等待此刻之前投递的所有写入落盘。

        写入是批量异步提交的，因此"刚 submit 完就去读"读不到数据。
        备份前、生成诊断包前、以及测试里都必须先排空队列。
        实现方式是投递一个屏障：队列是 FIFO，屏障的 future 完成
        意味着它之前的写入都已在同一批或更早的批次里提交。
        """
        if not self._running or self.read_only:
            return
        await self.submit_returning("SELECT 1", label="drain_barrier")

    # ── 读取 API ────────────────────────────────────────────────────────
    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._fetch_all_sync, sql, tuple(params))

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = await asyncio.to_thread(self._fetch_all_sync, sql, tuple(params), 1)
        return rows[0] if rows else None

    def _fetch_all_sync(self, sql: str, params: tuple[Any, ...],
                        limit: int | None = None) -> list[dict[str, Any]]:
        conn = self._read_conn()
        try:
            cursor = conn.execute(sql, params)
            rows = cursor.fetchmany(limit) if limit else cursor.fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error("查询失败 | %s | %s", sql[:120], exc)
            raise

    def fetch_all_sync(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """同步读取，供 CLI（replay / 备份）使用。"""
        return self._fetch_all_sync(sql, tuple(params))

    # ── writer 主循环 ───────────────────────────────────────────────────
    async def _writer_loop(self) -> None:
        logger.info("DB writer 启动")
        pending: list[WriteOp] = []
        last_flush = time.monotonic()
        stopping = False

        while not stopping or pending:
            timeout = max(0.05, self._flush_interval - (time.monotonic() - last_flush))
            try:
                op = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                if op is None:
                    stopping = True
                else:
                    pending.append(op)
                    # 继续贪心取，尽量批量提交
                    while len(pending) < self._batch_size:
                        try:
                            nxt = self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if nxt is None:
                            stopping = True
                            break
                        pending.append(nxt)
            except asyncio.TimeoutError:
                pass

            should_flush = (
                len(pending) >= self._batch_size
                or (time.monotonic() - last_flush) >= self._flush_interval
                or stopping
                # 有调用方正在 await 结果时立刻提交。否则每个需要返回主键的
                # 写入（建档、警报入库）都要白等满一个 flush 周期；
                # 一轮采集里发现几十个新币时，这些等待会串行累加成分钟级延迟。
                or any(op.future is not None for op in pending)
            )
            if pending and should_flush:
                # 在线程池执行：一批插入可能耗时数十毫秒，
                # 直接在事件循环里跑会饿死采集器与 API（容器仅 0.6 核）。
                # writer 串行 await，因此写连接不会被并发访问。
                await asyncio.to_thread(self._flush, pending)
                pending = []
                last_flush = time.monotonic()
                metrics.gauge("db_queue_depth", self._queue.qsize())

            if not self._running and self._queue.empty() and not pending:
                stopping = True

        logger.info("DB writer 退出")

    def _flush(self, ops: list[WriteOp]) -> None:
        conn = self._write_conn
        if conn is None:
            for op in ops:
                if op.future and not op.future.done():
                    op.future.get_loop().call_soon_threadsafe(
                        op.future.set_exception, RuntimeError("数据库未初始化")
                    )
            return

        started = time.perf_counter()
        results: list[tuple[WriteOp, int | None, Exception | None]] = []
        try:
            conn.execute("BEGIN")
            for op in ops:
                try:
                    cursor = conn.execute(op.sql, op.params)
                    results.append((op, cursor.lastrowid, None))
                except sqlite3.Error as exc:
                    # 单条失败不能拖垮整批：记录后继续
                    results.append((op, None, exc))
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            logger.error("批量写入提交失败 | %s 条", len(ops), exc_info=True)
            bus.emit(
                EventType.DB_WRITE_FAILED,
                module="storage",
                summary=f"批量提交失败: {type(exc).__name__}",
                payload={"batch_size": len(ops), "error": str(exc)[:300]},
            )
            for op in ops:
                self._reject_future(op, exc)
            return

        latency_ms = (time.perf_counter() - started) * 1000
        metrics.db_write_latencies_ms.append(latency_ms)
        metrics.incr("db_writes", len(ops))

        failures = 0
        for op, rowid, exc in results:
            if exc is not None:
                failures += 1
                # UNIQUE 冲突是正常的幂等保护（如重复里程碑），降级为 debug
                if "UNIQUE constraint failed" in str(exc):
                    logger.debug("写入被唯一约束拒绝 | %s", op.label or op.sql[:60])
                else:
                    logger.error("写入失败 | %s | %s", op.label or op.sql[:60], exc)
                self._reject_future(op, exc)
            elif op.future is not None and not op.future.done():
                op.future.get_loop().call_soon_threadsafe(
                    op.future.set_result, int(rowid or 0)
                )

        if failures:
            metrics.incr("db_write_errors", failures)

    @staticmethod
    def _reject_future(op: WriteOp, exc: Exception) -> None:
        if op.future is not None and not op.future.done():
            op.future.get_loop().call_soon_threadsafe(op.future.set_exception, exc)

    # ── 维护 ────────────────────────────────────────────────────────────
    def db_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def wal_size_bytes(self) -> int:
        p = Path(str(self.path) + "-wal")
        return p.stat().st_size if p.exists() else 0

    def checkpoint(self) -> None:
        if self._write_conn is not None:
            try:
                self._write_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                logger.warning("WAL checkpoint 失败", exc_info=True)

    def backup_to(self, dest: Path) -> None:
        """在线备份。

        必须用 sqlite 的 backup API（等价于 VACUUM INTO），
        绝不能直接 cp 正在写入的库文件——那样极可能拷到损坏的中间状态。
        """
        if self._write_conn is None:
            raise RuntimeError("数据库未初始化")
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            self._write_conn.backup(target)
        finally:
            target.close()


def json_dump(value: Any) -> str | None:
    """统一的 JSON 序列化（数据库列用）。None 直接透传，保持 NULL 语义。"""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def json_load(raw: Any) -> Any:
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
