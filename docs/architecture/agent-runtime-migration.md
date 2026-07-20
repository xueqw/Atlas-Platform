# Atlas Agent Runtime 迁移与三大能力落地标准

## 1. 目标与边界

Atlas 将智能工作台、Agent Studio、应用开发预览、评测、外部 API 与子 Agent 调用逐步收敛到统一的 LangGraph Runtime。迁移必须保持租户权限、不可变 Agent 版本、Redis/PostgreSQL/pgvector/MinIO 数据边界、Docker 代码沙箱、发布与回滚闭环兼容。

本阶段交付三项能力：

1. 受治理的多层 Agent 记忆；
2. 渐进加载、树状路由、召回加重排的 Skill 检索；
3. 组织者动态创建 Worker Agent、DAG 并行调度与结果汇总的 Multi-Agent 模式。

## 2. 统一运行时架构

统一运行时由五层构成：

- Planning Layer：支持 Direct、只读 ReAct、Plan-and-Execute 和 Multi-Agent DAG；规划、工具迭代和重规划均有硬上限。
- Memory System：Redis 保存短期热状态，PostgreSQL/pgvector 保存 Ledger、语义事实和长期证据；模型读取的记忆始终标为非可信数据。
- Tool Orchestration：工具注册表与服务端策略决定真实读写属性；模型声明不构成授权。读工具可按策略自动执行，写工具进入持久化确认票据流程。
- State Management：LangGraph PostgreSQL Checkpointer 保存执行检查点，Atlas RuntimeRun/RuntimeEvent 保存稳定产品契约；Redis 丢失不得破坏权威状态。
- Error Recovery：错误分类为 transient、validation、permission、dependency、plan、side-effect、terminal；仅无副作用的瞬时错误允许有界重试。

所有产品入口使用不可变的 `agent_version_id`。启用 `LANGGRAPH_RUNTIME_ENABLED` 后，版本化 Prompt Agent 经统一 Runtime 执行；无版本或无可归属租户用户时失败关闭，只有显式 `LANGGRAPH_RUNTIME_LEGACY_FALLBACK` 才允许在任何副作用发生前走旧适配器。代码 Agent 继续由受限 Docker Runner 执行，禁止降级为主进程执行。

## 3. Agent 记忆

### 3.1 七层模型

| 层 | 权威存储 | 用途 | 保留/删除要求 |
|---|---|---|---|
| Raw Event / Ledger | PostgreSQL append-only ledger | 记录输入、写入、修正、删除、评测和发布证据 | 幂等追加；删除追加 tombstone，不物理改写历史 |
| Session Context | Redis 热副本 + PostgreSQL checkpoint | 当前会话摘要、消息窗口 | TTL；Redis 丢失可从权威记录重建 |
| Task Working State | PostgreSQL checkpoint | 计划、步骤、局部变量、Worker 工作状态 | compare-and-swap 版本控制，拒绝并发旧写 |
| Semantic Memory | PostgreSQL + pgvector | 可召回的事实与相似度检索 | 双时间、同意状态、敏感度与过期策略过滤 |
| Episodic Memory | PostgreSQL | 一次任务轨迹、结果、指标、证据 | 必须脱敏后才能进入程序性候选 |
| Procedural Memory | PostgreSQL + 版本资产 | 多次成功轨迹固化出的流程候选 | 回放评测、人工批准后才能发布 Agent/Skill |
| 用户结构化档案卡 | PostgreSQL projection + Ledger | 地区、偏好、业务属性等结构化视图 | CAS 更新、租户/用户/Agent 隔离、tombstone 删除 |

### 3.2 Ledger–Views–Policy

- Ledger：所有事实、档案、工作状态和程序性候选的变更均由幂等事件驱动。
- Views：Session、Working State、有效事实、Profile Card 是可重建投影视图，不替代 Ledger。
- Policy：召回按 workspace、user、agent、run/worker 精确隔离，并检查有效时间、交易时间、过期、同意状态、敏感等级和调用方权限。

### 3.3 双时间

- Valid Time 表示事实在现实世界何时成立；采用 `[valid_from, valid_to)`。
- Transaction Time 表示系统何时知道该版本；采用 `[transaction_from, transaction_to)`。
- 修正“用户搬家”等事实时，新事实覆盖对应有效区间，旧事实保留历史交易版本；查询必须同时传入或默认解析 valid-at 和 transaction-as-of，禁止把旧地址污染到当前上下文。

### 3.4 程序性固化

多次任务轨迹先形成 Episodic Evidence，再生成 Agent/Skill Candidate。候选默认不可发现、不可路由；只有完成脱敏、离线回放评测、人工批准和版本发布后，才能写入正式 `AgentVersion` 或 `Skill`，并支持审计与回滚。

