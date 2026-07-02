from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/atlas.db"
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

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")


settings = Settings()
