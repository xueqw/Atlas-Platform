# Atlas 智能体平台 — 完整交付版设计文档

**版本**: v0.1.0
**日期**: 2026-06-23
**状态**: 草案
**项目路径**: `/Users/mac/Desktop/浙大网新/3期/-/`

---

## 目录

1. [项目概述](#1-项目概述)
2. [系统架构](#2-系统架构)
3. [模块设计](#3-模块设计)
4. [数据模型](#4-数据模型)
5. [API 设计](#5-api-设计)
6. [前端设计](#6-前端设计)
7. [分期交付计划](#7-分期交付计划)
8. [部署方案](#8-部署方案)
9. [参考项目与设计策略](#9-参考项目与设计策略)
10. [附录](#10-附录)

---

## 1. 项目概述

### 1.1 项目定位

Atlas 是一个**企业级智能体平台**。业务人员通过自然语言对话创建 AI 智能体，平台自动生成系统提示词、配置工具和关联知识库，用户确认后一键发布。

### 1.2 核心价值

| 传统方式 | Atlas |
|---------|-------|
| 手动填写提示词、配置参数 | 对话描述需求，AI 自动生成 |
| 需要理解模型参数 | 用户用大白话说诉求 |
| 创建后单独测试 | 生成配置时实时预览 |
| 技术和非技术人员割裂 | 降低到"会打字就能创建" |

### 1.3 目标用户

企业内部业务人员，不需要编程或 AI 专业知识。

### 1.4 当前项目状态

项目处于**旧版原型 → 新版升级**的迁移阶段：

- **旧版** (`public/` + `server.js`): 纯 HTML/CSS/JS + Node.js，JSON 文件存储。功能界面完整，但无流式、无数据库、无类型安全。
- **新版** (`web/` + `api/`): React + TypeScript + FastAPI + SQLAlchemy，SSE 流式对话 + SQL 持久化。架构先进但仅实现了核心对话链路。
- **本设计文档针对新版**，规划完整交付版的全部模块。

---

## 2. 系统架构

### 2.1 整体架构

```
┌──────────────────────────────────────────────────────┐
│              前端展示层                                │
│  React 18 + TypeScript + Vite · CSS Variables 主题    │
│                                                       │
│  ┌──────────┬──────────┬──────────┬──────────────┐  │
│  │ 智能工作台│ 智能体管理│ 应用开发  │ 知识库 · 工具 │  │
│  │ 模型管理  │ Skills   │ 数据报表  │ 权限管理      │  │
│  └──────────┴──────────┴──────────┴──────────────┘  │
└───────────────────────┬──────────────────────────────┘
                        │ SSE / REST (JSON)
┌───────────────────────┴──────────────────────────────┐
│              后端引擎层                                │
│  FastAPI + Python 3.13 · SQLAlchemy ORM              │
│                                                       │
│  ┌──────────────────────────────────────────────┐   │
│  │               Workflow 层                     │   │
│  │  任务边界定义 · 输入校验 · 输出规范 · 状态追踪   │   │
│  ├──────────────────────────────────────────────┤   │
│  │             Planner Agent (路由+编排)          │   │
│  │  理解意图 → 拆解步骤 → 匹配 Skill → 调度 Worker │   │
│  ├──────────┬──────────┬────────────────────────┤   │
│  │ Worker A │ Worker B │ Worker C ...           │   │
│  │Subagent  │ Subagent │ Subagent               │   │
│  │ +Skill   │ +Skill   │ +Skill                 │   │
│  └──────────┴──────────┴────────────────────────┘   │
│  ┌──────────────────────────────────────────────┐   │
│  │          Skill Registry · MCP Client          │   │
│  │        (技能注册/发现 · 外部工具接入)          │   │
│  └──────────────────────────────────────────────┘   │
└───────────────────────┬──────────────────────────────┘
                        │
┌───────────────────────┴──────────────────────────────┐
│              数据层                                    │
│  SQLite (开发) / PostgreSQL + pgvector (生产)          │
│  Redis (可选缓存) · Docker 部署                        │
└──────────────────────────────────────────────────────┘
```

### 2.2 技术选型

| 层 | 技术 | 理由 |
|---|---|---|
| 前端 | React 18 + TypeScript + Vite | 已落地，SPA 工作台 |
| 后端 | FastAPI + SQLAlchemy + Pydantic | 已落地，异步、类型安全 |
| 数据库 | SQLite → PostgreSQL+pgvector | 开发零配置，生产平滑切换 |
| 流式通信 | SSE (Server-Sent Events) | 已落地，单向推送足够 |
| 样式 | CSS Variables | 零运行时依赖 |
| 模型接入 | OpenAI 兼容接口 | 不绑定供应商 |
| MCP 集成 | Python MCP SDK | 对接社区 MCP Server 生态 |
| 部署 | Docker Compose | 已落地 |

### 2.3 三层引擎架构

```
用户输入 → Workflow (边界) → Planner (路由+编排) → Skill Workers (执行)
```

| 层 | 职责 | Atlas 实现 | 类比 |
|---|---|---|---|
| Workflow | 任务边界、输入校验、输出规范、状态追踪 | FastAPI 路由 + Conversation 模型 | 合同条款 |
| Planner Agent | 理解意图、拆解步骤、匹配 Skill、调度 Worker | 主 Agent (LLM 驱动) | 项目经理 |
| Skill Worker | 独立上下文执行特定 Skill，返回结果 | Subagent + 加载 Skill | 专业工种 |

**为什么必须是三层？**

- **上下文隔离**: Planner 不亲自执行，避免长任务冲掉对话历史
- **失败隔离**: 单个 Worker 失败只重试那一步
- **可扩展**: 新增 Skill 只需注册 Worker，Planner 零改动
- **并行执行**: 无依赖步骤可同时跑多个 Worker

**Skill Worker 的本质**: `Subagent（隔离执行）+ 加载的 Skill（提示词 + 工具 + 知识）`。没有比这更复杂的抽象。

---

## 3. 模块设计

Atlas 平台由 **12 个模块** 组成，分为 4 组：

| 分组 | 模块 | 说明 |
|------|------|------|
| 使用智能体 | 智能工作台、常用应用 | 日常使用入口 |
| 构建智能体 | 应用开发、应用模板 | 创建和定制智能体 |
| 资源中心 | 知识库、模型管理、Skills、连接器与工具、提示词管理 | 可复用资源 |
| 平台管理 | 数据报表、权限管理 | 运营与安全 |

---

### 3.1 智能工作台

**路由**: `/`
**状态**: ✅ 核心对话链路已实现，需补齐步骤可视化和三层引擎

**功能描述**:
用户与智能体对话的核心入口。用户在输入框描述需求，Planner Agent 在三层引擎背后拆解任务，调度 Skill Worker 执行。前端通过 SSE 流式渲染 AI 回复和执行过程。

**页面布局**:

```
┌──────┬─────────────────────────────────────────┐
│      │          Header                          │
│      ├──────────┬──────────────────────────────┤
│      │ 任务列表  │        对话区                  │
│ Nav  │ ·搜索     │                               │
│ 240px│ ·新建任务 │  ┌─────────────────────────┐ │
│      │ ·最近任务 │  │  欢迎页 / 对话消息         │ │
│      │           │  │  (SSE 流式渲染)           │ │
│      │           │  └─────────────────────────┘ │
│      │           │                               │
│      │           │  输入框 + 快捷操作 + 工具栏     │
└──────┴──────────┴──────────────────────────────┘
```

**交互说明**:

1. 左侧任务列表：搜索、新建任务、最近任务。点击任务切换对话。
2. 对话区：欢迎页（首次进入）→ 消息流（用户蓝色气泡 / AI 灰色气泡）。AI 回复 SSE 流式逐字渲染。
3. 底部输入区：文本输入 + @mention 唤起 Skills/工具/知识库 + 模型选择 + 发送按钮。
4. 快捷入口：文档处理、数据分析、深度研究、创建应用（点击填入预设 prompt）。
5. P1 新增：对话中显示步骤卡片（Planner 拆解的步骤 + Worker 执行状态）。

---

### 3.2 我的智能体

**路由**: `/agents`
**状态**: 🔄 待开发（P1）

**功能描述**:
管理所有已创建智能体的列表页。用户在此查看、搜索、进入配置智能体。

**页面布局**: 头部（标题 + 创建按钮）+ 3 列卡片网格。

**卡片内容**: 首字母图标（彩色背景）、名称、描述摘要、模型标签、发布状态（已发布/草稿）。

**交互说明**:
- 点击卡片 → 进入智能体配置页 (`/agents/:id`)
- 点击「创建智能体」→ 弹出 modal 输入名称+描述 → 创建后跳转配置页。或者跳转到「应用开发」对话式创建。
- 虚线占位卡片供"新建"入口

---

### 3.3 智能体配置

**路由**: `/agents/:id`
**状态**: 🔄 待开发（P1）

**功能描述**:
单个智能体的详细配置页面。左侧编辑配置，右侧实时测试对话。

**页面布局**: 左右分栏。

**左侧配置面板**:
- 智能体名称（文本输入）
- 功能描述（文本输入）
- 模型选择（下拉：gpt-4.1-mini / qwen-plus / deepseek-chat / ...）
- 系统提示词（多行文本编辑，min-height: 110px）
- 已连接资源信息（知识文档数、工具数）
- 保存配置按钮

**右侧测试面板**:
- 对话预览区（用户消息 + AI 回复 + 引用来源）
- 消息输入框 + 发送按钮
- 顶部标签显示当前模式（演示/模型）

**交互说明**:
- 修改左侧配置后点「保存」→ PUT 接口更新
- 右侧可即时发送测试消息验证配置效果
- 测试使用该智能体的 system prompt + 模型参数

---

### 3.4 应用开发

**路由**: `/builder`
**状态**: 🔄 待开发（P1）

**功能描述**:
对话式创建智能体。用户用自然语言描述需求，AI 自动生成智能体配置。这是平台的核心差异化功能。

**页面布局**: 左侧对话区 + 右侧实时配置预览。

**交互流程**:

1. AI 开场询问用户想创建什么类型的智能体
2. 用户用大白话描述（如"帮我做一个菜谱助手，输入食材推荐能做的菜"）
3. AI 追问澄清（菜系、营养分析、步骤详细度等）
4. AI 自动生成：智能体名称、系统提示词、推荐工具、关联知识库
5. 右侧面板实时刷新配置，用户可手动微调或继续对话调整
6. 用户确认后点击「发布智能体」

**右侧预览面板**:
- 生成的智能体名称
- 系统提示词（可编辑）
- 模型选择
- 关联知识库（可添加）
- 关联工具（可添加）
- 内嵌测试对话预览

**后端实现**: `agent_builder.py` — 多轮对话理解 + 调用 LLM 生成配置 JSON。

---

### 3.5 知识库

**路由**: `/knowledge`
**状态**: 🔄 待开发（P2）

**功能描述**:
上传和管理文本知识文档，为智能体提供可追溯的企业知识来源。

**页面布局**: 头部（标题 + 上传按钮）+ 数据表格。

**表格列**:

| 文件名称 | 状态 | 文本分块数 | 文件大小 | 上传时间 |
|---------|------|----------|---------|---------|

**上传流程**: 点击「上传文档」→ modal 弹出 → 选择文件 → 自动解析 → 分块入库。

**支持格式**: TXT, MD, CSV, JSON（P2 基础版）；PDF, DOCX（P3 增强版）。

**状态标签**: 处理中（橙色）/ 已就绪（绿色）/ 解析失败（红色）。

**分块策略**: 初始版按固定字符数（500字/块）切分；P3 增强版接入 Embedding 向量化。

---

### 3.6 模型管理

**路由**: `/models`
**状态**: 🔄 待开发（P2）

**功能描述**:
配置 OpenAI 兼容的模型供应商。支持多供应商，默认从环境变量读取。

**页面布局**: 头部标题 + 配置面板（表单）。

**配置字段**:
- 配置名称（如"默认 OpenAI"）
- Base URL (https://api.openai.com/v1)
- API Key（密码输入框）
- 默认模型（下拉选择：gpt-4.1-mini / gpt-4o / gpt-4.1 / ...）
- 连接测试按钮 + 连接状态指示
- 保存配置按钮

**多供应商支持**: 可添加多个配置项，智能体创建时选择使用哪个。

---

### 3.7 连接器与工具

**路由**: `/tools`
**状态**: 🔄 待开发（P2）

**功能描述**:
管理智能体可调用的外部工具和 API 连接器。支持 MCP Server 作为连接器类型接入。

**页面布局**: 头部（标题 + 新建按钮）+ 数据表格。

**表格列**:

| 名称 | 请求方法 | 地址 | 连接状态 | 操作 |
|------|---------|------|---------|------|
| 订单查询 API | GET | https://api.example.com/orders/{id} | 已连接 | 测试 删除 |

**新建工具 modal**:
- 工具名称
- 功能描述
- 请求方法（GET/POST/PUT/DELETE）
- 请求地址（支持 `{参数}` 占位符）
- 请求头（JSON）
- 连接类型（REST API / MCP Server / Webhook）

**MCP 集成**: 工具类型选择"MCP Server"时，填写 MCP Server 配置（command/args/env），后端通过 Python MCP SDK 连接和发现工具。

---

### 3.8 Skills 管理

**路由**: `/skills`
**状态**: 🔄 待开发（P3）

**功能描述**:
可复用的提示词 + 工具组合模板。Skill 是给智能体注入专项能力的"能力包"。

**页面布局**: 头部（标题 + 新建按钮）+ 3 列卡片网格。

**Skill 卡片**:
- 图标
- 名称
- 功能描述
- 标签（提示词模板 / 工具包 / 草稿 / 已启用）

**Skill 组成**:
```json
{
  "name": "文档摘要",
  "description": "上传文档，自动提取关键信息",
  "prompt_template": "你是一名文档分析专家...",
  "tools": ["file_reader", "text_parser"],
  "knowledge_refs": [],
  "status": "enabled"
}
```

**Skill 注册与调度**（三层引擎核心机制）:

1. **注册**: Skill 以 JSON 定义存储在数据库 `skills` 表，系统启动时加载到 Skill Registry
2. **发现**: Planner Agent 收到任务 → 查 Skill Registry 获取所有 Skill 元数据 → LLM 语义匹配
3. **执行**: 选定的 Skill 注入到 Subagent 的 system prompt → Subagent 独立上下文执行 → 结果返回 Planner

---

### 3.9 数据报表

**路由**: `/analytics`
**状态**: 🔄 待开发（P3）

**功能描述**:
查看平台的调用统计、Token 消耗和运行质量。

**页面布局**: 头部标题 + 4 个统计卡片 + 最近调用时间线。

**统计卡片**:
- 累计调用次数（含变化趋势）
- Token 用量（含变化趋势）
- 成功率（百分比 + 趋势）
- 平均延迟（秒 + 趋势）

**最近调用列表**: 时间线格式，每条显示智能体名称、问题摘要、状态、耗时、Token 数。

---

### 3.10 权限管理

**路由**: `/settings`
**状态**: 🔄 待开发（P3）

**功能描述**:
用户、角色、权限管理（RBAC）。多工作空间支持。

**P3 实现范围**:
- 用户注册/登录（JWT 认证）
- 角色定义（管理员/开发者/使用者）
- 基础权限控制（读/写/管理）
- 单工作空间模式（多工作空间为长期规划）

---

### 3.11 应用模板

**路由**: `/templates`
**状态**: 占位（P3 之后）

预置的智能体模板（客服、知识问答、数据分析等）。用户选择模板 + 简单定制即可创建。

---

### 3.12 提示词管理

**路由**: `/prompts`
**状态**: 占位（P3 之后）

独立的提示词片段库，可在创建智能体时引用和组装。

---

## 4. 数据模型

### 4.1 实体关系

```
┌─────────────┐     ┌──────────────────┐
│  agents     │────<│   conversations  │
│  (P1)       │     │   (P0·已有)       │
└─────────────┘     └────────┬─────────┘
                             │
                             ├────< messages (P0·已有)
                             │
┌─────────────┐              │
│  documents  │──────────────┤
│  (P2)       │   关联到智能体  │
└─────────────┘              │
                             │
┌─────────────┐              │
│   tools     │──────────────┤
│  (P2)       │              │
└─────────────┘              │
                             │
┌─────────────┐              │
│model_configs│──────────────┤
│  (P2)       │              │
└─────────────┘              │
                             │
┌─────────────┐              │
│   skills    │──────────────┤
│  (P3)       │              │
└─────────────┘              │
                             │
┌─────────────┐              │
│    users    │──────────────┤
│  (P3)       │              │
└─────────────┘              │
```

### 4.2 表结构

#### P0 已有

**conversations** — 任务会话
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| title | VARCHAR(160) | 会话标题（默认"新任务"） |
| created_at | DATETIME | 创建时间 |
| updated_at | DATETIME | 最后更新时间 |

**messages** — 对话消息
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| conversation_id | VARCHAR(36) FK | 所属会话 |
| role | VARCHAR(20) | user / assistant |
| content | TEXT | 消息内容 |
| sources | TEXT | 引用来源（JSON数组） |
| created_at | DATETIME | 消息时间 |

#### P1 新增

**agents** — 智能体定义
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| name | VARCHAR(100) | 名称 |
| description | TEXT | 功能描述 |
| model | VARCHAR(50) | 使用模型 |
| system_prompt | TEXT | 系统提示词 |
| status | VARCHAR(20) | draft / published / archived |
| created_at | DATETIME | 创建时间 |
| updated_at | DATETIME | 更新时间 |

#### P2 新增

**documents** — 知识文档
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| name | VARCHAR(200) | 文件名 |
| type | VARCHAR(20) | txt / md / csv / json / pdf |
| size | INTEGER | 字节数 |
| chunks | INTEGER | 分块数 |
| content | TEXT | 文本内容 |
| status | VARCHAR(20) | processing / ready / failed |
| created_at | DATETIME | 上传时间 |

**tools** — 工具/连接器
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| name | VARCHAR(100) | 工具名称 |
| description | TEXT | 功能描述 |
| method | VARCHAR(10) | HTTP 方法 |
| url | VARCHAR(500) | 请求地址 |
| headers | TEXT | 请求头 (JSON) |
| type | VARCHAR(20) | rest / mcp / webhook |
| mcp_config | TEXT | MCP 配置 (JSON) |
| status | VARCHAR(20) | connected / error / disabled |
| created_at | DATETIME | 创建时间 |

**model_configs** — 模型供应商
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| name | VARCHAR(100) | 配置名称 |
| provider | VARCHAR(50) | openai / qwen / deepseek / ... |
| base_url | VARCHAR(300) | API 地址 |
| api_key_encrypted | VARCHAR(500) | 加密的 API Key |
| default_model | VARCHAR(50) | 默认模型 |
| is_default | BOOLEAN | 是否默认配置 |
| created_at | DATETIME | 创建时间 |

#### P3 新增

**skills** — Skills 模板
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| name | VARCHAR(100) | Skill 名称 |
| description | TEXT | 功能描述 |
| prompt_template | TEXT | 提示词模板 |
| tools_json | TEXT | 关联工具 (JSON) |
| status | VARCHAR(20) | draft / enabled / disabled |
| created_at | DATETIME | 创建时间 |

**users** (RBAC 基础)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(36) PK | UUID |
| username | VARCHAR(50) | 用户名 |
| password_hash | VARCHAR(200) | 密码哈希 |
| role | VARCHAR(20) | admin / developer / user |
| created_at | DATETIME | 创建时间 |

---

## 5. API 设计

### 5.1 端点清单

#### P0 已有

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/conversations` | 任务列表 |
| POST | `/api/conversations` | 创建任务 |
| GET | `/api/conversations/{id}` | 任务详情（含消息） |
| DELETE | `/api/conversations/{id}` | 删除任务 |
| POST | `/api/conversations/{id}/messages/stream` | SSE 流式消息 |

#### P1 新增 (智能体)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agents` | 智能体列表 |
| POST | `/api/agents` | 创建智能体 |
| GET | `/api/agents/{id}` | 智能体详情 |
| PUT | `/api/agents/{id}` | 更新智能体配置 |
| DELETE | `/api/agents/{id}` | 删除智能体 |
| POST | `/api/builder/chat` | AgentBuilder 多轮对话（SSE） |
| POST | `/api/chat` | 智能工作台对话（接三层引擎） |

#### P2 新增 (资源)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/documents` | 文档列表 |
| POST | `/api/documents` | 上传文档 |
| DELETE | `/api/documents/{id}` | 删除文档 |
| GET | `/api/tools` | 工具列表 |
| POST | `/api/tools` | 新建工具 |
| PUT | `/api/tools/{id}` | 修改工具 |
| DELETE | `/api/tools/{id}` | 删除工具 |
| POST | `/api/tools/{id}/test` | 测试连接 |
| GET | `/api/models` | 模型配置列表 |
| PUT | `/api/models` | 更新模型配置 |

#### P3 新增 (平台)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/skills` | Skills 列表 |
| POST | `/api/skills` | 创建 Skill |
| PUT | `/api/skills/{id}` | 修改 Skill |
| DELETE | `/api/skills/{id}` | 删除 Skill |
| GET | `/api/analytics` | 统计报表 |
| POST | `/api/auth/login` | 登录 |
| GET | `/api/users` | 用户列表 |

### 5.2 后端文件架构

```
api/
  requirements.txt      — Python 依赖
  .env.example          — 环境变量模板
  app/
    __init__.py
    main.py             — FastAPI 入口 + CORS + 路由注册
    config.py           — Pydantic Settings (环境变量管理)
    database.py         — SQLAlchemy Engine + Session + Base
    models.py           — ORM 模型 (全部表)
    schemas.py          — Pydantic 请求/响应 Schema
    model_gateway.py    — OpenAI 兼容模型接入 + SSE 流式
    agent_service.py    — Planner Agent 主逻辑 (P1)
    agent_builder.py    — AI 生成智能体配置 (P1)
    skill_registry.py   — Skill 注册表 (P1)
    skill_worker.py     — Subagent + Skill 执行器 (P1)
  scripts/
    smoke_test.py       — API 冒烟测试
```

---

## 6. 前端设计

### 6.1 Shell 布局

```
┌──────┬──────────────────────────────────────┐
│      │          Header (h: 72px)             │
│      │  面包屑 · 搜索 ⌘K · 通知 · 头像       │
│ Nav  ├──────────────────────────────────────┤
│ 240px│                                       │
│      │       <RouterOutlet />                │
│      │       (min-height: calc(100vh-72px))  │
│      │                                       │
└──────┴──────────────────────────────────────┘
```

**导航分组** (复用现有):
1. 使用智能体: 智能工作台 · 常用应用
2. 构建智能体: 应用开发 · 应用模板
3. 资源中心: 知识库 · 模型 · Skills · 连接器与工具 · 提示词
4. 平台管理: 数据报表 · 权限管理

**底部状态栏**: 服务运行状态指示灯 + 版本标签 (React + FastAPI)

**响应式** (复用现有 CSS):
- ≥ 951px: 完整布局（240px 导航 + 内容区）
- 720px–950px: 窄导航（82px，只显示图标）
- < 720px: 全屏，导航隐藏

### 6.2 路由表

| 路由 | 组件 | 导航项 | 优先级 |
|------|------|--------|--------|
| `/` | WorkbenchPage | 智能工作台 | P0 ✅ |
| `/agents` | AgentListPage | — | P1 |
| `/agents/:id` | AgentConfigPage | 我的智能体 | P1 |
| `/builder` | BuilderPage | 应用开发 | P1 |
| `/templates` | TemplatesPage | 应用模板 | 占位 |
| `/knowledge` | KnowledgePage | 知识库 | P2 |
| `/models` | ModelsPage | 模型 | P2 |
| `/tools` | ToolsPage | 连接器与工具 | P2 |
| `/skills` | SkillsPage | Skills | P3 |
| `/prompts` | PromptsPage | 提示词 | 占位 |
| `/analytics` | AnalyticsPage | 数据报表 | P3 |
| `/settings` | SettingsPage | 权限管理 | P3 |

### 6.3 前端文件架构

```
web/
  index.html            — HTML 入口
  package.json          — 依赖定义
  vite.config.ts        — Vite 配置 (代理 /api → :8000)
  tsconfig.json         — TypeScript 配置
  src/
    main.tsx            — React 入口
    App.tsx             — Shell 布局 + 路由
    types.ts            — TypeScript 类型定义
    api.ts              — API 调用封装 (fetch + SSE)
    styles.css          — 全局样式 (CSS Variables 主题)
    pages/
      WorkbenchPage.tsx   — 智能工作台 (P0)
      AgentListPage.tsx   — 智能体列表 (P1)
      AgentConfigPage.tsx — 智能体配置 (P1)
      BuilderPage.tsx     — 应用开发 (P1)
      KnowledgePage.tsx   — 知识库 (P2)
      ModelsPage.tsx      — 模型管理 (P2)
      ToolsPage.tsx       — 工具管理 (P2)
      SkillsPage.tsx      — Skills 管理 (P3)
      AnalyticsPage.tsx   — 数据报表 (P3)
      SettingsPage.tsx    — 权限设置 (P3)
    components/
      Shell.tsx         — 全局布局 (复用)
      NavGroup.tsx      — 导航组 (复用)
      Conversation.tsx  — 对话组件
      Message.tsx       — 消息气泡
      Composer.tsx      — 输入框+工具栏
      AgentCard.tsx     — 智能体卡片
      Modal.tsx         — 通用弹窗
      StatsCard.tsx     — 统计卡片
      DataTable.tsx     — 通用表格
```

---

## 7. 分期交付计划

### P1 · 智能体闭环 (5-7 天)

**目标**: 用户能对话式创建智能体 → 配置 → 测试 → 发布

| 模块 | 后端 | 前端 | 新增文件 |
|------|------|------|---------|
| 智能体 CRUD | Agent 模型 + 5 端点 | AgentListPage + AgentConfigPage | models.py(+Agent), 2 page tsx |
| 应用开发 | AgentBuilder 服务 | BuilderPage | agent_builder.py, 1 page tsx |
| Skill 引擎 | SkillRegistry + SkillWorker | — | skill_registry.py, skill_worker.py |
| 工作台增强 | 步骤可视化 API | 对话步骤卡片组件 | agent_service.py |

**代码量**: 后端 ~600 行 · 前端 ~1000 行 · 1 张新表

### P2 · 资源中心 (3-4 天)

**目标**: 智能体可连接知识和外部工具

| 模块 | 后端 | 前端 | 新增文件 |
|------|------|------|---------|
| 知识库 | Document 模型 + 3 端点 | KnowledgePage + 上传 modal | 1 model + 1 page |
| 工具 | Tool 模型 + 6 端点 + MCP | ToolsPage + 配置 modal | 1 model + 1 page |
| 模型管理 | ModelConfig 模型 + 2 端点 | ModelsPage | 1 model + 1 page |

**代码量**: 后端 ~500 行 · 前端 ~800 行 · 3 张新表

### P3 · 平台增强 (4-5 天)

**目标**: 运营级产品 — 权限、报表、Skills 管理

| 模块 | 后端 | 前端 | 新增文件 |
|------|------|------|---------|
| Skills 管理 | Skill 模型 + 4 端点 | SkillsPage 卡片网格 | 1 model + 1 page |
| 数据报表 | 聚合查询 API | AnalyticsPage 统计面板 | 1 route + 1 page |
| 权限管理 | User 模型 + JWT 认证 | SettingsPage | auth 模块 + 1 page |

**代码量**: 后端 ~800 行 · 前端 ~700 行 · ~4 张新表

### 总计

| | P1 | P2 | P3 | 合计 |
|---|---|---|---|---|
| 后端文件 | 6 | 3 | 5 | 14 |
| 前端页面 | 4 | 3 | 3 | 10 |
| 数据表 | 1 | 3 | 4 | 8 |
| 工期 | 5-7天 | 3-4天 | 4-5天 | **12-16天** |

---

## 8. 部署方案

### 8.1 开发环境

```bash
# 后端
cd api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000

# 前端
cd web
npm install && npm run dev
```

| 服务 | 地址 |
|------|------|
| React 工作台 | http://localhost:5173 |
| FastAPI 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/api/health |

### 8.2 Docker 部署

```bash
# 启动 PostgreSQL + Redis
docker compose up -d postgres redis

# 修改 api/.env
DATABASE_URL=postgresql+psycopg://atlas:atlas_dev@localhost:5432/atlas

# 启动全栈
docker compose up -d
```

### 8.3 环境变量

```bash
DATABASE_URL=sqlite:///./data/atlas.db
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4.1-mini
APP_PORT=8000
```

---

## 9. 参考项目与设计策略

### 9.1 核心参考

| 项目 | 技术栈 | 参考模块 | 策略 |
|------|--------|---------|------|
| [Yszen AI](https://gitee.com/xcodinglifex/yszen-ai) | FastAPI + React + LangGraph + SQLite | Skills 系统 · Agent 引擎 · Harness 架构 | 架构最接近，代码可阅读理解 |
| [Conllect-LLM](https://gitee.com/one_love_op/conllect-llm) | FastAPI + React + PostgreSQL | 工具注册中心 · 模型网关 · Trace 追踪 | API 设计参考 |
| [Yuxi 语析](https://github.com/zhangziliang04/Yuxi) | FastAPI + Vue + Milvus | MCP 集成 · 知识图谱 · 沙盒 | MCP Client 实现参考 |

### 9.2 不采纳的理由

| 来源 | 原因 |
|------|------|
| Codex CLI | Rust 终端程序，与 Web 平台完全不兼容。借鉴其 Agent Loop 设计模式 |
| Dify | Flask + Celery 架构，200+ 依赖，框架冲突 |
| OpenHands | 通用编码 Agent，粒度太大。与 Atlas"对话创建智能体"定位不匹配 |

### 9.3 设计原则

- **依赖最小化**: 优先标准库。一个 PostgreSQL 覆盖业务 + 向量
- **渐进增强**: P1 闭环 → P2 扩展 → P3 平台化
- **模型无关**: 全部通过 OpenAI 兼容接口，不绑定供应商
- **参考不照抄**: 开源项目当设计文档读，用 FastAPI+React 原生实现

---

## 10. 附录

### 10.1 当前项目文件清单

```
├── api/
│   ├── app/
│   │   ├── __init__.py        — 空
│   │   ├── config.py          — Pydantic Settings
│   │   ├── database.py        — SQLAlchemy 引擎
│   │   ├── main.py            — FastAPI 应用 (7 端点)
│   │   ├── model_gateway.py   — LLM 网关 (OpenAI + Demo)
│   │   ├── models.py          — Conversation + Message
│   │   └── schemas.py         — Pydantic Schema
│   ├── scripts/smoke_test.py  — 冒烟测试
│   ├── data/atlas.db          — SQLite 数据库
│   └── requirements.txt       — Python 依赖
├── web/
│   ├── src/
│   │   ├── App.tsx            — Shell + 工作台 (单文件)
│   │   ├── api.ts             — fetch + SSE 封装
│   │   ├── main.tsx           — React 入口
│   │   ├── styles.css         — 全局样式
│   │   └── types.ts           — TypeScript 类型
│   ├── vite.config.ts         — Vite 配置
│   └── package.json           — 前端依赖
├── public/                    — 旧版原型 (保留参考)
│   ├── app.js                 — SPA 逻辑 (45KB)
│   ├── index.html             — 入口
│   ├── styles.css             — 主样式
│   └── workbench.css          — 工作台样式
├── server.js                  — 旧版 Node.js 服务 (保留参考)
├── docker-compose.yml         — PostgreSQL + Redis
├── start-dev.ps1              — Windows 启动脚本
└── README.md                  — 项目说明
```

### 10.2 旧版原型与新版的模块对照

| 模块 | 旧版 (public/) | 新版 (web/ + api/) |
|------|---------------|-------------------|
| 智能工作台 | ✅ HTML | ✅ React (仅SSE对话) |
| 我的智能体 | ✅ HTML + API | ❌ 待开发 |
| 智能体开发 | ✅ HTML + API | ❌ 待开发 |
| 知识库 | ✅ HTML + API | ❌ 待开发 |
| 模型管理 | ✅ 显示页 | ❌ 待开发 |
| 连接器与工具 | ✅ HTML + API | ❌ 待开发 |
| 数据报表 | ✅ 显示页 | ❌ 待开发 |
| 权限设置 | ❌ 占位 | ❌ 待开发 |
| 应用模板 | ❌ 占位 | ❌ 占位 |
| Skills | ❌ 无 | ❌ 待开发 |
| 应用开发 | ❌ 无 | ❌ 待开发（新增） |

### 10.3 未纳入本次交付的长期规划

- RAG 向量检索（需 pgvector + Embedding 模型）
- 工作流可视化编辑器（拖拽式编排）
- 多租户/组织管理
- 审计日志
- 计费系统
- 移动端适配
