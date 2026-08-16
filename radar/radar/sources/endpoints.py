"""币安钱包公开接口端点定义。

这些是**非官方文档承诺**的公开接口，随时可能变更或限流。
因此：所有请求都走统一客户端（带重试/退避/429 自适应），
所有响应都走容错解析（字段缺失不崩），并保留原始响应归档，
使得接口一旦改版能立刻被 fixtures 回归测试发现。

单接口能力速查（实测）：
  - trending   一次返回全部结果（total≈30~60，无需分页），字段最全，
               且内含 chart1h 一分钟价格序列与 auditInfo 免费风险数据。
  - meme_rush  三个阶段各 20 条，筹码维度最完整（dev/sniper/insider/bundler/
               newWallet/kol/pro + 买卖税 + 洗盘标签），是被动更新的主力。
  - meme_rank  仅 BSC 支持。
  - inflow     含 inflow（净流入美元）与 tokenRiskLevel/tokenRiskCodes。
  - signal     聪明钱信号，含 alertPrice/alertMarketCap/exitRate/maxGain。
  - social     社交热度榜，主流币居多，Meme 覆盖率低，故只给最低配额。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

BASE = "https://web3.binance.com/bapi/defi"

# 与官方 skill 保持一致的 UA；Accept-Encoding: identity 是必需的，
# 否则部分响应会返回无法解压的内容。
DEFAULT_HEADERS = {
    "Accept-Encoding": "identity",
    "User-Agent": "binance-web3/3.0 (Skill)",
    "source": "agent",
}

CHAIN_BSC = "56"
CHAIN_SOLANA = "CT_501"
ALL_CHAINS = frozenset({CHAIN_BSC, CHAIN_SOLANA})

# meme_rush 阶段码
STAGE_NEW = 10
STAGE_FINALIZING = 20
STAGE_MIGRATED = 30
STAGE_NAMES = {STAGE_NEW: "new", STAGE_FINALIZING: "finalizing", STAGE_MIGRATED: "migrated"}


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    path: str
    kind: str                    # list | detail | audit | signal | social
    chains: frozenset[str]
    # 该端点默认归属的调度层（可被调用方覆盖）
    tier: str = "discovery"

    @property
    def url(self) -> str:
        return f"{BASE}{self.path}"

    def supports(self, chain_id: str) -> bool:
        return chain_id in self.chains


EP_TRENDING = Endpoint(
    name="trending",
    method="POST",
    path="/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list/ai",
    kind="list",
    chains=ALL_CHAINS,
)
EP_MEME_RUSH = Endpoint(
    name="meme_rush",
    method="POST",
    path="/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai",
    kind="list",
    chains=ALL_CHAINS,
)
EP_MEME_RANK = Endpoint(
    name="meme_rank",
    method="GET",
    path="/v1/public/wallet-direct/buw/wallet/market/token/pulse/exclusive/rank/list/ai",
    kind="list",
    chains=frozenset({CHAIN_BSC}),
)
EP_INFLOW = Endpoint(
    name="inflow",
    method="POST",
    path="/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai",
    kind="list",
    chains=ALL_CHAINS,
)
EP_SIGNAL = Endpoint(
    name="signal",
    method="POST",
    path="/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai",
    kind="signal",
    chains=ALL_CHAINS,
)
EP_SOCIAL = Endpoint(
    name="social",
    method="GET",
    path="/v1/public/wallet-direct/buw/wallet/market/token/pulse/social/hype/rank/leaderboard/ai",
    kind="social",
    chains=ALL_CHAINS,
    tier="social",
)
EP_DETAIL = Endpoint(
    name="detail",
    method="GET",
    path="/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info/ai",
    kind="detail",
    chains=ALL_CHAINS,
    tier="s1",
)
EP_META = Endpoint(
    name="meta",
    method="GET",
    path="/v1/public/wallet-direct/buw/wallet/dex/market/token/meta/info/ai",
    kind="detail",
    chains=ALL_CHAINS,
    tier="watching",
)
EP_AUDIT = Endpoint(
    name="audit",
    method="POST",
    path="/v1/public/wallet-direct/security/token/audit",
    kind="audit",
    chains=ALL_CHAINS,
    tier="audit",
)

ALL_ENDPOINTS: tuple[Endpoint, ...] = (
    EP_TRENDING, EP_MEME_RUSH, EP_MEME_RANK, EP_INFLOW,
    EP_SIGNAL, EP_SOCIAL, EP_DETAIL, EP_META, EP_AUDIT,
)

ENDPOINTS_BY_NAME: dict[str, Endpoint] = {e.name: e for e in ALL_ENDPOINTS}


# ─────────────────────────────────────────────────────────────────────────
# 请求参数构造
# ─────────────────────────────────────────────────────────────────────────

def trending_body(chain_id: str, *, period: int = 30, limit: int = 50) -> dict[str, Any]:
    """热门榜。

    period: 10=1m 20=5m 30=1h 40=4h 50=24h
    过滤门槛沿用官方 skill 的 Trending 默认值——这些默认值本身
    就已经过滤掉了大量无成交的僵尸币，能显著节省后续请求预算。
    """
    return {
        "chainId": chain_id,
        "rankType": 10,
        "period": period,
        "limit": limit,
        "countMin": 10,
        "launchTimeMin": 15,
        "liquidityMin": 5000,
        "uniqueTraderMin": 10,
        "volumeMin": 10000,
        "tagFilter": [1, 2, 3],
    }


def meme_rush_body(chain_id: str, stage: int, *, limit: int = 20) -> dict[str, Any]:
    return {"chainId": chain_id, "rankType": stage, "limit": limit}


def meme_rank_params(chain_id: str, *, limit: int = 50) -> dict[str, Any]:
    return {"chainId": chain_id, "limit": limit}


def inflow_body(chain_id: str, *, period: str = "1h", limit: int = 50) -> dict[str, Any]:
    return {"chainId": chain_id, "period": period, "tagType": 2, "limit": limit}


def signal_body(chain_id: str, *, limit: int = 50) -> dict[str, Any]:
    return {"chainId": chain_id, "limit": limit}


def social_params(chain_id: str, *, language: str = "zh-CN",
                  time_range: int = 1, limit: int = 30) -> dict[str, Any]:
    return {
        "chainId": chain_id,
        "targetLanguage": language,
        "timeRange": time_range,
        "limit": limit,
    }


def detail_params(chain_id: str, contract_address: str) -> dict[str, Any]:
    return {"chainId": chain_id, "contractAddress": contract_address}


def meta_params(chain_id: str, contract_address: str) -> dict[str, Any]:
    return {"chainId": chain_id, "contractAddress": contract_address}


def audit_body(chain_id: str, contract_address: str) -> dict[str, Any]:
    """审计接口用 binanceChainId 而非 chainId，且要求每次请求唯一 requestId。"""
    return {
        "binanceChainId": chain_id,
        "contractAddress": contract_address,
        "requestId": str(uuid.uuid4()),
    }
