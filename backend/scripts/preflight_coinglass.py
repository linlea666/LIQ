#!/usr/bin/env python3
"""部署前验证旧 CoinGlass 代理配置；不输出密钥或业务数据。"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / "backend" / ".env"
CONFIG_PATH = ROOT / "backend" / "config" / "config.yaml"
BASE_URL = "https://www.keystore.com.cn/api/v1/proxy/coinglass"
PROBE_PATH = "/v4/api/futures/liquidation/exchange-list"


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    config_text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    if (
        f'base_url: "{BASE_URL}"' not in config_text
        or 'api_key_env: "COINGLASS_API_KEY"' not in config_text
    ):
        print("ERROR: config.yaml中的旧代理域名或密钥变量配置不匹配", file=sys.stderr)
        return 1

    values = _load_env(ENV_PATH)
    api_key = values.get("COINGLASS_API_KEY", "").strip()
    if not api_key:
        print(f"ERROR: {ENV_PATH} 缺少非空 COINGLASS_API_KEY", file=sys.stderr)
        return 2

    url = f"{BASE_URL}{PROBE_PATH}?{urllib.parse.urlencode({'range': '1h'})}"
    request = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"ERROR: CoinGlass预检HTTP {exc.code}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - CLI必须转换为明确退出码
        print(f"ERROR: CoinGlass预检失败: {type(exc).__name__}", file=sys.stderr)
        return 4

    code = payload.get("code") if isinstance(payload, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None
    if code not in (None, 0, "0", 20000, "20000") or not isinstance(data, list):
        print("ERROR: CoinGlass预检响应结构异常", file=sys.stderr)
        return 5
    print("CoinGlass预检通过: HTTP 200, response schema OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
