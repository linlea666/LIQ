"""验证巨鲸转账 USD 流向聚合 + prompt 渲染增强。"""
from __future__ import annotations

from models.whale import WhaleData, WhaleTransfer
from polls.macro import calc_whale_transfer_flows
from ai.prompts import build_user_prompt


def _wd(*transfers: WhaleTransfer) -> WhaleData:
    return WhaleData(ts=0, transfers=list(transfers))


def test_calc_flows_inflow_outflow_net():
    """基础：3 笔充入 + 2 笔提出，聚合流向应正确。"""
    wd = _wd(
        WhaleTransfer(ts=0, symbol="BTC", amount_usd=5_000_000,
                      from_label="wallet", to_label="binance exchange"),
        WhaleTransfer(ts=0, symbol="BTC", amount_usd=3_000_000,
                      from_label="wallet", to_label="okx exchange"),
        WhaleTransfer(ts=0, symbol="BTC", amount_usd=2_000_000,
                      from_label="wallet", to_label="coinbase exchange"),
        WhaleTransfer(ts=0, symbol="BTC", amount_usd=4_000_000,
                      from_label="bybit exchange", to_label="wallet"),
        WhaleTransfer(ts=0, symbol="BTC", amount_usd=1_500_000,
                      from_label="binance exchange", to_label="wallet"),
    )
    flows = calc_whale_transfer_flows(wd)
    assert flows["inflow_usd"] == 10_000_000.0
    assert flows["outflow_usd"] == 5_500_000.0
    assert flows["net_usd"] == 4_500_000.0
    assert len(flows["top_transfers"]) == 3
    # top 3 应按金额降序：5M inflow, 4M outflow, 3M inflow
    assert flows["top_transfers"][0]["amount_usd"] == 5_000_000
    assert flows["top_transfers"][0]["direction"] == "inflow"
    assert flows["top_transfers"][1]["amount_usd"] == 4_000_000
    assert flows["top_transfers"][1]["direction"] == "outflow"


def test_calc_flows_empty_returns_zeros():
    """空输入：所有字段返回 0。"""
    assert calc_whale_transfer_flows(None) == {
        "inflow_usd": 0.0, "outflow_usd": 0.0, "net_usd": 0.0, "top_transfers": []
    }
    assert calc_whale_transfer_flows(_wd())["inflow_usd"] == 0.0


def test_calc_flows_wallet_to_wallet_ignored_in_net():
    """钱包间转账应归类 wallet_to_wallet，不影响 inflow/outflow 总额。"""
    wd = _wd(
        WhaleTransfer(ts=0, symbol="BTC", amount_usd=2_000_000,
                      from_label="wallet_a", to_label="wallet_b"),
        WhaleTransfer(ts=0, symbol="BTC", amount_usd=1_000_000,
                      from_label="wallet", to_label="okx exchange"),
    )
    flows = calc_whale_transfer_flows(wd)
    assert flows["inflow_usd"] == 1_000_000.0
    assert flows["outflow_usd"] == 0.0
    assert flows["net_usd"] == 1_000_000.0
    directions = [t["direction"] for t in flows["top_transfers"]]
    assert "wallet_to_wallet" in directions


def test_prompt_renders_inflow_outflow_breakdown():
    """prompt §8e 应渲染 充入/提出 USD 拆分，而不仅是笔数。"""
    snapshot = {
        "coin": "BTC",
        "price": 77000,
        "whale_hl_alerts_count": 0,
        "whale_transfers_count": 3,
        "whale_net_direction": "充入交易所(看跌)",
        "whale_transfer_inflow_usd": 15_000_000,
        "whale_transfer_outflow_usd": 2_000_000,
        "whale_transfer_net_usd": 13_000_000,
        "whale_top_transfers": [
            {"direction": "inflow", "amount_usd": 10_000_000,
             "from_label": "wallet", "to_label": "binance exchange"},
            {"direction": "inflow", "amount_usd": 5_000_000,
             "from_label": "wallet", "to_label": "okx exchange"},
            {"direction": "outflow", "amount_usd": 2_000_000,
             "from_label": "bybit exchange", "to_label": "cold wallet"},
        ],
    }
    prompt = build_user_prompt(snapshot)
    assert "链上巨鲸转账" in prompt
    assert "充入交易所" in prompt
    assert "提出交易所" in prompt
    assert "净流向" in prompt
    # 净流入 >$10M 应触发 偏空 标注
    assert "偏空" in prompt
    # Top 转账金额明细应出现（格式化为中文单位：10_000_000 → "$1千万"）
    assert "1千万" in prompt
    assert "binance exchange" in prompt or "okx exchange" in prompt


def test_prompt_np_24h_change_pct_tag():
    """净持仓 24h 变化应附加百分比 + 显著性标签。"""
    snapshot = {
        "coin": "BTC",
        "price": 77000,
        "net_position_latest": 100_000_000,
        "net_position_trend": "上升(多头增仓)",
        "net_position_change_24h": 8_000_000,
    }
    prompt = build_user_prompt(snapshot)
    assert "净持仓(v2)" in prompt
    # 8% 变化应标注为显著增持
    assert "+8.0%" in prompt
    assert "显著" in prompt
    assert "增持" in prompt
    # 方向语义解读应出现
    assert "多头持仓递增" in prompt


