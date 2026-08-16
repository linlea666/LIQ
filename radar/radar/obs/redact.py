"""日志脱敏器。

设计原则：脱敏发生在日志入口，而不是依赖每个调用点自觉。
两道防线：
  1. 键名匹配——字典中命中敏感键名的值直接替换。
  2. 值匹配——启动时登记真实凭据值，此后任何日志文本中出现该值都会被替换。
     这道防线专门用于兜住"异常信息里带出了完整 headers/密码"这类间接泄漏。

注意：加密货币语境下 "token" 是正常业务词（token_id / tokenName / contractAddress），
因此绝不能把 "token" 作为通用敏感词，否则会把业务数据全部打码。
只匹配无歧义的复合词（access_token / auth_token 等）。
"""

from __future__ import annotations

from typing import Any

MASK = "***"

# 无歧义的敏感键名片段（小写比较）
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "session_id",
    "cookie",
    "private_key",
    "privatekey",
    "seed_phrase",
    "mnemonic",
    "smtp_pass",
    "credential",
)

# 运行期登记的真实凭据值；长度过短的值不登记，避免误伤正常文本
_secret_values: set[str] = set()
_MIN_SECRET_LEN = 6


def register_secret(value: str | None) -> None:
    """登记一个需要在所有日志输出中被替换掉的真实值。"""
    if value and len(value) >= _MIN_SECRET_LEN:
        _secret_values.add(value)


def clear_secrets_for_tests() -> None:
    _secret_values.clear()


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def scrub_text(text: str) -> str:
    """替换文本中出现的已登记凭据值。"""
    if not text or not _secret_values:
        return text
    result = text
    for secret in _secret_values:
        if secret in result:
            result = result.replace(secret, MASK)
    return result


def redact(value: Any, _depth: int = 0) -> Any:
    """递归脱敏任意结构（dict / list / tuple / str / 标量）。

    深度上限防止异常结构导致栈溢出；超限直接转字符串处理。
    """
    if _depth > 8:
        return scrub_text(str(value))

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            out[key] = MASK if is_sensitive_key(key) else redact(v, _depth + 1)
        return out

    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]

    if isinstance(value, str):
        return scrub_text(value)

    return value


def redact_email(address: str) -> str:
    """收件人地址在日志里只保留可辨识的最小信息。"""
    if "@" not in address:
        return MASK
    local, _, domain = address.partition("@")
    head = local[:2] if len(local) > 2 else local[:1]
    return f"{head}{MASK}@{domain}"
