"""解析器回归测试。

这些测试的价值不在于"代码能跑"，而在于**币安接口一旦改字段名就立刻失败**。
如果没有它们，接口改版后系统会继续正常运行、正常发警报，
只是所有筹码特征悄悄变成 None——这是最危险的失效模式。

fixtures 由 tests/capture_fixtures.py 从真实接口抓取，覆盖 BSC 与 Solana 双链。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.domain.models import _GROUP_FIELDS, TokenObservation  # noqa: E402
from radar.sources import parsers as P  # noqa: E402
from radar.sources.coerce import (  # noqa: E402
    to_percent,
    to_positive_float,
    to_timestamp_ms,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHAINS = ("56", "CT_501")


def load(name: str) -> dict:
    path = FIXTURES / f"{name}.json"
    if not path.exists():
        pytest.skip(f"缺少 fixture: {name}（运行 tests/capture_fixtures.py 生成）")
    return json.loads(path.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────
# 模型自洽性
# ─────────────────────────────────────────────────────────────────────────

def test_group_fields_all_exist_on_observation():
    """字段分组表里不能出现 TokenObservation 上不存在的字段名。

    否则该字段会被静默忽略：解析器辛苦解析出来的值永远进不了 TokenView。
    """
    annotations = set(TokenObservation.__annotations__)
    unknown: list[str] = []
    for group, fields in _GROUP_FIELDS.items():
        for fname in fields:
            if fname not in annotations:
                unknown.append(f"{group.value}.{fname}")
    assert not unknown, f"分组表引用了不存在的字段: {unknown}"


def test_group_fields_no_duplicates():
    """同一字段不能归属两个组，否则新鲜度判定会互相覆盖。"""
    seen: dict[str, str] = {}
    dupes: list[str] = []
    for group, fields in _GROUP_FIELDS.items():
        for fname in fields:
            if fname in seen:
                dupes.append(f"{fname}: {seen[fname]} / {group.value}")
            seen[fname] = group.value
    assert not dupes, f"字段重复归组: {dupes}"


# ─────────────────────────────────────────────────────────────────────────
# 类型转换语义
# ─────────────────────────────────────────────────────────────────────────

def test_zero_price_is_unknown_not_zero():
    """价格/市值为 0 必须当作 UNKNOWN。

    币安对刚创建的代币经常返回 0；若当作真实值，
    所有以市值为分母的比率特征会直接爆炸。
    """
    assert to_positive_float(0) is None
    assert to_positive_float("0") is None
    assert to_positive_float("0.000000") is None
    assert to_positive_float("-5") is None
    assert to_positive_float("1e-30") == pytest.approx(1e-30)


def test_out_of_range_percent_rejected():
    assert to_percent("150") is None      # 超过 100 视为解析错误
    assert to_percent("-1") is None
    assert to_percent("0") == 0.0         # 0% 是有意义的真实值
    assert to_percent("96.38692331585459") == pytest.approx(96.3869233, abs=1e-6)


def test_timestamp_seconds_are_upgraded():
    assert to_timestamp_ms(1786819447000) == 1786819447000
    assert to_timestamp_ms(1786819447) == 1786819447000
    assert to_timestamp_ms(0) is None
    assert to_timestamp_ms(-1) is None


def test_chart_extremes():
    raw = json.dumps({
        "p": {"1786815900000": "0.001", "1786815960000": "0.005", "1786816020000": "0.002"},
        "v": {"1786815900000": "100", "1786815960000": "250"},
    })
    high, low, volume = P.parse_chart_extremes(raw)
    assert high == pytest.approx(0.005)
    assert low == pytest.approx(0.001)
    assert volume == pytest.approx(350.0)


def test_chart_extremes_tolerates_garbage():
    assert P.parse_chart_extremes(None) == (None, None, None)
    assert P.parse_chart_extremes("not json") == (None, None, None)
    assert P.parse_chart_extremes("[]") == (None, None, None)
    assert P.parse_chart_extremes(json.dumps({"p": {}})) == (None, None, None)


# ─────────────────────────────────────────────────────────────────────────
# 列表端点解析
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("chain_id", CHAINS)
def test_trending_parses(chain_id: str):
    payload = load(f"trending_{chain_id}")
    data = P.check_envelope(payload)
    rows = P.extract_rows("trending", data)
    assert rows, "热门榜应至少返回一行"
    assert P.detect_missing_keys("trending", rows) == ()

    observations = [P.parse_trending_row(chain_id, r, 1_700_000_000_000) for r in rows]
    parsed = [o for o in observations if o is not None]
    assert len(parsed) == len(rows), "所有行都应解析出合约地址"

    # 核心字段的覆盖率：热门榜是筛选后的活跃币，不应大面积缺失
    for fname in ("price", "market_cap", "liquidity", "holders"):
        covered = sum(1 for o in parsed if getattr(o, fname) is not None)
        assert covered >= len(parsed) * 0.8, f"{fname} 覆盖率过低: {covered}/{len(parsed)}"

    assert all(o.seen_on_trending for o in parsed)
    assert all(o.chain_id == chain_id for o in parsed)


@pytest.mark.parametrize("chain_id", CHAINS)
@pytest.mark.parametrize("stage,stage_name", [(10, "new"), (20, "finalizing"), (30, "migrated")])
def test_meme_rush_parses(chain_id: str, stage: int, stage_name: str):
    payload = load(f"memerush_{stage_name}_{chain_id}")
    data = P.check_envelope(payload)
    rows = P.extract_rows("meme_rush", data)
    assert rows
    assert P.detect_missing_keys("meme_rush", rows) == ()

    parsed = [
        o for o in (P.parse_meme_rush_row(chain_id, r, 1_700_000_000_000, stage) for r in rows)
        if o is not None
    ]
    assert len(parsed) == len(rows)

    # meme_rush 是筹码维度的主力来源，top10 必须有高覆盖率
    covered = sum(1 for o in parsed if o.top10_percent is not None)
    assert covered >= len(parsed) * 0.8, f"top10 覆盖率过低: {covered}/{len(parsed)}"
    assert all(o.launch_time_ms for o in parsed), "createTime 应能解析出上线时间"

    # volume/count 必须落在无时间窗的聚合字段，绝不能污染 1h 口径
    assert all(o.volume_1h is None for o in parsed)
    assert all(o.count_1h is None for o in parsed)


@pytest.mark.parametrize("chain_id", CHAINS)
def test_inflow_parses(chain_id: str):
    payload = load(f"inflow_{chain_id}")
    data = P.check_envelope(payload)
    rows = P.extract_rows("inflow", data)
    assert rows
    assert P.detect_missing_keys("inflow", rows) == ()

    parsed = [
        o for o in (P.parse_inflow_row(chain_id, r, 1_700_000_000_000) for r in rows)
        if o is not None
    ]
    assert len(parsed) == len(rows)
    assert sum(1 for o in parsed if o.net_inflow is not None) >= len(parsed) * 0.9

    # period 非 1h 时不得写入 1h 列
    other = [
        o for o in (
            P.parse_inflow_row(chain_id, r, 1_700_000_000_000, period="24h") for r in rows
        )
        if o is not None
    ]
    assert all(o.volume_1h is None and o.count_1h is None for o in other)


@pytest.mark.parametrize("chain_id", CHAINS)
def test_signal_parses(chain_id: str):
    payload = load(f"signal_{chain_id}")
    data = P.check_envelope(payload)
    rows = P.extract_rows("signal", data)
    assert rows
    assert P.detect_missing_keys("signal", rows) == ()

    parsed = [
        o for o in (P.parse_signal_row(chain_id, r, 1_700_000_000_000) for r in rows)
        if o is not None
    ]
    assert len(parsed) == len(rows)
    assert all(o.smart_money_count is not None for o in parsed)
    # exitRate 是 0-100 标度；100 表示聪明钱已全部离场
    assert all(o.exit_rate is None or 0 <= o.exit_rate <= 100 for o in parsed)
    assert any(o.signal_direction in ("buy", "sell") for o in parsed)


@pytest.mark.parametrize("chain_id", CHAINS)
def test_social_parses(chain_id: str):
    payload = load(f"social_{chain_id}")
    data = P.check_envelope(payload)
    rows = P.extract_rows("social", data)
    assert rows
    assert P.detect_missing_keys("social", rows) == ()

    parsed = [
        o for o in (P.parse_social_row(chain_id, r, 1_700_000_000_000) for r in rows)
        if o is not None
    ]
    assert parsed
    assert sum(1 for o in parsed if o.social_hype is not None) >= len(parsed) * 0.8


def test_meme_rank_parses_bsc_only():
    payload = load("memerank_56")
    data = P.check_envelope(payload)
    rows = P.extract_rows("meme_rank", data)
    assert rows
    parsed = [
        o for o in (P.parse_meme_rank_row("56", r, 1_700_000_000_000) for r in rows)
        if o is not None
    ]
    assert len(parsed) == len(rows)
    assert sum(1 for o in parsed if o.binance_score is not None) >= len(parsed) * 0.8


# ─────────────────────────────────────────────────────────────────────────
# 单币端点解析
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("chain_id", CHAINS)
def test_detail_parses(chain_id: str):
    payload = load(f"detail_{chain_id}")
    data = P.check_envelope(payload)
    obs = P.parse_detail(chain_id, "0xtest", data, 1_700_000_000_000)
    assert obs.endpoint == "detail"
    # price 为 null 时必须回退 aggPrice，否则极新的币永远拿不到价格
    assert obs.price is not None, "price/aggPrice 回退失效"
    assert obs.total_supply is not None
    assert obs.bonding_progress is not None


@pytest.mark.parametrize("chain_id", CHAINS)
def test_audit_unavailable_is_unknown_not_safe(chain_id: str):
    """hasResult=false 时必须解析成 UNKNOWN。

    这是本项目最容易犯的致命错误：刚创建几分钟的币几乎全部
    返回 hasResult=false + riskLevel=-1，若按"没命中风险=安全"处理，
    风险门等于完全失效。
    """
    payload = load(f"audit_{chain_id}")
    data = P.check_envelope(payload)
    obs = P.parse_audit(chain_id, "0xtest", data, 1_700_000_000_000)

    if not data.get("hasResult") or not data.get("isSupported"):
        assert obs.audit_available is False
        assert obs.audit_risk_level is None
        assert obs.honeypot is None


def test_audit_negative_risk_level_is_unknown():
    data = {
        "hasResult": True, "isSupported": True,
        "riskLevel": -1, "riskLevelEnum": "LOW",
        "extraInfo": None, "riskItems": [],
    }
    obs = P.parse_audit("56", "0xtest", data, 1)
    assert obs.audit_available is True
    assert obs.audit_risk_level is None, "riskLevel=-1 不是低风险，而是无结果"


def test_audit_honeypot_detected():
    data = {
        "hasResult": True, "isSupported": True, "riskLevel": 5,
        "extraInfo": {"buyTax": "0", "sellTax": "99", "isVerified": False},
        "riskItems": [{
            "id": "CONTRACT_RISK",
            "details": [{
                "title": "Honeypot Risk Found",
                "isHit": True,
                "riskType": "RISK",
            }],
        }],
    }
    obs = P.parse_audit("56", "0xtest", data, 1)
    assert obs.honeypot is True
    assert obs.audit_risk_level == 5
    assert obs.sell_tax_pct == pytest.approx(99.0)
    assert obs.contract_verified is False


# ─────────────────────────────────────────────────────────────────────────
# 容错与信封
# ─────────────────────────────────────────────────────────────────────────

def test_envelope_rejects_business_error():
    with pytest.raises(P.SchemaDrift):
        P.check_envelope({"code": "100001", "message": "illegal parameter", "data": None})
    with pytest.raises(P.SchemaDrift):
        P.check_envelope({"code": "000000", "success": False, "data": []})
    with pytest.raises(P.SchemaDrift):
        P.check_envelope({"code": "000000", "data": None})


def test_missing_keys_detected_when_field_disappears():
    """模拟币安把 marketCap 改名：必须被检测出来。"""
    rows = [{"contractAddress": "0x1", "price": "1", "liquidity": "1", "holders": "1"}]
    missing = P.detect_missing_keys("trending", rows)
    assert "marketCap" in missing


def test_row_without_contract_is_skipped():
    assert P.parse_trending_row("56", {"symbol": "X"}, 1) is None
    assert P.parse_meme_rush_row("56", {"symbol": "X"}, 1, 10) is None
    assert P.parse_inflow_row("56", {"tokenName": "X"}, 1) is None


def test_parsers_never_crash_on_all_null_row():
    """所有字段为 null 的行必须能安全解析（只要有合约地址）。"""
    row = {k: None for k in (
        "price", "marketCap", "liquidity", "holders", "launchTime",
        "holdersTop10Percent", "chart1h", "auditInfo", "tokenTag", "metaInfo",
    )}
    row["contractAddress"] = "0xdead"
    obs = P.parse_trending_row("56", row, 1)
    assert obs is not None
    assert obs.price is None and obs.market_cap is None
    assert obs.tags == ()
