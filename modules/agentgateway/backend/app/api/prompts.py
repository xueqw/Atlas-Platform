from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from typing import List

from app.core.database import get_session
from app.models.db import PromptTemplate
from app.models.schemas import PromptTemplateCreate, PromptTemplateResponse

router = APIRouter(prefix="/prompt-templates", tags=["prompt-templates"])


@router.get("", response_model=List[PromptTemplateResponse])
def list_templates(session: Session = Depends(get_session)):
    templates = session.exec(select(PromptTemplate).order_by(PromptTemplate.created_at.desc())).all()
    return [
        PromptTemplateResponse(
            id=t.id,
            name=t.name,
            category=t.category,
            preview=t.preview or t.content[:100],
            content=t.content,
            created_at=t.created_at.isoformat(),
        )
        for t in templates
    ]


@router.post("", response_model=PromptTemplateResponse)
def create_template(body: PromptTemplateCreate, session: Session = Depends(get_session)):
    template = PromptTemplate(
        name=body.name,
        category=body.category,
        content=body.content,
        preview=body.content[:100],
    )
    session.add(template)
    session.commit()
    session.refresh(template)
    return PromptTemplateResponse(
        id=template.id,
        name=template.name,
        category=template.category,
        preview=template.preview,
        content=template.content,
        created_at=template.created_at.isoformat(),
    )