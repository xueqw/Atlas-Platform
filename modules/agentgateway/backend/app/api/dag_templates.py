"""DAG Template Library API — list templates and create agents from templates."""

import json
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.core.database import get_session
from app.models.db import DAGTemplate, Agent, DAGGraph, AgentStatus
from app.models.schemas import DAGTemplateResponse, AgentResponse, DAGGraphResponse

router = APIRouter(prefix="/dag-templates", tags=["dag-templates"])


@router.get("", response_model=List[DAGTemplateResponse])
def list_templates(session: Session = Depends(get_session)):
    templates = session.exec(
        select(DAGTemplate).order_by(DAGTemplate.sort_order)
    ).all()
    return [
        DAGTemplateResponse(
            id=t.id,
            name=t.name,
            description=t.description,
            category=t.category,
            graph_json=t.graph_json,
            icon=t.icon,
            sort_order=t.sort_order,
        )
        for t in templates
    ]


@router.post("/{template_id}/apply", response_model=dict)
def apply_template(template_id: int, session: Session = Depends(get_session)):
    template = session.get(DAGTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Create agent from template
    agent = Agent(
        name=f"{template.name} (新建)",
        description=template.description,
        status=AgentStatus.DRAFT,
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)

    # Create initial DAG graph from template
    dag = DAGGraph(
        agent_id=agent.id,
        graph_json=template.graph_json,
        state_schema="{}",
        version=1,
    )
    session.add(dag)
    session.commit()

    return {"agent_id": agent.id, "name": agent.name, "template_name": template.name}
