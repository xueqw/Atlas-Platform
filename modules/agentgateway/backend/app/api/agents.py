from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select, func, desc
from typing import List
from pydantic import BaseModel
import json

from app.core.database import get_session
from app.models.db import Agent, PromptConfig, ModelConfig, AgentStatus, PromptVersion, DAGGraph
from app.models.schemas import (
    AgentCreate, AgentUpdate, AgentResponse,
    PromptConfigSchema, ModelConfigSchema, PublishResponse, PromptVersionResponse,
    ReleaseGateResponse,
)
from app.api.pipeline import update_node_status
from app.core.compiler import Compiler
from app.core.hermes import BuildEvaluator, TestCase
from app.core.release_gate import evaluate_release_gate
from app.core.model_caps import DEFAULT_CHAT_MODEL_ID, DEFAULT_CHAT_PROVIDER

router = APIRouter(prefix="/agents", tags=["agents"])

def _agent_response(agent: Agent, session: Session) -> AgentResponse:
    prompt = None
    model = None
    if agent.prompt_config_id:
        pc = session.get(PromptConfig, agent.prompt_config_id)
        if pc:
            prompt = PromptConfigSchema(
                role_name=pc.role_name,
                role_description=pc.role_description,
                output_format=pc.output_format,
                constraints=pc.constraints,
                system_prompt=pc.system_prompt,
            )
    if agent.model_config_id:
        mc = session.get(ModelConfig, agent.model_config_id)
        if mc:
            model = ModelConfigSchema(
                provider=mc.provider,
                model_name=mc.model_name,
                temperature=mc.temperature,
                max_tokens=mc.max_tokens,
                top_p=mc.top_p,
                streaming=mc.streaming,
            )
    return AgentResponse(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        status=AgentStatus(agent.status),
        version=agent.version,
        prompt_config=prompt,
        model=model,
        created_at=agent.created_at.isoformat(),
        updated_at=agent.updated_at.isoformat(),
    )


@router.get("", response_model=List[AgentResponse])
def list_agents(session: Session = Depends(get_session)):
    agents = session.exec(select(Agent).order_by(Agent.updated_at.desc())).all()
    return [_agent_response(a, session) for a in agents]


