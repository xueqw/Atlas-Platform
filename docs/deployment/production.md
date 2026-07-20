# Atlas 生产部署手册

## 1. 落地边界

| 数据 | 生产位置 | 说明 |
|---|---|---|
| 用户、工作区、Agent、运行记录 | PostgreSQL | 不再使用 SQLite 作为生产主库 |
| 知识分块、长期记忆向量 | PostgreSQL + pgvector | 创建数据库时自动启用 vector 扩展 |
| 知识原文件、Agent 版本归档 | MinIO | key 带 workspace 前缀 |
| OAuth state、待确认操作、短期记忆 | Redis | 所有记录必须带 TTL |
| Agent 可编辑代码 | `/var/lib/atlas/agent-code` | 独立持久化并进入每日备份 |
| 平台密钥 | `/etc/atlas/secrets` | 文件权限 640，数据库动态凭据加密保存 |
| 代码 Agent | Docker Runner | 默认禁网、只读、无 capabilities、资源受限 |
| HTML 预览 | sandbox iframe | 不允许 same-origin，CSP 禁止联网和父页导航 |

## 2. 上线前准备

服务器需要 Python 3.10+、Node.js 20+、Docker、Compose v1 或 v2、Nginx。创建密钥时不要把值写入命令历史或日志：

```bash
sudo install -d -m 750 -o root -g atlas /etc/atlas/secrets
sudo openssl rand -hex 24 > /etc/atlas/secrets/postgres_password
sudo openssl rand -hex 24 > /etc/atlas/secrets/redis_password
sudo openssl rand -hex 24 > /etc/atlas/secrets/minio_root_password
sudo python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())' > /etc/atlas/secrets/secret_encryption_key
sudo chmod 640 /etc/atlas/secrets/*
```

从 `.env.production.example` 生成 `api/.env`，从 `compose.env.example` 生成根目录 `.env`。URL 中的 `{password}` 会从对应的密码文件安全替换；供应商密钥优先使用 `OPENAI_API_KEY_FILE` 等文件字段。这两个 `.env` 都不得提交。

## 3. 基础服务与 Runner

```bash
docker compose up -d postgres redis minio 2>/dev/null || docker-compose up -d postgres redis minio
docker build -t atlas-agent-runner:py311 runner/
```

公司网络无法访问公共镜像仓库时，在可联网机器下载 `linux/amd64` 镜像并导出：

```bash
crane pull --platform linux/amd64 docker.m.daocloud.io/pgvector/pgvector:pg16 pgvector.tar
crane pull --platform linux/amd64 docker.m.daocloud.io/library/redis:7-alpine redis.tar
crane pull --platform linux/amd64 docker.m.daocloud.io/minio/minio:RELEASE.2025-04-22T22-12-26Z minio.tar
crane pull --platform linux/amd64 docker.m.daocloud.io/library/python:3.11.13-alpine python.tar
```

经受控 SFTP 上传并核对 SHA-256 后，在服务器执行 `docker load -i <file>`，再执行上面的启动和构建命令。镜像代理只用于下载构建依赖，不承载 Atlas 业务流量。

## 4. SQLite 首次迁移

迁移前停止写流量并备份原目录。目标 PostgreSQL 必须是空库：

```bash
export DATABASE_URL='postgresql+psycopg://atlas:***@127.0.0.1:5432/atlas'
cd api
.venv/bin/python ../scripts/migrate_sqlite_to_postgres.py data/atlas.db --target "$DATABASE_URL"
```

迁移脚本按外键顺序复制共同字段；旧知识文档没有原文件时 `object_key` 为空，需重新上传原件后才具备完整恢复能力。旧 Agent 草稿目录应复制到 `/var/lib/atlas/agent-code`。

## 5. 启动与验收

构建前端并通过 systemd 启动 FastAPI，再让 Nginx 反向代理到 `127.0.0.1:8000`。上线至少执行：

```bash
curl -fsS http://127.0.0.1:8000/api/health
sudo systemctl is-active atlas nginx docker
```

健康响应中的 `database`、`redis`、`object_storage`、`code_runner` 必须全部为 `true`。功能验收包括登录、知识上传与检索、Agent 短期/长期记忆隔离、代码超时终止、预览无法读取父页面、发布版本和 API Key 调用。

自适应执行必须独立灰度，不能因部署自动开启：

```dotenv
LANGGRAPH_RUNTIME_ENABLED=false
ADAPTIVE_RUNTIME_ENABLED=false
LANGGRAPH_RUNTIME_LEGACY_FALLBACK=false
```

先在测试 workspace 验证 PostgreSQL Checkpointer、父/子 RuntimeRun、并行 Worker、
独立 Reviewer、五类 verdict、事件游标和取消/恢复，再依次开启 LangGraph 与
Adaptive。回滚时先关闭 `ADAPTIVE_RUNTIME_ENABLED`，保留已写入的 Plan/Review
状态和事件；副作用开始后的运行不得切换模板或 legacy 重放。

## 6. 备份与恢复

