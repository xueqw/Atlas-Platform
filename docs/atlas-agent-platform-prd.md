# Atlas 智能体平台 PRD

版本：v0.2  
日期：2026-06-24  
状态：草案  
适用项目：React + TypeScript + FastAPI + SQLAlchemy 版本

## 1. 背景与目标

Atlas 是面向企业内部的智能体平台，目标是让业务人员和开发人员在同一个网页平台内完成智能体创建、知识库接入、工具连接、测试、发布和使用。

当前项目已经从旧版本地原型迁移到新版 Web 架构，具备 FastAPI 后端、React 工作台、SQLAlchemy 持久化、SSE 流式对话、多模型网关、知识库接口和连接器/工具调用雏形。下一阶段产品重点是把平台从“智能对话工作台”升级为“可开发、可发布、可复用的企业 Agent 应用平台”。

本 PRD 覆盖两条主线：

1. 普通 RAG 知识库能力：文档上传、解析、切块、检索、引用、对话增强。
2. Web IDE 代码化开发能力：在网页端编写 Agent/App 代码，发布后供其他用户使用。

## 2. 当前项目状态

### 2.1 已具备能力

当前后端版本为 `0.2.0`，核心入口为 FastAPI `app`，前端为 Vite + React 工作台。

已实现或已具备雏形的能力包括：

- 多轮会话：创建、读取、删除会话，消息持久化。
- SSE 流式回答：通过 `/api/conversations/{conversation_id}/messages/stream` 返回 token、sources、tool event 和 done。
- 智能体配置：`Agent` 模型已包含名称、描述、系统提示词、模型、绑定知识库、状态。
- 知识库数据模型：`KnowledgeBase`、`Document`、`DocumentChunk` 已落库。
- 文档解析：支持 PDF、DOCX、TXT、Markdown、CSV、JSON。
- RAG 检索：支持 embedding 语义检索，embedding 不可用时回退关键词检索。
- 引用来源：`Message.sources` 已预留并在回答保存时写入来源 JSON。
- 多模型网关：支持按模型名路由到智谱 GLM、阿里云百炼等 OpenAI-compatible 接口。
- 连接器/工具：已有飞书 OAuth、GitHub MCP 接入、工具调用循环和写操作确认闸门。
- Chroma demo API：已有 `/api/knowledge/index`、`/api/knowledge/search`、`/api/knowledge/{collection_name}`。

### 2.2 RAG 当前说明

当前 RAG 能力从代码结构上已经可以实现普通知识库问答：上传文档后解析、切块、生成向量、检索 chunk，再把知识库资料注入对话上下文。

但在当前用户本机环境中，由于开发环境/IDE 安装受限，暂未完成端到端本地验证。该问题属于本机开发调试条件限制，不是产品设计上的不可行项。PRD 按“普通 RAG 功能可实现，但需补充环境验证和演示脚本”处理。

### 2.3 需要收敛的问题

当前代码里存在两条 RAG 路线：

- 应用内知识库路线：`KnowledgeBase` / `Document` / `DocumentChunk` + 数据库向量字段 + 对话检索增强。
- Chroma demo 路线：`api/app/services/rag.py` + `/api/knowledge/index/search`。

产品上应统一为“平台知识库”。技术实现可以保留 Chroma 作为向量库，但对前端和用户只暴露一套知识库概念。

## 3. 产品定位

Atlas 定位为企业私有化 Agent 应用平台。

核心价值：

- 业务人员可以通过网页配置和使用 Agent。
- 开发人员可以通过 Web IDE 代码化开发 Agent/App。
- 企业可以把知识库、模型、连接器、工具、权限和审计统一管理。
- 已发布的 Agent/App 可以被团队其他用户发现、运行和复用。

与普通聊天机器人不同，Atlas 的重点不是单次问答，而是 Agent 应用的完整生命周期：

```text
创建 -> 开发 -> 测试 -> 发布 -> 使用 -> 观测 -> 迭代
```

## 4. 用户角色

### 4.1 平台管理员

负责模型供应商、知识库、连接器、权限、安全策略和审计配置。

### 4.2 Agent 开发者

可以在网页中创建 Agent，编写代码，绑定知识库和工具，调试后发布应用。

