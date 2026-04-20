"""P0-2 · Funding Rate 单位取证脚本

目的：取证 Coinglass 返回的 funding_rate 数值 vs Binance 官方接口 vs OKX 官方接口，
     判断 LIQ 系统是否存在 10×（或 100×）的单位缩放错误。

背景：
    backend/polls/derivatives.py L214-221 直接 `float(rate)` 喂下游，
    `> 0.0005 → "多头拥挤"` 阈值假设的是**小数**（如 0.0001 = 0.01%）。
    若 Coinglass 返回的是**百分比**（如 0.01 = 0.01%，即 1e-4 的 100 倍），
    阈值判定会错位两个数量级，出现 -0.81% 实为 -0.081% 的 10× 错误。

使用方式：
    1) 本地有 Coinglass API key 时：`export COINGLASS_API_KEY=xxx` 然后直接跑
       `python3 backend/tools/diag_funding_unit.py`
    2) 如无 API key / 沙箱限制时：
       - 本脚本同时用 httpx 直接打 Binance / OKX 公共接口（无需 key），可独立跑
       - 把终端输出全文贴回给 AI Agent 审核

输出样例：
    === BTC / ETH / SOL 永续合约 funding rate 原始对比 ===
    BTC
      Binance   : raw=0.0001   (0.01%)
      OKX (Perp): raw=0.000123 (0.0123%)
      Coinglass : raw=0.000125 (avg)

    判定：若 Coinglass raw 与 Binance/OKX 在相同数量级（~1e-4） → 解析正确
          若差 10/100 倍 → 存在单位错误

作者：LIQ audit · 只做取证，不改业务代码。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Optional

# 与项目运行时解耦：本脚本允许在没有 LIQ 依赖的裸环境跑，故用 httpx 直打公共 API。
try:
    import httpx
except ImportError as e:  # pragma: no cover
    print("请先 `pip install httpx`", file=sys.stderr)
    raise


SYMBOLS_BINANCE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SYMBOLS_OKX = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
SYMBOLS_COINGLASS = ["BTC", "ETH", "SOL"]


async def fetch_binance_funding(symbol: str) -> Optional[float]:
    """Binance 永续当前 funding rate（小数形式，例如 0.0001 = 0.01%）。"""
    url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(url)
            r.raise_for_status()
            data = r.json()
            raw = data.get("lastFundingRate")
            if raw is None:
                return None
            return float(raw)
    except Exception as e:  # noqa: BLE001
        print(f"[Binance {symbol}] 请求失败: {e}", file=sys.stderr)
        return None


async def fetch_okx_funding(instId: str) -> Optional[float]:
    """OKX 永续当前 funding rate（小数形式，如 0.00012 = 0.012%）。"""
    url = f"https://www.okx.com/api/v5/public/funding-rate?instId={instId}"
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(url)
            r.raise_for_status()
            data = r.json()
            arr = data.get("data") or []
            if not arr:
                return None
            return float(arr[0].get("fundingRate") or 0.0)
    except Exception as e:  # noqa: BLE001
        print(f"[OKX {instId}] 请求失败: {e}", file=sys.stderr)
        return None


async def fetch_coinglass_funding(
    symbol: str, api_key: Optional[str]
) -> tuple[Optional[float], list[tuple[str, float]]]:
    """Coinglass 当前各交易所 funding rate。返回 (avg, [(ex, raw), …])。"""
    if not api_key:
        return None, []
    url = "https://open-api-v4.coinglass.com/api/futures/funding-rate/exchange-list"
    try:
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.get(
                url,
                headers={"accept": "application/json", "CG-API-KEY": api_key},
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("data") or []
            for it in items:
                if str(it.get("symbol", "")).upper() != symbol.upper():
                    continue
                margin_list = it.get("stablecoin_margin_list") or it.get("uMarginList") or []
                by_ex: list[tuple[str, float]] = []
                for ex in margin_list:
                    name = ex.get("exchange") or ex.get("exchangeName") or "?"
                    rate = ex.get("funding_rate") or ex.get("rate")
                    if rate is None:
                        continue
                    try:
                        by_ex.append((name, float(rate)))
                    except (TypeError, ValueError):
                        continue
                avg = (sum(r for _, r in by_ex) / len(by_ex)) if by_ex else None
                return avg, by_ex
            return None, []
    except Exception as e:  # noqa: BLE001
        print(f"[Coinglass {symbol}] 请求失败: {e}", file=sys.stderr)
        return None, []


def _fmt_rate(r: Optional[float]) -> str:
    if r is None:
        return "N/A"
    # 同时展示小数和百分比，便于肉眼判断量级
    return f"raw={r:.8f}  (={r * 100:+.4f}%)"


async def main() -> int:
    api_key = os.getenv("COINGLASS_API_KEY")
    if not api_key:
        print(
            "⚠ COINGLASS_API_KEY 未设置 → 仅打 Binance / OKX 公共接口取证。\n"
            "   请在能访问 Coinglass 的环境设好 key 后重跑以完整判定。\n",
            file=sys.stderr,
        )

    print("═" * 70)
    print("P0-2 · Funding Rate 单位取证 (Binance / OKX 小数形 vs Coinglass)")
    print("═" * 70)

    summary: list[dict[str, Any]] = []

    for bin_sym, okx_sym, cg_sym in zip(
        SYMBOLS_BINANCE, SYMBOLS_OKX, SYMBOLS_COINGLASS, strict=False
    ):
        print(f"\n── {cg_sym} ──────────────────────────────────────────────")

        bn_task = fetch_binance_funding(bin_sym)
        okx_task = fetch_okx_funding(okx_sym)
        cg_task = fetch_coinglass_funding(cg_sym, api_key)

        bn, okx, (cg_avg, cg_by_ex) = await asyncio.gather(bn_task, okx_task, cg_task)

        print(f"  Binance       : {_fmt_rate(bn)}")
        print(f"  OKX (Perp)    : {_fmt_rate(okx)}")
        print(f"  Coinglass avg : {_fmt_rate(cg_avg)}")
        if cg_by_ex:
            print("  Coinglass 各交易所明细：")
            for name, v in cg_by_ex[:8]:
                print(f"    {name:<12s}: {_fmt_rate(v)}")

        # 判定
        verdict = ""
        if bn is not None and cg_avg is not None and bn != 0:
            ratio = cg_avg / bn
            # 如比例落在 0.1-10 区间 → 同量级正确
            # 若 ratio ≈ 100 或 0.01 → 严重单位错误
            if 0.1 <= abs(ratio) <= 10:
                verdict = f"✅ 与 Binance 同量级 (ratio={ratio:+.3f})"
            elif 10 < abs(ratio) <= 150:
                verdict = f"⚠ 疑似 100× 放大 (ratio={ratio:+.2f})"
            elif 0.005 <= abs(ratio) < 0.1:
                verdict = f"⚠ 疑似 100× 缩小 (ratio={ratio:+.5f})"
            else:
                verdict = f"🚨 量级差异极大 (ratio={ratio:+.3e})"
        print(f"  判定: {verdict or '数据不足无法判定'}")

        summary.append({
            "coin": cg_sym,
            "binance": bn,
            "okx": okx,
            "coinglass_avg": cg_avg,
            "coinglass_exchanges": cg_by_ex,
            "verdict": verdict,
        })

    print("\n" + "═" * 70)
    print("JSON 原文（便于贴回审计）：")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print("═" * 70)
    print(
        "\n使用说明：\n"
        "  1. 若所有币种的 'Coinglass avg' 与 'Binance' 都在同量级（~1e-4），\n"
        "     说明 LIQ 的 float(rate) 解析正确，无需修改。\n"
        "  2. 若 Coinglass avg 持续 ≈ Binance 的 100 倍，则 polls/derivatives.py\n"
        "     需在 L218 处除以 100；阈值 0.0005 保持不变。\n"
        "  3. 若仅个别交易所（如 HTX）异常，这属于交易所孤立偏差，\n"
        "     在 derivatives.py L232 的中位数过滤已经处理，无需修复。\n"
    )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
