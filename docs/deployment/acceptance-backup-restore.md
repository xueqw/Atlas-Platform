# Atlas 隔离数据面与备份恢复验收

## 安全边界

本流程只管理名称为 `atlas-acceptance` 或以 `atlas-acceptance-` 开头的
Docker Compose project。脚本拒绝其他 project，恢复脚本也拒绝恢复到备份的
源 project，因此不会覆盖 Atlas 生产卷。

所有生成的密钥、备份和证据默认保存在被 Git 忽略的
`.acceptance/<project>/`。PostgreSQL、Redis 和 MinIO 默认绑定到
`127.0.0.1:15432`、`16379`、`19000/19001`，不会占用生产默认端口。

Redis 是非权威热状态：备份清单必须包含 `redis.included=false`。恢复时 Redis
会被清空，旧 session、TTL 缓存和一次性工具授权票据不得恢复；Runtime、会话
上下文和事件游标应由 PostgreSQL 权威记录重建。

## 依赖

- Docker Engine 或 Docker Desktop；
- Docker Compose v2；
- Bash、Python 3、OpenSSL、`sha256sum` 或 `shasum`；
- 可拉取 `pgvector/pgvector:pg16`、固定版本 MinIO、Redis 7 和固定版本
  `minio/mc` 镜像。

macOS 验收机的工具依赖由仓库根目录 `Brewfile.acceptance` 管理：

```bash
brew bundle --file Brewfile.acceptance
```

本地 Linux 容器运行时可使用独立 Colima profile，避免复用开发环境：

```bash
colima start --profile atlas-acceptance --cpu 4 --memory 6 --disk 8 --runtime docker
docker context use colima-atlas-acceptance
```

`api/requirements.txt`、`web/package-lock.json` 和 AgentGateway 各自的锁定依赖仍是
应用依赖的唯一来源；不要用宿主 Homebrew PostgreSQL/Redis 替代下面的目标版本
Compose 验收。宿主命令仅用于管理、诊断与灾备操作。

受限网络可以显式指定已审核的镜像代理，Compose 不会静默改写镜像来源：

```bash
export POSTGRES_IMAGE=docker.m.daocloud.io/pgvector/pgvector:pg16
export REDIS_IMAGE=docker.m.daocloud.io/library/redis:7-alpine
export MINIO_IMAGE=docker.m.daocloud.io/minio/minio:RELEASE.2025-04-22T22-12-26Z
export MINIO_MC_IMAGE=docker.m.daocloud.io/minio/mc:RELEASE.2025-04-16T18-13-26Z
```

先确认 Docker daemon 可用：

```bash
docker info
docker compose version
```

## 生命周期管理

```bash
make acceptance-init
make acceptance-up
make acceptance-status
make acceptance-seed
make acceptance-backup
make acceptance-down
```

也可以直接调用：

```bash
scripts/acceptance/manage.sh up
scripts/acceptance/manage.sh urls
scripts/acceptance/manage.sh status
```

如需并行环境，为每套环境指定不同 project 和端口：

```bash
ATLAS_ACCEPTANCE_PROJECT=atlas-acceptance-ci-42 \
POSTGRES_HOST_PORT=25432 REDIS_HOST_PORT=26379 \
MINIO_HOST_PORT=29000 MINIO_CONSOLE_HOST_PORT=29001 \
scripts/acceptance/manage.sh up
```

## 一键恢复演练

```bash
make acceptance-rehearse
```

演练会依次执行：

1. 启动 source acceptance project；
2. 写入 PostgreSQL/pgvector、MinIO 和 Redis 哨兵；
3. 使用 `pg_dump -Fc --no-owner --no-privileges` 创建逻辑备份；
4. 使用 `mc mirror` 导出 MinIO bucket，并为每个对象记录大小和 SHA-256；
5. 明确不备份 Redis；
6. 停止 source，创建一个全新的 restore project；
7. 在单事务中执行 PostgreSQL restore，恢复 MinIO 对象，清空 Redis；
8. 重新下载 MinIO bucket 并比对完整对象索引；
9. 输出结构化 restore JSON，并校验 PostgreSQL 哨兵、至少一个 MinIO 对象和
   Redis 空状态。

成功时最后一行是恢复报告路径，例如：

```text
.acceptance/atlas-acceptance-restore-20260716180000/evidence/restore-<backup-id>.json
```

restore project 会保持运行，便于继续执行 API、Runtime 和租户隔离冒烟；source
project 保持停止。检查完成后分别清理：

```bash
ATLAS_ACCEPTANCE_PROJECT=atlas-acceptance scripts/acceptance/manage.sh clean
ATLAS_ACCEPTANCE_PROJECT=atlas-acceptance-restore-20260716180000 \
  scripts/acceptance/manage.sh clean
```

## 手工分步恢复

恢复必须使用不同的隔离 project：

```bash
BACKUP=.acceptance/atlas-acceptance/backups/<backup-id>
ATLAS_ACCEPTANCE_PROJECT=atlas-acceptance-restore-manual \
  scripts/acceptance/manage.sh up
ATLAS_ACCEPTANCE_PROJECT=atlas-acceptance-restore-manual \
  scripts/acceptance/manage.sh restore "$BACKUP"
```

脚本首先校验 `COMPLETE`、manifest、顶层 SHA-256 和 MinIO 对象索引。任一校验
失败即停止，不写目标服务。

## 完整发布门禁

备份恢复通过后，还必须针对 restore project 执行真实数据面测试：

```bash
RUN_ATLAS_PRODUCTION_DATA_PLANE_TESTS=1 \
ATLAS_ACCEPTANCE_POSTGRES_URL='postgresql+psycopg://atlas:<acceptance-password>@127.0.0.1:15432/atlas' \
ATLAS_ACCEPTANCE_REDIS_URL='redis://default:<acceptance-password>@127.0.0.1:16379/15' \
.venv/bin/python -m pytest -q scripts/acceptance/test_production_data_plane.py
```

Docker Hub 受限时，Runner 也应显式记录经过审核的基础镜像代理：

```bash
docker build \
  --build-arg PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11.13-alpine \
  -t atlas-agent-runner:py311 runner/

cd api
ATLAS_RUN_DOCKER_ACCEPTANCE=1 \
ATLAS_DOCKER_ACCEPTANCE_IMAGE=atlas-agent-runner:py311 \
../.venv/bin/python -m pytest -q \
  --basetemp=../.acceptance/pytest-docker-runner \
  tests/test_code_runner_docker_release.py
```

Colima 只共享配置的宿主目录，因此 `--basetemp` 必须位于工作区内；这与生产
Agent code workspace 的共享路径一致，也避免把系统 `/private/var` 误当成可挂载目录。

随后用生产配置启动 API，严格检查 `/api/health` JSON 中 `status=ok` 且
`database`、`redis`、`object_storage`、`code_runner` 全部为 `true`，再执行统一
Runtime 重启恢复、MinIO SHA、Docker Runner 限制和跨 workspace/user/agent/worker
隔离冒烟。`curl -f` 本身不充分，因为 degraded 健康响应仍可能返回 HTTP 200。

完整证据至少包括：

- `manifest.json`、`SHA256SUMS`、`minio-index.tsv` 和 `COMPLETE`；
- restore JSON 中的 source/target project、RTO、pgvector 版本、对象数、Redis
  非恢复声明及 `result=pass`；
- 真实数据面 pytest 输出、API health JSON、Docker Runner 和租户隔离结果；
- Git SHA、Compose 配置和实际镜像 digest。

缺少任一真实基础设施证据时，不得完成 OpenSpec production-data-plane 验收项。
