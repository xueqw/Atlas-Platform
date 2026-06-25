# M4 Tool Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn tool calls into discrete, audited `tool` workflow steps with a deterministic read/write policy, and bring the write-confirmation path into the run audit — without rewriting the model-driven tool loop.

**Architecture:** A pure `tool_strategy.build_tool_policy()` derives the run's tool policy (in-scope tools + read/write tags) from already-known data, no extra LLM call. `send_message`'s `events()` wraps the existing `stream_agent` loop: each `tool_call`/`tool_result` pair opens/closes a `tool` workflow step. The write-confirmation reply path reuses the original `run_id` (carried in `_pending_actions`) to append an audited tool step and finish the run.

**Tech Stack:** FastAPI + SQLAlchemy + SSE (backend), React + TS + Vite (frontend), pytest (tests). Spec: `docs/superpowers/specs/2026-06-25-m4-tool-execution-design.md`.

## Global Constraints

- Branch: `feature/m3-skill-hub` (continues M1/M2/M3; not main). Do NOT commit `.codegraph/.gitignore`.
- Backend test runner: `api/.venv/Scripts/python.exe -m pytest -q` (run from `api/`). pytest is scoped to `api/tests/` via `api/pytest.ini`.
- Frontend gates (run from `web/`): `npx tsc --noEmit && npx vite build` — both must pass.
- Existing suite is 20 passing tests; must stay green.
- Tool spec shape (verbatim): `{"type": "function", "function": {"name": str, "description": str, "parameters": dict}}`.
- `tool` is an existing `WorkflowStep.type` value (frontend plan card already renders it as 技能/工具 label). `add_step(run_id, index, type, title, executor, skill_id=None, input_data=None) -> step_id`; `finish_step(step_id, status, output=None, error="") -> None`.
- Frontend is highly compressed single-line JSX — match the existing style, do not reformat neighboring code.

---

### Task 1: `build_tool_policy` pure function

**Files:**
- Create: `api/app/tool_strategy.py`
- Test: `api/tests/test_tool_strategy.py`

**Interfaces:**
- Produces: `build_tool_policy(tool_specs: list[dict], write_names: set[str], connector_of: dict[str, str], requires_tools: bool) -> dict` returning `{"requires_tools": bool, "tools": [{"name","connector","access"}], "write_tools": [str]}`. `access` is `"write"` if name in `write_names` else `"read"`; `connector` is `connector_of.get(name, "github")`.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_tool_strategy.py
from app.tool_strategy import build_tool_policy

def _spec(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}

def test_empty_specs_gives_empty_policy():
    p = build_tool_policy([], set(), {}, False)
    assert p == {"requires_tools": False, "tools": [], "write_tools": []}

def test_read_tool_tagged_read():
    p = build_tool_policy([_spec("search_repos")], set(), {}, True)
    assert p["requires_tools"] is True
    assert p["tools"] == [{"name": "search_repos", "connector": "github", "access": "read"}]
    assert p["write_tools"] == []

def test_static_connector_lookup_and_write():
    p = build_tool_policy([_spec("send_feishu_message")], {"send_feishu_message"},
                          {"send_feishu_message": "feishu"}, True)
    assert p["tools"] == [{"name": "send_feishu_message", "connector": "feishu", "access": "write"}]
    assert p["write_tools"] == ["send_feishu_message"]

def test_mcp_write_name_marks_write_and_defaults_github():
    p = build_tool_policy([_spec("create_issue")], {"create_issue"}, {}, True)
    assert p["tools"] == [{"name": "create_issue", "connector": "github", "access": "write"}]
    assert p["write_tools"] == ["create_issue"]

def test_mixed_read_and_write():
    specs = [_spec("search_repos"), _spec("create_issue")]
    p = build_tool_policy(specs, {"create_issue"}, {}, True)
    assert [t["access"] for t in p["tools"]] == ["read", "write"]
    assert p["write_tools"] == ["create_issue"]