### 3.5 Agent Studio 规划记忆完整性

Agent Studio 的规划记忆是七层记忆在设计阶段的受治理投影，遵循同一套
Ledger–Views–Policy 约束：

- 首条实质性用户需求生成 `goal_anchor`，记录来源消息、`valid_from` 和
  `transaction_time`；模型不能修改该证据。
- 模型输出只作为 memory candidate。约束、偏好、风险和决策采用规范化增量
  去重；`requirement_summary` 只有保留目标核心概念，或近期用户明确表达
  “改成/换成/重新做”等目标切换时才可替换。
- 高影响字段的接受与拒绝都写入 `memory_update_audit`，包含字段、候选摘要、
  决策、原因、来源 turn 和交易时间；审计不得包含凭据或原始敏感 Prompt。
- `apply_readiness=ready` 必须以已验证的 proposal 为证据。仅有模型文本、
  消息数量或历史污染摘要时不得进入可应用状态。
- 旧会话缺少锚点时，从持久化的原始 `user_request` 或最早实质性用户消息
  惰性派生；不同 conversation、workspace、user 和 Agent 的锚点、候选、
  审计和投影视图不得交叉。

规划记忆元数据存入现有 `planning_state_json`，Redis 仅作为热状态；权威会话
与审计仍走当前持久化边界，因此无需破坏性数据库迁移。

### 3.6 规划器空响应与确认恢复

OpenAI-compatible provider 的 HTTP 200 不等于一次有效规划完成。适配器必须
识别 SSE 与非 SSE JSON completion，统计可见内容并保留 finish reason。零可用
内容抛出 typed failure，同一逻辑 turn 最多重试一次；重试耗尽后发送明确的
WebSocket `error` 与失败 run event，不写入空 assistant、不推进 stage。

A2UI 确认先按稳定 request ID 幂等落账，再继续同一规划上下文。重复 frame 或
重连不能重复 decision，也不能回退到未确认状态。`awaiting_confirmation` 必须有
pending A2UI，`ready_to_apply` 必须有已验证 proposal。运维排查时同时检查：

1. run event 是否从 `context_load` 继续到 proposal compose，或明确记录 retry/failed；
2. 会话中是否存在空 assistant（新版本应为零）；
3. pending A2UI、decision ledger 与 proposal artifact 是否一致；
4. memory audit 是否拒绝了无用户证据的跨域摘要覆写。

## 4. Skill 检索

### 4.1 元数据契约

每个可发布 Skill 必须包含：名称、差异化摘要、最多三级 `category_path`、`use_when`、`do_not_use_when`、输入/输出 JSON Schema、权限、语义版本和状态。摘要必须描述触发场景，不得只重复功能名称。

### 4.2 渐进加载与树状路由

1. 第一阶段只加载名称、摘要、分类、正负触发场景等安全元数据；不得泄漏正文。
2. Skill Router 先选择一级树分支，再在分支内召回；分支不明确时返回 `no_selection`，不得全量正文匹配。
3. 关键词和 pgvector 形成召回分数，场景匹配、负样本和阈值形成重排分数。
4. 分数过低、前两名过近、命中负样本或权限不满足时不自动选择。
5. 手动选择与自动选择取并集后再次执行权限/状态过滤。
6. 只有被审计决策选中的 Skill 才可按 `decision_id` 加载完整正文。

工作台和 Agent Studio 必须支持搜索、分类/状态/权限筛选、相关度/名称/版本/更新时间排序和稳定分页。

## 5. Multi-Agent 模式

### 5.1 组织者

组织者接收全局目标和可信工具白名单，通过模型或确定性降级方案生成 `WorkerSpec[]` 与 `TaskPlan[]`。只有组织者可以创建、寻址和终止 Worker；最大 Worker 数、并发数、单任务超时、重试次数和重规划次数均由服务端限制。

组织者创建的 Worker 是运行级临时 Agent，具备独立角色、目标、输入/输出 Schema、工具子集、最大步骤和超时。组织者可在程序性候选通过治理流程后，将验证过的 Worker 固化为正式 Agent 版本。

### 5.2 Worker 隔离与通信

- Worker 只接收自己的系统角色、目标和 `TaskEnvelope`，不能读取兄弟 Worker 的消息、提示词、工具、临时状态或未授权工件。
- 输入统一为版本化 `TaskEnvelope` JSON，输出统一为版本化 `ResultEnvelope` JSON；边界执行 Schema 校验。
- 大结果写入 MinIO，Worker 之间仅传递带 workspace/run/worker 绑定的 `artifact_refs`。
- 工具权限为用户授权、组织者授权、Worker 工具集与当前授权的交集；写操作票据绑定参数摘要、资源版本、范围、有效期和 nonce，消费后不可重放。