def test_prompt_np_unit_disclaimer_no_usd_symbol():
    """净持仓单位修复：不再用 $ 符号渲染（避免 AI 把 coin 计数误解为 USD 金额）。

    对应 AI 反馈："净持仓变化绝对值过小（$1万），参考价值低"
    根因：Coinglass v2 net-position 单位为基础币 coin 计数，但旧 prompt 用 _fmt_usd_for_prompt 渲染。
    """
    snapshot = {
        "coin": "BTC",
        "price": 77000,
        "net_position_latest": 1_500_000,
        "net_position_trend": "上升(多头增仓)",
        "net_position_change_24h": 120_000,
    }
    prompt = build_user_prompt(snapshot)
    assert "净持仓(v2)" in prompt
    # 必须含单位说明
    assert "coin 计数" in prompt or "非 USD 金额" in prompt
    # 必须指引 AI 以趋势+百分比为主
    assert "趋势方向" in prompt and "百分比变化" in prompt
    # 确认核心渲染不出现 "$1,500,000" 这种错误 USD 形式
    assert "$1,500,000" not in prompt
    assert "$120,000" not in prompt


def test_prompt_kl_v2_table_renders_bounce_quality_and_breakout_stage():
    """§9g 关键位表格应渲染 bounce_quality / breakout_stage 两列。

    对应 AI 反馈："关键位状态机中的 bounce_quality 和 breakout_stage 字段为空"
    根因：后端已计算这两个字段并塞进 kl.levels[i]，但旧 prompt 表格从未输出它们。
    """
    snapshot = {
        "coin": "BTC",
        "price": 77000,
        "key_levels": {
            "active_count": 2,
            "levels": [
                {
                    "price": 76800, "side": "support", "state": "bounced",
                    "strength_tier": "S", "confluence_score": 85, "source_count": 4,
                    "cascade_risk": 0.0, "sweep_usd": 0, "sources": ["POC", "MA120"],
                    "distance_pct": -0.3, "test_count": 2,
                    "bounce_quality": "proactive", "breakout_stage": 0,
                },
                {
                    "price": 77500, "side": "resistance", "state": "broken",
                    "strength_tier": "A", "confluence_score": 72, "source_count": 3,
                    "cascade_risk": 0.6, "sweep_usd": 0, "sources": ["liq_cluster"],
                    "distance_pct": 0.6, "test_count": 1,
                    "bounce_quality": "", "breakout_stage": 2,
                },
            ],
            "signals": [],
        },
    }
    prompt = build_user_prompt(snapshot)
    # 表头必须含两列
    assert "反弹质量" in prompt
    assert "突破阶段" in prompt
    # 主动吸筹渲染
    assert "主动" in prompt
    assert "量能≥1.5" in prompt
    # stage2 回踩中渲染
    assert "stage2" in prompt
    assert "回踩中" in prompt


def test_prompt_tp1_tp2_semantic_labels():
    """§11a 应明确标注 TP1近 / TP2远 + 说明行。"""
    snapshot = {
        "coin": "BTC",
        "price": 77000,
        "sniper_entries": [{
            "direction": "long",
            "entry_price": 76500,
            "stop_loss": 75800,
            "take_profit_1": 77500,
            "take_profit_2": 78500,
            "rr_ratio_1": 1.5,
            "rr_ratio_2": 2.9,
            "logic": ["test"],
        }],
    }
    prompt = build_user_prompt(snapshot)
    assert "TP1=近目标" in prompt or "TP1近" in prompt
    assert "TP2=远目标" in prompt or "TP2远" in prompt


def test_section_8e_empty_renders_friendly_fallback():
    """§8e 无任何巨鲸活动时应渲染章节+降级提示（避免 AI 误报为'数据缺失'）。"""
    snapshot = {
        "coin": "BTC",
        "price": 77000,
        "whale_hl_alerts_count": 0,
        "whale_transfers_count": 0,
        "whale_net_direction": "",
        "whale_hl_positions": [],
        "whale_transfer_inflow_usd": 0,
        "whale_transfer_outflow_usd": 0,
        "whale_transfer_net_usd": 0,
        "whale_top_transfers": [],
    }
    up = build_user_prompt(snapshot)
    assert "### 8e. 巨鲸追踪" in up
    assert "近期无巨鲸活动采集到" in up
    assert "非数据缺失" in up
    assert "常态信号" in up


def test_section_8e_populated_renders_without_fallback():
    """§8e 有活跃数据时不应出现降级提示。"""
    snapshot = {
        "coin": "BTC",
        "price": 77000,
        "whale_hl_alerts_count": 2,
        "whale_transfers_count": 3,
        "whale_net_direction": "充入交易所(看跌)",
        "whale_transfer_inflow_usd": 5_000_000,
        "whale_transfer_outflow_usd": 1_000_000,
        "whale_transfer_net_usd": 4_000_000,
        "whale_top_transfers": [
            {"direction": "inflow", "amount_usd": 5_000_000,
             "from_label": "wallet", "to_label": "binance exchange"},
        ],
    }
    up = build_user_prompt(snapshot)
    assert "### 8e. 巨鲸追踪" in up
    assert "近期无巨鲸活动采集到" not in up
    assert "Hyperliquid 巨鲸警报: 2条" in up
    assert "充入交易所" in up


def test_system_prompt_tp_iron_law():
    """系统提示应含 TP1/TP2 语义铁律 + R:R 以 TP2 为准。"""
    from ai.prompts import build_system_prompt
    sp = build_system_prompt()
    assert "TP1 / TP2 语义铁律" in sp or "TP1/TP2" in sp
    assert "TP1 = 近目标" in sp
    assert "TP2 = 远目标" in sp
    assert "以 TP2 为准" in sp
