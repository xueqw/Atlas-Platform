## Context

Atlas 是 FastAPI + React 应用。当前生产实例由 systemd 和 Nginx 运行，数据默认落在 SQLite，知识原文件不保留，代码 Agent 通过主进程 `subprocess` 执行。目标服务器已安装 Docker，但应用主服务继续由 systemd 管理，以降低本次迁移风险。

## Goals / Non-Goals

**Goals:**
- 建立 PostgreSQL、Redis、MinIO 三类明确的数据边界，并保持开发环境可回退。
- 让知识原文件、Agent 代码、短期记忆和长期记忆都有可追踪生命周期。
- 让不可信代码离开 Atlas 主进程，并以无网络、只读根文件系统和资源上限运行。
- 提供每日自动备份、恢复脚本和可观察的依赖健康状态。

**Non-Goals:**
- 本次不引入 Kubernetes、多机调度和 GPU 沙箱。
- 本次不自动把历史 SQLite 向量转换为数据库原生 vector 列；先保留兼容字段，提供迁移入口。
- 本次不实现完整企业 KMS，生产密钥通过 systemd EnvironmentFile 或 Docker secret 文件注入。

## Decisions

1. 业务表使用 PostgreSQL，创建 `vector` 扩展；SQLAlchemy 模型继续兼容 SQLite，保证本地测试不依赖 Docker。
2. 对象存储使用 S3 兼容接口。知识原文件和 Agent 项目归档使用 `workspace_id` 前缀；MinIO 不可用时生产环境失败关闭，开发环境可回退本地目录。
3. Redis 状态统一使用带命名空间和 TTL 的 JSON 接口。短期记忆存 Redis，长期记忆存 PostgreSQL；长期记忆只在显式调用接口时写入，避免模型自行保存敏感信息。
4. Python 代码由 Docker CLI 启动一次性容器。容器禁网、丢弃 capabilities、只读根文件系统、限制 CPU/内存/PID，并挂载一次性只读项目目录。
5. 预览 iframe 不允许 `allow-same-origin`，并在 `srcDoc` 注入 CSP，阻断网络、顶层导航和外部资源。
6. 密钥支持 `*_FILE` 方式读取挂载文件；数据库中不新增密钥明文字段。

## Risks / Trade-offs

- [Docker 冷启动增加代码 Agent 延迟] → 先使用轻量 Python 镜像并设置明确超时，后续再引入常驻 Runner 池。
- [MinIO 或 Redis 故障影响上传和临时状态] → 健康接口暴露依赖状态，生产模式失败关闭，避免静默丢数据。
- [SQLite 到 PostgreSQL 迁移存在停机窗口] → 备份旧库，先启动依赖，再执行迁移和回归，失败则回滚 systemd 目录与环境文件。
- [对象存储与数据库写入不是同一事务] → 上传失败不落数据库；数据库失败时尽力删除已上传对象。

## Migration Plan

1. 备份当前代码、`.env`、SQLite 数据和 Nginx/systemd 配置。
2. 启动 PostgreSQL、Redis、MinIO，创建 bucket 和 pgvector 扩展。
3. 导入 SQLite 业务数据或在无法自动转换时保留只读备份并记录迁移结果。
4. 部署新代码、安装依赖、构建前端，更新环境文件后重启 Atlas。
5. 执行健康检查、登录、知识上传检索、记忆隔离、代码沙箱与预览检查。
6. 若阻断性检查失败，恢复备份目录和原环境文件并重启旧服务。

## Open Questions

- 企业正式上线前需确定外部 KMS 产品、备份异地位置、RPO/RTO 和日志保留周期。