### 4.3 业务使用者

从应用市场或工作台中选择已发布 Agent，完成问答、文档处理、数据分析、消息发送、GitHub 操作等任务。

### 4.4 审核人员

对高权限 Agent、含代码应用、带外部连接器的应用进行发布审核。

## 5. 目标范围

### 5.1 P0 范围

P0 目标是形成可演示的企业 Agent 平台闭环。

- 智能工作台可选择 Agent、模型、知识库和连接器。
- 普通 RAG 可上传文档并在对话中返回带引用答案。
- Agent 可保存、编辑、发布。
- 已发布 Agent 可在列表中被其他用户看到并使用。
- Web IDE 提供第一版代码化开发体验。
- 用户代码在服务端沙箱内运行，不进入 FastAPI 主进程。

### 5.2 非 P0 范围

以下能力不进入第一版交付，但保留架构位置：

- 多人实时协同编辑。
- 完整 Git 工作流和分支管理。
- 复杂拖拽式 workflow 编排器。
- 多租户计费。
- 私有云 Kubernetes 弹性调度。
- 完整企业 IAM / SSO。
- 任意语言运行时。第一版优先支持 Node/TypeScript。

## 6. 核心用户故事

### 6.1 使用 RAG Agent

作为业务用户，我希望上传产品文档，并在工作台中向 Agent 提问，系统能基于文档回答，并显示答案引用来源。

验收标准：

- 支持上传 PDF、DOCX、TXT、MD、CSV、JSON。
- 单文件大小限制默认 10 MB。
- 上传后展示文档名、状态、chunk 数、创建时间。
- 提问时可选择知识库。
- 回答前端展示引用卡片，至少包括文档名、页码、原文片段和相关度分数。
- embedding 不可用时，系统可回退关键词检索，并提示当前为降级检索。

### 6.2 创建并发布 Agent

作为 Agent 开发者，我希望创建一个 Agent，配置模型、系统提示词、知识库和工具，测试通过后发布给团队其他人使用。

验收标准：

- 可创建 Agent 草稿。
- 可编辑名称、描述、系统提示词、模型、绑定知识库、启用连接器。
- 可在发布前进行测试对话。
- 发布后状态从 `draft` 变为 `published`。
- 其他用户可以在 Agent 列表或应用市场看到已发布 Agent。

### 6.3 Web IDE 代码化开发

作为开发者，我希望在网页端编写 Agent/App 代码，保存版本，运行预览，并发布给其他用户使用。

验收标准：

- 提供浏览器内代码编辑器。
- 支持项目文件树、文件编辑、保存。
- 支持 `manifest` 描述应用名称、入口、权限、模型、知识库和工具。
- 支持运行预览，返回日志和错误。
- 支持发布版本，发布后其他用户可运行该版本。
- 用户代码必须运行在沙箱环境，不能直接访问主服务文件系统、数据库密钥和未授权网络。

## 7. 功能需求

### 7.1 智能工作台

功能说明：

- 展示最近任务列表。
- 支持新建会话、打开历史会话、删除会话。
- 输入框支持普通文本、附件解析、知识库选择、模型选择、连接器勾选。
- 对话区支持流式渲染 token。
- 对话区支持展示来源引用、工具调用过程、确认请求和错误信息。

关键接口：

- `GET /api/conversations`
- `POST /api/conversations`
- `GET /api/conversations/{conversation_id}`
- `DELETE /api/conversations/{conversation_id}`
- `POST /api/conversations/{conversation_id}/messages/stream`

### 7.2 知识库管理

功能说明：

- 创建知识库。
- 删除知识库。
- 上传文档。
- 删除文档。
- 查看文档列表、chunk 数、索引状态。
- 在聊天时选择知识库。

文档处理流程：

```text
上传文件
-> 读取原始 bytes
-> 按文件类型抽取文本
-> 清洗文本
-> 按 chunk size + overlap 切块
-> 调 embedding 服务
-> 保存 Document 和 DocumentChunk
-> 对话时检索相关 chunk
-> 注入模型上下文
-> 保存并展示 sources
```

检索策略：