```

- [ ] **Step 2: Run to verify failure**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest tests/test_tool_strategy.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.tool_strategy'`.

- [ ] **Step 3: Implement**

```python
# api/app/tool_strategy.py
"""MCP Strategy Agent（PRD M4，P0 确定性形态）。

不调 LLM：本次工具策略可由已知数据纯函数推导——
启用了哪些连接器决定 tool_specs，is_write/mcp_write_names 决定读写。
输出落进 plan_json.tool_policy 供审计，并取代 send_message 里临时的 needs_confirm 闭包。
"""


def build_tool_policy(tool_specs: list[dict], write_names: set[str],
                      connector_of: dict[str, str], requires_tools: bool) -> dict:
    """把本次可用工具整理成策略：每个工具标 connector + read/write。

    tool_specs   —— OpenAI 兼容工具声明（含静态飞书 + 动态 GitHub MCP）
    write_names  —— 写操作工具名集合（静态 is_write ∪ mcp_write_names）
    connector_of —— 静态工具名→连接器；不在表里的（GitHub MCP）默认 "github"
    requires_tools —— Strategy Agent 计划里的判断，透传
    """
    tools = []
    for spec in tool_specs:
        name = spec["function"]["name"]
        access = "write" if name in write_names else "read"
        tools.append({"name": name, "connector": connector_of.get(name, "github"), "access": access})
    return {
        "requires_tools": bool(requires_tools),
        "tools": tools,
        "write_tools": [t["name"] for t in tools if t["access"] == "write"],
    }
```

- [ ] **Step 4: Run to verify pass**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest tests/test_tool_strategy.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add api/app/tool_strategy.py api/tests/test_tool_strategy.py
git commit -m "feat: tool_strategy.build_tool_policy (MCP strategy, deterministic, TDD)"
```

---

### Task 2: `workflow.next_index` helper

**Files:**
- Modify: `api/app/workflow.py`
- Test: `api/tests/test_workflow_next_index.py`

**Interfaces:**
- Produces: `next_index(run_id: str) -> int` — count of existing steps for the run (used by the confirm path, which runs outside `events()` and so can't reuse its local `next_idx`).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_workflow_next_index.py
from app import workflow as wf

def test_next_index_counts_steps():
    run_id = wf.create_run("conv-x", None, None, "ws-x", "hi")
    assert wf.next_index(run_id) == 0
    wf.add_step(run_id, 0, "respond", "生成回答", "llm")
    assert wf.next_index(run_id) == 1
    wf.add_step(run_id, 1, "tool", "调用工具：x", "feishu")
    assert wf.next_index(run_id) == 2
```

- [ ] **Step 2: Run to verify failure**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest tests/test_workflow_next_index.py -q`
Expected: FAIL — `AttributeError: module 'app.workflow' has no attribute 'next_index'`.

- [ ] **Step 3: Implement**

Add `func` to the SQLAlchemy import at the top of `api/app/workflow.py`. The file currently imports only `from .models import ...` and `from .database import SessionLocal`; add:

```python
from sqlalchemy import func, select
```

Then append this function to `api/app/workflow.py`:

```python
def next_index(run_id: str) -> int:
    """该 run 已有多少 step——确认写操作的审计路径用它续号（脱离 events() 的局部 next_idx）。"""
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(WorkflowStep).where(WorkflowStep.run_id == run_id)) or 0
```

- [ ] **Step 4: Run to verify pass**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest tests/test_workflow_next_index.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add api/app/workflow.py api/tests/test_workflow_next_index.py
git commit -m "feat: workflow.next_index helper for confirm-path step numbering"
```

---

### Task 3: Build tool policy in `send_message` + replace `needs_confirm`

**Files:**
- Modify: `api/app/main.py` (the `send_message` setup block + `events()` plan block)

**Interfaces:**
- Consumes: `build_tool_policy` (Task 1).
- Produces: a `tool_policy` dict in scope of `events()`, persisted at `plan["tool_policy"]`; `needs_confirm` derived from `tool_policy["write_tools"]`.

