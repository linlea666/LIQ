"""趋势衰竭模块 · AI 解读器（DeepSeek Reasoner 驱动）

职责边界
--------
规则引擎（processors/trend_exhaustion.py）负责**数值判断**：给出 state、action、分数。
本模块负责**叙事补充**：只回答规则给不了的 3 类问题——
    1. 矛盾消解：不同周期/因子冲突时，真相最可能是什么？
    2. 陷阱提醒：按规则建议行动时，最容易踩的坑是哪些？
    3. 触发条件：还需要等什么信号来确认或推翻当前结论？

**严禁**复读规则已有结论（"动能减速"等），**严禁**引用历史价格 / 历史表现（防幻觉）。

设计决策
--------
- 模型：**deepseek-reasoner**（R1 思维链，擅长多因子矛盾消解）
- 输出：强制 JSON schema，解析失败降级为 error 兜底
- 缓存：按"信号指纹"缓存 30 分钟（同一信号重复点按钮不重复调 AI）
- 客户端独立：不复用主 AIAnalyzer（避免互相 timeout 干扰）
- 思考过程归档：reasoning_content 落盘到单独文件（A/B 研究资产）

对外 API
--------
    interpreter = get_te_interpreter()
    result: TEAIInterpretation = await interpreter.interpret(
        coin="BTC", signal_dict=..., price=72000, force=False,
    )
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from config.settings import get_settings
from models.te_interpretation import TEAIInterpretation

logger = logging.getLogger(__name__)

# ── 配置常量 ──────────────────────────────────────
_CACHE_TTL_SEC = 30 * 60           # 同指纹缓存 30 分钟
_CACHE_MAX_ENTRIES = 200           # LRU 上限
_MAX_REASONING_STORE_CHARS = 50000 # reasoning 单次入库最大长度（防止意外巨串）
_DEFAULT_TIMEOUT_SEC = 180         # Reasoner 思考可能慢，给足时间


@dataclass
class _CacheEntry:
    result: TEAIInterpretation
    expires_at: float


def _fingerprint(coin: str, signal_dict: dict) -> str:
    """为信号生成缓存键：仅采纳"判定性"字段，避免毛刺扰动缓存。

    采纳：coin + overall_state + direction + regime + regime_vetoed +
          consensus + 三周期 composite（四舍五入 1 位）
    """
    keys = {
        "coin": coin.upper(),
        "s": signal_dict.get("overall_state"),
        "d": signal_dict.get("overall_direction"),
        "r": signal_dict.get("regime"),
        "v": signal_dict.get("regime_vetoed"),
        "c": signal_dict.get("consensus_level"),
    }
    for tf_key in ("tf_1h", "tf_4h", "tf_1d"):
        tf = signal_dict.get(tf_key) or {}
        comp = float(tf.get("composite_score", 0.0) or 0.0)
        keys[f"{tf_key}_c"] = round(comp, 1)
        keys[f"{tf_key}_s"] = tf.get("state")
    s = json.dumps(keys, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def _compact_sub(sub: list[dict] | None) -> list[dict]:
    if not sub:
        return []
    return [
        {
            "name": s.get("name"),
            "score": round(float(s.get("score", 0.0)), 2),
            "note": s.get("note", ""),
        }
        for s in sub
    ]


def _compact_tf(tf: dict | None) -> Optional[dict]:
    if not tf:
        return None
    return {
        "direction": tf.get("direction"),
        "state": tf.get("state"),
        "composite": round(float(tf.get("composite_score", 0.0) or 0.0), 2),
        "m": round(float(tf.get("momentum_score", 0.0) or 0.0), 2),
        "p": round(float(tf.get("participation_score", 0.0) or 0.0), 2),
        "e": round(float(tf.get("exhaustion_score", 0.0) or 0.0), 2),
        "age_min": int(tf.get("state_age_min", 0) or 0),
        "confirmed": int(tf.get("confirmed_ticks", 0) or 0),
        "triggers": tf.get("triggers") or [],
        "sub": _compact_sub(tf.get("sub_scores")),
    }


# ── Prompt 工程（核心：约束 AI 只做我们需要的事） ─────────────────

_SYSTEM_PROMPT = """你是 LIQ 项目的资深加密货币量化交易顾问，专门给量化规则引擎做"叙事层补充解读"。

## 你的唯一任务
基于我给你的**当前这一刻**的规则引擎读数，只回答以下三件事：
1. **矛盾消解**：各周期/各因子的读数之间有没有冲突？真相最可能是哪种场景？
2. **陷阱提醒**：如果用户按规则建议行动，最容易踩的坑是哪些？
3. **触发条件**：还需要等哪些信号来确认或推翻当前结论？