- 优先使用 embedding 向量相似度。
- 未配置 embedding key 或 embedding 请求失败时，自动回退关键词重叠。
- 当 embedding 正常但无命中时，不强行回退关键词，避免噪声引用。
- 引用内容限制为可读片段，不直接向前端暴露整篇长文。

### 7.3 模型网关

功能说明：

- 支持多供应商模型配置。
- 按模型名前缀自动路由。
- 支持模型连通性测试。
- 支持演示模式：未配置 key 时使用本地 demo answer。

当前供应商：

- 智谱 AI：`glm-*`
- 阿里云百炼：`qwen-*`
- OpenAI-compatible 默认接口

后续扩展：

- Ollama 本地模型。
- 硅基流动生成模型。
- 模型调用用量统计。
- 供应商级限流和失败切换。

### 7.4 连接器与工具

功能说明：

- 用户在对话前勾选本次允许使用的连接器。
- 模型可通过 tool calls 调用工具。
- 读操作可直接执行。
- 写操作必须先向用户请求确认。

当前连接器：

- 飞书：OAuth 授权，发送飞书消息。
- GitHub：通过官方远程 MCP Server 动态接入工具。

写操作确认流程：

```text
模型请求写操作
-> 后端识别为高风险工具
-> SSE 返回确认提示
-> 用户回复“确认”或“取消”
-> 确认后执行工具
-> 返回执行结果
```

### 7.5 Agent 管理

功能说明：

- Agent 列表。
- 创建 Agent。
- 编辑 Agent。
- 删除 Agent。
- 设置状态：草稿、已发布、归档。
- 绑定知识库。
- 默认模型设置。

第一版 Agent 数据：

- 名称
- 描述
- 系统提示词
- 模型
- 知识库 ID
- 状态
- 创建时间
- 更新时间

### 7.6 应用市场 / 发布中心

功能说明：

- 展示已发布 Agent/App。
- 支持按名称、类型、创建者、更新时间搜索。
- 支持查看详情。
- 支持启动使用。
- 支持版本回滚。

发布对象分两类：

- Agent：配置型智能体，由 prompt、模型、知识库、工具组成。
- App：代码型应用，由 manifest、源码、构建产物和运行时组成。

发布状态：

- `draft`：草稿，仅作者可见。
- `reviewing`：审核中。
- `published`：已发布，授权用户可见。
- `archived`：已归档，不再新增使用。

## 8. Web IDE 产品设计

### 8.1 设计目标

Web IDE 的目标是让开发者在网页端完成 Agent/App 的代码化开发，并把开发成果发布为可被其他用户使用的应用。

第一版不追求完整替代本地 IDE，而是提供企业 Agent 应用开发所需的最小闭环：

```text
编辑代码 -> 保存版本 -> 沙箱运行 -> 预览调试 -> 发布共享
```

### 8.2 开发模式

平台同时保留两种开发模式：

1. 云端模式：代码保存在平台服务端，运行在云端沙箱，发布后供团队使用。
2. 本地伴侣模式：后续可选，通过 CLI、桌面端、VS Code 插件或本地 MCP Server 读写用户本机项目。

P0 只做云端模式。本地伴侣模式进入 P2 规划。

### 8.3 应用项目结构

第一版代码型应用建议使用固定结构：

```text
app.manifest.json
src/agent.ts
src/tools.ts
src/ui.tsx
README.md
```

`app.manifest.json` 示例：

```json
{
  "name": "销售支持助手",
  "description": "基于产品资料和 CRM 工具辅助销售团队完成客户答疑。",
  "runtime": "node18",
  "entry": "src/agent.ts",
  "ui": "src/ui.tsx",
  "model": "glm-4-flash",
  "knowledgeBaseIds": ["kb_product"],
  "connectors": ["feishu", "github"],
  "permissions": {
    "network": ["api.company.internal"],
    "secrets": ["CRM_API_TOKEN"],
    "tools": ["crm.lookup", "feishu.send_message"]
  }
}
```

### 8.4 Web IDE 页面组成

页面区域：

- 左侧：项目列表 / 文件树。
- 中间：代码编辑器。
- 右侧：预览区 / Agent 测试对话。
- 底部：终端日志、运行输出、问题列表。
- 顶部：保存、运行、发布、版本、环境选择。

第一版推荐使用 Monaco Editor。

