from urllib.parse import unquote, urlparse

from app.config import Settings


def test_password_only_redis_url_uses_default_acl_user(tmp_path):
    secret = tmp_path / "redis_password"
    secret.write_text("p@ss:/word", encoding="utf-8")

    settings = Settings(
        redis_url="redis://:{password}@127.0.0.1:6379/0",
        redis_password_file=str(secret),
    )
    parsed = urlparse(settings.redis_url)

    assert parsed.username == "default"
    assert unquote(parsed.password or "") == "p@ss:/word"


def test_named_redis_acl_user_is_preserved(tmp_path):
    secret = tmp_path / "redis_password"
    secret.write_text("secret", encoding="utf-8")

    settings = Settings(
        redis_url="rediss://memory-user:{password}@redis.internal:6380/2",
        redis_password_file=str(secret),
    )

    assert urlparse(settings.redis_url).username == "memory-user"