**Context:** In `send_message`, `tool_specs` and `mcp_write_names` are built in the setup block (around main.py:347-357), and `needs_confirm` is currently a closure there. The plan (`requires_tools`) is only known inside `events()` after `strategy.plan(...)`. So: compute `write_names`/`connector_of` in setup, build the policy inside `events()` right before `wf.save_plan`.

- [ ] **Step 1: Add the import**

At the top of `api/app/main.py`, next to the existing `from . import strategy` / `from . import workflow as wf` style imports, add:

```python
from .tool_strategy import build_tool_policy
```

- [ ] **Step 2: Replace the `needs_confirm` closure with policy inputs**

Find this block in `send_message` (main.py ~346-357):

```python
    # 组装本次可用工具：静态连接器（飞书）+ 动态 MCP 连接器（GitHub）
    tool_specs = agent_tools.specs(payload.connectors)
    mcp_write_names: set[str] = set()
    if "github" in payload.connectors and github_mcp.is_configured():
        try:
            gh_specs, mcp_write_names = await github_mcp.tool_specs()
            tool_specs = tool_specs + gh_specs
        except Exception:
            pass  # GitHub MCP 拉取失败就跳过其工具，不影响整体对话

    def needs_confirm(name: str) -> bool:
        return agent_tools.is_write(name) or name in mcp_write_names
```

Replace it with (compute the write set + connector map; `needs_confirm` now reads the policy built in `events()`):

```python
    # 组装本次可用工具：静态连接器（飞书）+ 动态 MCP 连接器（GitHub）
    tool_specs = agent_tools.specs(payload.connectors)
    mcp_write_names: set[str] = set()
    if "github" in payload.connectors and github_mcp.is_configured():
        try:
            gh_specs, mcp_write_names = await github_mcp.tool_specs()
            tool_specs = tool_specs + gh_specs
        except Exception:
            pass  # GitHub MCP 拉取失败就跳过其工具，不影响整体对话

    # M4：MCP Strategy Agent 的输入——写操作名集合 + 静态工具的连接器归属
    static_write_names = {s["function"]["name"] for s in tool_specs
                          if agent_tools.is_write(s["function"]["name"])}
    write_names = static_write_names | mcp_write_names
    connector_of = {name: t["connector"] for name, t in agent_tools.TOOLS.items()}
```

- [ ] **Step 3: Build + persist the policy inside `events()`**

Find the plan block in `events()` (main.py ~382-393):

```python
            plan = await strategy.plan(
                input_text=payload.content, has_knowledge_base=has_kb,
                enabled_connectors=payload.connectors,
                agent_prompt=agent.system_prompt if agent else None, model=model,
                skill_catalog=skill_catalog,
            )
            # M3：手动勾选 ∪ 自动选取，覆写为解析后的对象数组随 plan_json 落库
            from .skills_engine import build_skill_instructions, resolve_skills
            selected_skills = resolve_skills(skill_catalog, manual_skill_ids, plan.get("skills", []))
            plan["skills"] = selected_skills
            wf.save_plan(run_id, plan)
```

Insert the policy build right before `wf.save_plan(run_id, plan)`:

```python
            # M3：手动勾选 ∪ 自动选取，覆写为解析后的对象数组随 plan_json 落库
            from .skills_engine import build_skill_instructions, resolve_skills
            selected_skills = resolve_skills(skill_catalog, manual_skill_ids, plan.get("skills", []))
            plan["skills"] = selected_skills
            # M4：MCP Strategy Agent——确定性工具策略，落进 plan_json 供审计
            tool_policy = build_tool_policy(tool_specs, write_names, connector_of, plan.get("requires_tools", False))
            plan["tool_policy"] = tool_policy
            wf.save_plan(run_id, plan)
```

- [ ] **Step 4: Point `needs_confirm` at the policy**

The `stream_agent(...)` call (main.py ~426) passes `needs_confirm=needs_confirm`. Replace that argument with a policy-derived lambda. Find:

