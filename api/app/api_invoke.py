"""外部 API Key 调用入口（PRD §6）：发布后的 Agent 通过 API Key 对外提供调用能力。

跟其余 /api/agents/* 路由的本质区别：这里完全不依赖 cookie/session
（不用 current_user/current_workspace_id），信任边界由 API Key 本身决定，
所以单独成文件、单独的 router，不挂在 agents_router 下面。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .api_keys import check_key_status, hash_key, origin_allowed
from .apps import invoke_agent
from .database import get_db
from .deploy_policy import parse_deploy_config
from .models import Agent, AgentApiKey, WorkflowRun
from .schemas import InvokeRequest, InvokeResponse
from . import workflow as wf
from .config import settings

invoke_router = APIRouter(prefix="/api/agents", tags=["invoke"])


def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    header_key = request.headers.get("x-api-key")
    return header_key.strip() if header_key else None


@invoke_router.post("/{agent_id}/invoke", response_model=InvokeResponse)
async def invoke_agent_endpoint(agent_id: str, payload: InvokeRequest, request: Request, db: Session = Depends(get_db)):
    plaintext = _extract_key(request)
    if not plaintext:
        raise HTTPException(status_code=401, detail="缺少 API Key（Authorization: Bearer <key> 或 X-Api-Key 头）")

    key = db.scalar(select(AgentApiKey).where(AgentApiKey.key_hash == hash_key(plaintext)))
    if not key or key.agent_id != agent_id:
        raise HTTPException(status_code=401, detail="API Key 无效")

    now = datetime.now(timezone.utc)
    rejection = check_key_status(key, now)
    if rejection:
        raise HTTPException(status_code=403, detail=rejection)

    if not origin_allowed(key.allowed_origins, request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="请求来源不在允许列表内")

    agent = db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")

    deploy_config = parse_deploy_config(agent.deploy_config_json)
    if not deploy_config.get("api_access", False):
        raise HTTPException(status_code=403, detail="该智能体未开启外部 API 调用")

    if agent.status != "published":
        raise HTTPException(status_code=403, detail="该智能体尚未发布")

    if key.daily_quota is not None:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        used_today = db.scalar(
            select(func.count()).select_from(WorkflowRun)
            .where(WorkflowRun.agent_id == agent_id, WorkflowRun.source == "api",
                   WorkflowRun.created_at >= day_start)
        ) or 0
        if used_today >= key.daily_quota:
            raise HTTPException(status_code=429, detail="今日调用额度已用完")

    # With the unified runtime enabled, its durable projection owns the product
    # WorkflowRun. Creating a second legacy run here would double-count quota
    # and split the audit trail.
    run_id = None if settings.langgraph_runtime_enabled else wf.create_run(
        None, agent.id, None, agent.workspace_id, payload.input, source="api"
    )
    result = await invoke_agent(agent, payload.input, db)

    if run_id is not None:
        if result["ok"]:
            wf.finish_run(run_id, "succeeded", output={"answer": result["output"], "version_no": result.get("version_no")})
        else:
            wf.finish_run(run_id, "failed", error=result.get("error") or "调用失败")
    else:
        run_id = result.get("run_id")

    key.last_used_at = now
    db.commit()

    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result.get("error") or "调用失败")

    return InvokeResponse(
        output=result["output"],
        elapsed_ms=result["elapsed_ms"],
        version_no=result.get("version_no"),
        run_id=run_id,
    )
