# Atlas Agent Runtime Pipeline 功能 PRD

版本：v0.1  
日期：2026-06-25  
状态：草案  
所属模块：智能体运行时 / Workflow / Skill Hub / MCP 工具策略

## 1. 背景

Atlas 当前已经具备智能工作台、会话持久化、SSE 流式回答、知识库 RAG、多模型网关、连接器和工具调用雏形。随着后续接入 Skill Hub、Web IDE、MCP 工具和发布后的 Agent/App，单纯把所有逻辑堆在一次聊天请求里会导致职责混乱、权限难控、执行过程不可追踪。

因此需要建设一套统一的 Agent Runtime Pipeline，把用户请求从“输入”到“规划”到“执行”到“输出”的过程标准化。

本功能 PRD 定义 Atlas 的核心执行管线：

```text
Workflow 管理输入/输出边界
-> Strategy Agent 规划与调度
-> MCP Strategy Agent 制定工具策略
-> Skill Agent 执行专业任务
-> Workflow 汇总、校验、落库、返回
```

## 2. 产品目标

### 2.1 核心目标

- 让每次 Agent 执行都具备明确的输入、输出、权限、状态和审计边界。
- 支持 Strategy Agent 将复杂任务拆解为多个可执行步骤。
- 支持 Skill Agent 按专业能力执行具体任务。
- 支持 MCP Strategy Agent 选择外部工具、连接器和 MCP Server。
- 支持 Workflow Runtime 对工具写操作、权限、错误、重试、输出 schema 进行统一管理。
- 为后续 Skill Hub、Web IDE、应用发布市场和多 Agent 编排提供基础运行时。

### 2.2 非目标

P0 不实现完整可视化流程编排器。

P0 不要求多 Agent 并行调度，先支持串行步骤和有限并发预留。

P0 不要求 MCP Strategy Agent 完全独立部署，可先由 Strategy Agent 兼任，但数据模型和事件协议需预留。

P0 不支持任意用户代码直接在主服务进程执行。涉及代码型 Skill 或 App 时，必须走沙箱。

## 3. 术语定义

### 3.1 Workflow Runtime

Workflow Runtime 是执行边界层，不负责“思考”，负责管理一次任务运行的生命周期。

职责：

- 接收输入。
- 校验输入 schema。
- 加载用户、Agent、知识库、Skill、连接器和权限上下文。
- 创建 run 和 step。
- 管理状态流转。
- 拦截高风险工具调用。
- 执行输出校验。
- 保存消息、来源、工具事件、错误和审计日志。

### 3.2 Strategy Agent

Strategy Agent 是任务规划层，负责理解用户目标、拆分步骤、选择 Skill、决定执行顺序。

职责：

- 判断用户意图。
- 识别是否需要知识库、Skill、工具、连接器。
- 生成结构化执行计划。
- 给每个 step 分配执行者。
- 根据中间结果调整后续步骤。

### 3.3 MCP Strategy Agent

MCP Strategy Agent 是外部工具策略层，负责判断任务是否需要 MCP Server 或连接器工具，并生成工具调用策略。

职责：

- 根据任务步骤选择 MCP Server。
- 选择具体 tool。
- 判断 tool 是读操作还是写操作。
- 生成工具参数 schema。
- 识别需要用户确认或管理员授权的操作。

P0 可由 Strategy Agent 兼任，P1 拆成独立模块。

### 3.4 Skill Agent

Skill Agent 是专业执行层，负责加载 Skill 内容并完成具体任务。

职责：

- 加载 Skill Hub 中的 Skill 定义。
- 使用 Skill 的步骤、约束、输出格式执行任务。
- 可调用 RAG 检索、模型生成、工具调用。
- 输出结构化结果给 Workflow Runtime。

### 3.5 Tool / MCP Layer

Tool / MCP Layer 是实际动作层，负责调用飞书、GitHub、CRM、数据库、浏览器、文件系统、Web IDE 沙箱等外部能力。

该层不决定“该不该调用”，只执行已授权的调用。