生产备份只包含 PostgreSQL 权威数据、MinIO bucket 对象、Agent code 和非敏感
Compose 配置。PostgreSQL 使用 custom logical dump，并在发布备份前执行
`pg_restore --list`；MinIO 必须通过 S3 对象 API (`mc mirror`) 导出，不能复制
运行中的 `/data` 私有卷。Redis 是可重建的 TTL/热状态层，**永不进入备份**。

每日以 root 运行（也可由具备 Docker 与 Agent code 读取权限的专用备份账号运行）：

```bash
sudo env \
  ATLAS_COMPOSE_PROJECT=atlas-production \
  BACKUP_ROOT=/var/backups/atlas \
  BACKUP_RETENTION_DAYS=14 \
  MINIO_ROOT_USER=atlas \
  MINIO_ROOT_PASSWORD_FILE=/etc/atlas/secrets/minio_root_password \
  /opt/atlas/scripts/backup.sh
```

脚本先写同一文件系统中的隐藏临时目录，全部完成后再原子重命名。可用备份目录
必须同时包含 `manifest.json`、`SHA256SUMS`、`minio-index.tsv` 和与目录 ID 一致的
`COMPLETE`；未完成目录不会被恢复或被 retention 当作历史备份删除。默认保留 14
天，retention 只删除命名、完成标记均符合生产 backup schema 的过期目录。

PostgreSQL、MinIO 与 Agent code 之间不存在分布式快照事务。正式每日调度必须先让
Atlas 进入只读/排空写入的一致性窗口，等待在途 Runtime 与对象上传结束后再执行
`backup.sh`，完成后恢复写流量；调度系统应记录窗口开始、备份 ID 和完成状态。若
业务 SLA 不允许短暂只读，应在上线前补充应用级维护锁或版本水位协议，不能把三个
存储各自成功误认为跨存储强一致快照。

恢复命令默认仅执行严格校验，不写任何服务。它会校验 schema、路径、SHA-256、
MinIO 逐对象内容索引、Agent code archive 和 PostgreSQL custom dump 列表：

```bash
sudo env \
  ATLAS_COMPOSE_PROJECT=atlas-production \
  BACKUP_ROOT=/var/backups/atlas \
  MINIO_ROOT_PASSWORD_FILE=/etc/atlas/secrets/minio_root_password \
  /opt/atlas/scripts/restore.sh /var/backups/atlas/<backup-id>
```

生产写恢复只能在维护窗口执行。`ATLAS_COMPOSE_PROJECT` 必须显式选择实际容器组，
所有 Compose 命令都固定使用 `--project-name`；`ATLAS_RESTORE_TARGET` 必须与该 project
完全一致，确认值再同时绑定 project 和本次精确 backup ID，避免确认 A 却误写 B，
或把隔离演练、旧备份误写生产。
PostgreSQL 通过 `--single-transaction --exit-on-error` 全成或全败；
MinIO 恢复后会重新下载整个 bucket 并比对逐对象 hash/index；Redis 执行
`FLUSHALL SYNC` 后同时验证 `DBSIZE=0` 且不存在任何 keyspace，因此旧 session、
短期记忆 TTL 和一次性工具授权票据不会复活：

```bash
BACKUP_ID=<backup-id>
sudo env \
  BACKUP_ROOT=/var/backups/atlas \
  MINIO_ROOT_USER=atlas \
  MINIO_ROOT_PASSWORD_FILE=/etc/atlas/secrets/minio_root_password \
  ATLAS_COMPOSE_PROJECT=atlas-production \
  ATLAS_RESTORE_TARGET=atlas-production \
  ATLAS_RESTORE_CONFIRM="RESTORE:atlas-production:$BACKUP_ID" \
  /opt/atlas/scripts/restore.sh "/var/backups/atlas/$BACKUP_ID" --apply
```

Agent code 在同父目录中完成 staging 后原子替换。备份内 `config/` 仅供人工差异
比对，不会覆盖生产配置；`api/.env`、根 `.env`、平台密钥和供应商凭据必须从
密钥管理系统恢复，备份不会包含它们。

不要用生产 `restore.sh --apply` 做演练。正式使用前至少通过
[`acceptance-backup-restore.md`](./acceptance-backup-restore.md) 的
`make acceptance-rehearse` 在不同的 `atlas-acceptance-*` Compose project 完成一次
隔离恢复，并留存 restore JSON、对象索引、镜像 digest 和 RTO 证据。建议目标 RPO
24 小时、RTO 4 小时；企业上线后再按 SLA 调整。

## 7. 回滚

保留发布前代码目录、`api/.env`、SQLite 文件、systemd 和 Nginx 配置。若依赖健康、迁移行数或核心冒烟失败，停止新服务，恢复旧目录和环境文件并重启原 `atlas.service`。不要删除 PostgreSQL/MinIO/Redis 卷，待问题定位后再清理。

## 8. 安全检查

- `api/.env`、根 `.env`、`secrets/`、备份目录和部署压缩包不得进入 Git。
- 历史中出现过的供应商 Key、GitHub PAT 和连接器凭据必须轮换。
- Docker socket 只授予受控服务账号；Runner 容器不注入 Atlas 环境变量。
- MinIO、Redis、PostgreSQL 只绑定内网或 `127.0.0.1`，不得直接暴露公网。
- 每季度执行依赖更新、镜像漏洞扫描、恢复演练和跨租户权限测试。