### 8.5 运行与预览

运行流程：

```text
用户点击运行
-> 后端创建 app_run
-> 复制当前草稿代码到临时工作区
-> 在 Docker/Node sandbox 中安装依赖或加载预置依赖
-> 启动运行
-> 将日志、错误、预览地址返回前端
```

要求：

- 运行超时默认 60 秒。
- 内存默认限制 512 MB。
- CPU 默认限制 1 核。
- 默认禁止访问宿主文件系统。
- 默认禁止访问内网和公网，按 manifest 白名单开放。
- secrets 只能按应用授权注入。

### 8.6 发布流程

发布流程：

```text
保存草稿
-> 运行校验
-> 权限检查
-> 安全扫描
-> 创建 app_version
-> 标记 release
-> 出现在应用市场
```

发布后其他用户访问的是不可变版本，不直接访问开发者草稿。

### 8.7 代码存储

代码不应只保存在服务器某个目录中。推荐数据结构：

- `apps`：应用主表。
- `app_versions`：版本表。
- `app_files`：源码文件表或对象存储索引。
- `app_releases`：发布记录。
- `app_runs`：运行记录。
- `app_permissions`：权限声明和审批结果。

第一版可以把小文件存在数据库；后续迁移到对象存储或 Git 仓库。

### 8.8 沙箱安全要求

用户代码不能在 FastAPI 主进程内执行。

最低安全要求：

- Docker 隔离。
- CPU、内存、运行时长限制。
- 只读源码目录。
- 临时目录隔离，运行结束清理。
- 网络访问白名单。
- secrets 最小授权注入。
- 日志脱敏。
- 写操作必须确认或审核。
- 运行记录可审计。

## 9. 数据需求

### 9.1 已有数据模型

当前已有：

- `Conversation`
- `Message`
- `Agent`
- `KnowledgeBase`
- `Document`
- `DocumentChunk`
- `ConnectorToken`

### 9.2 新增数据模型

建议新增：

```text
App
AppVersion
AppFile
AppRelease
AppRun
AppPermission
SandboxLog
```

核心字段建议：

`App`

- id
- name
- description
- owner_id
- type：agent / app
- status
- created_at
- updated_at

`AppVersion`

- id
- app_id
- version
- manifest_json
- source_hash
- status
- created_at

`AppFile`

- id
- app_version_id
- path
- content
- size
- checksum

`AppRelease`

- id
- app_id
- app_version_id
- visibility
- released_by
- released_at

`AppRun`

- id
- app_id
- app_version_id
- user_id
- status
- started_at
- ended_at
- error

## 10. API 需求

### 10.1 Agent API

- `GET /api/agents`
- `POST /api/agents`
- `PUT /api/agents/{agent_id}`
- `DELETE /api/agents/{agent_id}`
- `POST /api/agents/{agent_id}/publish`

### 10.2 Knowledge API

- `GET /api/knowledge-bases`
- `POST /api/knowledge-bases`
- `DELETE /api/knowledge-bases/{knowledge_base_id}`
- `POST /api/knowledge-bases/{knowledge_base_id}/documents`
- `DELETE /api/documents/{document_id}`

### 10.3 Web IDE API

- `GET /api/apps`
- `POST /api/apps`
- `GET /api/apps/{app_id}`
- `PUT /api/apps/{app_id}`
- `GET /api/apps/{app_id}/files`
- `PUT /api/apps/{app_id}/files/{path}`
- `POST /api/apps/{app_id}/runs`
- `GET /api/apps/{app_id}/runs/{run_id}`
- `GET /api/apps/{app_id}/runs/{run_id}/logs`
- `POST /api/apps/{app_id}/versions`
- `POST /api/apps/{app_id}/publish`
- `GET /api/marketplace/apps`

### 10.4 Sandbox API 内部接口

- `POST /internal/sandbox/runs`
- `DELETE /internal/sandbox/runs/{run_id}`
- `GET /internal/sandbox/runs/{run_id}/logs`

内部接口不直接暴露给前端。

## 11. 前端需求

### 11.1 导航结构

建议主导航：

- 智能工作台
- 应用市场
- 应用开发
- Web IDE
- 知识库
- 模型
- 连接器与工具
- 权限管理
- 数据报表