## 4. 用户故事

### 4.1 业务用户执行复杂任务

作为业务用户，我希望输入一句自然语言需求，平台能自动拆分任务、调用知识库和工具，并把执行过程清晰展示出来。

示例：

```text
帮我根据产品知识库生成客户拜访简报，并发到飞书给我。
```

预期：

- 平台识别这是“生成简报 + 发送消息”的复合任务。
- 自动使用销售简报 Skill。
- 检索产品知识库。
- 生成简报。
- 发送飞书前请求用户确认。
- 用户确认后执行发送。
- 对话中展示每一步状态和最终结果。

### 4.2 Agent 开发者配置 Skill

作为 Agent 开发者，我希望给 Agent 绑定多个 Skill，让运行时根据用户请求自动选择合适 Skill。

验收标准：

- Agent 可绑定多个 Skill。
- 每个 Skill 有触发条件、能力描述和输出格式。
- 用户提问时，Strategy Agent 能选择 0 个、1 个或多个 Skill。
- 被选中的 Skill 会记录在 run logs 中。

### 4.3 管理员治理工具调用

作为管理员，我希望所有 MCP 工具调用都经过权限和风险判断，尤其是写操作必须确认或审核。

验收标准：

- 读操作可自动执行。
- 写操作默认进入确认流程。
- 高风险操作可要求管理员审批。
- 所有工具调用记录调用人、Agent、参数摘要、结果、耗时和状态。

## 5. Pipeline 总览

### 5.1 标准执行流程

```text
User Request
-> Workflow.start()
-> Input Guard
-> Context Assembly
   - User
   - Agent
   - Knowledge Base
   - Skills
   - Connectors
   - Permissions
   - Conversation History
-> Strategy Agent.plan()
-> MCP Strategy Agent.plan_tools()
-> Workflow.create_steps()
-> Skill Agent.execute(step)
-> Tool / MCP calls
-> Workflow.collect_outputs()
-> Output Guard
-> Persist
   - Messages
   - Sources
   - Tool Events
   - Run Logs
   - Audit Logs
-> SSE / API Response
```

### 5.2 P0 简化流程

P0 可以先实现为：

```text
Workflow Runtime
-> Strategy Agent
-> Skill Agent
-> Tools / MCP
-> Workflow Runtime
```

其中 MCP Strategy Agent 的职责先合并在 Strategy Agent 内，但数据结构保留 `tool_strategy` 字段。

## 6. 功能需求

### 6.1 Workflow Runtime

Workflow Runtime 必须管理一次执行的完整生命周期。

能力：

- 创建 `workflow_run`。
- 创建 `workflow_step`。
- 管理状态：`created`、`planning`、`running`、`waiting_confirmation`、`succeeded`、`failed`、`cancelled`。
- 校验输入 schema。
- 汇总 Agent 配置、知识库、Skill、连接器和权限。
- 对每一步做超时控制。
- 记录 step 输入、输出、错误和耗时。
- 在输出返回前执行 Output Guard。

输入：

```json
{
  "conversation_id": "uuid",
  "agent_id": "uuid",
  "content": "用户请求",
  "knowledge_base_id": "uuid",
  "skill_ids": ["skill_x"],
  "connectors": ["feishu", "github"],
  "attachments": []
}
```

输出：

```json
{
  "run_id": "uuid",
  "status": "succeeded",
  "answer": "最终回答",
  "sources": [],
  "steps": [],
  "tool_events": []
}
```

### 6.2 Input Guard

Input Guard 负责在任何 Agent 规划前拦截非法输入。

规则：

- 空文本拒绝。
- 超长输入截断或拒绝。
- 未授权知识库拒绝。
- 未授权 Skill 拒绝。
- 未授权 connector 拒绝。
- 附件大小和文件类型校验。

### 6.3 Context Assembly

Context Assembly 负责组装运行上下文。

上下文包括：

