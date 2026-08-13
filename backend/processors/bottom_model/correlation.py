"""子信号相关性审计与重复计分声明。

存在意义：六因子内部有多个子信号在表达同一份底层证据（例如估值簇的
MVRV-Z / NUPL / Reserve Risk / 价格-200W 都是"价格相对长期持有成本的位置"），
等权相加会把一份证据放大成四份。本模块**不修改任何评分**，只量化并声明
这层相关性，让人和外部 AI 都能看到"哪些证据不是独立的"。

设计：
- 纯函数，输入与因子引擎相同的序列 dict，复用 factors.align 做日期对齐。
- 相关系数在最近 3 年重叠日上计算（更长的窗口会混入不同市场制度）。
- 除量化相关性外，另附一份**跨层重叠清单**：同一现象在 Stress / Confirmation
  / 假底过滤三层中被不同形式使用的地方，人工声明，防止外部 AI 重复计分。
"""

from __future__ import annotations

from typing import Any, Optional

from processors.bottom_model.factors import (
    Rows,
    align,
    change_rate_series,
    liq_total_series,
    ratio_series,
)

# 相关性窗口（自然日）与最小重叠样本
_WINDOW_DAYS = 1095
_MIN_OVERLAP = 60

# 分组：(组 key, 组名, 说明, [(序列 key, 显示名), ...])
GROUPS: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "valuation", "估值簇",
        "均在表达价格相对长期持有成本/长期价值的位置，不可当作彼此独立的证据",
        (
            ("mvrv_zscore", "MVRV Z-Score"),
            ("nupl", "NUPL"),
            ("reserve_risk", "Reserve Risk"),
            ("price_vs_200w", "价格/200W均线"),
            ("sth_mvrv", "STH-MVRV"),
        ),
    ),
    (
        "capitulation", "投降簇",
        "均由链上亏损兑现行为驱动，SOPR 与已实现亏损共享同一批成交",
        (
            ("realized_loss_abs", "已实现亏损"),
            ("lth_realized_loss_abs", "LTH 已实现亏损"),
            ("sopr", "aSOPR"),
            ("sth_sopr", "STH-SOPR"),
        ),
    ),
    (
        "demand", "需求簇",
        "四者经济含义不同：溢价与现货净 taker 是直接需求方向，ETF 是受日历影响的"
        "资金流，稳定币增速只是流动性弹药。相关系数由本次冻结数据动态生成，"
        "不能凭低相关直接宣称统计独立。",
        (
            ("coinbase_premium_rate", "Coinbase 溢价"),
            ("spot_net_taker_usd", "现货净 taker"),
            ("etf_flow_usd", "ETF 净流"),
            ("stablecoin_growth_30d", "稳定币 30d 增速"),
        ),
    ),
    (
        "leverage", "杠杆簇",
        "清算是 OI 出清的直接结果，资金费与 OI 同属杠杆定价，存在机械关联",
        (
            ("oi_agg_usd", "聚合 OI"),
            ("cme_oi_usd", "CME OI"),
            ("liq_total_usd", "清算量"),
            ("funding_oiw", "OI 加权资金费"),
        ),
    ),
)

# 结构性冗余：由定义而非由数据得出，因此**不给相关系数**——用推导出来的
# 序列去算相关性只会得到 -1.00 这种同义反复的假精度。
STRUCTURAL_REDUNDANCIES: tuple[dict[str, str], ...] = (
    {
        "topic": "LTH 净持仓变化 与 STH 供应变化",
        "basis": "STH 供应 + LTH 供应 ≈ 流通供应，而流通供应的日增量仅为区块"
                 "产出（当前约 450 BTC/日，相对存量可忽略）",
        "conclusion": "两者变化近似互为负号，LTH 净增持不构成独立于"
                      "sth_supply_drop 的新证据，故模型不单独采集该指标",
    },
)

