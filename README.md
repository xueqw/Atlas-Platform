# Atlas 智能体平台

基于 React + TypeScript + FastAPI + SQLAlchemy 的企业智能体平台。
打通任务会话、SSE 流式回答、知识库语义检索、多模型网关，以及可扩展的连接器 / 工具体系。

## 功能

### 智能工作台
- 多轮任务会话，自动保存与恢复
- SSE 流式回答，带知识库引用来源
- 可选择智能体、知识库、模型、连接器

### 知识库（RAG 语义检索）
- 上传 PDF / DOCX / TXT / Markdown / CSV / JSON，自动解析与切块
- bge-m3 向量化（硅基流动）+ 余弦相似度语义检索，关键词兜底
- 跨语言检索（中文提问也能命中英文文献）
- 带文档名 + 页码 + 原文的引用卡片

### 模型网关
- 多供应商按模型名自动路由：智谱 GLM、阿里通义千问
- 连通性测试，密钥配置在 `api/.env`

### 连接器与工具
- 工具调用循环：智能体自主决定调用工具 -> 执行 -> 回填 -> 续答
- 写操作确认闸门：发消息 / 建 issue 等执行前需用户回「确认」
- **飞书**（OAuth 2.0）：代发飞书消息（发给自己 / 邮箱 / 手机号）
- **GitHub**（官方远程 MCP Server）：动态接入 40+ 工具，平台即 MCP 客户端
- 对话框底部可勾选启用哪些连接器

### Agent 应用开发
- 统一新建入口：会话式开发、空白代码项目、业务模板和 Prompt Agent
- 持续 Coding Agent 会话：后续消息修改同一个项目并生成新版本
- 开发、测试、评测、落地配置、发布五阶段流程，状态服务端持久化
- Skills、知识库、连接器和模型绑定，运行时返回来源、步骤和工具调用
- HTML 页面自动预览，代码、日志和运行对话在同一开发工作区
- 发布自动保存当前工作区，线上固定运行不可变发布版本
- API Key、额度、来源限制、调用日志、版本对比与回滚
- 高风险写连接器需要工作空间所有者批准

## 一键启动

```powershell
cd E:/codex/ai-agent-platform-demo
.\start-dev.cmd
```

代码或后端配置更新后强制重启：

```powershell
.\start-dev.cmd -Restart
```

- React 工作台：<http://localhost:5173>
- FastAPI 文档：<http://localhost:8000/docs>

默认使用 `api/data/atlas.db`，无需安装数据库。

## 技术栈

React - TypeScript - FastAPI - SQLAlchemy - MCP - bge-m3
