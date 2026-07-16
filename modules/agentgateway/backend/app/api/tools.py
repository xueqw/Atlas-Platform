from fastapi import APIRouter, HTTPException
from sqlmodel import Session, select
from app.core.database import engine
from app.models.db import ToolConfig
from app.models.schemas import ToolConfigCreate, ToolConfigUpdate, ToolConfigResponse
from app.api.pipeline import update_tool_node_status

router = APIRouter(prefix="/agents/{agent_id}/tools", tags=["tools"])


def _row_to_response(row: ToolConfig) -> ToolConfigResponse:
    return ToolConfigResponse(
        id=row.id or 0,
        agent_id=row.agent_id,
        name=row.name,
        description=row.description,
        parameters=row.parameters,
        mock_endpoint=row.mock_endpoint,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


@router.get("")
def list_tools(agent_id: int):
    with Session(engine) as session:
        rows = session.exec(
            select(ToolConfig).where(ToolConfig.agent_id == agent_id).order_by(ToolConfig.created_at)
        ).all()
        return [_row_to_response(r) for r in rows]


@router.post("", status_code=201)
def create_tool(agent_id: int, body: ToolConfigCreate):
    with Session(engine) as session:
        item = ToolConfig(
            agent_id=agent_id,
            name=body.name,
            description=body.description,
            parameters=body.parameters,
            mock_endpoint=body.mock_endpoint,
        )
        session.add(item)
        session.commit()
        session.refresh(item)
        update_tool_node_status(session, agent_id, True)
        return _row_to_response(item)


@router.put("/{tool_id}")
def update_tool(agent_id: int, tool_id: int, body: ToolConfigUpdate):
    with Session(engine) as session:
        item = session.get(ToolConfig, tool_id)
        if not item or item.agent_id != agent_id:
            raise HTTPException(status_code=404, detail="Not found")
        if body.name is not None:
            item.name = body.name
        if body.description is not None:
            item.description = body.description
        if body.parameters is not None:
            item.parameters = body.parameters
        if body.mock_endpoint is not None:
            item.mock_endpoint = body.mock_endpoint
        session.add(item)
        session.commit()
        session.refresh(item)
        return _row_to_response(item)


@router.delete("/{tool_id}")
def delete_tool(agent_id: int, tool_id: int):
    with Session(engine) as session:
        item = session.get(ToolConfig, tool_id)
        if not item or item.agent_id != agent_id:
            raise HTTPException(status_code=404, detail="Not found")
        session.delete(item)
        session.commit()
        remaining = session.exec(select(ToolConfig).where(ToolConfig.agent_id == agent_id)).all()
        update_tool_node_status(session, agent_id, len(remaining) > 0)
        return {"ok": True}