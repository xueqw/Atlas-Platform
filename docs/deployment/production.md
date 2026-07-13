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

## 6. 备份与恢复

每日以 root 运行：

```bash
BACKUP_ROOT=/var/backups/atlas BACKUP_RETENTION_DAYS=14 /opt/atlas/scripts/backup.sh
```

先只校验，不覆盖数据：

```bash
/opt/atlas/scripts/restore.sh /var/backups/atlas/<timestamp>
```

恢复必须在维护窗口显式确认：

```bash
ATLAS_RESTORE_CONFIRM=RESTORE /opt/atlas/scripts/restore.sh /var/backups/atlas/<timestamp> --apply
```

正式使用前至少做一次隔离环境恢复演练。建议目标 RPO 24 小时、RTO 4 小时；企业上线后再按 SLA 调整。

## 7. 回滚

保留发布前代码目录、`api/.env`、SQLite 文件、systemd 和 Nginx 配置。若依赖健康、迁移行数或核心冒烟失败，停止新服务，恢复旧目录和环境文件并重启原 `atlas.service`。不要删除 PostgreSQL/MinIO/Redis 卷，待问题定位后再清理。

## 8. 安全检查

- `api/.env`、根 `.env`、`secrets/`、备份目录和部署压缩包不得进入 Git。
- 历史中出现过的供应商 Key、GitHub PAT 和连接器凭据必须轮换。
- Docker socket 只授予受控服务账号；Runner 容器不注入 Atlas 环境变量。
- MinIO、Redis、PostgreSQL 只绑定内网或 `127.0.0.1`，不得直接暴露公网。
- 每季度执行依赖更新、镜像漏洞扫描、恢复演练和跨租户权限测试。