- 用户身份和权限。
- 当前会话历史。
- Agent system prompt。
- Agent 绑定的知识库。
- 用户本次选择的知识库。
- Agent 绑定或用户本次选择的 Skill。
- 连接器状态。
- 可用 MCP tool specs。
- 附件解析结果。

输出给 Strategy Agent 的上下文必须是摘要化和结构化的，不直接塞入所有原始文档。

### 6.4 Strategy Agent

Strategy Agent 输出结构化计划。

计划格式：

```json
{
  "goal": "生成客户拜访简报并发送飞书",
  "skills": ["sales-brief"],
  "requires_knowledge": true,
  "requires_tools": true,
  "steps": [
    {
      "id": "step_1",
      "type": "retrieve",
      "title": "检索产品知识库",
      "executor": "rag"
    },
    {
      "id": "step_2",
      "type": "skill",
      "title": "生成销售拜访简报",
      "executor": "skill_agent",
      "skill_id": "sales-brief"
    },
    {
      "id": "step_3",
      "type": "tool",
      "title": "发送飞书消息",
      "executor": "mcp_tool",
      "connector": "feishu",
      "risk": "write"
    }
  ]
}
```

要求：

- 必须输出 JSON。
- 每个 step 必须有明确输入和预期输出。
- 不允许直接输出未授权工具。
- 当没有合适 Skill 时，允许退化为普通对话。
- 对高风险写操作必须标记 `risk=write`。

### 6.5 MCP Strategy Agent

MCP Strategy Agent 负责把计划中的工具需求转成具体 MCP/tool 调用策略。

输入：

- Strategy Agent 生成的 plan。
- 当前启用 connectors。
- MCP Server 返回的 tools。
- 用户权限。

输出：

```json
{
  "tool_strategy": [
    {
      "step_id": "step_3",
      "server": "feishu",
      "tool": "send_message",
      "operation": "write",
      "requires_confirmation": true,
      "arguments_schema": {
        "receive_id": "string",
        "content": "string"
      }
    }
  ]
}
```

规则：

- 读操作：可自动执行，但必须记录日志。
- 写操作：默认用户确认。
- 高风险写操作：管理员审批。
- 未授权工具：不可进入执行计划。
- 工具参数必须经过 schema 校验。

### 6.6 Skill Agent

Skill Agent 负责执行具体专业任务。

Skill 类型：

- `instruction`：只注入方法论和输出格式。
- `workflow`：包含固定步骤。
- `tool-backed`：需要调用外部工具。
- `code`：由 Web IDE 编写，必须沙箱运行。

P0 支持：

- `instruction`
- `workflow`
- 简单 `tool-backed`

P0 不直接支持代码型 Skill 在主服务运行。

Skill Agent 输入：

```json
{
  "step_id": "step_2",
  "skill_id": "sales-brief",
  "task": "生成销售拜访简报",
  "context": {
    "knowledge_hits": [],
    "conversation_summary": "",
    "user_input": ""
  },
  "output_schema": {}
}
```

Skill Agent 输出：

```json
{
  "step_id": "step_2",
  "status": "succeeded",
  "content": "生成的简报",
  "structured_output": {},
  "sources": []
}
```

### 6.7 Tool / MCP Execution

工具执行层负责实际调用。

要求：

- 所有工具调用必须由 Workflow Runtime 分配 run_id 和 step_id。
- 工具调用前做权限校验。
- 工具调用后保存结果摘要。
- 工具错误必须结构化返回，不允许直接中断整个对话，除非该 step 是必需步骤。

## 7. SSE 事件协议

当前项目已经通过 SSE 返回 token、sources、tool_call、tool_result 等事件。Pipeline 功能需要扩展事件类型。

### 7.1 事件列表

```text
run_started
planning_started
plan_created
step_started
step_progress
sources
tool_call
confirm_required
tool_result
step_completed
step_failed
token
run_completed
run_failed
done
```

### 7.2 示例

```json
{"type":"run_started","run_id":"run_123"}
```

```json
{"type":"plan_created","steps":[{"id":"step_1","title":"检索产品知识库"}]}
```

