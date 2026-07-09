"""Agent API Key（PRD §6）：生成、哈希、校验。

明文 Key 只在创建/重置的响应里出现一次，之后只存 hash。Key 本身足够长且随机
（192 位 urlsafe），不需要像密码那样加盐+慢哈希，普通 sha256 足够抗撞。
"""
import hashlib
import secrets
from datetime import datetime, timezone

KEY_PREFIX = "sk-"


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def verify_key(key: str, key_hash: str) -> bool:
    return secrets.compare_digest(hash_key(key), key_hash)


def display_prefix(key: str) -> str:
    """列表页脱敏展示用：取前缀几位，例如 "sk-Ab12Cd34"。"""
    return key[:10]


def check_key_status(key, now: datetime | None = None) -> str | None:
    """Invoke 入口用：校验一条 AgentApiKey 是否可用。返回 None=通过，否则返回拒绝原因。"""
    if key.status != "active":
        return "该 API Key 已停用"
    if key.expires_at:
        expires = key.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < (now or datetime.now(timezone.utc)):
            return "该 API Key 已过期"
    return None


def origin_allowed(allowed_origins: str, origin: str | None) -> bool:
    """allowed_origins 为空=不限制；否则按逗号分隔精确匹配请求的 Origin。"""
    allowed = [item.strip() for item in (allowed_origins or "").split(",") if item.strip()]
    if not allowed:
        return True
    return origin in allowed
