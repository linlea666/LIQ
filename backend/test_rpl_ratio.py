"""
独立测试脚本：从 checkonchain.com 提取 Realised Profit/Loss Ratio (all holders)
用法: python test_rpl_ratio.py
"""

import asyncio
import base64
import json
import re
import struct
import logging

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

URL = "https://charts.checkonchain.com/btconchain/realised/realisedpnl_ratio_all/realisedpnl_ratio_all_light.html"


def decode_bdata(bdata_str: str, dtype: str = "f8") -> list[float]:
    """解码 Plotly bdata (base64 编码的二进制数组) 为 float 列表"""
    raw = base64.b64decode(bdata_str)
    if dtype == "f8":
        n = len(raw) // 8
        return list(struct.unpack(f"<{n}d", raw))
    raise ValueError(f"Unsupported dtype: {dtype}")


def extract_plotly_traces(html: str) -> list[dict]:
    """从 HTML 中提取 Plotly.newPlot 的 traces 数据"""
    # Plotly.newPlot("id", [traces], layout, config)
    # 匹配 Plotly.newPlot( 后面的第二个参数（traces 数组）
    match = re.search(
        r'Plotly\.newPlot\(\s*"[^"]+"\s*,\s*(\[.*?\])\s*,\s*\{',
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("无法在 HTML 中找到 Plotly.newPlot 调用")

    traces_str = match.group(1)

    # Plotly 用 \u002f 转义 /，Python json 模块可直接处理
    traces = json.loads(traces_str)
    return traces


def process_trace(trace: dict) -> dict:
    """处理单条 trace：解码 bdata y 值，保留原始 x 日期"""
    name = trace.get("name", "unknown")
    x_dates = trace.get("x", [])

    y_raw = trace.get("y", {})
    if isinstance(y_raw, dict) and "bdata" in y_raw:
        dtype = y_raw.get("dtype", "f8")
        y_values = decode_bdata(y_raw["bdata"], dtype)
    elif isinstance(y_raw, list):
        y_values = y_raw
    else:
        raise ValueError(f"Trace '{name}': 无法识别 y 数据格式")

    return {
        "name": name,
        "x_dates": x_dates,
        "y_values": y_values,
        "length": len(y_values),
    }


async def fetch_rpl_ratio() -> dict:
    """抓取并解析 RPL Ratio 数据，返回最新值"""
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as session:
        logger.info("正在请求 %s ...", URL)
        async with session.get(URL) as resp:
            resp.raise_for_status()
            html = await resp.text()
            logger.info("HTML 获取成功，长度 %d 字符", len(html))

    traces = extract_plotly_traces(html)
    logger.info("提取到 %d 条 traces", len(traces))

    results = {}
    for i, trace in enumerate(traces):
        processed = process_trace(trace)
        results[processed["name"]] = processed
        logger.info(
            "Trace %d [%s]: %d 个数据点, 日期范围 %s ~ %s",
            i,
            processed["name"],
            processed["length"],
            processed["x_dates"][0] if processed["x_dates"] else "N/A",
            processed["x_dates"][-1] if processed["x_dates"] else "N/A",
        )

    # 提取最新的 RPL Ratio
    rpl_pos = results.get("Realised Profit/Loss Ratio (+ve)")
    rpl_neg = results.get("Realised Profit/Loss Ratio (-ve)")
    price_trace = results.get("Price")

    if not rpl_pos or not rpl_neg:
        raise ValueError("缺少 RPL Ratio traces")

    # 从末尾往前找第一个非 NaN 值
    import math

    latest_date = None
    latest_ratio = None

    for idx in range(len(rpl_pos["y_values"]) - 1, -1, -1):
        pos_val = rpl_pos["y_values"][idx]
        neg_val = rpl_neg["y_values"][idx]

        # +ve trace 存正值, -ve trace 存负值, 两者互补 (一个有值另一个为0/NaN)
        if not math.isnan(pos_val) and pos_val != 0:
            latest_ratio = pos_val
            latest_date = rpl_pos["x_dates"][idx]
            break
        elif not math.isnan(neg_val) and neg_val != 0:
            latest_ratio = neg_val
            latest_date = rpl_neg["x_dates"][idx]
            break

    latest_price = None
    if price_trace:
        for idx in range(len(price_trace["y_values"]) - 1, -1, -1):
            val = price_trace["y_values"][idx]
            if not math.isnan(val):
                latest_price = val
                break

    return {
        "date": latest_date,
        "rpl_ratio": latest_ratio,
        "btc_price": latest_price,
        "total_points": rpl_pos["length"],
    }


async def main():
    print("=" * 60)
    print("测试: CheckOnChain RPL Ratio 数据提取")
    print("=" * 60)

    try:
        result = await fetch_rpl_ratio()
        print()
        print(f"  最新日期:     {result['date']}")
        print(f"  RPL Ratio:    {result['rpl_ratio']:.4f}" if result['rpl_ratio'] else "  RPL Ratio:    N/A")
        print(f"  BTC Price:    ${result['btc_price']:,.2f}" if result['btc_price'] else "  BTC Price:    N/A")
        print(f"  总数据点:     {result['total_points']}")
        print()

        # 展示最近 5 天的数据
        print("最近 5 天 RPL Ratio:")
        print("-" * 40)

        html_text = None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(URL) as resp:
                html_text = await resp.text()

        traces = extract_plotly_traces(html_text)
        import math
        for trace in traces:
            if "Ratio" not in trace.get("name", ""):
                continue
            processed = process_trace(trace)
            name = processed["name"]
            dates = processed["x_dates"]
            values = processed["y_values"]
            for idx in range(-5, 0):
                d = dates[idx]
                v = values[idx]
                v_str = f"{v:.4f}" if not math.isnan(v) else "NaN"
                print(f"  {d[:10]}  {name}: {v_str}")
            print()

        print("测试通过！数据可正常提取。")

    except Exception as e:
        logger.error("测试失败: %s", e, exc_info=True)
        print(f"\n测试失败: {e}")


if __name__ == "__main__":
    asyncio.run(main())