### 5.3 调度与失败

- DAG 必须无环、依赖存在；无依赖任务按并发上限并行。
- 瞬时失败和超时按任务上限重试；硬失败取消依赖任务。
- 进程重启时，已 dispatch 未完成的任务根据稳定 envelope ID 恢复或终止。
- 组织者最多重规划两次；无可调度任务时失败关闭，不允许无限循环。
- 汇总按稳定顺序输出，并显式报告 partial、conflict、failed、cancelled；冲突键不得静默覆盖。

### 5.4 自适应执行与独立审查

统一 Runtime 在开始执行前选择 `react`、`plan-execute-review` 或
`multi-agent-plan-execute-review`。复杂任务必须经过独立 Reviewer Subagent；执行
Worker、组织者和 Reviewer 不得由同一身份自审。Reviewer 只接收版本化
`ReviewRequestEnvelope`，没有写工具或调度权限，并返回 `PASS`、`REVISE`、
`REPLAN`、`REJECT` 或 `ESCALATE`。只有完整覆盖验收标准和任务的合法 `PASS`
允许成功终止。详细状态机、Schema、事件和 API 见
[`adaptive-agent-execution.md`](./adaptive-agent-execution.md)。

## 6. 验收标准

### 6.1 功能验收

- Runtime：工作台、Agent Studio、预览、评测、外部 API 和 subagent 的版本化 Prompt Agent 可进入同一 Runtime 契约；事件 ID 去重、游标续传、取消、失败和恢复可见。
- Memory：七层记录/投影存在；跨 workspace/user/agent/worker 查询为零泄漏；搬家修正的当前查询只返回新地址，历史双时间查询仍可返回旧地址；并发旧版本写返回冲突；删除产生 tombstone。
- Skill：未选正文不出现在规划上下文；树先于 Skill 级匹配；负样本和低置信度产生不选择；搜索/筛选/排序/分页稳定；路由决策可审计重放。
- Multi-Agent：组织者可从一个目标创建 Worker 和 DAG；至少两个独立任务真实并行；Schema 不符、超时、重试、取消、恢复和冲突均有持久化状态；票据跨进程重放被拒绝；候选未经批准不可发布。
- Adaptive Runtime：简单任务选择 ReAct；复杂任务选择 PER；可拆解任务选择 Multi-Agent PER；复杂运行没有独立 Reviewer PASS 时不能成功；五种 verdict、返工/重规划预算和升级恢复全部可审计。
- 发布闭环：程序性候选发布生成真实 AgentVersion/Skill 版本，支持回滚，所有动作有操作者和租户审计。

### 6.2 自动化与真实数据面

发布前必须同时提供以下证据，任何一项缺失都不得标记生产验收完成：

```bash
cd api && python -m pytest -q
cd web && npm test -- --run && npm run build
cd modules/agentgateway/backend && python -m pytest -q
cd modules/agentgateway/frontend && npm run build
```

真实基础设施验收必须使用 PostgreSQL/pgvector、Redis、MinIO 与 Docker Runner，而不是 mock：

- PostgreSQL Checkpointer 重启恢复、并发幂等启动、事件游标回放；
- pgvector 写入与最近邻查询；
- Redis 热状态写入、TTL 与 Redis 丢失后的 PostgreSQL 恢复；
- MinIO workspace 前缀隔离、工件摘要校验；
- Docker 禁网、只读根文件系统、cap-drop、CPU/内存/PID/超时限制；
- 备份产物校验和、隔离环境恢复、恢复后核心冒烟。

### 6.3 发布门禁

- 默认关闭统一 Runtime 流量开关；配置 PostgreSQL Checkpointer 且真实验证通过后才能按租户灰度。
- 禁止在发生任何外部副作用后回退旧运行时。
- 独立质检 Agent 必须仅按本文件和 OpenSpec 验收，读取代码与证据但不参与实现；所有 P0/P1 阻断问题关闭后方可发布。

## 7. 主要实现位置

- Runtime：`api/app/runtime_*`、`api/app/apps.py`、`web/src/runtime-events.ts`
- Memory：`api/app/memory_ledger.py`、`api/app/memory.py`、`api/app/governance_models.py`
- Skill：`api/app/skill_router.py`、`api/app/main.py`、`web/src/GovernanceDrawer.tsx`
- Multi-Agent：`api/app/multi_agent_runtime.py`、`multi_agent_repository.py`、`multi_agent_service.py`、`multi_agent_api.py`
- AgentGateway bridge：`web/src/application-runtime-bridge.ts`、`modules/agentgateway/frontend/src/components/shared/AtlasRuntimeBridge.tsx`