### 11.2 知识库页面

页面能力：

- 创建知识库。
- 上传文档。
- 展示文档列表。
- 展示索引状态。
- 删除文档。
- 测试检索。

### 11.3 Web IDE 页面

页面能力：

- 文件树。
- Monaco Editor。
- 保存状态提示。
- 运行按钮。
- 发布按钮。
- 日志面板。
- Agent 测试面板。
- manifest 可视化表单。

### 11.4 应用市场页面

页面能力：

- 展示已发布 Agent/App。
- 搜索和筛选。
- 查看详情。
- 立即使用。
- 对作者展示编辑入口。

## 12. 权限与安全

### 12.1 权限维度

- 谁可以创建 Agent。
- 谁可以发布 Agent。
- 谁可以上传知识库。
- 谁可以绑定连接器。
- 谁可以写代码。
- 谁可以运行代码应用。
- 谁可以访问某个发布应用。

### 12.2 代码安全

代码型应用默认高风险，应有独立审核策略。

高风险行为：

- 访问外部网络。
- 使用 secrets。
- 调用写操作工具。
- 长时间后台运行。
- 访问企业内部系统。

高风险行为必须显式授权。

## 13. 里程碑

### M1：RAG 演示闭环

目标：完成普通 RAG 端到端演示。

范围：

- 知识库创建。
- 文档上传。
- 文档解析和切块。
- embedding 或关键词检索。
- 对话中选择知识库。
- 回答展示引用来源。
- 补充 smoke test 或 demo script。

### M2：Agent 发布闭环

目标：完成配置型 Agent 创建、编辑、发布、使用。

范围：

- Agent 列表。
- Agent 编辑页。
- 发布状态。
- 应用市场展示。
- 已发布 Agent 可被其他用户使用。

### M3：Web IDE MVP

目标：完成代码型 Agent/App 的网页端开发和沙箱运行。

范围：

- Monaco Editor。
- 项目文件树。
- 服务端代码存储。
- Node/TypeScript runtime。
- Docker sandbox。
- 运行日志。
- 发布版本。

### M4：企业治理增强

目标：补齐权限、审核、审计和运维能力。

范围：

- 用户和角色。
- 发布审核。
- 运行审计。
- 用量统计。
- 连接器授权管理。
- secrets 管理。

## 14. 验收标准

P0 总体验收：

- 用户可以在网页创建知识库并上传文档。
- 用户可以选择知识库提问并看到引用来源。
- 用户可以创建、编辑、发布 Agent。
- 其他用户可以看到并使用已发布 Agent。
- 开发者可以在 Web IDE 中编辑代码并运行预览。
- 用户代码不在 FastAPI 主进程运行。
- 平台记录运行日志和错误。
- 未配置模型 key 时，平台仍能进入 demo 模式。

## 15. 风险与对策

### 15.1 RAG 本地环境风险

风险：用户本机由于 IDE 或依赖安装限制，无法完成端到端验证。

对策：

- 提供 `start-dev.ps1` 一键启动。
- 提供后端 smoke test。
- 提供最小 RAG demo script。
- 把 embedding 不可用时的关键词回退作为演示兜底。

### 15.2 用户代码执行风险

风险：用户代码可能读取敏感环境变量、攻击内网、消耗资源或破坏宿主机。

对策：

- 强制沙箱运行。
- 网络白名单。
- secrets 最小授权。
- 运行超时和资源限制。
- 发布审核和审计。

### 15.3 产品范围过大

风险：同时做 RAG、Agent、连接器、Web IDE、沙箱，会导致第一版过重。

对策：

- P0 只做普通 RAG、配置型 Agent 发布、Web IDE MVP。
- 本地 companion、多人协作、Git 化工作流后置。

## 16. 推荐结论

下一阶段应优先把当前能力收敛成三条可演示主线：

1. 知识库问答：上传资料，基于资料回答，展示引用。
2. Agent 发布：配置 Agent，发布后供其他用户使用。
3. Web IDE：网页写代码，沙箱运行，发布为应用。

产品表达上，Atlas 不应只定位为“知识库问答系统”，而应定位为：

> 面向企业的可代码化开发和发布的私有 Agent 应用平台。

