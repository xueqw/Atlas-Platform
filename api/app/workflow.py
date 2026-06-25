"""Agent Runtime Pipeline 运行时（PRD M1）。

把一次聊天执行包装成一个 workflow_run + 若干 workflow_step，统一记录状态/输入/输出/耗时。
所有写操作各自开 SessionLocal——因为这些函数在 SSE 流式生成器里调用，已脱离请求的 db 作用域
（与 main.py 里 save_assistant 的处理方式一致）。
"""
import json
from datetime import datetime, timezone

from .database import SessionLocal
from .models import WorkflowRun, WorkflowStep


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_run(conversation_id: str, agent_id: str | None, user_id: str | None,
               workspace_id: str, input_text: str) -> str:
    """开一个 run（status=planning），返回 run_id。规划完成后调 save_plan 转 running。"""
    with SessionLocal() as db:
        run = WorkflowRun(
            workspace_id=workspace_id, conversation_id=conversation_id,
            agent_id=agent_id, user_id=user_id, input_text=input_text,
            status="planning", started_at=_now(),
        )
        db.add(run)
        db.commit()
        return run.id


def save_plan(run_id: str, plan: dict) -> None:
    """落库 Strategy Agent 计划并把 run 推进到 running。"""
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        if not run:
            return
        run.plan_json = json.dumps(plan, ensure_ascii=False)
        run.status = "running"
        db.commit()


def add_step(run_id: str, index: int, type: str, title: str, executor: str,
             skill_id: str | None = None, input_data: dict | None = None) -> str:
    """新建一个 step（status=running），返回 step_id。"""
    with SessionLocal() as db:
        step = WorkflowStep(
            run_id=run_id, index=index, type=type, title=title, executor=executor,
            skill_id=skill_id, status="running", started_at=_now(),
            input_json=json.dumps(input_data, ensure_ascii=False) if input_data else "",
        )
        db.add(step)
        db.commit()
        return step.id


def finish_step(step_id: str, status: str, output: dict | None = None, error: str = "") -> None:
    with SessionLocal() as db:
        step = db.get(WorkflowStep, step_id)
        if not step:
            return
        step.status = status
        step.ended_at = _now()
        if output is not None:
            step.output_json = json.dumps(output, ensure_ascii=False)
        if error:
            step.error = error
        db.commit()


def finish_run(run_id: str, status: str, output: dict | None = None, error: str = "") -> None:
    with SessionLocal() as db:
        run = db.get(WorkflowRun, run_id)
        if not run:
            return
        run.status = status
        run.ended_at = _now()
        if output is not None:
            run.output_json = json.dumps(output, ensure_ascii=False)
        if error:
            run.error = error
        db.commit()