```json
{"type":"confirm_required","step_id":"step_3","message":"即将发送飞书消息，确认请回复「确认」。"}
```

```json
{"type":"run_completed","run_id":"run_123","status":"succeeded"}
```

## 8. 数据模型

### 8.1 新增表

建议新增：

```text
workflow_runs
workflow_steps
skill_runs
tool_calls
agent_skills
skills
skill_versions
```

### 8.2 workflow_runs

字段：

- id
- conversation_id
- agent_id
- user_id
- input_text
- status
- plan_json
- output_json
- error
- started_at
- ended_at
- created_at

### 8.3 workflow_steps

字段：

- id
- run_id
- index
- type
- title
- executor
- skill_id
- status
- input_json
- output_json
- error
- started_at
- ended_at

### 8.4 skills

字段：

- id
- name
- description
- type
- trigger_phrases
- status
- latest_version_id
- created_at
- updated_at

### 8.5 skill_versions

字段：

- id
- skill_id
- version
- manifest_json
- content
- output_schema_json
- required_connectors_json
- required_permissions_json
- created_at

### 8.6 agent_skills

字段：

- id
- agent_id
- skill_id
- enabled
- created_at

### 8.7 tool_calls

字段：

- id
- run_id
- step_id
- connector
- tool_name
- operation
- arguments_json
- result_json
- status
- requires_confirmation
- confirmed_at
- error
- started_at
- ended_at

## 9. API 需求

### 9.1 Workflow API

P0 可复用当前聊天接口，但内部创建 run。

后续独立 API：

```text
POST /api/workflows/runs
GET  /api/workflows/runs/{run_id}
GET  /api/workflows/runs/{run_id}/steps
POST /api/workflows/runs/{run_id}/cancel
POST /api/workflows/runs/{run_id}/confirm
```

### 9.2 Skill Hub API

```text
GET    /api/skills
POST   /api/skills
GET    /api/skills/{skill_id}
PUT    /api/skills/{skill_id}
DELETE /api/skills/{skill_id}
POST   /api/skills/{skill_id}/publish
POST   /api/agents/{agent_id}/skills
DELETE /api/agents/{agent_id}/skills/{skill_id}
```

### 9.3 MCP Strategy API

P1 后可独立：

```text
GET  /api/mcp/servers
GET  /api/mcp/servers/{server_id}/tools
POST /api/mcp/plan-tools
```

## 10. 前端需求

### 10.1 工作台执行过程展示

对话过程中展示：

- 正在规划。
- 已生成步骤。
- 当前执行步骤。
- 检索到的来源。
- 工具调用。
- 等待确认。
- 执行完成。

### 10.2 Run Detail 页面

用于开发者和管理员查看一次运行详情。

展示：

- 用户输入。
- Agent。
- 使用的 Skill。
- 使用的知识库。
- 使用的连接器。
- 执行计划。
- 每个 step 的输入、输出、状态、耗时。
- 工具调用日志。
- 错误详情。

### 10.3 Skill Hub 页面

展示：

- Skill 列表。
- 类型。
- 状态。
- 触发词。
- 绑定的 Agent 数。
- 最近运行次数。
- 编辑入口。

## 11. 权限与安全

### 11.1 权限边界

Workflow Runtime 在规划和执行前必须判断：

- 用户是否可使用该 Agent。
- 用户是否可访问该知识库。
- 用户是否可使用该 Skill。
- 用户是否可启用该 connector。
- 用户是否可执行该 MCP tool。
- 当前操作是否需要确认或审批。

### 11.2 写操作确认

写操作包括但不限于：

- 发送消息。
- 创建 GitHub issue。
- 修改文件。
- 写数据库。
- 调用业务系统写接口。
- 发布应用。

默认策略：

- 普通写操作：用户确认。
- 高风险写操作：管理员审批。
- 未授权写操作：拒绝。

### 11.3 代码型 Skill

代码型 Skill 必须运行在沙箱：