# 跨层重叠：同一现象在不同层的不同用法（人工声明，非计算所得）
CROSS_LAYER_OVERLAPS: tuple[dict[str, str], ...] = (
    {
        "topic": "周线结构（LL→HL→HH）",
        "usage": "仅出现在 Confirmation 层的 structure_stage",
        "note": "bottom-v3 起已从 Stress 的价格结构因子移除，避免同一转向"
                "事件同时抬高两个仪表",
    },
    {
        "topic": "价格与 STH 成本线",
        "usage": "Stress 价格结构因子看水平（低于成本线多少），"
                 "Confirmation 层看事件（是否收复）",
        "note": "有意的水平/事件二分，不是重复计分，但两者高度联动，"
                "价格一旦收复会同时改善两个数字",
    },
    {
        "topic": "OI 回堆风险",
        "usage": "Confirmation 的 funding_oi_regime 给低分，"
                 "同时假底过滤器的 oi_rebuild 再扣分",
        "note": "同一风险在确认层与惩罚层各作用一次，合计影响偏强；"
                "惩罚幅度的经验校准留待历史频率层（P2）完成后再定",
    },
    {
        "topic": "ETF 资金流",
        "usage": "需求因子的 etf_momentum、Confirmation 的 etf_turn、"
                 "假底过滤器的 etf_outflow",
        "note": "同一序列在三处出现；窗口仅 2024-01 起，证据质量已自动降权",
    },
)


def pearson(rows_a: Rows, rows_b: Rows,
            window_days: int = _WINDOW_DAYS) -> Optional[tuple[float, int]]:
    """两序列在最近 window_days 重叠日上的皮尔逊相关系数与样本数。"""
    pairs = align(rows_a, rows_b)[-window_days:]
    n = len(pairs)
    if n < _MIN_OVERLAP:
        return None
    xs = [a for _, a, _ in pairs]
    ys = [b for _, _, b in pairs]
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 1e-18 or var_y <= 1e-18:
        return None
    return cov / (var_x * var_y) ** 0.5, n


def _series_map(data: dict[str, Rows]) -> dict[str, Rows]:
    """补齐派生序列，使分组可以直接按 key 取数。"""
    derived: dict[str, Rows] = dict(data)
    derived["price_vs_200w"] = ratio_series(
        data.get("btc_price_onchain", []), data.get("ma_200w", []),
    )
    derived["realized_loss_abs"] = [
        (day, abs(v)) for day, v in data.get("realized_loss", [])
    ]
    derived["lth_realized_loss_abs"] = [
        (day, abs(v)) for day, v in data.get("lth_realized_loss", [])
    ]
    derived["liq_total_usd"] = liq_total_series(data)
    # 稳定币市值是单调增长的水平量，用水平值算相关只会得到"都随时间上涨"的
    # 伪相关；因子层用的也是 30d 增速，这里保持同一口径
    derived["stablecoin_growth_30d"] = change_rate_series(
        data.get("stablecoin_total_mcap", []), 30,
    )
    return derived


def compute_correlation_audit(data: dict[str, Rows]) -> dict[str, Any]:
    """分组两两相关系数 + 跨层重叠声明。计算量为十余对 × ≤1095 天。"""
    series = _series_map(data)
    groups: list[dict[str, Any]] = []
    for key, label, note, members in GROUPS:
        pairs: list[dict[str, Any]] = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                key_a, name_a = members[i]
                key_b, name_b = members[j]
                result = pearson(series.get(key_a, []), series.get(key_b, []))
                if result is None:
                    continue
                rho, n = result
                pairs.append({
                    "a": name_a, "b": name_b, "rho": round(rho, 2), "n": n,
                })
        if not pairs:
            continue
        strong = [p for p in pairs if abs(p["rho"]) >= 0.70]
        groups.append({
            "key": key,
            "label": label,
            "note": note,
            "pairs": sorted(pairs, key=lambda p: -abs(p["rho"])),
            "max_abs_rho": max(abs(p["rho"]) for p in pairs),
            "strong_pairs": len(strong),
        })
    return {
        "window_days": _WINDOW_DAYS,
        "groups": groups,
        "cross_layer_overlaps": [dict(item) for item in CROSS_LAYER_OVERLAPS],
        "structural_redundancies": [dict(item) for item in STRUCTURAL_REDUNDANCIES],
    }
