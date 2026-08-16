#!/usr/bin/env python3
"""fstream.binance.com 推送诊断（一次性运维脚本）。

背景：服务器上 futures WS（ticker !ticker@arr 与 aggTrade 组合流）出现
「连接成功但 45s 收不到任何推送」反复重连，spot 流正常。
本脚本直连多种 URL 形态各收 N 秒，对比判定是网络层问题还是订阅形态问题。

用法（服务器容器内或宿主机均可，需 aiohttp）：
    cd backend
    python3 scripts/diagnose_fstream.py            # 每个 URL 收 30s
    python3 scripts/diagnose_fstream.py --secs 60

判读：
    - 所有 fstream URL 都 0 消息、spot 正常 → 网络层（服务器到 fstream 的
      推送被中断/劫持），代码无法修复，考虑代理或接受合约侧降级
    - 单流 /ws/ 有消息、组合流 /stream? 没有 → 订阅形态问题，改代码
    - 全部正常 → 问题出在长连接维持（观察运行中进程的连接复用/心跳）
"""
from __future__ import annotations

import argparse
import asyncio
import time

import aiohttp

TARGETS = [
    ("futures 组合流(当前用法)", "wss://fstream.binance.com/stream?streams=btcusdt@aggTrade"),
    ("futures 单流", "wss://fstream.binance.com/ws/btcusdt@aggTrade"),
    ("futures ticker(老代码同病)", "wss://fstream.binance.com/ws/!ticker@arr"),
    ("spot 组合流(对照组)", "wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade"),
]


async def probe(name: str, url: str, secs: int) -> None:
    t0 = time.time()
    msg_count = 0
    first_msg_at = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, heartbeat=20,
                                          timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                connect_ms = (time.time() - t0) * 1000
                deadline = time.time() + secs
                while time.time() < deadline:
                    try:
                        msg = await asyncio.wait_for(
                            ws.receive(), timeout=max(deadline - time.time(), 0.1),
                        )
                    except asyncio.TimeoutError:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        msg_count += 1
                        if first_msg_at is None:
                            first_msg_at = time.time() - t0
                    elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.CLOSING,
                                      aiohttp.WSMsgType.ERROR):
                        print(f"  [{name}] 连接中断: {msg.type}")
                        break
        first = f"{first_msg_at:.1f}s" if first_msg_at is not None else "无"
        rate = msg_count / secs
        verdict = "OK" if msg_count > 0 else "!! 零消息（推送不通）"
        print(f"  [{name}]\n    connect={connect_ms:.0f}ms 首包={first} "
              f"{secs}s 收包={msg_count} ({rate:.1f}/s) → {verdict}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [{name}] 连接失败: {type(exc).__name__}: {exc}")


async def main_async(secs: int) -> None:
    print(f"=== fstream 推送诊断（每目标 {secs}s）===")
    for name, url in TARGETS:
        print(f"\n· {url}")
        await probe(name, url, secs)
    print("\n判读指引见脚本头部注释。")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--secs", type=int, default=30)
    args = p.parse_args()
    asyncio.run(main_async(args.secs))


if __name__ == "__main__":
    main()
