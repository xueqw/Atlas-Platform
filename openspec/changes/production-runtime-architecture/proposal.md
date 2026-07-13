## Why

Atlas 当前已具备 Demo 级 Agent、知识库和应用开发闭环，但核心数据仍依赖单机 SQLite、进程内临时状态和主服务直接执行代码，无法满足企业与学校场景的隔离、持久化、恢复和安全要求。

## What Changes

- 将生产业务数据切换到 PostgreSQL，并启用 pgvector 承载知识和长期记忆向量。
- 使用 MinIO 保存知识库原文件和 Agent 项目快照，保留本地存储作为开发回退。
- 使用 Redis 保存 OAuth state、待确认操作、短期记忆和其他有过期时间的运行状态。
- 为 Agent 增加可按用户、工作区和 Agent 隔离的短期与长期记忆。
- 代码型 Agent 改为 Docker 隔离执行，限制网络、CPU、内存、进程数和执行时间。
- HTML 预览使用严格 sandbox iframe 与 CSP，禁止读取 Atlas 页面和登录态。
- 密钥仅从环境变量或挂载文件读取，并提供每日备份与恢复脚本。

## Capabilities

### New Capabilities
- `production-data-plane`: PostgreSQL、MinIO、Redis、密钥读取、健康检查和租户存储边界。
- `agent-memory`: Agent 短期记忆、长期记忆、过期、删除和权限隔离。
- `isolated-code-runtime`: 代码型 Agent 的 Docker 隔离执行和 HTML 安全预览。
- `backup-recovery`: PostgreSQL、MinIO、Agent 代码和配置的备份、保留与恢复验证。

### Modified Capabilities

无。

## Impact

影响 `api/app` 的配置、模型、知识上传、运行状态和代码执行，影响 Web 端预览 iframe；新增 PostgreSQL/pgvector、Redis、MinIO 和 Docker 运行依赖，并调整服务器部署与备份方式。
