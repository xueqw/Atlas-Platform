# Atlas 智能体平台

## React + FastAPI 迁移版

当前主版本已经迁移到 React、TypeScript、FastAPI 和 SQLAlchemy，并打通任务会话、SSE 流式回答与数据库持久化。

一键启动：

```powershell
cd E:\codex\ai-agent-platform-demo
.\start-dev.ps1
```

- React 工作台：<http://localhost:5173>
- FastAPI 文档：<http://localhost:8000/docs>

默认使用 `api/data/atlas.db`，无需安装数据库。切换 PostgreSQL：

```powershell
docker compose up -d postgres redis
Copy-Item api\.env.example api\.env
```

然后把 `api/.env` 中的连接修改为：

```text
DATABASE_URL=postgresql+psycopg://atlas:atlas_dev@localhost:5432/atlas
```

接入真实模型时，在同一文件设置 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。

验证命令：

```powershell
cd api
.\.venv\Scripts\python.exe scripts\smoke_test.py
cd ..\web
npm run build
```

## 旧版零依赖原型

一个零第三方依赖的本地智能体平台原型，包含智能体管理、知识文档、工具列表、测试对话和运行概览。

## 启动

```powershell
cd E:\codex\ai-agent-platform-demo
node server.js
```

浏览器访问 <http://localhost:3000>。

## 连接真实模型（可选）

支持任何 OpenAI Chat Completions 兼容接口：

```powershell
$env:OPENAI_API_KEY="your-key"
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:OPENAI_MODEL="gpt-4.1-mini"
node server.js
```

不设置密钥时会自动使用本地演示回复。数据保存在 `data/db.json`。

## 当前范围

- 智能体创建、编辑和本地持久化
- TXT/Markdown/CSV/JSON 文本知识上传
- 带引用来源的测试对话
- OpenAI 兼容模型接入
- 响应式管理控制台

生产化还需要补充数据库、身份认证、文档解析、向量检索、工具沙箱、流式响应和审计系统。