- 不进入 FastAPI 主进程。
- 限制 CPU、内存、运行时长。
- 限制文件系统。
- 限制网络访问。
- secrets 最小授权注入。
- 日志脱敏。

## 12. 错误处理

错误类型：

- `input_invalid`
- `permission_denied`
- `planning_failed`
- `skill_not_found`
- `skill_execution_failed`
- `tool_not_authorized`
- `tool_execution_failed`
- `confirmation_timeout`
- `output_invalid`
- `runtime_timeout`

错误返回必须包含：

- code
- message
- run_id
- step_id，可选
- recoverable
- suggested_action

## 13. 观测与审计

需要记录：

- run 数。
- 成功率。
- 平均耗时。
- 每个 step 耗时。
- Skill 使用次数。
- MCP tool 使用次数。
- 写操作确认率。
- 错误率。
- token 用量。
- RAG 命中数量。

管理员可查看：

- 谁在什么时候运行了哪个 Agent。
- 使用了哪些 Skill。
- 调用了哪些工具。
- 是否执行了写操作。
- 写操作是否经过确认。
- 输出是否通过 guard。

## 14. 里程碑

### M1：Workflow Run 雏形

- 当前聊天接口内部创建 run。
- 保存 run 和 step。
- SSE 返回 run_started、step_started、step_completed。
- 写操作继续沿用现有确认机制。

### M2：Strategy Agent 结构化规划

- Strategy Agent 输出 JSON plan。
- Workflow 根据 plan 创建 steps。
- 支持 RAG step、Skill step、Tool step。

### M3：Skill Hub 接入

- Skill 表和 SkillVersion 表。
- Agent 绑定 Skill。
- 对话时自动选择或手动选择 Skill。
- Skill Agent 执行 instruction/workflow Skill。

### M4：MCP Strategy Agent 独立

- MCP Server 和 tool catalog。
- 工具策略生成。
- 读写风险识别。
- 工具参数 schema 校验。

### M5：可视化运行详情与治理

- Run Detail 页面。
- Tool Call 审计。
- Skill 使用统计。
- 管理员审批策略。

## 15. 验收标准

P0 验收：

- 用户发起一次复杂请求后，系统能创建 workflow run。
- 系统能输出至少 2 个结构化 step。
- 每个 step 有状态、输入、输出和耗时。
- RAG 检索结果能作为 step 输出进入后续 Skill。
- 工具写操作能触发确认。
- 最终回答保存到会话。
- run detail 可查询。
- 权限不足时任务在执行前被拒绝。

P1 验收：

- Agent 可绑定多个 Skill。
- Strategy Agent 能根据用户输入选择 Skill。
- MCP Strategy Agent 能生成工具策略。
- Skill Agent 能按 Skill 输出结构化结果。
- 管理员可查看完整 run、step、tool call 审计。

## 16. 示例流程

用户输入：

```text
帮我根据产品知识库生成客户拜访简报，并发到飞书给我。
```

执行：

```text
Workflow Runtime
- 校验输入
- 加载用户权限
- 加载 Agent、知识库、Skill、飞书连接器

Strategy Agent
- 识别任务：生成简报 + 发送消息
- 选择 sales-brief Skill
- 生成三步计划

MCP Strategy Agent
- 判断发送飞书需要 feishu.send_message
- 标记为写操作
- 要求用户确认

Skill Agent
- 执行 sales-brief
- 使用 RAG 检索产品知识库
- 生成结构化简报

Workflow Runtime
- 请求用户确认发送飞书
- 确认后调用工具
- 保存 run、steps、sources、tool calls
- 返回最终结果
```

## 17. 结论

Atlas Agent Runtime Pipeline 是平台从“聊天应用”升级为“企业 Agent 应用平台”的核心基础设施。

建议产品口径：

> Workflow 管边界，Strategy Agent 管规划，MCP Strategy Agent 管工具策略，Skill Agent 管专业执行。

P0 可以先把 MCP Strategy Agent 合并在 Strategy Agent 内部，但从数据模型、SSE 事件和审计日志上预留独立层，避免后续重构成本。

