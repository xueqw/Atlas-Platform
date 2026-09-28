from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    database_url: str = "sqlite:///./data/atlas.db"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    # Embeddings default to OpenAI when OPENAI_API_KEY is configured. The
    # explicit EMBEDDING_* settings support any OpenAI-compatible endpoint.
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = ""
    retrieval_min_score: float = 0.50
    # GitHub connector through the official remote MCP server.
    github_pat: str = ""
    github_mcp_url: str = "https://api.githubcopilot.com/mcp/"
    app_port: int = 8000

    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")


settings = Settings()
