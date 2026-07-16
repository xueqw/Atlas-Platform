from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from app.core.database import create_db_and_tables
from app.core.observability import log_startup_status
from app.api import agents, prompts, models_list, conversations, ws, seed, knowledge, pipeline, capabilities, builder, tools, node_types, dag, evaluation, monitoring, dag_templates, node_config_chat, planner

_log = logging.getLogger(__name__)


def _warn_unhealthy_credentials() -> None:
    """Log one aggregated WARNING for any missing/placeholder credential.

    Never blocks startup (dev runs fine without every provider configured) and
    never logs a key value — only ``name=status`` pairs. Turns a later mystery
    401/404 into an at-startup pointer to which key is unset.
    """
    try:
        from app.core.credential_health import unhealthy_summary
        summary = unhealthy_summary()
        if summary:
            _log.warning(
                "credential health: %s — these providers will 401/404 until a "
                "real key is set in backend/.env (no key value is logged).",
                summary,
            )
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    seed.seed_model_registry()
    seed.seed_capabilities()
    seed.seed_node_types()
    seed.seed_hermes_rules()
    seed.seed_dag_templates()
    seed.seed_planner_defaults()
    seed.seed_expert_templates()
    log_startup_status()
    _warn_unhealthy_credentials()
    yield


app = FastAPI(title="Agent Factory API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Dev: 同机不同形式的 localhost / 127.0.0.1 + 任意私网 IP（10.* / 172.16-31.* / 192.168.*
    # / 100.64-127.* CGNAT / 30.* 等内网段）。生产应在反向代理上收紧；这里只覆盖联调期。
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:13000",
        "http://127.0.0.1:13000",
    ],
    allow_origin_regex=r"^https?://(?:localhost|127\.0\.0\.1|\[::1\]|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agents.router, prefix="/api")
app.include_router(prompts.router, prefix="/api")
app.include_router(models_list.router, prefix="/api")
app.include_router(conversations.router, prefix="/api")
app.include_router(ws.router, prefix="/api")

app.include_router(knowledge.router, prefix="/api")
app.include_router(pipeline.router, prefix="/api")
app.include_router(capabilities.router, prefix="/api")
app.include_router(builder.router, prefix="/api")
app.include_router(tools.router, prefix="/api")
app.include_router(node_types.router, prefix="/api")
app.include_router(dag.router, prefix="/api")
app.include_router(evaluation.router, prefix="/api")
app.include_router(monitoring.router, prefix="/api")
app.include_router(dag_templates.router, prefix="/api")
app.include_router(node_config_chat.router, prefix="/api")
app.include_router(planner.router, prefix="/api")


@app.get("/api/health")
async def health():
    return {"status": "ok"}