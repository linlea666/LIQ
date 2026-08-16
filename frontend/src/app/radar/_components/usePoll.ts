"use client";

/**
 * 轮询 hook。
 *
 * 雷达没有 WebSocket（采集周期是 15-60 秒级，推送带来的复杂度换不到价值），
 * 所以所有页面都靠轮询。这里统一处理三件每个页面都会写错的事：
 *
 *   1. **卸载后不再 setState**——页面切走时未完成的请求回来会报警告；
 *   2. **错误不清空旧数据**——一次网络抖动就把满屏内容清空，
 *      比显示"数据是 30 秒前的"糟糕得多；
 *   3. **失败要看得见**——静默失败的雷达会显示陈旧数据，
 *      让人以为"最近没有新币"，而实际上采集早就停了。
 */

import { useCallback, useEffect, useRef, useState } from "react";

let nextRequestId = 0;

export interface PollState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** 数据获取成功的时刻。界面必须显示它，否则无法判断内容有多陈旧。 */
  updatedAt: number | null;
  refresh: () => void;
}

export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = [],
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);

  const alive = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  // 只接受迄今为止最新一次请求的结果。没有这道闸，一个慢请求
  // 可能在更新的请求之后才返回，用旧数据覆盖新数据并把 updatedAt
  // 刷成"刚刚更新"——恰好是这个 hook 存在要防的那种谎报
  const latestAccepted = useRef(-1);

  const run = useCallback(async () => {
    const requestId = nextRequestId++;
    try {
      const result = await fetcherRef.current();
      if (!alive.current || requestId < latestAccepted.current) return;
      latestAccepted.current = requestId;
      setData(result);
      setError(null);
      setUpdatedAt(Date.now());
    } catch (err) {
      if (!alive.current || requestId < latestAccepted.current) return;
      latestAccepted.current = requestId;
      // 刻意保留 data：显示"30 秒前的数据 + 错误提示"
      // 远比清空整屏更有用
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    setLoading(true);
    run();
    const timer = setInterval(run, intervalMs);
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, intervalMs, ...deps]);

  return { data, error, loading, updatedAt, refresh: run };
}
