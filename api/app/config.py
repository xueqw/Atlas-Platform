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
    # Embeddings default to OpenAI when OPENAI_API_KEY is configured. The
    # explicit EMBEDDING_* settings support any OpenAI-compatible endpoint.
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = ""
    # Legacy SiliconFlow settings remain supported for existing deployments.
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_embedding_model: str = "BAAI/bge-m3"
    retrieval_min_score: float = 0.50
    # Optional Feishu connector (OAuth 2.0 authorization-code flow).
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_redirect_uri: str = "http://localhost:8000/api/connectors/feishu/callback"
    # GitHub connector through the official remote MCP server.
    github_pat: str = ""
    github_mcp_url: str = "https://api.githubcopilot.com/mcp/"
    app_port: int = 8000

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")


settings = Settings()
