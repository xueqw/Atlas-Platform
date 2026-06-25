# M4 设计 —— 工具步真执行 + MCP 策略层 + 写审计

> 状态:已批准(2026-06-25)。承接 M1/M2/M3,在分支 `feature/m3-skill-hub` 上继续。
> 关联:PRD `docs/prd/atlas-agent-runtime-pipeline-prd.md` §3.3(MCP Strategy Agent)、§3.5(Tool/MCP Layer)、§4.3(管理员治理工具调用)。

## 1. 目标与边界

让工具调用从「埋在 respond 步的 LLM 循环里、不可见、零审计」变成**计划内的独立可审计步骤**,并补上写操作确认路径的审计缺口。

三块:
- **MCP Strategy Agent**(确定性策略层):一次性算出「本次哪些工具在范围内 + 每个 read/write + 是否需确认」,落 `plan_json.tool_policy` 供审计。
- **tool 步真执行**:保留 `stream_agent` 的模型驱动工具循环(灵活、可多轮推理),但每次工具调用登记为独立 `tool` workflow_step(step_started/completed + 读写标记 + 入参摘要/结果/耗时)。
- **写审计**:确认后执行的写操作也补记为 tool step,挂回原 run。

**核心设计决策**(brainstorming 收敛,用户钉死):
1. **包裹式**,不重写工具执行——保留 LLM 驱动的 `stream_agent`,只给它装「记录仪 + 闸门」。
2. **确定性策略模块**,不调额外 LLM——策略可由已知数据(enabled_connectors + plan.requires_tools + is_write/mcp_write_names)纯函数推导。
3. **补审计**——确认后执行的写操作也记为 tool step,挂回原 run。

**YAGNI 砍掉**(PRD 列了但 P0 不做,数据/事件协议预留即可):独立 MCP 进程/服务、参数 schema 预生成、tool 重试策略表、并行工具执行、tool 步嵌套父子结构。

## 2. 新组件:`api/app/tool_strategy.py`(纯函数,TDD 核心)

```python
def build_tool_policy(
    enabled_connectors: list[str],
    tool_specs: list[dict],
    mcp_write_names: set[str],
    requires_tools: bool,
) -> dict
```

输出(同时落进 `plan_json.tool_policy`):

```json
{
  "requires_tools": true,
  "tools": [
    {"name": "send_feishu_message", "connector": "feishu", "access": "write"},
    {"name": "search_github_repos", "connector": "github", "access": "read"}
  ],
  "write_tools": ["send_feishu_message"]
}
```

规则:
- `tools` 由 `tool_specs` 的函数名展开;`connector` 按工具名归属推断(静态飞书工具 → `feishu`;其余且 `github_mcp.is_configured()` → `github`)。
- `access` = `write` 当 `agent_tools.is_write(name)` 或 `name in mcp_write_names`,否则 `read`。
- `write_tools` = access 为 write 的名字集合。
- 空连接器 / 空 tool_specs → `tools=[]`、`write_tools=[]`。

它**取代** `send_message` 里临时的 `needs_confirm` 闭包:改成 `name in policy["write_tools"]`。

特性:不调 LLM、零延迟、可独立单测。这是 PRD「MCP Strategy Agent」的 P0 形态——职责单一、确定性、可测的策略单元。

## 3. tool 步真执行(`events()` 里包裹 `stream_agent`)

`stream_agent` 已按 `tool_call → 执行 → tool_result` 严格成对、顺序 yield(见 `model_gateway.py` 工具循环)。在 `events()` 消费循环里加两个分支:

- 收到 `tool_call`:`tool_step_id = wf.add_step(run_id, next_idx, "tool", f"调用工具：{name}", connector, input_data={"args": args, "access": access})` → 发 `step_started`(step_type=tool)→ `next_idx += 1`。`connector`/`access` 查 `tool_policy`。
- 收到 `tool_result`:`wf.finish_step(tool_step_id, "succeeded", output={"result": result[:500]})` → 发 `step_completed`。
- 仍照常把 `tool_call` / `tool_result` 透传给前端(现在前端会消费)。

成对配对:因 `stream_agent` 严格「先 yield tool_call,执行,再 yield tool_result」,用单个「当前打开的 tool step id」变量即可,无需栈。

