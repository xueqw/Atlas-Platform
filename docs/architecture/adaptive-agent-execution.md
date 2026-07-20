# Atlas 自适应 Agent 执行框架

## 1. 目标

统一 Runtime 在运行开始时只做一次策略判断，并从以下模板中选择：

| 请求值 | 执行方式 | 适用范围 |
|---|---|---|
| `auto` | 服务端路由后选择下列模板 | 默认 |
| `react` | 有界 ReAct/只读工具循环 | 单目标、低风险、无依赖 |
| `plan-execute-review` | 单执行 Worker + 独立 Reviewer | 复杂但不适合并行 |
| `multi-agent-plan-execute-review` | DAG 并行 Worker + 独立 Reviewer | 可拆分的多个工作流 |

`auto` 决策由 `TaskStrategyRouter` 管理。确定性规则形成安全下限；可选模型分类只提供结构信号，不能把高风险、写操作、依赖任务或并行任务降级到 ReAct。决策包含 policy version、reason codes、score 和 confidence，并在第一个执行节点前写入 `strategy.selected`。

## 2. 复杂任务状态机

```text
classify
   │
   ▼
plan → validate plan → execute workers → create reviewer → review → decide
 ▲                         │                                 │
 │                         └──────── REVISE ────────────────┤
 └────────────────────────── REPLAN ────────────────────────┤
                                                            ├─ PASS → finalize
                                                            ├─ REJECT → failed
                                                            └─ ESCALATE → paused
```

- `REVISE` 只重做 ReviewResult 指定的任务，并生成 `RevisionRequestEnvelope`。
- `REPLAN` 创建新的不可变 Plan version，不覆写旧计划和结果。
- revise/replan 默认最多各两轮；预算耗尽时失败关闭。
- `ESCALATE` 必须由同一 workspace/user 的调用者提交显式 `approve|deny`；批准后重新规划，拒绝后取消。
- 只有独立 Reviewer 的合法 `PASS` 可以使复杂运行进入 `succeeded`。

## 3. Agent 间 Schema

所有边界使用 `extra=forbid` 的版本化 JSON：

- `StrategySignals.v1` / `StrategyDecision.v1`
- `PlanEnvelope.v1`
- `WorkerSpec.v1`
- `TaskEnvelope.v1`
- `WorkerResultEnvelope.v1`（映射到底层 `ResultEnvelope.v1`）
- `ReviewRequestEnvelope.v1`
- `ReviewResultEnvelope.v1`
- `RevisionRequestEnvelope.v1`

信封绑定 `run_id/workspace_id/user_id/agent_id`。未知字段、未知依赖、循环 DAG、重复 ID、Scope 不匹配、Reviewer 自审、验收标准覆盖不完整和非法 verdict 全部拒绝。Worker 的嵌套 input/output JSON Schema 在 Task/Result 两侧由固定版本的 JSON Schema 2020-12 校验器完整执行，不只检查第一层类型；非法 Schema 本身也失败关闭。大内容只通过已登记、同租户的 artifact ID/storage ref 传递。

JSON Schema 可由 `adaptive_contract_schemas()` 和 `contract_schemas()` 发布或用于 Agent Studio 表单校验。

## 4. Orchestrator、Worker 和 Reviewer

### Orchestrator

- 唯一允许创建 Worker 的组件；模型只能提出计划数据。
- 验证 DAG、Worker/Task 上限、Schema、工具交集和 Plan version。
- 将每轮执行映射到确定性的独立 `OrchestrationScope`，重启时复用已持久化结果。

### Worker

- 每个 Worker 通过独立 `RuntimeRun(source=subagent)` 运行。
- 只接收自己的角色、目标、TaskEnvelope 和授权 artifact refs。
- 无依赖任务最多三路并行；输出经过 ResultEnvelope 和 Worker output schema 双重校验。
- 工具权限是用户请求、Orchestrator grant、WorkerSpec 和当前票据的交集；写任务先在父 Runtime 持久化 `side_effect.boundary_started`，再允许子 Orchestration 进入工具调用。
- 未授权写任务返回 `paused`，子 Orchestration 签发绑定物理 task、worker、工具、参数摘要、scope、resource version 和 nonce 的票据；父 Runtime 将其包装为现有 actor-bound `interrupt.requested`。只有原 workspace/user 使用完整绑定批准后，才恢复同一个物理子任务；拒绝则取消父运行。票据单次消费，不能重放或换参。
- 如果进程恰好在持久化 `paused + authorization_requests` 后、附加父 interrupt ID 前退出，resume 入口会用确定性 nonce 幂等找回或补建同一父 interrupt，拒绝无绑定恢复，并要求调用者使用新事件中的完整绑定重试。
- 如果 interrupt 的 approved/denied 决定已经提交、父 Runtime state 尚未推进就退出，相同完整绑定只会在父运行仍 `paused` 且仍声明同一 interrupt ID 时恢复性应用；图恢复首节点随后清除该绑定，正常 anti-replay 继续生效。