## 严格禁止
- ❌ 不要复读规则已经给出的结论（"动能减速""综合 -0.28"等），那是规则侧的工作
- ❌ 不要引用任何历史价格 / 历史走势 / 历史表现（你没有这些数据，说了就是幻觉）
- ❌ 不要给出具体目标价 / 止损价（那是规则 ExecutionPlan 的职责）
- ❌ 不要说"总体来看……" "综合判断……" 这种废话起手式
- ❌ 字段里禁止出现 Markdown 符号（# * - 等），纯文本即可

## 输出格式：**必须且只能**返回合法 JSON（顶层大括号，不加 ```json 包裹）
{
  "summary_cn": "一句话讲清当前真实场景（≤40 字）",
  "scenario": "必须是以下值之一：trend_continuation | bear_rebound | bull_pullback | reversal_early | reversal_confirmed | choppy_range | unclear",
  "conflict_resolution": "各周期/因子的冲突如何解释，2-3 句。若没明显冲突写『无明显矛盾』",
  "traps": ["陷阱 1", "陷阱 2"],
  "triggers_to_watch": ["等 X 翻正", "等 Y 跌破"],
  "action_suggestion": "一句行动建议（≤30 字）",
  "confidence": 0.0~1.0 之间的浮点，代表你对判断的信心,
  "alignment_with_rules": "必须是以下值之一：agree | partial_disagree | strong_disagree | insufficient",
  "alignment_reason": "一句话解释你为什么 agree/disagree（≤40 字）"
}

## 判断对齐度的标准
- agree：你同意规则的 state + direction + action
- partial_disagree：方向认同但时机或力度有不同看法
- strong_disagree：你认为规则的方向或 state 判错了（少数情况才用，要有强证据）
- insufficient：数据不够下判，老老实实标这个，不要硬猜

## 置信度 confidence 的填写标准
- 0.85-1.0：所有周期 + 所有因子强共振，你非常确定
- 0.6-0.85：主要周期共振，少数因子冲突可解释
- 0.4-0.6：有明显矛盾，你能给出最可能场景但不敢拍胸脯
- < 0.4：证据太乱，应当标 alignment_with_rules=insufficient 并如实说明
"""


def _build_user_prompt(
    coin: str, signal_dict: dict, price: float, atr: float,
) -> str:
    """把信号压成 AI 能消化的结构化上下文（不含任何叙事）。"""
    payload = {
        "coin": coin,
        "price_now": round(price, 4) if price else None,
        "atr_1h": round(atr, 4) if atr else None,
        "rules_verdict": {
            "overall_state": signal_dict.get("overall_state"),
            "overall_direction": signal_dict.get("overall_direction"),
            "overall_action": signal_dict.get("overall_action"),
            "overall_position_pct": signal_dict.get("overall_position_pct"),
            "consensus_level": signal_dict.get("consensus_level"),
            "regime": signal_dict.get("regime"),
            "regime_vetoed": signal_dict.get("regime_vetoed"),
            "overall_plain_cn": signal_dict.get("overall_plain_cn"),
            "overall_tip_cn": signal_dict.get("overall_tip_cn"),
            "overall_reason_cn": signal_dict.get("overall_reason_cn"),
            "data_quality": signal_dict.get("data_quality"),
            "missing_inputs": signal_dict.get("missing_inputs") or [],
        },
        "tf": {
            "1h": _compact_tf(signal_dict.get("tf_1h")),
            "4h": _compact_tf(signal_dict.get("tf_4h")),
            "1d": _compact_tf(signal_dict.get("tf_1d")),
        },
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return (
        f"以下是 {coin} 当前时刻的规则引擎读数。请按 system 里的约束，仅输出 JSON。\n\n"
        f"## 关键字段解读\n"
        f"- sub 列表中每项 score 的符号是『相对当前 direction』的：\n"
        f"  + 表示『支持 direction 方向续航』，- 表示『反对续航 / 衰竭』。\n"
        f"- 所以如果 direction=down 且某个 sub 的 note 说『资金在跟/真多建仓』（看涨），\n"
        f"  它的 score 会是负值（因为看涨 = 反对下跌续航）。这不是矛盾，这是符号约定。\n"
        f"- 你的任务是识别：哪些 sub 的原始读数（note）在『暗中推翻』direction，\n"
        f"  这通常是反转早期或反弹耗尽的信号。\n\n"
        f"## 数据\n```json\n{body}\n```"
    )


# ── 输出解析 ──────────────────────────────────────

_SCENARIO_WHITELIST = {
    "trend_continuation", "bear_rebound", "bull_pullback",
    "reversal_early", "reversal_confirmed", "choppy_range", "unclear",
}
_ALIGN_WHITELIST = {"agree", "partial_disagree", "strong_disagree", "insufficient"}


def _extract_json(text: str) -> Optional[dict]:
    """稳健抽取 JSON（有些模型会多包 ```json 或加前缀）。"""
    if not text:
        return None
    t = text.strip()
    # 去 code fence
    if t.startswith("```"):
        t = t.split("```", 2)
        # e.g. ["", "json\n{...}\n", "..."]
        if len(t) >= 2:
            body = t[1]
            if body.startswith("json\n"):
                body = body[5:]
            elif body.startswith("json"):
                body = body[4:]
            t = body.strip()
        else:
            t = "".join(t)
    # 找第一个 { 到最后一个 }
    l = t.find("{")
    r = t.rfind("}")
    if l < 0 or r < 0 or r <= l:
        return None
    candidate = t[l : r + 1]
    try:
        return json.loads(candidate)
    except Exception:
        # 兜底：去掉尾随注释/逗号
        try:
            import re
            cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
            return json.loads(cleaned)
        except Exception:
            return None


