"""D12 · 新闻智能 Agent · 轻量 chat 通道

职责：
  - 独立于主 AIAnalyzer 的轻量 chat 客户端（deepseek-v4-flash 非思考模式）
  - 供 Layer 2 结构化（news_structurer）和 Layer 3c 简报（news_brief）复用
  - 与主 AIAnalyzer 共享 DEEPSEEK_API_KEY，但独立 model / timeout / 限流

协议：
  实现 BaseAIAnalyzer 协议（news_structurer / news_brief 中定义）：
      async call_chat(*, system_prompt, user_prompt, temperature=0.2,
                      max_tokens=2000) -> tuple[raw_text, meta{tokens, latency_ms, model}]

落实日志锚点：
  - D.D12_DS_DUAL_TASK：
      * 初始化时 mark status=ok/warn 上报 key_set / model
      * 每次 call_chat 成功累计 chat_calls / total_tokens / avg_latency_ms
      * 连续失败 >=3 次 → status=warn
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from openai import AsyncOpenAI

from config.settings import get_settings

logger = logging.getLogger(__name__)


class NewsChatAnalyzer:
    """轻量 chat 通道（共享主 key · 独立限流）"""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        timeout_sec: Optional[int] = None,
        max_retries: Optional[int] = None,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> None:
        cfg = get_settings().ai
        news_cfg = cfg.news_agent

        self._model = model or news_cfg.model
        self._timeout = int(timeout_sec if timeout_sec is not None else news_cfg.timeout_sec)
        self._max_retries = int(max_retries if max_retries is not None else news_cfg.max_retries)
        self._default_temperature = float(news_cfg.temperature)

        effective_key = api_key or news_cfg.api_key or cfg.api_key
        effective_base = api_base or news_cfg.api_base or cfg.api_base

        self._client: Optional[AsyncOpenAI] = None
        if effective_key:
            kwargs: dict = {"api_key": effective_key}
            if effective_base:
                kwargs["base_url"] = effective_base
            self._client = AsyncOpenAI(**kwargs)

        # 运行时指标
        self._lock = threading.Lock()
        self._chat_calls = 0
        self._fail_streak = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_latency_ms = 0
        self._last_error: str = ""
        self._last_success_ts: int = 0

        logger.info(
            "NewsChatAnalyzer init | model=%s timeout=%ds retries=%d "
            "api_base=%s key_set=%s client_ok=%s",
            self._model, self._timeout, self._max_retries,
            effective_base or "(default)", bool(effective_key),
            self._client is not None,
        )

    # ── 属性 ──
    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return self._model

    # ── 主 API ──
    async def call_chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
        max_tokens: int = 2000,
    ) -> tuple[str, dict]:
        """返回 (raw_text, meta)。meta = {tokens, latency_ms, model, prompt_tokens, completion_tokens}"""
        if self._client is None:
            raise RuntimeError("NewsChatAnalyzer: API key not configured")

        temp = self._default_temperature if temperature is None else float(temperature)

        last_err: Optional[Exception] = None
        for attempt in range(1, max(1, self._max_retries) + 1):
            t0 = time.time()
            try:
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temp,
                    max_tokens=max_tokens,
                    timeout=self._timeout,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                latency_ms = int((time.time() - t0) * 1000)
                raw_text = (resp.choices[0].message.content or "") if resp.choices else ""
                pt = getattr(resp.usage, "prompt_tokens", 0) or 0
                ct = getattr(resp.usage, "completion_tokens", 0) or 0
                meta = {
                    "tokens": pt + ct,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "latency_ms": latency_ms,
                    "model": self._model,
                }
                self._tally_success(pt, ct, latency_ms)
                return raw_text, meta

            except Exception as e:  # noqa: BLE001
                last_err = e
                latency_ms = int((time.time() - t0) * 1000)
                logger.warning(
                    "[D12] news chat attempt %d/%d failed | %.0fms | %s: %s",
                    attempt, self._max_retries, latency_ms,
                    type(e).__name__, str(e)[:200],
                )
                # 429 / timeout 短暂冷却再重试
                if attempt < self._max_retries:
                    await asyncio.sleep(min(5.0, 0.5 * (2 ** (attempt - 1))))

        self._tally_failure(last_err)
        raise last_err if last_err else RuntimeError("news chat failed")

    # ── 指标 ──
    def snapshot_metrics(self) -> dict:
        with self._lock:
            return {
                "chat_calls": self._chat_calls,
                "fail_streak": self._fail_streak,
                "total_prompt_tokens": self._total_prompt_tokens,
                "total_completion_tokens": self._total_completion_tokens,
                "total_tokens": self._total_prompt_tokens + self._total_completion_tokens,
                "avg_latency_ms": (
                    int(self._total_latency_ms / self._chat_calls)
                    if self._chat_calls > 0 else 0
                ),
                "last_error": self._last_error,
                "last_success_ts": self._last_success_ts,
                "model": self._model,
                "available": self.available,
            }

    # ── 内部 ──
    def _tally_success(self, pt: int, ct: int, latency_ms: int) -> None:
        with self._lock:
            self._chat_calls += 1
            self._fail_streak = 0
            self._total_prompt_tokens += int(pt)
            self._total_completion_tokens += int(ct)
            self._total_latency_ms += int(latency_ms)
            self._last_error = ""
            self._last_success_ts = int(time.time())

    def _tally_failure(self, err: Optional[Exception]) -> None:
        with self._lock:
            self._fail_streak += 1
            self._last_error = f"{type(err).__name__}: {str(err)[:200]}" if err else "unknown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例（给 loop / structurer / brief 共享）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ANALYZER: Optional[NewsChatAnalyzer] = None
_LOCK = threading.Lock()


def get_news_chat_analyzer() -> NewsChatAnalyzer:
    global _ANALYZER
    if _ANALYZER is None:
        with _LOCK:
            if _ANALYZER is None:
                _ANALYZER = NewsChatAnalyzer()
    return _ANALYZER


def reset_news_chat_analyzer() -> None:
    """测试用"""
    global _ANALYZER
    with _LOCK:
        _ANALYZER = None


def create_news_chat_analyzer(**kwargs) -> NewsChatAnalyzer:
    """显式构造（测试 / 多实例场景）"""
    return NewsChatAnalyzer(**kwargs)
