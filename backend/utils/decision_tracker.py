"""D1-D17 架构决策落实追踪器（Decision Tracker）

职责：
  - 集中管理 D1-D17 架构决策点的运行时落实状态
  - 各模块在关键路径上调用 `tracker.mark(D_id, status, **metrics)` 上报
  - 启动时打印清单 · 定期打印健康摘要 · 提供 JSON 供健康端点

使用约定（跨模块统一）：
  from utils.decision_tracker import get_tracker, D
  get_tracker().mark(D.D05_NEWS_SOURCES, status="ok", fetched=50, dedupe_dropped=3)

设计要点：
  1. 单例（全局唯一 tracker），跨模块共享状态
  2. "累加式" metrics（如 calls_total）与"当前快照式"（如 last_fetch_ts）并存
  3. 日志使用 `[Dxx]` 前缀，便于 grep
  4. 严格无侧效应：tracker 崩溃不应影响主流程（exc 兜底）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D1-D17 决策点常量（符号化，避免拼错）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class D:
    """D1-D17 决策点 ID 常量"""

    D01_REGIME        = "D01"  # 引入 MarketRegime 状态机
    D02_DUAL_ENGINE   = "D02"  # 七层漏斗 + 双引擎架构
    D03_SAFETY_GATE   = "D03"  # SafetyGate 5 道护栏
    D04_BACKTEST_LOOP = "D04"  # 回测闭环扩展
    D05_CASCADE_FIX   = "D05"  # 修复 cascade_risk 双重计数 bug
    D06_BEGINNER_UI   = "D06"  # 小白模式 ExecutionPlanCard
    D07_NEWS_SOURCES  = "D07"  # 新闻多源接入（OKX ×2）
    D08_NEWS_PIPELINE = "D08"  # 新闻三层消化流水线
    D09_NEWS_BRIEF    = "D09"  # 滚动新闻简报（AI 24h 记忆）
    D10_FLIP_FLOP     = "D10"  # Flip-Flop 反复拉扯检测
    D11_GEO_RISK      = "D11"  # 地缘风险 6 级专门模块
    D12_DS_DUAL_TASK  = "D12"  # DeepSeek 双任务共享 API Key
    D13_NEWS_AGENT    = "D13"  # News Intelligence Agent 独立运行
    D14_AI_TRADER     = "D14"  # AI 从审计员→独立交易员
    D15_FUSION        = "D15"  # Signal Fusion Layer L7.5
    D16_PHASED_ROADMAP = "D16"  # P0/P1/P2 分期路线
    D17_FACTOR_MATRIX = "D17"  # 多维度看盘表 7 板块


Status = Literal["pending", "in_progress", "ok", "warn", "failed", "skipped"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 记录模型
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DecisionDefinition(BaseModel):
    """决策点静态定义"""

    id: str
    title: str
    owner_module: str                    # 归属模块的主要文件/路径
    success_criteria: str                # 可检查的成功标准（供日志/文档）
    metrics_schema: list[str] = Field(default_factory=list)
    # 该决策期望上报的 metrics key 名，便于校验


class DecisionRecord(BaseModel):
    """决策点运行时记录"""

    id: str
    definition: DecisionDefinition
    status: Status = "pending"

    # 最新快照
    metrics: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""                     # 人类可读的一句话状态
    last_update_ts: int = 0              # 秒级
    last_ok_ts: int = 0                  # 最近一次 status=ok 时间
    last_warn_ts: int = 0
    last_fail_ts: int = 0

    # 累加计数
    total_marks: int = 0
    ok_count: int = 0
    warn_count: int = 0
    fail_count: int = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D1-D17 默认注册表
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DEFAULT_DEFINITIONS: list[DecisionDefinition] = [
    DecisionDefinition(
        id=D.D01_REGIME,
        title="引入 MarketRegime 状态机",
        owner_module="processors/market_regime.py",
        success_criteria="每次 _recompute 产出 RegimeSnapshot；regime 切换可追溯",
        metrics_schema=["regime", "confidence", "switch_count_1h"],
    ),
    DecisionDefinition(
        id=D.D02_DUAL_ENGINE,
        title="七层漏斗 + 双引擎架构",
        owner_module="engine.py _recompute",
        success_criteria="每次 recompute 生成 ExecutionPlan + AITraderReport 双份",
        metrics_schema=["math_plan_ok", "ai_report_ok", "pipeline_ms"],
    ),
    DecisionDefinition(
        id=D.D03_SAFETY_GATE,
        title="SafetyGate 5 道护栏",
        owner_module="processors/safety_gate.py",
        success_criteria="5 道护栏都跑；触发时 block_reason 明确",
        metrics_schema=["triggered", "g1", "g2", "g3", "g4", "g5"],
    ),
    DecisionDefinition(
        id=D.D04_BACKTEST_LOOP,
        title="回测闭环：分歧+单源胜率持续回填",
        owner_module="processors/signal_tracker.* + fusion",
        success_criteria="每日新增样本 > 0；historical_win_rate 可查",
        metrics_schema=["new_samples_24h", "total_samples", "win_rate"],
    ),
    DecisionDefinition(
        id=D.D05_CASCADE_FIX,
        title="修复 cascade_risk 双重计数（cascade_norm 4× 高估）",
        owner_module="processors/key_level_tracker_v2.py + confluence_scoring.py",
        success_criteria="修复后 A 级信号中 cascade_risk>60% 占比显著下降",
        metrics_schema=["cfg_cascade_norm", "a_tier_count", "a_tier_cascade_gt60_pct"],
    ),
    DecisionDefinition(
        id=D.D06_BEGINNER_UI,
        title="小白模式 ExecutionPlanCard",
        owner_module="frontend ExecutionPlanCard.tsx",
        success_criteria="前端默认展示一卡片：红绿灯+动作+仓位+关键位",
        metrics_schema=["renders", "fallback_to_advanced"],
    ),
    DecisionDefinition(
        id=D.D07_NEWS_SOURCES,
        title="新闻多源接入（OKX 行业 + 博主 ×2）",
        owner_module="sources/news/okx.py + registry.py",
        success_criteria="registry.get_all()>=2；每源最近成功时间 <30min",
        metrics_schema=["sources_registered", "last_fetch_age_sec", "items_24h"],
    ),
    DecisionDefinition(
        id=D.D08_NEWS_PIPELINE,
        title="新闻三层消化流水线（规则→AI→账本）",
        owner_module="news_filter + news_structurer + ledger",
        success_criteria="Layer1 过滤率 70-90%；Layer2 成功率 >95%",
        metrics_schema=[
            "raw_in", "layer1_kept", "layer1_pass_rate",
            "layer2_structured", "layer2_success_rate",
        ],
    ),
    DecisionDefinition(
        id=D.D09_NEWS_BRIEF,
        title="滚动新闻简报（AI 24h 记忆锚）",
        owner_module="processors/news_brief.py",
        success_criteria="每小时更新；char_count<3000；diff_from_prev 可读",
        metrics_schema=["version", "char_count", "based_on_events", "age_sec"],
    ),
    DecisionDefinition(
        id=D.D10_FLIP_FLOP,
        title="Flip-Flop 反复拉扯检测",
        owner_module="processors/narrative_tracker.py",
        success_criteria="24h 反复 ≥2 次的主题触发 warning",
        metrics_schema=["flip_flop_themes_count", "worst_theme", "worst_count_24h"],
    ),
    DecisionDefinition(
        id=D.D11_GEO_RISK,
        title="地缘风险 6 级专门模块",
        owner_module="processors/geo_risk_tracker.py",
        success_criteria="overall_level 可查；escalation 触发 SafetyGate 建议",
        metrics_schema=["overall_level", "active_themes", "escalation_24h", "has_blackswan"],
    ),
    DecisionDefinition(
        id=D.D12_DS_DUAL_TASK,
        title="DeepSeek 双任务共享 API Key",
        owner_module="ai/analyzer.py",
        success_criteria="deepseek-v4-flash（主 + 新闻，均为非思考模式）各自记录调用",
        metrics_schema=["main_ai_calls", "chat_calls", "main_ai_avg_ms", "chat_avg_ms"],
    ),
    DecisionDefinition(
        id=D.D13_NEWS_AGENT,
        title="News Intelligence Agent 独立 loop",
        owner_module="engine.py _news_agent_loop",
        success_criteria="独立周期运行（默认 15min）；失败不影响主 AI",
        metrics_schema=["cycle_count", "last_cycle_ms", "last_error"],
    ),
    DecisionDefinition(
        id=D.D14_AI_TRADER,
        title="AI 从审计员→独立交易员",
        owner_module="ai/analyzer.py + ai/snapshot.py",
        success_criteria="产出 AITraderReport；trading_plans ≥1；factor_matrix 完整",
        metrics_schema=["plans_count", "conviction", "factor_sections", "factor_rows"],
    ),
    DecisionDefinition(
        id=D.D15_FUSION,
        title="Signal Fusion Layer L7.5",
        owner_module="processors/signal_fusion.py",
        success_criteria="每次 recompute 产出 FinalDecision；consensus 显式展示",
        metrics_schema=["consensus", "conflict_rate_24h", "final_score"],
    ),
    DecisionDefinition(
        id=D.D16_PHASED_ROADMAP,
        title="P0/P1/P2 分期路线",
        owner_module="(tracking only)",
        success_criteria="手动维护：P0 全 ok → P1 开放 → P2 开放",
        metrics_schema=["current_phase", "p0_ok_count", "p1_ok_count"],
    ),
    DecisionDefinition(
        id=D.D17_FACTOR_MATRIX,
        title="多维度看盘表（7 板块 AIFactorMatrix）",
        owner_module="ai_trader_report.AIFactorMatrix",
        success_criteria="每次 AI 分析产出 7 板块（A-G），每板块 ≥1 行",
        metrics_schema=["sections", "total_rows", "missing_sections"],
    ),
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tracker 主体（单例）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DecisionTracker:
    """线程安全的单例追踪器"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, DecisionRecord] = {}
        self._last_summary_ts: int = 0
        self._summary_interval_sec: int = 3600  # 默认每小时打印汇总
        for d in _DEFAULT_DEFINITIONS:
            self._records[d.id] = DecisionRecord(id=d.id, definition=d)

    # ── 注册（允许外部追加/覆盖） ──
    def register(self, definition: DecisionDefinition) -> None:
        with self._lock:
            if definition.id in self._records:
                # 保留累加统计，仅刷新定义
                rec = self._records[definition.id]
                rec.definition = definition
            else:
                self._records[definition.id] = DecisionRecord(
                    id=definition.id, definition=definition
                )

    # ── 主 API：关键路径上报 ──
    def mark(
        self,
        d_id: str,
        status: Optional[Status] = None,
        detail: str = "",
        log: bool = True,
        **metrics: Any,
    ) -> None:
        """上报一次决策点状态。

        - status 可选；为 None 表示"仅刷新 metrics 不改状态"
        - detail 可选；一句话描述本次事件
        - log=True 时同步写一行 `[Dxx] owner | key=val ...` 日志
        """
        try:
            with self._lock:
                rec = self._records.get(d_id)
                if rec is None:
                    # 未注册的 id 也接受（退化模式，但打 warn）
                    logger.warning("[tracker] mark unknown decision id=%s", d_id)
                    rec = DecisionRecord(
                        id=d_id,
                        definition=DecisionDefinition(
                            id=d_id,
                            title="(unregistered)",
                            owner_module="?",
                            success_criteria="",
                        ),
                    )
                    self._records[d_id] = rec

                now = int(time.time())
                rec.total_marks += 1
                rec.last_update_ts = now
                if detail:
                    rec.detail = detail
                if metrics:
                    rec.metrics.update(metrics)

                if status is not None:
                    rec.status = status
                    if status == "ok":
                        rec.ok_count += 1
                        rec.last_ok_ts = now
                    elif status == "warn":
                        rec.warn_count += 1
                        rec.last_warn_ts = now
                    elif status == "failed":
                        rec.fail_count += 1
                        rec.last_fail_ts = now

            if log:
                self._log_mark(d_id, status, detail, metrics)
        except Exception as e:  # noqa: BLE001 — tracker 不能抛
            logger.error("[tracker] mark failed d_id=%s err=%s", d_id, e)

    # ── 启动清单 ──
    def log_boot(self) -> None:
        """启动时打印 D1-D17 清单到日志。"""
        logger.info("════════════════════════════════════════════════════════════════")
        logger.info(" LIQ · D1-D17 架构决策清单（Boot Report）")
        logger.info("════════════════════════════════════════════════════════════════")
        with self._lock:
            for d_id in sorted(self._records.keys()):
                rec = self._records[d_id]
                logger.info(
                    " %s %-8s │ %s │ status=%s",
                    rec.id, _status_badge(rec.status), rec.definition.title, rec.status,
                )
        logger.info("════════════════════════════════════════════════════════════════")

    # ── 周期健康摘要 ──
    def log_summary(self, force: bool = False) -> None:
        """按节流间隔打印健康摘要。force=True 忽略节流。"""
        now = int(time.time())
        with self._lock:
            if not force and (now - self._last_summary_ts) < self._summary_interval_sec:
                return
            self._last_summary_ts = now
            snapshot = list(self._records.values())

        logger.info("────────────────────────────────────────────────────────────────")
        logger.info(" LIQ · D1-D17 落实健康摘要 · t=%d", now)
        logger.info("────────────────────────────────────────────────────────────────")
        for rec in sorted(snapshot, key=lambda r: r.id):
            age = (now - rec.last_update_ts) if rec.last_update_ts > 0 else -1
            metrics_brief = self._format_metrics(rec.metrics, limit=4)
            logger.info(
                " %s %s │ status=%-6s age=%s │ ok=%d warn=%d fail=%d │ %s",
                rec.id,
                _status_badge(rec.status),
                rec.status,
                (f"{age}s" if age >= 0 else "—"),
                rec.ok_count,
                rec.warn_count,
                rec.fail_count,
                metrics_brief,
            )
        logger.info("────────────────────────────────────────────────────────────────")

    def set_summary_interval(self, sec: int) -> None:
        """配置汇总日志间隔（测试/开发可调小）"""
        with self._lock:
            self._summary_interval_sec = max(10, sec)

    # ── 供健康端点读取 ──
    def get_summary_dict(self) -> dict[str, Any]:
        # D16 是"聚合状态"——读取前先基于其它 16 项的当前状态刷新一次
        self._refresh_d16_phase_status()
        with self._lock:
            items: list[dict[str, Any]] = []
            for rec in sorted(self._records.values(), key=lambda r: r.id):
                items.append({
                    "id": rec.id,
                    "title": rec.definition.title,
                    "owner_module": rec.definition.owner_module,
                    "success_criteria": rec.definition.success_criteria,
                    "status": rec.status,
                    "detail": rec.detail,
                    "metrics": dict(rec.metrics),
                    "last_update_ts": rec.last_update_ts,
                    "last_ok_ts": rec.last_ok_ts,
                    "last_warn_ts": rec.last_warn_ts,
                    "last_fail_ts": rec.last_fail_ts,
                    "total_marks": rec.total_marks,
                    "ok_count": rec.ok_count,
                    "warn_count": rec.warn_count,
                    "fail_count": rec.fail_count,
                })
            return {
                "ts": int(time.time()),
                "decisions": items,
                "overall_health": _overall_health([rec.status for rec in self._records.values()]),
            }

    # ── D16 聚合状态自动推断 ──
    def _refresh_d16_phase_status(self) -> None:
        """D16 · P0/P1/P2 路线：根据 D01-D15 + D17 的当前状态自动聚合

        规则（**pending 不视为降级**，避免启动期自证循环产生 warn）：
          - 有 fail                         → failed / "P2_degraded"
          - 有 warn（其它项真实 warn）       → warn   / phase 按 ok_count 分段
          - 有 pending（启动/冷启）          → pending / "P0_booting" 或 "P1_rolling" 或 "P2_partial"
          - 全部 ok                         → ok     / "P2_done"
        """
        try:
            others = [
                D.D01_REGIME, D.D02_DUAL_ENGINE, D.D03_SAFETY_GATE,
                D.D04_BACKTEST_LOOP, D.D05_CASCADE_FIX, D.D06_BEGINNER_UI,
                D.D07_NEWS_SOURCES, D.D08_NEWS_PIPELINE, D.D09_NEWS_BRIEF,
                D.D10_FLIP_FLOP, D.D11_GEO_RISK, D.D12_DS_DUAL_TASK,
                D.D13_NEWS_AGENT, D.D14_AI_TRADER, D.D15_FUSION,
                D.D17_FACTOR_MATRIX,
            ]
            pending: list[str] = []
            ok_count = warn_count = fail_count = 0
            with self._lock:
                for did in others:
                    rec = self._records.get(did)
                    if rec is None or rec.status == "pending":
                        pending.append(did)
                    elif rec.status == "ok":
                        ok_count += 1
                    elif rec.status == "warn":
                        warn_count += 1
                    elif rec.status == "failed":
                        fail_count += 1

            def _phase_from_ok(ok: int) -> str:
                return "P0" if ok < 6 else "P1" if ok < 15 else "P2_partial"

            if fail_count > 0:
                phase_status: Status = "failed"
                phase = "P2_degraded"
            elif warn_count > 0:
                # 存在其它真实 warn，D16 也跟随 warn，phase 反映进度
                phase_status = "warn"
                phase = _phase_from_ok(ok_count)
            elif pending:
                # 仅启动/冷启中，不算降级
                phase_status = "pending"
                if ok_count == 0:
                    phase = "P0_booting"
                elif ok_count < 15:
                    phase = "P1_rolling"
                else:
                    phase = "P2_partial"
            else:
                phase_status = "ok"
                phase = "P2_done"
            self.mark(
                D.D16_PHASED_ROADMAP,
                status=phase_status,
                log=False,
                current_phase=phase,
                observed_ok=ok_count,
                observed_warn=warn_count,
                observed_fail=fail_count,
                pending_count=len(pending),
                pending_ids=",".join(pending) if pending else "",
            )
        except Exception:
            logger.debug("[D16] refresh phase status failed", exc_info=True)

    def get_record(self, d_id: str) -> Optional[DecisionRecord]:
        with self._lock:
            return self._records.get(d_id)

    # ── 辅助 ──
    def _log_mark(
        self,
        d_id: str,
        status: Optional[Status],
        detail: str,
        metrics: dict[str, Any],
    ) -> None:
        badge = _status_badge(status) if status else "·"
        owner = self._records[d_id].definition.owner_module if d_id in self._records else "?"
        kv = self._format_metrics(metrics, limit=8)
        msg = f"[{d_id}] {badge} {owner}"
        if detail:
            msg += f" │ {detail}"
        if kv:
            msg += f" │ {kv}"
        logger.info(msg)

    @staticmethod
    def _format_metrics(metrics: dict[str, Any], limit: int = 4) -> str:
        if not metrics:
            return ""
        parts: list[str] = []
        for i, (k, v) in enumerate(metrics.items()):
            if i >= limit:
                parts.append("...")
                break
            parts.append(f"{k}={_safe_val(v)}")
        return " ".join(parts)


def _safe_val(v: Any) -> str:
    try:
        if isinstance(v, float):
            return f"{v:.4g}"
        if isinstance(v, (dict, list)):
            s = json.dumps(v, ensure_ascii=False)
            return s if len(s) <= 80 else s[:77] + "..."
        return str(v)
    except Exception:  # noqa: BLE001
        return "<unserializable>"


def _status_badge(status: Optional[str]) -> str:
    return {
        "ok": "✓",
        "pending": "·",
        "in_progress": "…",
        "warn": "!",
        "failed": "✗",
        "skipped": "-",
    }.get(status or "pending", "?")


def _overall_health(statuses: list[str]) -> str:
    if any(s == "failed" for s in statuses):
        return "unhealthy"
    if any(s == "warn" for s in statuses):
        return "degraded"
    if all(s == "ok" for s in statuses):
        return "all_ok"
    return "partial"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例获取
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_tracker: Optional[DecisionTracker] = None
_tracker_lock = threading.Lock()


def get_tracker() -> DecisionTracker:
    """全局单例入口"""
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = DecisionTracker()
    return _tracker