排序:respond 步保持不变(LLM 综合步,先于 tool 步登记);tool 步作为兄弟步按发生顺序插在其后,index 递增。时间戳反映真实交错,审计可读。

错误处理:`execute_tool` 抛错 → 该 tool step `finish_step(..., "failed", error=...)`,**不中断整个 run**(沿用现有「GitHub 拉取失败就跳过」的容错基调)。

## 4. 写审计(确认路径并入原 run)

现状:写工具 → `stream_agent` 抛 `confirm_required` → 存 `_pending_actions[conv]` → run 标 `waiting_confirmation`。用户回「确认」是**另一次请求**,直接 `execute_tool`,**不建 run / 不记 step / 零审计**。

改造:
- `confirm_required` 触发时,`_pending_actions[conv]` 多存 `run_id` + `access`(原本只存 name/args)。
- 确认回复路径 `confirm_events()`:
  - approved → `step_id = wf.add_step(原run_id, next_index, "tool", f"调用工具：{name}", connector, input_data={"args", "access": "write"})` → 执行 → `wf.finish_step(step_id, "succeeded"/"failed", ...)` → `wf.finish_run(原run_id, "succeeded")`。
  - denied → `wf.finish_run(原run_id, "cancelled")`(不建工具 step;run 状态表达「已取消」)。
- next_index:确认路径脱离原 events() 作用域,用「该 run 现有 step 数」作为新 index(查 `WorkflowStep` count by run_id),避免 index 冲突。

效果:无论自动读工具还是确认写工具,**所有真实工具调用都有 step 审计**(PRD §4.3)。

## 5. 前端(最小)

- `api.ts streamMessage`:加 `onTool` 回调,消费 `tool_call`(开始:name+access)/`tool_result`(完成:name+result 摘要)。`access` 从 `tool_call` 事件带出(后端在 emit tool_call 时附上 access)。
- `App.tsx`:会话区渲染**工具调用行**(🔧 工具名 + read/write 标 + 进行中/成功/失败),复用 M3 chip 样式族(`.skill-chip` 家族派生 `.tool-chip`)。
- 计划卡已有 `tool` 步类型标签,无需改。

## 6. 数据流(完整一次带写工具的执行)

```
用户消息(带 feishu 连接器)
 → send_message:build tool_specs / mcp_write_names
 → create_run(planning)
 → events():
    planning → strategy.plan(requires_tools=true)
    → build_tool_policy(...) → plan["tool_policy"] → save_plan(running)
    → [可选 retrieve step]
    → respond step started
    → stream_agent 循环:
        模型决定调 send_feishu_message(write)
        → confirm_required → _pending_actions{name,args,run_id,access} → run=waiting_confirmation
 用户回「确认」(新请求)
 → send_message 顶部命中 pending
 → confirm_events():approved
    → add_step(run_id,"tool","调用工具：send_feishu_message",feishu,{args,access:write})
    → execute_tool → finish_step(succeeded)
    → finish_run(succeeded)
```

## 7. 测试

- **单元(TDD,`tool_strategy`)**:① 空连接器 → tools=[]、write_tools=[];② 纯读工具 → access=read;③ 飞书写工具(is_write)→ write;④ GitHub MCP 写名(mcp_write_names)→ write;⑤ requires_tools 透传。
- **后端集成**:带连接器发一条会触发只读工具的消息 → 断言 run 含独立 `tool` step(status=succeeded)+ `plan_json.tool_policy` 已写入。
- **写确认闭环**:触发 confirm → 回「确认」→ 断言原 run 多了一个 `tool` step 且 run 转 succeeded;回「取消」→ run 转 cancelled、无 tool step。
- **回归**:`tsc --noEmit` + `vite build` 通过;现有 20 后端测试不破。

## 8. 影响文件清单

- 新增:`api/app/tool_strategy.py`、`api/tests/test_tool_strategy.py`、`api/tests/test_chat_tools.py`(集成)。
- 改:`api/app/main.py`(events 包裹 tool 步、confirm 路径审计、needs_confirm 改用 policy)、`web/src/api.ts`(onTool)、`web/src/App.tsx`(工具调用行)、`web/src/styles.css`(.tool-chip)。
- 不改:`api/app/model_gateway.py`(stream_agent 保持,已 emit tool_call/tool_result);`api/app/workflow.py`(add_step/finish_step 直接复用)。