```python
            async for ev in stream_agent(history, model, tool_specs, execute_tool, needs_confirm=needs_confirm):
```

Replace with:

```python
            async for ev in stream_agent(history, model, tool_specs, execute_tool,
                                         needs_confirm=lambda name: name in tool_policy["write_tools"]):
```

- [ ] **Step 5: Verify backend still green (no behavior regression yet)**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (22 passed — 20 existing + Task 1's 5... note count grows; just confirm 0 failures).

- [ ] **Step 6: Commit**

```bash
git add api/app/main.py
git commit -m "feat: build tool_policy in send_message, drive needs_confirm from it"
```

---

### Task 4: Tool calls become discrete `tool` workflow steps

**Files:**
- Modify: `api/app/main.py` (the `stream_agent` consumption loop in `events()`)
- Test: `api/tests/test_chat_tools.py`

**Interfaces:**
- Consumes: `tool_policy` (Task 3), `wf.add_step`/`wf.finish_step`.
- Produces: per tool call, a `tool` `WorkflowStep` (status `succeeded`/`failed`) and SSE `step_started`/`step_completed`; the passed-through `tool_call` SSE is enriched with `access`.

**Context:** `stream_agent` yields events strictly as `tool_call` → (execute) → `tool_result` per call, sequentially. So a single "currently-open tool step id" variable suffices. The current loop (main.py ~426-437) handles `confirm_required` and `token`, then `yield sse(ev)` for everything else.

- [ ] **Step 1: Write the failing integration test**

```python
# api/tests/test_chat_tools.py
import json
from app import workflow as wf
from app.main import build_tool_policy  # re-exported via import; if absent import from app.tool_strategy

def test_tool_call_becomes_a_step(monkeypatch):
    """events() 消费 stream_agent 的 tool_call/tool_result 时，落一个 tool step。"""
    # 用 next_index 前后差值断言新增了 step；这里直接验证 add/finish 行为契约
    run_id = wf.create_run("conv-t", None, None, "ws-t", "发个飞书")
    before = wf.next_index(run_id)
    sid = wf.add_step(run_id, before, "tool", "调用工具：send_feishu_message", "feishu",
                      input_data={"args": "{}", "access": "write"})
    wf.finish_step(sid, "succeeded", output={"result": "已发送"})
    assert wf.next_index(run_id) == before + 1
```

> Note: the full SSE-level assertion is covered by Task 6's live smoke (it needs a real model key to drive `stream_agent` into a tool call). This unit test pins the step-recording contract the loop depends on.

- [ ] **Step 2: Run to verify pass-or-fail**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest tests/test_chat_tools.py -q`
Expected: PASS (the contract already holds via Task 2). If the `from app.main import build_tool_policy` line errors, change it to `from app.tool_strategy import build_tool_policy` and re-run.

- [ ] **Step 3: Rewrite the consumption loop to record tool steps**

Find the loop in `events()` (main.py ~426-437):

```python
            async for ev in stream_agent(history, model, tool_specs, execute_tool,
                                         needs_confirm=lambda name: name in tool_policy["write_tools"]):
                if ev["type"] == "confirm_required":
                    args = json.loads(ev["args"] or "{}") if isinstance(ev["args"], str) else ev["args"]
                    _pending_actions[conversation_id] = {"name": ev["name"], "args": args}
                    prompt = agent_tools.describe_call(ev["name"], args) + "\n\n确认请回复「确认」，取消请回复「取消」。"
                    parts.append(prompt)
                    awaiting_confirm = True
                    yield sse({"type": "token", "content": prompt})
                    continue
                if ev["type"] == "token":
                    parts.append(ev["content"])
                yield sse(ev)
```

Replace with (adds `tool_call`/`tool_result` step handling; keeps token/confirm logic; stores `run_id`+`access` into the pending record for Task 5):

```python
            tool_step_id: str | None = None
            async for ev in stream_agent(history, model, tool_specs, execute_tool,
                                         needs_confirm=lambda name: name in tool_policy["write_tools"]):
                if ev["type"] == "confirm_required":
                    args = json.loads(ev["args"] or "{}") if isinstance(ev["args"], str) else ev["args"]
                    access = "write" if ev["name"] in tool_policy["write_tools"] else "read"
                    _pending_actions[conversation_id] = {"name": ev["name"], "args": args,
                                                         "run_id": run_id, "access": access}
                    prompt = agent_tools.describe_call(ev["name"], args) + "\n\n确认请回复「确认」，取消请回复「取消」。"
                    parts.append(prompt)
                    awaiting_confirm = True
                    yield sse({"type": "token", "content": prompt})
                    continue
                if ev["type"] == "tool_call":
                    info = next((t for t in tool_policy["tools"] if t["name"] == ev["name"]),
                                {"connector": "", "access": "read"})
                    tool_step_id = wf.add_step(run_id, next_idx, "tool", f"调用工具：{ev['name']}",
                                               info["connector"], input_data={"args": ev["args"], "access": info["access"]})
                    yield sse({"type": "step_started", "step_id": tool_step_id, "index": next_idx,
                               "step_type": "tool", "title": f"调用工具：{ev['name']}"})
                    next_idx += 1
                    yield sse({**ev, "access": info["access"]})
                    continue
                if ev["type"] == "tool_result":
                    if tool_step_id:
                        wf.finish_step(tool_step_id, "succeeded", output={"result": (ev.get("content") or "")[:500]})
                        yield sse({"type": "step_completed", "step_id": tool_step_id, "status": "succeeded"})
                        tool_step_id = None
                    yield sse(ev)
                    continue
                if ev["type"] == "token":
                    parts.append(ev["content"])
                yield sse(ev)
```

- [ ] **Step 4: Verify backend green**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (0 failures).

- [ ] **Step 5: Commit**

```bash
git add api/app/main.py api/tests/test_chat_tools.py
git commit -m "feat: record each tool call as a discrete audited workflow step"
```

---

### Task 5: Confirmed writes get audited as tool steps

**Files:**
- Modify: `api/app/main.py` (the `confirm_events()` block near main.py ~308-323)

**Interfaces:**
- Consumes: `pending["run_id"]`, `pending["access"]` (set in Task 4 Step 3); `wf.next_index`, `wf.add_step`, `wf.finish_step`, `wf.finish_run`.

**Context:** The confirm path sits at the TOP of `send_message`, before `create_run`. It must reuse the original `run_id` carried in the pending record. The current `confirm_events()` runs `execute_tool` with zero audit.

- [ ] **Step 1: Rewrite `confirm_events()` to audit the confirmed action**

Find (main.py ~307-323):

```python
    # === 待确认的写操作：把本条消息当作「确认/取消」处理，不走模型 ===
    pending = _pending_actions.pop(conversation_id, None)
    if pending and (_affirm(payload.content) or _deny(payload.content)):
        approved = _affirm(payload.content)
        db.add(Message(conversation_id=conversation_id, role="user", content=payload.content)); db.commit()

        async def confirm_events():
            if approved:
                result = await execute_tool(pending["name"], json.dumps(pending["args"], ensure_ascii=False))
            else:
                result = "好的，已取消，未执行。"
            save_assistant(result)
            for ch in result:
                yield f"data: {json.dumps({'type': 'token', 'content': ch}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        return StreamingResponse(confirm_events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

Replace the `confirm_events` body with:

```python
        async def confirm_events():
            pend_run = pending.get("run_id")
            if approved:
                connector = agent_tools.TOOLS.get(pending["name"], {}).get("connector", "github")
                step_id = None
                if pend_run:
                    idx = wf.next_index(pend_run)
                    step_id = wf.add_step(pend_run, idx, "tool", f"调用工具：{pending['name']}", connector,
                                          input_data={"args": json.dumps(pending["args"], ensure_ascii=False),
                                                      "access": pending.get("access", "write")})
                    yield f"data: {json.dumps({'type': 'step_started', 'step_id': step_id, 'index': idx, 'step_type': 'tool', 'title': '调用工具：' + pending['name']}, ensure_ascii=False)}\n\n"
                result = await execute_tool(pending["name"], json.dumps(pending["args"], ensure_ascii=False))
                if pend_run and step_id:
                    wf.finish_step(step_id, "succeeded", output={"result": (result or "")[:500]})
                    wf.finish_run(pend_run, "succeeded", output={"answer": result})
                    yield f"data: {json.dumps({'type': 'step_completed', 'step_id': step_id, 'status': 'succeeded'}, ensure_ascii=False)}\n\n"
            else:
                result = "好的，已取消，未执行。"
                if pend_run:
                    wf.finish_run(pend_run, "cancelled")
            save_assistant(result)
            for ch in result:
                yield f"data: {json.dumps({'type': 'token', 'content': ch}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
```

- [ ] **Step 2: Verify backend green**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (0 failures).

- [ ] **Step 3: Commit**

```bash
git add api/app/main.py
git commit -m "feat: audit confirmed write actions as tool steps on the original run"
```

---

### Task 6: Frontend — tool call rows

**Files:**
- Modify: `web/src/api.ts` (`streamMessage` — add `onTool`)
- Modify: `web/src/App.tsx` (consume `onTool`, render tool rows)
- Modify: `web/src/styles.css` (`.tool-chip`)

**Interfaces:**
- Consumes: SSE `tool_call` (now carries `access`) and `tool_result`.
- Produces: `onTool?:(t:{phase:'call'|'result';name:string;access?:string;content?:string})=>void` as the last param of `streamMessage`; a `tools` state array in `AppShell` rendered as rows in the conversation.

- [ ] **Step 1: Extend `streamMessage`**

In `web/src/api.ts`, the `streamMessage` signature ends with `...,skillIds?:string[],onSkills?:(...)=>void)`. Add `onTool` after `onSkills`:

```typescript
,onTool?:(t:{phase:'call'|'result';name:string;access?:string;content?:string})=>void){
```

Then in the event dispatch chain (the `if(data.type===...)` run), add two handlers next to the `skills_selected` one:

```typescript
if(data.type==='tool_call')onTool?.({phase:'call',name:data.name,access:data.access});if(data.type==='tool_result')onTool?.({phase:'result',name:data.name,content:data.content});
```

- [ ] **Step 2: Add `tools` state + reset + wiring in `App.tsx`**

In `AppShell`, next to `[appliedSkills,setAppliedSkills]=useState<SelectedSkill[]>([])`, add:

```tsx
,[toolCalls,setToolCalls]=useState<{name:string;access?:string;done:boolean}[]>([])
```

In `submit`, next to `setAppliedSkills([])` (both the pre-stream reset and the `finally`), add `setToolCalls([])`.

In the `streamMessage(...)` call, after the `s=>setAppliedSkills(s)` argument, add the `onTool` handler:

```tsx
,t=>setToolCalls(prev=>t.phase==='call'?[...prev,{name:t.name,access:t.access,done:false}]:prev.map((x,i)=>i===prev.map(y=>y.name).lastIndexOf(t.name)&&!x.done?{...x,done:true}:x))
```

- [ ] **Step 3: Thread `toolCalls` into `Workbench` and render**

Add `toolCalls={toolCalls}` to the `<Workbench ... />` call site (next to `appliedSkills` is already passed via `plan`/`step`; add the new prop). Add `toolCalls` to the `Workbench` destructure and its type:

```tsx
toolCalls:{name:string;access?:string;done:boolean}[];
```

Render the tool rows in the conversation, right after the `{busy&&step&&...}` run-step block:

```tsx
{toolCalls.length>0&&<div className="run-tools">{toolCalls.map((t,i)=><span key={i} className={`tool-chip tool-${t.access==='write'?'write':'read'}`}>🔧 {t.name}<i>{t.access==='write'?'写':'读'}</i>{t.done?<b className="tool-ok">✓</b>:<b className="tool-run">…</b>}</span>)}</div>}
```

- [ ] **Step 4: Add CSS**

Append to `web/src/styles.css`:

```css
/* M4 tool call rows */
.run-tools{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.tool-chip{display:inline-flex;align-items:center;gap:5px;font-size:11px;border-radius:9px;padding:2px 8px;border:1px solid #dce2ec;color:#3a4660;background:#fff}
.tool-chip i{font-style:normal;font-size:9px;border-radius:6px;padding:0 5px}
.tool-chip b{font-style:normal;font-size:10px}
.tool-read{background:#f1f7f0;border-color:#cfe6cb}.tool-read i{background:#3a9d5d;color:#fff}
.tool-write{background:#fef3ef;border-color:#f3c9b8}.tool-write i{background:#d2682f;color:#fff}
.tool-ok{color:#3a9d5d}.tool-run{color:var(--muted)}
```

- [ ] **Step 5: Verify frontend gates**

Run (from `web/`): `npx tsc --noEmit && npx vite build`
Expected: both succeed.

- [ ] **Step 6: Commit**

```bash
git add web/src/api.ts web/src/App.tsx web/src/styles.css
git commit -m "feat(web): render tool calls as read/write rows in the conversation"
```

---

### Task 7: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Backend suite**

Run (from `api/`): `.venv/Scripts/python.exe -m pytest -q`
Expected: all pass (existing 20 + tool_strategy 5 + next_index 1 + chat_tools 1 = 27).

- [ ] **Step 2: Frontend gates**

Run (from `web/`): `npx tsc --noEmit && npx vite build`
Expected: clean.

- [ ] **Step 3: Live smoke (real server, GitHub connector)**

Restart backend (kill :8000, start uvicorn from `api/`). With `api/.venv/Scripts/python.exe` + httpx: login admin → create a conversation → stream a message likely to trigger a read tool (e.g. "搜索 github 上 anthropic 的仓库") with `connectors=["github"]`. Assert the SSE stream carries at least one `step_started` with `step_type=tool`, and `GET /api/conversations/{id}/runs` → newest run's `plan_json` (json.loads the string) has a `tool_policy` with non-empty `tools`, and the run's `steps` include a `type=tool` step with `status=succeeded`.

> If no model key / GitHub PAT is configured in this env, the model won't emit a tool call; in that case assert only that `plan_json.tool_policy` is present and well-formed (the deterministic policy does not depend on the model). Note this clearly in the smoke output.

- [ ] **Step 4: Final commit (if any cleanup)**

```bash
git add -A && git commit -m "chore: M4 tool execution verification pass"
```

---

## Self-Review

**Spec coverage:** spec §2 (tool_strategy) → Task 1; §3 (tool steps) → Tasks 3+4; §4 (write audit) → Tasks 2+5; §5 (frontend) → Task 6; §6 (data flow) → exercised by Task 7 smoke; §7 (tests) → Tasks 1/2/4 unit + Task 7 integration. No gaps.

**Placeholder scan:** every code step shows full code; no TBD/TODO. Task 4's SSE-level assertion is deferred to Task 7 live smoke with an explicit reason (needs a real model key to drive `stream_agent`), and a contract-level unit test pins the recording behavior — not a placeholder.

**Type consistency:** `build_tool_policy(tool_specs, write_names, connector_of, requires_tools)` signature identical across Task 1 (def), Task 3 (call). `tool_policy` keys `tools`/`write_tools`/`requires_tools` consistent in Tasks 3/4. `_pending_actions` record shape `{name,args,run_id,access}` written in Task 4 Step 3, read in Task 5 Step 1. `add_step`/`finish_step`/`next_index`/`finish_run` signatures match `workflow.py`. Frontend `onTool` shape identical in api.ts (Task 6 Step 1) and App.tsx (Task 6 Step 2).
