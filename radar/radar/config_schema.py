"""可配置参数注册表：配置页的唯一权威来源。

三个消费方共用这一份定义，保证永不漂移：
  1. 前端配置页——分组、中文说明、输入控件类型、取值范围全部由此驱动；
  2. 管理 API 写入校验——不在注册表里的路径一律拒绝，防止把 config.yaml
     里"装饰性"的键（代码从未读取）暴露给用户造成"改了不生效"的假象；
  3. settings 启动合并——overrides.yaml 里的非法条目在启动时被丢弃并告警，
     坏配置永远到不了运行组件。

**为什么不暴露全部 yaml 键**：暴露一个代码不读的参数，等于在界面上
放一个假开关。注册表只收录经过逐键核对、确认代码真正读取的参数。

路径规则：点号分隔的字典键；`chains` 列表按 id 寻址（chains.56.enabled）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

# ═════════════════════════════════════════════════════════════════════════
# 分组
# ═════════════════════════════════════════════════════════════════════════

GROUPS: tuple[tuple[str, str, str], ...] = (
    ("collect", "采集与调度", "抓取哪些链、每次抓多少、各层轮询频率与限速自适应"),
    ("capacity", "内存与容量", "内存代币上限、淘汰与重启恢复"),
    ("disk", "快照与磁盘", "快照写入频率、降采样与留存"),
    ("risk", "风险门", "硬拒条件与研究降级门"),
    ("quality", "数据质量", "新鲜度阈值、罚分与晋升质量闸门"),
    ("strategy", "评分与状态机", "五维权重、特征防爆参数、状态迁移阈值（改动会生成新配置指纹）"),
    ("alerts", "警报与邮件", "触发冷却、Near-Miss、SMTP 与限流"),
    ("tracking", "追踪与观测", "里程碑、Outcome 窗口、纸面仓位与日志"),
)

VALID_GROUP_IDS = {g[0] for g in GROUPS}

# ═════════════════════════════════════════════════════════════════════════
# 参数定义
# ═════════════════════════════════════════════════════════════════════════

# kind 取值：int / float / bool / str / choice / num_list / str_list / json
@dataclass(frozen=True)
class Param:
    path: str
    kind: str
    label: str
    group: str
    desc: str = ""
    lo: float | None = None
    hi: float | None = None
    choices: tuple[Any, ...] | None = None
    ascending: bool = False        # num_list 是否要求严格递增
    allow_empty: bool = False      # str / *_list 是否允许为空
    unit: str = ""
    # 目前全部参数都是重启生效；保留字段是为了未来支持热生效时
    # 前端无需任何改动
    restart_required: bool = True


def _p(path: str, kind: str, label: str, group: str, **kw: Any) -> Param:
    assert group in VALID_GROUP_IDS, f"未知分组: {group}"
    return Param(path=path, kind=kind, label=label, group=group, **kw)


def _build_params() -> tuple[Param, ...]:
    out: list[Param] = []
    add = out.append

    # ── 采集与调度 ─────────────────────────────────────────────────────
    for cid, cname in (("56", "BSC"), ("CT_501", "Solana")):
        add(_p(f"chains.{cid}.enabled", "bool", f"启用 {cname} 链", "collect",
               desc=f"关闭后不再采集 {cname} 上的任何代币；已有数据保留"))
    add(_p("collectors.list_page_size", "int", "单次列表抓取条数", "collect",
           lo=10, hi=100, unit="条",
           desc="每个榜单接口单页返回的代币数；越大发现越全但解析与内存开销越高"))
    add(_p("collectors.trending_period", "choice", "热门榜时间窗", "collect",
           choices=(10, 20, 30, 40, 50),
           desc="10=1分钟 20=5分钟 30=1小时 40=4小时 50=24小时"))
    add(_p("collectors.inflow_period", "choice", "聪明钱流入榜窗口", "collect",
           choices=("5m", "1h", "4h", "24h")))
    add(_p("collectors.social_language", "str", "社交热度榜语言", "collect"))
    add(_p("collectors.extract_chart_extremes", "bool", "提取分钟级价格极值", "collect",
           desc="从详情接口的 1 分钟序列提取区间高低点后丢弃原始序列，供 ATH 追踪"))
    add(_p("collectors.meme_rush_stages", "num_list", "Meme Rush 阶段", "collect",
           lo=10, hi=30, desc="10=New 20=Finalizing 30=Migrated；至少保留一个阶段"))
    add(_p("scheduler.global_rpm", "int", "全局请求硬上限", "collect",
           lo=10, hi=300, unit="rpm",
           desc="对币安接口的绝对请求上限；90 是保守假设值，调大有被限流风险"))
    add(_p("scheduler.target_rpm", "int", "常态目标请求量", "collect",
           lo=10, hi=300, unit="rpm", desc="须 ≤ 全局硬上限，差额留给突发与重试"))
    add(_p("scheduler.jitter_ratio", "float", "轮询抖动比例", "collect",
           lo=0.0, hi=0.5, desc="打散请求时间避免整点尖峰"))
    add(_p("scheduler.request_timeout_sec", "int", "单请求超时", "collect",
           lo=3, hi=60, unit="秒"))
    add(_p("scheduler.max_retries", "int", "请求重试次数", "collect", lo=0, hi=5))
    add(_p("scheduler.retry_backoff_base_sec", "float", "重试退避基数", "collect",
           lo=0.5, hi=30.0, unit="秒"))
    add(_p("scheduler.adaptive.window_sec", "int", "429 统计窗口", "collect",
           lo=60, hi=3600, unit="秒"))
    add(_p("scheduler.adaptive.rate_limit_threshold", "float", "429 降速阈值", "collect",
           lo=0.01, hi=0.5, desc="窗口内 429 占比超过该值即全局降速"))
    add(_p("scheduler.adaptive.downscale_ratio", "float", "降速系数", "collect",
           lo=0.3, hi=0.95))
    add(_p("scheduler.adaptive.min_rpm", "int", "降速下限", "collect",
           lo=5, hi=200, unit="rpm", desc="须 ≤ 常态目标请求量"))
    add(_p("scheduler.adaptive.recover_after_sec", "int", "恢复观察期", "collect",
           lo=60, hi=7200, unit="秒", desc="连续无 429 达到该时长后逐步恢复配额"))
    tier_labels = {
        "discovery": "发现层（P0，永不降频）", "social": "社交热度层",
        "audit": "审计层", "burst": "警报爆发层", "s2": "S2 强信号层",
        "s1": "S1 信号层", "s0": "S0 观察层", "watching": "观察池层",
        "reject": "拒绝样本层",
    }
    for tier, tlabel in tier_labels.items():
        add(_p(f"scheduler.tiers.{tier}.max_rpm", "float", f"{tlabel} rpm 上限",
               "collect", lo=0.5, hi=100.0, unit="rpm"))
        add(_p(f"scheduler.tiers.{tier}.interval_sec", "int", f"{tlabel} 轮询间隔",
               "collect", lo=0, hi=86400, unit="秒",
               desc="0 表示按需触发（如审计层）"))
    add(_p("scheduler.burst_window_sec", "int", "警报后高频采样时长", "collect",
           lo=60, hi=3600, unit="秒",
           desc="触发警报的币临时进入最高频采样，服务延迟入场收益与纸面成交"))
    add(_p("scheduler.onboarding_max_per_min", "int", "存量币入库限速", "collect",
           lo=10, hi=500, unit="个/分钟",
           desc="冷启动时列表涌入的存量币建档限速，防止瞬时打爆写队列"))

    # ── 内存与容量 ─────────────────────────────────────────────────────
    add(_p("registry.max_tokens_in_memory", "int", "内存代币上限", "capacity",
           lo=200, hi=20000,
           desc="超出后按价值最低优先淘汰内存视图；数据库历史完整保留。"
                "每 1000 币约占 20MB 内存，容器上限 512MB"))
    add(_p("registry.evict_batch", "int", "单次淘汰批量", "capacity", lo=10, hi=1000))
    add(_p("registry.restore_limit", "int", "重启恢复代币数", "capacity",
           lo=0, hi=10000))
    add(_p("registry.restore_max_age_hours", "int", "重启恢复时间窗", "capacity",
           lo=1, hi=336, unit="小时"))

    # ── 快照与磁盘 ─────────────────────────────────────────────────────
    add(_p("features.snapshot_min_interval_sec", "int", "高价值状态快照间隔", "disk",
           lo=10, hi=3600, unit="秒",
           desc="S1 及以上状态的基准写盘间隔；它们的密集历史是复盘原料"))
    for st, slabel in (("DISCOVERED", "新发现"), ("WATCHING", "观察池"),
                       ("S0", "S0"), ("DORMANT", "沉寂"), ("DEAD", "死亡"),
                       ("BLOCKED", "已拦截")):
        add(_p(f"features.snapshot_min_interval_by_state.{st}", "int",
               f"{slabel}状态快照间隔", "disk", lo=30, hi=86400, unit="秒",
               desc="低价值状态放宽写盘频率以控制磁盘增长；"
                    "状态变更与 Near-Miss 不受此限"))
    add(_p("storage.downsample_after_hours", "float", "降采样启动时点", "disk",
           lo=1, hi=720, unit="小时", desc="超过该时长的快照按间隔抽稀"))
    add(_p("storage.downsample_interval_sec", "int", "降采样保留间隔", "disk",
           lo=60, hi=86400, unit="秒"))
    add(_p("storage.raw_list_retention_hours", "float", "原始列表响应留存", "disk",
           lo=1, hi=720, unit="小时"))
    add(_p("storage.raw_detail_retention_days", "float", "原始详情响应留存", "disk",
           lo=1, hi=1000, unit="天"))
    add(_p("storage.cleanup_interval_sec", "int", "清理任务间隔", "disk",
           lo=300, hi=86400, unit="秒"))
    add(_p("storage.reject_sample_ratio", "float", "拒绝样本采样比例", "disk",
           lo=0.0, hi=1.0, desc="被风险门拦下的币按此比例持续低频追踪，用于反事实研究"))

    # ── 风险门 ─────────────────────────────────────────────────────────
    add(_p("risk.execution_blocker.audit_risk_level_min", "int",
           "审计风险硬拒等级", "risk", lo=1, hi=5,
           desc="币安审计 riskLevel 达到该值即硬拒，永不产生交易型警报"))
    add(_p("risk.execution_blocker.sell_tax_max_pct", "float", "卖出税上限", "risk",
           lo=0, hi=100, unit="%"))
    add(_p("risk.execution_blocker.buy_tax_max_pct", "float", "买入税上限", "risk",
           lo=0, hi=100, unit="%"))
    add(_p("risk.execution_blocker.honeypot_blocks", "bool", "蜜罐硬拒", "risk"))
    add(_p("risk.research_gate.top10_max_pct_by_age", "json",
           "Top10 集中度分年龄阈值", "risk",
           desc='JSON 数组，按年龄从小到大排列，最后一档 max_age_min 为 null。'
                '例：[{"max_age_min":30,"threshold":65.0},'
                '{"max_age_min":null,"threshold":45.0}]'))
    add(_p("risk.research_gate.combined_concentration_max_pct", "float",
           "组合集中度上限", "risk", lo=0, hi=100, unit="%",
           desc="dev + sniper + insider + bundler 合计占比"))
    add(_p("risk.research_gate.dev_max_pct", "float", "开发者持仓上限", "risk",
           lo=0, hi=100, unit="%"))
    add(_p("risk.research_gate.min_liquidity_usd", "float", "最低流动性", "risk",
           lo=0, hi=10_000_000, unit="USD"))
    add(_p("risk.research_gate.min_liquidity_mc_ratio", "float",
           "最低流动性/市值比", "risk", lo=0, hi=1))
    add(_p("risk.research_gate.wash_trading_tags", "str_list",
           "洗盘标签黑名单", "risk",
           desc="内部标签名（见 domain/tags.py），命中即进研究降级门；"
                "拼写错误会让该规则静默失效，修改前先核对标签表"))
    add(_p("risk.audit.min_liquidity_usd", "float", "审计触发流动性门槛", "risk",
           lo=0, hi=10_000_000, unit="USD",
           desc="低于该流动性的币不消耗审计接口配额"))
    add(_p("risk.audit.recheck_before_s1", "bool", "晋升 S1 前复查审计", "risk"))
    add(_p("risk.audit.ttl_sec", "int", "审计结果有效期", "risk",
           lo=3600, hi=864000, unit="秒"))

    # ── 数据质量 ───────────────────────────────────────────────────────
    fresh_groups = (
        ("market", "行情"), ("holders", "持有人"), ("distribution", "筹码分布"),
        ("smart_money", "聪明钱"), ("social", "社交"), ("audit", "审计"),
        ("supply", "供应量"),
    )
    for key, klabel in fresh_groups:
        add(_p(f"quality.freshness.{key}.fresh_sec", "int",
               f"{klabel}数据新鲜期", "quality", lo=30, hi=2_592_000, unit="秒"))
        add(_p(f"quality.freshness.{key}.stale_sec", "int",
               f"{klabel}数据过期线", "quality", lo=60, hi=5_184_000, unit="秒",
               desc="须大于新鲜期；超过后按超时程度线性罚分"))
        add(_p(f"quality.missing_penalty.{key}", "float",
               f"{klabel}缺失罚分", "quality", lo=0, hi=50))
        add(_p(f"quality.stale_penalty.{key}", "float",
               f"{klabel}过期最大罚分", "quality", lo=0, hi=50))
    add(_p("quality.mc_deviation_warn", "float", "市值偏离警告线", "quality",
           lo=0.01, hi=1.0, desc="reported 与 price×supply 的偏离比例"))
    add(_p("quality.mc_deviation_conflict", "float", "市值偏离冲突线", "quality",
           lo=0.01, hi=2.0, desc="须大于警告线；超过即视为数据冲突并罚分"))
    add(_p("quality.mc_conflict_penalty", "float", "市值冲突罚分", "quality",
           lo=0, hi=50))
    add(_p("quality.min_for_s1", "float", "S1 晋升质量闸门", "quality",
           lo=0, hi=100, desc="数据质量低于该分禁止晋升 S1，不可被高机会分覆盖"))
    add(_p("quality.min_for_s2", "float", "S2 晋升质量闸门", "quality",
           lo=0, hi=100, desc="须 ≥ S1 闸门"))

    # ── 评分与状态机 ───────────────────────────────────────────────────
    weight_labels = {
        "holder_momentum": "持有人动量", "capital_flow": "资金流",
        "smart_money": "聪明钱", "liquidity_quality": "流动性质量",
        "distribution_health": "筹码健康度", "social_momentum": "社交动量",
        "valuation_upside": "估值空间",
    }
    for key, wlabel in weight_labels.items():
        add(_p(f"scoring.opportunity_weights.{key}", "float",
               f"权重·{wlabel}", "strategy", lo=0, hi=100,
               desc="按总和归一化，无需凑整 100；总和必须大于 0"))
    for key, vlabel in (("base_mc", "基准情景"), ("strong_mc", "强势情景"),
                        ("viral_mc", "病毒情景"), ("mania_mc", "狂热情景")):
        add(_p(f"scoring.valuation_scenarios.{key}", "float",
               f"估值情景·{vlabel}", "strategy", lo=100_000, hi=10_000_000_000,
               unit="USD", desc="止盈参考市值，四档须递增；是情景假设而非价格预测"))
    add(_p("features.min_market_cap_denominator", "float", "市值分母下限",
           "strategy", lo=1000, hi=1_000_000, unit="USD",
           desc="比率类特征的防爆参数：市值低于此值按此值计算"))
    add(_p("features.min_liquidity_denominator", "float", "流动性分母下限",
           "strategy", lo=100, hi=1_000_000, unit="USD"))
    add(_p("features.min_holder_denominator", "int", "持有人基数下限", "strategy",
           lo=1, hi=1000, desc="holders 1→3 不应被当作 +200% 强信号"))
    add(_p("features.winsorize_growth_cap", "float", "变化率截断上限", "strategy",
           lo=1, hi=100, unit="倍"))
    add(_p("features.lookback_windows_sec", "num_list", "特征回看窗口", "strategy",
           lo=60, hi=86400, ascending=True, unit="秒",
           desc="计算速度/加速度的历史窗口，须递增"))
    add(_p("state_machine.min_dwell_sec", "int", "状态最短驻留", "strategy",
           lo=0, hi=3600, unit="秒", desc="防抖动：进入新状态后至少驻留该时长"))
    add(_p("state_machine.exit_confirm_cycles", "int", "退出确认周期数", "strategy",
           lo=1, hi=10, desc="连续 N 个评分周期低于退出阈值才真正退出"))
    trans_fields: dict[str, tuple[tuple[str, str, float, float], ...]] = {
        "s0": (("enter_opportunity", "进入机会分", 0, 100),
               ("exit_opportunity", "退出机会分", 0, 100),
               ("max_rug_risk", "Rug 风险上限", 0, 100),
               ("min_data_quality", "数据质量下限", 0, 100)),
        "s1": (("enter_opportunity", "进入机会分", 0, 100),
               ("exit_opportunity", "退出机会分", 0, 100),
               ("max_rug_risk", "Rug 风险上限", 0, 100),
               ("min_data_quality", "数据质量下限", 0, 100),
               ("min_liquidity_usd", "最低流动性", 0, 10_000_000),
               ("min_confidence", "置信度下限", 0, 100)),
        "s2": (("enter_opportunity", "进入机会分", 0, 100),
               ("exit_opportunity", "退出机会分", 0, 100),
               ("max_rug_risk", "Rug 风险上限", 0, 100),
               ("min_data_quality", "数据质量下限", 0, 100),
               ("min_confidence", "置信度下限", 0, 100),
               ("min_smart_money_count", "最少聪明钱数", 0, 100)),
    }
    for state, fields in trans_fields.items():
        for fkey, flabel, lo, hi in fields:
            kind = "int" if fkey == "min_smart_money_count" else "float"
            if state == "s2":
                desc = ("【V2 起弃用】S2 改走确认制（见 S2 确认参数），"
                        "此键保留仅为向后兼容，不再参与晋升判定")
            elif fkey.startswith(("enter", "exit")):
                desc = "进入阈值必须高于退出阈值（滞回防抖）"
            else:
                desc = ""
            add(_p(f"state_machine.transitions.{state}.{fkey}", kind,
                   f"{state.upper()}·{flabel}", "strategy", lo=lo, hi=hi,
                   desc=desc))
    add(_p("state_machine.s2_confirmation.enabled", "bool",
           "S2 确认制开关", "strategy",
           desc="开启后 S1 为观察池、S2 由时间+行为确认晋升；"
                "关闭则回退 V1 的机会分晋升"))
    add(_p("state_machine.s2_confirmation.min_age_from_s1_sec", "int",
           "S2 确认·最短存活", "strategy", lo=60, hi=86400, unit="秒",
           desc="进入 S1 后至少存活该时长才可确认晋升 S2"))
    add(_p("state_machine.s2_confirmation.max_drawdown_from_peak_pct", "float",
           "S2 确认·回撤上限", "strategy", lo=5, hi=95, unit="%",
           desc="确认期最高价回撤超过该值视为破位，不予确认"))
    add(_p("state_machine.s2_confirmation.min_price_vs_anchor_ratio", "float",
           "S2 确认·锚点价比例下限", "strategy", lo=0.1, hi=1.5,
           desc="现价不得低于 S1 锚点价的该比例"))
    add(_p("state_machine.s2_confirmation.hard_veto_lp_drop_pct", "float",
           "S2 硬否决·LP 抽离", "strategy", lo=5, hi=90, unit="%",
           desc="流动性较 S1 锚点下降超过该值即时转入派发观察"))
    add(_p("state_machine.s2_confirmation.hard_veto_exit_rate", "float",
           "S2 硬否决·聪明钱离场率", "strategy", lo=10, hi=100, unit="%",
           desc="聪明钱离场率达到该值即时转入派发观察"))
    add(_p("state_machine.distribution.enter_score", "float",
           "派发·进入分", "strategy", lo=0, hi=100))
    add(_p("state_machine.distribution.exit_score", "float",
           "派发·退出分", "strategy", lo=0, hi=100, desc="须低于进入分"))
    add(_p("state_machine.dormant_after_stale_sec", "int", "沉寂判定时长",
           "strategy", lo=600, hi=604800, unit="秒",
           desc="数据停更超过该时长进 DORMANT（可复活）"))
    add(_p("state_machine.dead_min_liquidity_usd", "float", "死亡流动性线",
           "strategy", lo=0, hi=100_000, unit="USD",
           desc="流动性低于该值判定 DEAD；DEAD 仅限硬性死亡"))
    add(_p("state_machine.dead_price_collapse_pct", "float", "死亡崩塌跌幅",
           "strategy", lo=50, hi=99.9, unit="%",
           desc="价格较近期高点跌幅超过该值且连续确认后判定 DEAD"))
    add(_p("state_machine.dead_confirm_cycles", "int", "死亡确认周期数",
           "strategy", lo=1, hi=10,
           desc="价格崩塌需连续 N 次评估确认才判死，防单次坏数据误杀"))

    # ── 警报与邮件 ─────────────────────────────────────────────────────
    add(_p("alerts.near_miss_margin", "float", "Near-Miss 边距", "alerts",
           lo=0, hi=20, desc="差该分数以内未触发的落库不发信，供阈值反事实研究"))
    add(_p("alerts.near_miss_cooldown_sec", "int", "Near-Miss 冷却", "alerts",
           lo=60, hi=86400, unit="秒"))
    for kind_key, klabel in (("s1", "S1"), ("s2", "S2"),
                             ("distribution", "派发")):
        add(_p(f"alerts.cooldown_sec.{kind_key}", "int",
               f"{klabel} 警报冷却", "alerts", lo=60, hi=86400, unit="秒",
               desc="同一代币同类警报的最小间隔"))
    add(_p("alerts.distribution_gate.min_market_cap_usd", "float",
           "派发邮件·市值门槛", "alerts", lo=0, hi=10_000_000, unit="USD",
           desc="低于该市值不发派发邮件（警报照常落库）；已崩盘的币不值得通知"))
    add(_p("alerts.distribution_gate.min_liquidity_usd", "float",
           "派发邮件·流动性门槛", "alerts", lo=0, hi=1_000_000, unit="USD",
           desc="低于该流动性不发派发邮件；另要求此前发过 S1/S2 警报"))
    add(_p("alerts.anomaly.warmup_hours", "float", "异常检测冷启动期", "alerts",
           lo=1, hi=720, unit="小时", desc="累计运行不足该时长时只记录不告警"))
    add(_p("alerts.anomaly.baseline_window_hours", "float", "异常检测基线窗口",
           "alerts", lo=24, hi=2160, unit="小时"))
    add(_p("alerts.anomaly.deviation_multiple", "float", "异常偏离倍数", "alerts",
           lo=2, hi=100, desc="信号速率偏离历史基线该倍数时告警"))
    add(_p("email.enabled", "bool", "启用邮件通知", "alerts"))
    add(_p("email.smtp_host", "str", "SMTP 服务器", "alerts",
           desc="凭据（账号/密码）只走 radar/.env，不在此配置"))
    add(_p("email.smtp_port", "int", "SMTP 端口", "alerts", lo=1, hi=65535))
    add(_p("email.from_name", "str", "发件人名称", "alerts"))
    add(_p("email.to", "str_list", "收件人列表", "alerts",
           desc="启用邮件时至少一个收件人"))
    add(_p("email.max_per_hour", "int", "每小时邮件上限", "alerts", lo=1, hi=120,
           desc="超量合并为摘要邮件，防狂热行情下的邮件风暴"))
    add(_p("email.digest_on_overflow", "bool", "超量转摘要", "alerts"))
    add(_p("email.digest_interval_sec", "int", "摘要最小间隔", "alerts",
           lo=60, hi=86400, unit="秒",
           desc="摘要是限速的溢出通道，必须自己节流，否则会架空每小时上限"))
    add(_p("email.send_s1", "bool", "发送 S1 警报邮件", "alerts",
           desc="V2 默认关闭：S1 是观察池，只落库并开启高频采样；"
                "确认级通知由 S2 发出"))
    add(_p("email.send_s2", "bool", "发送 S2 警报邮件", "alerts"))
    add(_p("email.send_distribution", "bool", "发送派发警报邮件", "alerts"))
    add(_p("email.send_weekly_report", "bool", "发送通知质量周报", "alerts",
           desc="每周汇总近 7 天推送的实际表现（RUG 率、命中率、中位峰值）"))
    add(_p("email.weekly_report_hour_local", "int", "周报发送时刻", "alerts",
           lo=0, hi=23, unit="点（本地时区，每周一）"))
    add(_p("email.daily_kpi_hour_local", "int", "每日 KPI 邮件时刻", "alerts",
           lo=0, hi=23, unit="点（本地时区）"))
    add(_p("email.outbox_max_retries", "int", "发信重试次数", "alerts", lo=0, hi=20))
    add(_p("email.outbox_retry_backoff_sec", "int", "发信重试退避", "alerts",
           lo=10, hi=3600, unit="秒"))

    # ── 追踪与观测 ─────────────────────────────────────────────────────
    add(_p("tracker.milestones_usd", "num_list", "市值里程碑", "tracking",
           lo=1000, hi=100_000_000_000, ascending=True, unit="USD",
           desc="仅记录首次上穿；档位须递增"))
    add(_p("tracker.milestone_hysteresis_pct", "float", "里程碑滞回", "tracking",
           lo=0, hi=20, unit="%", desc="防止在档位附近反复穿越刷事件"))
    add(_p("tracker.outcome_horizons_hours", "num_list", "Outcome 观察窗口",
           "tracking", lo=1, hi=8760, ascending=True, unit="小时"))
    add(_p("tracker.entry_delay_sec", "num_list", "延迟入场采样点", "tracking",
           lo=1, hi=3600, ascending=True, unit="秒",
           desc="模拟看到警报后 N 秒才入场的真实收益"))
    add(_p("tracker.sustained_min_hold_sec", "int", "持续 ATH 判定时长",
           "tracking", lo=30, hi=3600, unit="秒"))
    add(_p("tracker.sustained_min_volume_usd", "float", "持续 ATH 成交额门槛",
           "tracking", lo=0, hi=1_000_000, unit="USD"))
    add(_p("tracker.paper_position_sizes", "num_list", "纸面仓位规模", "tracking",
           lo=10, hi=100_000, ascending=True, unit="USD"))
    add(_p("tracker.liquidity_slippage_factor", "float", "滑点惩罚系数", "tracking",
           lo=0, hi=10, desc="仓位占流动性比例的滑点估计上界，非真实报价"))
    add(_p("observability.log_level", "choice", "日志级别", "tracking",
           choices=("DEBUG", "INFO", "WARNING", "ERROR")))
    add(_p("observability.log_file_max_mb", "int", "日志单文件上限", "tracking",
           lo=8, hi=512, unit="MB"))
    add(_p("observability.log_file_backup_count", "int", "日志轮转保留数",
           "tracking", lo=1, hi=50))

    return tuple(out)


PARAMS: tuple[Param, ...] = _build_params()
PARAMS_BY_PATH: dict[str, Param] = {p.path: p for p in PARAMS}


# ═════════════════════════════════════════════════════════════════════════
# 路径与合并
# ═════════════════════════════════════════════════════════════════════════

_MISSING = object()


def get_path(cfg: Any, path: str, default: Any = None) -> Any:
    """按点号路径取值；列表节点按元素的 id 字段寻址（用于 chains）。"""
    node = cfg
    for seg in path.split("."):
        if isinstance(node, list):
            node = next(
                (x for x in node
                 if isinstance(x, dict) and str(x.get("id")) == seg),
                _MISSING,
            )
        elif isinstance(node, dict):
            node = node.get(seg, _MISSING)
        else:
            return default
        if node is _MISSING:
            return default
    return node


def set_path(tree: dict[str, Any], path: str, value: Any) -> None:
    """在覆盖树（纯嵌套字典）里写入一个叶子。"""
    parts = path.split(".")
    node = tree
    for seg in parts[:-1]:
        nxt = node.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            node[seg] = nxt
        node = nxt
    node[parts[-1]] = value


def remove_path(tree: dict[str, Any], path: str) -> None:
    """删除覆盖树里的一个叶子，并清掉因此变空的中间节点。"""
    parts = path.split(".")
    stack: list[tuple[dict[str, Any], str]] = []
    node: Any = tree
    for seg in parts[:-1]:
        if not isinstance(node, dict) or seg not in node:
            return
        stack.append((node, seg))
        node = node[seg]
    if isinstance(node, dict):
        node.pop(parts[-1], None)
    while stack:
        parent, key = stack.pop()
        if isinstance(parent.get(key), dict) and not parent[key]:
            del parent[key]


def iter_leaves(tree: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """展开覆盖树为 (点号路径, 值) 列表。非字典值即叶子（含列表）。"""
    out: list[tuple[str, Any]] = []
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.extend(iter_leaves(value, path))
        else:
            out.append((path, value))
    return out


def deep_merge(base: Any, override: Any) -> Any:
    """深合并：覆盖树叠在默认配置上，返回新对象（不改动入参）。

    特殊规则：默认值是"带 id 的字典列表"（chains）而覆盖值是字典时，
    按 id 对列表元素做局部合并——这让 chains.56.enabled 这样的路径
    可以只覆盖单条链的单个字段。
    普通列表被覆盖时整体替换（列表语义上是原子值）。
    """
    if isinstance(base, list) and isinstance(override, dict):
        merged = []
        for item in base:
            if isinstance(item, dict) and str(item.get("id")) in override:
                patch = override[str(item["id"])]
                merged.append(deep_merge(item, patch)
                              if isinstance(patch, dict) else item)
            else:
                merged.append(item)
        return merged
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = deep_merge(base[key], value) if key in base else value
        return out
    return override


def effective_hash(effective: dict[str, Any]) -> str:
    """生效配置的规范化指纹。

    对合并结果的排序 JSON 计算，而不是对文件字节——
    这样"默认值 + 覆盖"的任何组合都有唯一指纹，
    且 yaml 注释、空行这类无语义变化不再扰动指纹。
    """
    blob = json.dumps(effective, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ═════════════════════════════════════════════════════════════════════════
# 校验
# ═════════════════════════════════════════════════════════════════════════

def validate_value(param: Param, value: Any) -> tuple[Any, str | None]:
    """单值校验与规范化。返回 (规范化后的值, 错误信息或 None)。"""
    if param.kind == "bool":
        if not isinstance(value, bool):
            return None, "必须是布尔值"
        return value, None

    if param.kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "必须是数字"
        if param.kind == "int":
            if float(value) != int(value):
                return None, "必须是整数"
            value = int(value)
        else:
            value = float(value)
        return _check_bounds(param, value)

    if param.kind == "str":
        if not isinstance(value, str):
            return None, "必须是字符串"
        value = value.strip()
        if not value and not param.allow_empty:
            return None, "不能为空"
        return value, None

    if param.kind == "choice":
        assert param.choices is not None
        if value not in param.choices:
            return None, f"必须是 {list(param.choices)} 之一"
        return value, None

    if param.kind == "num_list":
        if not isinstance(value, list) or not value:
            return None, "必须是非空数字数组"
        cleaned: list[float] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                return None, "数组元素必须是数字"
            _, err = _check_bounds(param, float(item))
            if err:
                return None, f"元素 {item}: {err}"
            cleaned.append(item)
        if param.ascending and any(
            b <= a for a, b in zip(cleaned, cleaned[1:])
        ):
            return None, "数组必须严格递增"
        return cleaned, None

    if param.kind == "str_list":
        if not isinstance(value, list):
            return None, "必须是字符串数组"
        cleaned_s = [str(x).strip() for x in value if str(x).strip()]
        if not cleaned_s and not param.allow_empty:
            return None, "不能为空"
        return cleaned_s, None

    if param.kind == "json":
        validator = _JSON_VALIDATORS.get(param.path)
        if validator is None:
            return None, "该参数暂不支持写入"
        return validator(value)

    return None, f"未知参数类型: {param.kind}"


def _check_bounds(param: Param, value: float) -> tuple[Any, str | None]:
    if param.lo is not None and value < param.lo:
        return None, f"不能小于 {param.lo}"
    if param.hi is not None and value > param.hi:
        return None, f"不能大于 {param.hi}"
    return value, None


def _validate_top10_by_age(value: Any) -> tuple[Any, str | None]:
    if not isinstance(value, list) or not value:
        return None, "必须是非空 JSON 数组"
    cleaned: list[dict[str, Any]] = []
    prev_age: float | None = None
    for i, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"max_age_min", "threshold"}:
            return None, f"第 {i + 1} 项必须只含 max_age_min 与 threshold 两个字段"
        age = item["max_age_min"]
        threshold = item["threshold"]
        if age is not None and (isinstance(age, bool)
                                or not isinstance(age, (int, float)) or age <= 0):
            return None, f"第 {i + 1} 项 max_age_min 必须是正数或 null"
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                or not (0 < float(threshold) <= 100):
            return None, f"第 {i + 1} 项 threshold 必须在 (0, 100] 内"
        if age is None and i != len(value) - 1:
            return None, "max_age_min 为 null 的兜底档必须放在最后"
        if age is not None:
            if prev_age is not None and age <= prev_age:
                return None, "max_age_min 必须严格递增"
            prev_age = float(age)
        cleaned.append({"max_age_min": age, "threshold": float(threshold)})
    if cleaned[-1]["max_age_min"] is not None:
        return None, "最后一档 max_age_min 必须为 null（兜底档）"
    return cleaned, None


_JSON_VALIDATORS: dict[str, Callable[[Any], tuple[Any, str | None]]] = {
    "risk.research_gate.top10_max_pct_by_age": _validate_top10_by_age,
}


def check_overrides(tree: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """校验整棵覆盖树。返回 (规范化后的覆盖树, 按路径的错误表)。

    未知路径一律报错而不是静默忽略：写错一个字母的参数名
    如果被悄悄丢弃，用户会以为自己已经改了配置。
    """
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    if tree in (None, {}):
        return clean, errors
    if not isinstance(tree, dict):
        return clean, {"<root>": "覆盖配置必须是字典"}
    for path, value in iter_leaves(tree):
        param = PARAMS_BY_PATH.get(path)
        if param is None:
            errors[path] = "未知或不可配置的参数路径"
            continue
        coerced, err = validate_value(param, value)
        if err:
            errors[path] = err
        else:
            set_path(clean, path, coerced)
    return clean, errors


def cross_validate(effective: dict[str, Any]) -> list[str]:
    """跨字段一致性校验，作用在合并后的生效配置上。"""
    errors: list[str] = []

    def num(path: str, default: float = 0.0) -> float:
        value = get_path(effective, path, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    if num("scheduler.target_rpm") > num("scheduler.global_rpm"):
        errors.append("常态目标请求量不能超过全局硬上限")
    if num("scheduler.adaptive.min_rpm") > num("scheduler.target_rpm"):
        errors.append("降速下限不能超过常态目标请求量")

    for state in ("s0", "s1", "s2"):
        enter = num(f"state_machine.transitions.{state}.enter_opportunity")
        exit_ = num(f"state_machine.transitions.{state}.exit_opportunity")
        if enter <= exit_:
            errors.append(f"{state.upper()} 进入阈值必须高于退出阈值（滞回防抖）")
    if num("state_machine.distribution.enter_score") <= num(
            "state_machine.distribution.exit_score"):
        errors.append("派发进入分必须高于退出分")

    freshness = get_path(effective, "quality.freshness", {}) or {}
    for key, cfg in freshness.items():
        if isinstance(cfg, dict) and float(cfg.get("fresh_sec", 0)) >= float(
                cfg.get("stale_sec", float("inf"))):
            errors.append(f"数据质量·{key}: 新鲜期必须小于过期线")
    if num("quality.mc_deviation_warn") >= num("quality.mc_deviation_conflict"):
        errors.append("市值偏离警告线必须小于冲突线")
    if num("quality.min_for_s1") > num("quality.min_for_s2"):
        errors.append("S1 质量闸门不能高于 S2 质量闸门")

    weights = get_path(effective, "scoring.opportunity_weights", {}) or {}
    try:
        total = sum(float(v) for v in weights.values())
    except (TypeError, ValueError):
        total = 0.0
    if total <= 0:
        errors.append("评分权重总和必须大于 0")

    scenarios = [num(f"scoring.valuation_scenarios.{k}")
                 for k in ("base_mc", "strong_mc", "viral_mc", "mania_mc")]
    if any(b <= a for a, b in zip(scenarios, scenarios[1:])):
        errors.append("四档估值情景必须严格递增")

    chains = get_path(effective, "chains", []) or []
    if not any(isinstance(c, dict) and c.get("enabled") for c in chains):
        errors.append("至少启用一条链")

    if get_path(effective, "email.enabled") and not (
            get_path(effective, "email.to") or []):
        errors.append("启用邮件时收件人列表不能为空")

    if num("storage.downsample_after_hours") <= 0:
        errors.append("降采样启动时点必须大于 0")

    return errors


# ═════════════════════════════════════════════════════════════════════════
# 前端描述
# ═════════════════════════════════════════════════════════════════════════

def describe(defaults: dict[str, Any],
             overrides: dict[str, Any]) -> list[dict[str, Any]]:
    """生成配置页所需的完整描述：分组 → 参数 → 默认值/生效值/覆盖标记。"""
    effective = deep_merge(defaults, overrides) if overrides else defaults
    groups: list[dict[str, Any]] = []
    for gid, glabel, gdesc in GROUPS:
        params: list[dict[str, Any]] = []
        for p in PARAMS:
            if p.group != gid:
                continue
            default_value = get_path(defaults, p.path)
            params.append({
                "path": p.path,
                "kind": p.kind,
                "label": p.label,
                "desc": p.desc,
                "lo": p.lo,
                "hi": p.hi,
                "choices": list(p.choices) if p.choices else None,
                "ascending": p.ascending,
                "unit": p.unit,
                "restart_required": p.restart_required,
                "default": default_value,
                "value": get_path(effective, p.path, default_value),
                "overridden": get_path(overrides, p.path, _MISSING)
                is not _MISSING,
            })
        groups.append({
            "id": gid, "label": glabel, "desc": gdesc, "params": params,
        })
    return groups