@router.post("", response_model=AgentResponse)
def create_agent(body: AgentCreate, session: Session = Depends(get_session)):
    prompt = PromptConfig()
    session.add(prompt)
    session.commit()

    model = ModelConfig(provider=DEFAULT_CHAT_PROVIDER, model_name=DEFAULT_CHAT_MODEL_ID)
    session.add(model)
    session.commit()

    agent = Agent(
        name=body.name,
        description=body.description,
        prompt_config_id=prompt.id,
        model_config_id=model.id,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return _agent_response(agent, session)


@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: int, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _agent_response(agent, session)


@router.put("/{agent_id}", response_model=AgentResponse)
def update_agent(agent_id: int, body: AgentUpdate, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if body.name is not None:
        agent.name = body.name
    if body.description is not None:
        agent.description = body.description

    if body.prompt_config and agent.prompt_config_id:
        update_node_status(session, agent_id, "p", "in_progress")
        pc = session.get(PromptConfig, agent.prompt_config_id)

        # Snapshot current state before overwriting (only if content differs)
        new_config_json = json.dumps({
            "role_name": body.prompt_config.role_name,
            "role_description": body.prompt_config.role_description,
            "output_format": body.prompt_config.output_format,
            "constraints": body.prompt_config.constraints,
            "system_prompt": body.prompt_config.system_prompt,
        }, ensure_ascii=False)

        latest = session.exec(
            select(PromptVersion)
            .where(PromptVersion.agent_id == agent_id)
            .order_by(PromptVersion.version_number.desc())
        ).first()

        if latest is None or latest.prompt_config != new_config_json:
            max_ver = latest.version_number if latest else 0
            session.add(PromptVersion(
                agent_id=agent_id,
                version_number=max_ver + 1,
                prompt_config=new_config_json,
            ))

            # Prune oldest if > 50
            total = session.exec(
                select(func.count()).where(PromptVersion.agent_id == agent_id)
            ).one()
            if total > 50:
                oldest = session.exec(
                    select(PromptVersion)
                    .where(PromptVersion.agent_id == agent_id)
                    .order_by(PromptVersion.version_number.asc())
                ).first()
                if oldest:
                    session.delete(oldest)

        pc.role_name = body.prompt_config.role_name
        pc.role_description = body.prompt_config.role_description
        pc.output_format = body.prompt_config.output_format
        pc.constraints = body.prompt_config.constraints
        pc.system_prompt = body.prompt_config.system_prompt
        session.add(pc)
        quality = 1.0 if (pc.system_prompt and len(pc.system_prompt) >= 50) else 0.5
        update_node_status(session, agent_id, "p", "complete", quality_score=quality)

    if body.model and agent.model_config_id:
        update_node_status(session, agent_id, "m", "in_progress")
        mc_params = body.model
        mc = session.get(ModelConfig, agent.model_config_id)
        mc.provider = mc_params.provider
        mc.model_name = mc_params.model_name
        mc.temperature = mc_params.temperature
        mc.max_tokens = mc_params.max_tokens
        mc.top_p = mc_params.top_p
        mc.streaming = mc_params.streaming
        session.add(mc)
        quality = 1.0 if mc.model_name else 0.0
        update_node_status(session, agent_id, "m", "complete", quality_score=quality)

    agent.version += 1
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return _agent_response(agent, session)


@router.delete("/{agent_id}", response_model=dict)
def delete_agent(agent_id: int, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.status == AgentStatus.PUBLISHED:
        raise HTTPException(status_code=409, detail="Published agents must be deprecated before deletion")
    session.delete(agent)
    session.commit()
    return {"message": "Agent deleted"}


@router.post("/{agent_id}/publish", response_model=PublishResponse)
def publish_agent(agent_id: int, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if this is a DAG-based agent
    dag = session.exec(
        select(DAGGraph).where(DAGGraph.agent_id == agent_id).order_by(desc(DAGGraph.version))
    ).first()

    if dag:
        # DAG-based agent: compile (structural validation) before the quality gate.
        result = Compiler.compile(dag.graph_json, agent.name, version=dag.version)
        if not result.success:
            raise HTTPException(status_code=422, detail={"errors": result.errors})
    else:
        # Legacy pipeline agent: structural prerequisites.
        pc = session.get(PromptConfig, agent.prompt_config_id) if agent.prompt_config_id else None
        mc = session.get(ModelConfig, agent.model_config_id) if agent.model_config_id else None
        errors = []
        if not pc or not pc.system_prompt.strip():
            errors.append("system_prompt is required")
        if not mc or not mc.model_name.strip():
            errors.append("model_name is required")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})

    # Quality gate: must pass before status → published. Missing eval/run data
    # blocks publishing (no default pass). Interception path unchanged — gate not
    # passed → 422 — only the detail is richer (structured failures + summary).
    gate = evaluate_release_gate(agent_id, session)
    if not gate.passed:
        raise HTTPException(
            status_code=422,
            detail={"errors": gate.reasons, "failures": gate.failures, "gate": gate.summary},
        )

    agent.status = AgentStatus.PUBLISHED
    session.add(agent)
    session.commit()
    message = "Agent published (DAG)" if dag else "Agent published"
    return PublishResponse(success=True, message=message, agent_id=agent.id, status="published")


@router.get("/{agent_id}/release-gate", response_model=ReleaseGateResponse)
def get_release_gate(agent_id: int, session: Session = Depends(get_session)):
    """Read-only release-gate evaluation — drives the pre-publish UI.

    Returns whether the agent would pass, the failing reasons (text), the
    structured failures, and the evidence summary (latest evaluation +
    dimension averages + observability metrics) without changing status.
    """
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    gate = evaluate_release_gate(agent_id, session)
    return ReleaseGateResponse(
        passed=gate.passed,
        reasons=gate.reasons,
        summary=gate.summary,
        failures=gate.failures,
    )


@router.get("/{agent_id}/prompt-versions", response_model=List[PromptVersionResponse])
def list_prompt_versions(agent_id: int, session: Session = Depends(get_session)):
    agent = session.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    versions = session.exec(
        select(PromptVersion)
        .where(PromptVersion.agent_id == agent_id)
        .order_by(PromptVersion.version_number.desc())
    ).all()
    return [
        PromptVersionResponse(
            id=v.id or 0,
            agent_id=v.agent_id,
            version_number=v.version_number,
            prompt_config=v.prompt_config,
            created_at=v.created_at.isoformat() if v.created_at else "",
        )
        for v in versions
    ]


@router.get("/{agent_id}/prompt-versions/{version_id}", response_model=PromptVersionResponse)
def get_prompt_version(agent_id: int, version_id: int, session: Session = Depends(get_session)):
    v = session.get(PromptVersion, version_id)
    if not v or v.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Version not found")
    return PromptVersionResponse(
        id=v.id or 0,
        agent_id=v.agent_id,
        version_number=v.version_number,
        prompt_config=v.prompt_config,
        created_at=v.created_at.isoformat() if v.created_at else "",
    )


@router.delete("/{agent_id}/prompt-versions/{version_id}")
def delete_prompt_version(agent_id: int, version_id: int, session: Session = Depends(get_session)):
    v = session.get(PromptVersion, version_id)
    if not v or v.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Version not found")
    session.delete(v)
    session.commit()
    return {"ok": True}


class OptimizePromptRequest(BaseModel):
    current_prompt: dict
    recent_messages: list[dict] = []


@router.post("/{agent_id}/optimize-prompt")
async def optimize_prompt(agent_id: int, body: OptimizePromptRequest):
    from app.core.prompt_optimizer import optimize_prompt as run_optimize
    from app.core.observability import with_generation

    with with_generation(
        name="prompt_optimizer",
        model="glm-4-flash",
        provider="glm",
        input={"current_prompt": body.current_prompt},
    ) as gen:
        result = await run_optimize(body.current_prompt, body.recent_messages)
        if gen is not None:
            try:
                gen.update(output=result)
            except Exception:
                pass

    return result
