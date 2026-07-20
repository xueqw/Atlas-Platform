import os
from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


def _env_file_chain() -> tuple[str, ...]:
    """Load a shared secrets file while keeping worktree-local overrides.

    Git worktrees intentionally do not copy ignored ``.env`` files. A local
    ``ATLAS_SHARED_ENV_FILE`` pointer lets every worktree reuse the developer's
    existing secret source without duplicating or exposing key values.
    """
    shared = os.environ.get("ATLAS_SHARED_ENV_FILE", "").strip()
    if not shared and _ENV_FILE.exists():
        for raw in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = raw.partition("=")
            if separator and key.strip() == "ATLAS_SHARED_ENV_FILE":
                shared = value.strip().strip('"').strip("'")
                break
    files: list[str] = []
    if shared:
        shared_path = Path(shared).expanduser()
        if shared_path.exists():
            files.append(str(shared_path))
    files.append(str(_ENV_FILE))
    return tuple(files)


class Settings(BaseSettings):
    environment: str = "development"
    database_url: str = "sqlite:///./data/atlas.db"
    database_password_file: str = ""
    redis_url: str = "redis://localhost:6379/0"
    redis_password_file: str = ""
    redis_required: bool = False
    object_storage_backend: str = "local"
    object_storage_required: bool = False
    object_storage_endpoint: str = "localhost:9000"
    object_storage_access_key: str = ""
    object_storage_secret_key: str = ""
    object_storage_secure: bool = False
    object_storage_bucket: str = "atlas"
    local_storage_root: str = "./data/objects"
    agent_code_root: str = "./data/agent-code"

    code_runner_backend: str = "local"
    code_runner_required: bool = False
    code_runner_image: str = "atlas-agent-runner:py311"
    code_runner_timeout_seconds: int = 10
    code_runner_memory: str = "256m"
    code_runner_cpus: float = 0.5
    code_runner_pids_limit: int = 64

    short_memory_ttl_seconds: int = 86400
    long_memory_default_ttl_days: int = 365
    backup_retention_days: int = 14
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"

    # 知识库向量检索：embedding 走硅基流动 bge-m3
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    retrieval_min_score: float = 0.50

    # 飞书连接器（OAuth 2.0 授权码流程）
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_redirect_uri: str = "http://localhost:3001/api/connectors/feishu/callback"

    # 前后端分离部署参数
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"

    # 入口认证：账号密码 + httpOnly cookie 会话
    session_ttl_hours: int = 24 * 7
    test_account_password: str = "atlas123"

    # GitHub 连接器（通过官方远程 MCP Server）
    github_pat: str = ""
    github_mcp_url: str = "https://api.githubcopilot.com/mcp/"

    # 通用远程 MCP 连接器（connectors/remote_mcp.py REGISTRY 的 .env 回退 key）
    baidu_maps_api_key: str = ""
    amap_key: str = ""
    firecrawl_api_key: str = ""
    context7_api_key: str = ""

    app_port: int = 8000

    # LangGraph runtime migration. Disabled by default so legacy production
    # paths remain the rollback target until the runtime acceptance gates pass.
    langgraph_runtime_enabled: bool = False
    adaptive_runtime_enabled: bool = False
    langgraph_shadow_mode: bool = False
    langgraph_runtime_legacy_fallback: bool = False
    langgraph_checkpoint_database_url: str = ""
    langgraph_checkpoint_database_password_file: str = ""

    # Docker/systemd secrets can be mounted as files instead of appearing in
    # process definitions. A non-empty *_FILE value takes precedence.
    openai_api_key_file: str = ""
    zhipu_api_key_file: str = ""
    siliconflow_api_key_file: str = ""
    github_pat_file: str = ""
    feishu_app_secret_file: str = ""
    object_storage_secret_key_file: str = ""
    secret_encryption_key: str = ""
    secret_encryption_key_file: str = ""

    model_config = SettingsConfigDict(env_file=_env_file_chain(), extra="ignore")

    def model_post_init(self, __context) -> None:
        for url_name, password_file in (
            ("database_url", self.database_password_file),
            ("redis_url", self.redis_password_file),
            ("langgraph_checkpoint_database_url", self.langgraph_checkpoint_database_password_file),
        ):
            if password_file:
                password = self._secret_path(password_file).read_text(encoding="utf-8").strip()
                setattr(self, url_name, getattr(self, url_name).replace("{password}", quote(password, safe="")))
        # redis-py 6 treats an explicitly empty ACL username in
        # ``redis://:<password>@...`` as a two-argument AUTH for user ``""``.
        # Redis' requirepass config protects the built-in ``default`` user, so
        # normalize the conventional password-only URL to that explicit user.
        # This keeps existing production templates working without exposing the
        # secret or changing callers of ``Redis.from_url``.
        if self.redis_url.startswith("redis://:"):
            self.redis_url = self.redis_url.replace("redis://:", "redis://default:", 1)
        elif self.redis_url.startswith("rediss://:"):
            self.redis_url = self.redis_url.replace("rediss://:", "rediss://default:", 1)
        for name in (
            "openai_api_key", "zhipu_api_key", "siliconflow_api_key",
            "github_pat", "feishu_app_secret", "object_storage_secret_key", "secret_encryption_key",
        ):
            path = getattr(self, f"{name}_file", "")
            if path:
                setattr(self, name, self._secret_path(path).read_text(encoding="utf-8").strip())

    @staticmethod
    def _secret_path(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (_ENV_FILE.parent / path).resolve()


settings = Settings()