### Reviewer

- 每轮审查创建独立、临时的 Reviewer orchestration 和 child RuntimeRun。
- 不继承执行 Worker 的消息或隐藏推理，只读取 ReviewRequestEnvelope。
- 工具注册表为空、effective tools 为空，不能写入、发布、删除、授权或调度 Worker。
- 输出由服务端附加身份字段，再执行 ReviewResult Schema、Scope、任务覆盖和验收标准覆盖校验；任何确定性 finding 失败时，模型返回 `PASS` 也会失败关闭。

## 5. 持久化与事件

父 `RuntimeRun.state_json` 保存策略、Plan、结果索引、review/revision/replan 轮次和终态；LangGraph PostgreSQL Checkpointer 保存节点检查点；Worker/Reviewer 的编排记录、Task/Result 和审计进入治理表。Redis 仍只保存热游标、锁和 TTL 状态，清空 Redis 不改变权威状态。

新事件包括：

- `strategy.selected`
- `plan.created`
- `worker.created` / `worker.completed`
- `review.requested` / `review.completed`
- `review.revision_requested` / `review.replan_requested`
- `run.paused` / `run.escalated` / `escalation.resolved`
- `side_effect.boundary_started` / `interrupt.requested` / `interrupt.resolved`

Web reducer 对未知事件只推进合法游标，不因新增类型崩溃。智能工作台、Agent
Studio 和应用开发内嵌的 AgentGateway 共用同一组 Runtime Event，并在用户执行
任务时直接展示运行指挥台：

- 简单任务展示 `THINK → ACT → OBSERVE` 的 ReAct 循环，不伪造 Subagent；
- 复杂任务展示 Orchestrator、策略选择原因、Plan 目标和执行团队；
- 每个 Subagent 独立展示角色、目标、任务、依赖、工具数量和实时状态；
- Reviewer 作为独立卡片展示，Review 历史保留 `PASS / REVISE / REPLAN /
  REJECT / ESCALATE` 裁决；
- 授权暂停、副作用边界、异常恢复、事件连接和节点详情继续可见；
- App Builder 通过版本化、来源受限的 Bridge 把同一事件流投影到内嵌测试对话，
  不在 iframe 中建立第二套运行权威。

## 6. API

`POST /api/runtime/runs` 增加：

```json
{
  "execution_strategy": "auto"
}
```

该字段进入幂等请求指纹。同一个 idempotency key 改用其他策略会返回冲突。Run handle 返回最终 `execution_strategy`。

Reviewer 返回 `ESCALATE` 后：

```json
{
  "escalation_decision": "approve"
}
```

提交到 `POST /api/runtime/runs/{run_id}/resume`。该决定不等同于工具写授权，不能生成或替代 ToolAuthorizationTicket。

Worker 写授权继续使用 Runtime resume 的完整 interrupt 绑定：

```json
{
  "interrupt_id": "...",
  "nonce": "...",
  "decision": "approve",
  "parameter_digest": "...",
  "resource_version": "..."
}
```

这五个字段必须同时提交；`deny` 直接取消运行。批准只对
`interrupt.requested.authorization_requests` 中列出的精确工具参数生效。

## 7. 开关与回滚

```dotenv
LANGGRAPH_RUNTIME_ENABLED=false
ADAPTIVE_RUNTIME_ENABLED=false
LANGGRAPH_RUNTIME_LEGACY_FALLBACK=false
```

生产启用顺序：先启用 LangGraph，再对测试 workspace 启用 Adaptive。任一开关默认都为 false。关闭 Adaptive 后恢复原 Phase 1 ReAct 选择；关闭 LangGraph 后恢复 legacy 路径。任何已开始副作用的运行禁止切换策略或 legacy 重放。

## 8. 验收

- 路由覆盖简单、复杂顺序、并行、高风险和显式 override。
- 五种 Review verdict、Schema 非法、自审、缺少 criterion/task、预算耗尽均有自动化测试。
- 至少两个 Worker 真实并行，Reviewer 是第三个独立 child RuntimeRun。
- 跨租户 artifact、result、review 和 run 查询失败关闭。
- 策略/Plan/Review 在新进程中可恢复；同一幂等键不重复运行。
- 副作用 snapshot 持久化后发生崩溃时不得调用 legacy adapter。
- Adaptive 写任务的 approve 路径只执行一次写调用，deny 路径不执行写调用；两条路径均通过现有 Runtime interrupt 闸门。
- Web reducer/build、完整 API 回归和 OpenSpec strict validation 全部通过。
- 智能工作台、Agent Studio 与应用开发预览均能在真实使用路径中显示
  Orchestrator、Subagents、独立 Reviewer 和最终裁决；ReAct 路径显示单智能体
  循环，并有组件测试与生产构建验证。