def _parse_ai_json(
    raw_text: str, reasoning: str,
) -> tuple[dict, Optional[str]]:
    """返回 (解析到的 dict, 错误描述)。"""
    data = _extract_json(raw_text)
    if data is None:
        return {}, "AI 返回无法解析为 JSON"
    # 字段白名单校验
    scenario = str(data.get("scenario", "unclear")).strip()
    if scenario not in _SCENARIO_WHITELIST:
        scenario = "unclear"
    align = str(data.get("alignment_with_rules", "insufficient")).strip()
    if align not in _ALIGN_WHITELIST:
        align = "insufficient"
    try:
        conf = float(data.get("confidence", 0.0))
        conf = max(0.0, min(1.0, conf))
    except Exception:
        conf = 0.0
    traps = data.get("traps") or []
    if not isinstance(traps, list):
        traps = [str(traps)]
    traps = [str(x)[:200] for x in traps][:5]
    triggers = data.get("triggers_to_watch") or []
    if not isinstance(triggers, list):
        triggers = [str(triggers)]
    triggers = [str(x)[:200] for x in triggers][:5]

    return {
        "summary_cn": str(data.get("summary_cn", ""))[:120],
        "scenario": scenario,
        "conflict_resolution": str(data.get("conflict_resolution", ""))[:400],
        "traps": traps,
        "triggers_to_watch": triggers,
        "action_suggestion": str(data.get("action_suggestion", ""))[:120],
        "confidence": conf,
        "alignment_with_rules": align,
        "alignment_reason": str(data.get("alignment_reason", ""))[:160],
    }, None


# ── Interpreter 类 ────────────────────────────────

