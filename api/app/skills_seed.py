"""内置 skill 幂等灌入：给每个 workspace 各预置一份（按 name 去重）。"""
from sqlalchemy import select

from .database import SessionLocal
from .models import Skill, Workspace

BUILTIN_SKILLS = [
    {
        "name": "销售拜访简报",
        "description": "当用户要把零散资料/记录整理成客户拜访简报、销售简报时使用。",
        "trigger_phrases": "拜访简报,销售简报,客户简报",
        "content": "把信息整理成结构化拜访简报：① 客户背景 ② 关键要点 ③ 风险/异议 ④ 下一步行动（含负责人与时间）。语言简洁、要点化。",
    },
    {
        "name": "会议纪要",
        "description": "当用户要把会议内容/讨论整理成会议纪要时使用。",
        "trigger_phrases": "会议纪要,纪要,meeting notes",
        "content": "把内容整理成会议纪要：① 议题 ② 讨论结论 ③ 待办事项（任务-负责人-截止时间）。只保留事实与决议。",
    },
    {
        "name": "中英润色",
        "description": "当用户要润色、改写或翻译中英文文字，要求更专业地道时使用。",
        "trigger_phrases": "润色,改写,翻译,polish",
        "content": "在保留原意与专业术语的前提下润色表达，使其更地道专业；同时给出中英文对照。不擅自增删信息。",
    },
]


def seed_builtin_skills() -> None:
    with SessionLocal() as db:
        workspaces = db.scalars(select(Workspace)).all()
        for ws in workspaces:
            existing = {
                s.name for s in db.scalars(
                    select(Skill).where(Skill.workspace_id == ws.id, Skill.builtin == True)  # noqa: E712
                ).all()
            }
            for spec in BUILTIN_SKILLS:
                if spec["name"] in existing:
                    continue
                db.add(Skill(workspace_id=ws.id, builtin=True, type="instruction", **spec))
        db.commit()
