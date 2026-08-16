"""抓取币安钱包接口的真实响应，保存为 golden fixtures。

用途：
  1. 写解析器时对照真实字段（而不是猜）。
  2. 作为回归测试基线——币安接口字段一旦变动，解析测试会立刻失败，
     而不是等到线上悄悄产生一堆错误评分。

重新抓取（接口有变动时）：
    python3 tests/capture_fixtures.py

注意：抓取需要外网，且币安对部分地区 IP 有限制。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
BASE = "https://web3.binance.com/bapi/defi"
UA = "binance-web3/3.0 (Skill)"


def call(url: str, method: str = "GET", body: dict | None = None) -> dict:
    headers = {"Accept-Encoding": "identity", "User-Agent": UA}
    data = None
    if method == "POST":
        headers["content-type"] = "application/json"
        data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def qs(params: dict) -> str:
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in params.items() if v is not None})


def targets(chain_id: str) -> list[tuple[str, str, str, dict | None]]:
    """(fixture 名, method, url, body)"""
    items: list[tuple[str, str, str, dict | None]] = [
        (
            f"trending_{chain_id}",
            "POST",
            f"{BASE}/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list/ai",
            {
                "chainId": chain_id,
                "rankType": 10,
                "period": 30,
                "limit": 20,
                "countMin": 10,
                "launchTimeMin": 15,
                "liquidityMin": 5000,
                "uniqueTraderMin": 10,
                "volumeMin": 10000,
                "tagFilter": [1, 2, 3],
            },
        ),
        (
            f"memerush_new_{chain_id}",
            "POST",
            f"{BASE}/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai",
            {"chainId": chain_id, "rankType": 10, "limit": 20},
        ),
        (
            f"memerush_finalizing_{chain_id}",
            "POST",
            f"{BASE}/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai",
            {"chainId": chain_id, "rankType": 20, "limit": 20},
        ),
        (
            f"memerush_migrated_{chain_id}",
            "POST",
            f"{BASE}/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai",
            {"chainId": chain_id, "rankType": 30, "limit": 20},
        ),
        (
            f"inflow_{chain_id}",
            "POST",
            f"{BASE}/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai",
            {"chainId": chain_id, "period": "1h", "tagType": 2, "limit": 20},
        ),
        (
            f"signal_{chain_id}",
            "POST",
            f"{BASE}/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai",
            {"chainId": chain_id, "limit": 20},
        ),
        (
            f"social_{chain_id}",
            "GET",
            f"{BASE}/v1/public/wallet-direct/buw/wallet/market/token/pulse/social/hype/rank/leaderboard/ai?"
            + qs({"chainId": chain_id, "targetLanguage": "zh-CN", "timeRange": 1, "limit": 20}),
            None,
        ),
    ]
    if chain_id == "56":
        items.append(
            (
                "memerank_56",
                "GET",
                f"{BASE}/v1/public/wallet-direct/buw/wallet/market/token/pulse/exclusive/rank/list/ai?"
                + qs({"chainId": "56", "limit": 20}),
                None,
            )
        )
    return items


def pick_contract(payload: dict) -> str | None:
    """从列表响应里挑一个合约地址，用于抓详情/审计 fixture。"""
    data = payload.get("data")
    rows = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("list", "rows", "records", "items", "data"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    if not rows:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("contractAddress", "tokenAddress", "address", "ca"):
            if row.get(key):
                return str(row[key])
        token = row.get("token") or row.get("tokenInfo")
        if isinstance(token, dict):
            for key in ("contractAddress", "tokenAddress", "address"):
                if token.get(key):
                    return str(token[key])
    return None


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    ok, failed = 0, 0
    sample_contracts: dict[str, str] = {}

    for chain_id in ("56", "CT_501"):
        for name, method, url, body in targets(chain_id):
            try:
                payload = call(url, method, body)
                (FIXTURE_DIR / f"{name}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                rows = payload.get("data")
                count = len(rows) if isinstance(rows, list) else "obj"
                print(f"  OK   {name:32s} code={payload.get('code')} items={count}")
                ok += 1
                if chain_id not in sample_contracts:
                    ca = pick_contract(payload)
                    if ca:
                        sample_contracts[chain_id] = ca
            except (urllib.error.URLError, OSError, ValueError) as exc:
                print(f"  FAIL {name:32s} {type(exc).__name__}: {exc}")
                failed += 1
            time.sleep(0.8)

    # 详情 / 审计：依赖上面拿到的样本合约
    for chain_id, ca in sample_contracts.items():
        detail_url = (
            f"{BASE}/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai?"
            + qs({"chainId": chain_id, "contractAddress": ca})
        )
        meta_url = (
            f"{BASE}/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai?"
            + qs({"chainId": chain_id, "contractAddress": ca})
        )
        jobs = [
            (f"detail_{chain_id}", "GET", detail_url, None),
            (f"meta_{chain_id}", "GET", meta_url, None),
            (
                f"audit_{chain_id}",
                "POST",
                f"{BASE}/v1/public/wallet-direct/security/token/audit",
                {
                    "binanceChainId": chain_id,
                    "contractAddress": ca,
                    "requestId": str(uuid.uuid4()),
                },
            ),
        ]
        for name, method, url, body in jobs:
            try:
                payload = call(url, method, body)
                (FIXTURE_DIR / f"{name}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"  OK   {name:32s} code={payload.get('code')} ca={ca[:12]}…")
                ok += 1
            except (urllib.error.URLError, OSError, ValueError) as exc:
                print(f"  FAIL {name:32s} {type(exc).__name__}: {exc}")
                failed += 1
            time.sleep(0.8)

    print(f"\n完成：成功 {ok}，失败 {failed}，fixture 目录 {FIXTURE_DIR}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