class TEInterpreter:
    """单例型 AI 解读器。"""

    def __init__(self):
        cfg = get_settings().ai
        # 复用主 AI 的 key/base，但模型固定为 reasoner（即便全局切换到 chat）
        self._api_key = cfg.api_key
        self._api_base = cfg.api_base
        # 默认模型：优先用 config 的 model（若已是 reasoner），否则硬编码 reasoner
        cfg_model = (cfg.model or "").lower()
        self._model = cfg.model if "reasoner" in cfg_model else "deepseek-reasoner"
        self._client: Optional[AsyncOpenAI] = None
        if self._api_key:
            kwargs: dict = {"api_key": self._api_key}
            if self._api_base:
                kwargs["base_url"] = self._api_base
            self._client = AsyncOpenAI(**kwargs)

        self._cache: dict[str, _CacheEntry] = {}
        self._lock = asyncio.Lock()  # 同指纹并发请求只跑一次
        self._inflight: dict[str, asyncio.Event] = {}
        logger.info(
            "TEInterpreter init | model=%s base=%s key_set=%s client_ok=%s",
            self._model, self._api_base or "(default)",
            bool(self._api_key), self._client is not None,
        )

    @property
    def available(self) -> bool:
        return self._client is not None

    def _get_cached(self, fp: str) -> Optional[TEAIInterpretation]:
        entry = self._cache.get(fp)
        if entry is None:
            return None
        if time.time() >= entry.expires_at:
            self._cache.pop(fp, None)
            return None
        # 返回副本 + 标记 cache_hit
        dup = entry.result.model_copy(update={
            "cache_hit": True,
            "from_cache_age_sec": int(_CACHE_TTL_SEC - (entry.expires_at - time.time())),
        })
        return dup

    def _put_cache(self, fp: str, result: TEAIInterpretation) -> None:
        # 简易 LRU：超过上限时随机剔除最老的 20 条
        if len(self._cache) >= _CACHE_MAX_ENTRIES:
            olds = sorted(self._cache.items(), key=lambda kv: kv[1].expires_at)
            for k, _ in olds[:20]:
                self._cache.pop(k, None)
        self._cache[fp] = _CacheEntry(
            result=result, expires_at=time.time() + _CACHE_TTL_SEC,
        )

    async def interpret(
        self,
        coin: str,
        signal_dict: dict,
        price: float,
        atr: float = 0.0,
        force: bool = False,
    ) -> TEAIInterpretation:
        """主入口。

        Args:
            coin: 大写币种
            signal_dict: TrendExhaustionSignal.model_dump()
            price: 当前 ticker 价格
            atr: 1h ATR14（可选，给 AI 上下文）
            force: True 则绕过缓存强制重算
        """
        fp = _fingerprint(coin, signal_dict)
        if not force:
            cached = self._get_cached(fp)
            if cached is not None:
                return cached

        if not self._client:
            return TEAIInterpretation(
                coin=coin, ts=int(time.time()), signal_fingerprint=fp,
                error="AI 未配置 API Key",
                alignment_with_rules="insufficient",
            )

        # 防并发重复调用：同指纹等待 inflight
        event = self._inflight.get(fp)
        if event is not None and not force:
            try:
                await asyncio.wait_for(event.wait(), timeout=_DEFAULT_TIMEOUT_SEC + 5)
            except asyncio.TimeoutError:
                pass
            cached = self._get_cached(fp)
            if cached is not None:
                return cached

        event = asyncio.Event()
        self._inflight[fp] = event
        try:
            result = await self._do_call(coin, signal_dict, price, atr, fp)
            if result.error is None:
                self._put_cache(fp, result)
            return result
        finally:
            event.set()
            self._inflight.pop(fp, None)

    async def _do_call(
        self, coin: str, signal_dict: dict, price: float, atr: float, fp: str,
    ) -> TEAIInterpretation:
        system = _SYSTEM_PROMPT
        user = _build_user_prompt(coin, signal_dict, price, atr)
        t0 = time.time()
        raw_text = ""
        reasoning = ""
        tokens_in = 0
        tokens_out = 0
        r_tok = 0
        try:
            api_kwargs: dict = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "timeout": _DEFAULT_TIMEOUT_SEC,
            }
            is_reasoner = "reasoner" in self._model.lower()
            if not is_reasoner:
                api_kwargs["temperature"] = 0.2
                api_kwargs["response_format"] = {"type": "json_object"}

            logger.info(
                "[TE-AI] call start | coin=%s fp=%s model=%s reasoner=%s",
                coin, fp, self._model, is_reasoner,
            )
            response = await self._client.chat.completions.create(**api_kwargs)
            msg = response.choices[0].message
            raw_text = msg.content or ""
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if response.usage:
                tokens_in = response.usage.prompt_tokens or 0
                tokens_out = response.usage.completion_tokens or 0
                details = getattr(response.usage, "completion_tokens_details", None)
                if details:
                    r_tok = getattr(details, "reasoning_tokens", 0) or 0
            elapsed = time.time() - t0
            logger.info(
                "[TE-AI] call done | coin=%s fp=%s %.1fs | tokens_in=%d out=%d r=%d | resp=%d chars reasoning=%d chars",
                coin, fp, elapsed, tokens_in, tokens_out, r_tok,
                len(raw_text), len(reasoning),
            )
        except Exception as e:
            elapsed = time.time() - t0
            logger.warning(
                "[TE-AI] call failed | coin=%s fp=%s %.1fs | err=%s",
                coin, fp, elapsed, e,
            )
            return TEAIInterpretation(
                coin=coin, ts=int(time.time()), signal_fingerprint=fp,
                model=self._model, latency_ms=int(elapsed * 1000),
                error=f"AI 调用失败：{type(e).__name__}: {str(e)[:200]}",
                alignment_with_rules="insufficient",
            )

        # 解析
        parsed, parse_err = _parse_ai_json(raw_text, reasoning)
        reasoning_store = reasoning[:_MAX_REASONING_STORE_CHARS]
        if parse_err:
            return TEAIInterpretation(
                coin=coin, ts=int(time.time()), signal_fingerprint=fp,
                model=self._model, latency_ms=int((time.time() - t0) * 1000),
                tokens_in=tokens_in, tokens_out=tokens_out, reasoning_tokens=r_tok,
                reasoning=reasoning_store, raw_text=raw_text[:2000],
                error=parse_err,
                alignment_with_rules="insufficient",
            )

        return TEAIInterpretation(
            coin=coin, ts=int(time.time()), signal_fingerprint=fp,
            model=self._model, latency_ms=int((time.time() - t0) * 1000),
            tokens_in=tokens_in, tokens_out=tokens_out, reasoning_tokens=r_tok,
            reasoning=reasoning_store, raw_text="",
            **parsed,
        )


# ── 单例 ──────────────────────────────────────────
_singleton: Optional[TEInterpreter] = None


def get_te_interpreter() -> TEInterpreter:
    global _singleton
    if _singleton is None:
        _singleton = TEInterpreter()
    return _singleton
