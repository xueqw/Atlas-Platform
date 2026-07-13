#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$EUID" -ne 0 ]]; then
  echo "backup.sh must run as root to read Docker volume data" >&2
  exit 4
fi
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/atlas}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_ROOT/$STAMP"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
mkdir -p "$TARGET"

cd "$ROOT"
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose); else COMPOSE=(docker-compose); fi
"${COMPOSE[@]}" exec -T postgres pg_dump -U atlas -d atlas --format=custom > "$TARGET/postgres.dump"

MINIO_ID="$("${COMPOSE[@]}" ps -q minio)"
REDIS_ID="$("${COMPOSE[@]}" ps -q redis)"
MINIO_DATA="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$MINIO_ID")"
REDIS_DATA="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$REDIS_ID")"
tar -C "$MINIO_DATA" -czf "$TARGET/minio-data.tar.gz" .
tar -C "$REDIS_DATA" -czf "$TARGET/redis-data.tar.gz" .

AGENT_CODE_ROOT="${AGENT_CODE_ROOT:-/var/lib/atlas/agent-code}"
if [[ -d "$AGENT_CODE_ROOT" ]]; then
  tar -C "$AGENT_CODE_ROOT" -czf "$TARGET/agent-code.tar.gz" .
fi

cp docker-compose.yml "$TARGET/docker-compose.yml"
(cd "$TARGET" && shasum -a 256 ./* > SHA256SUMS)
touch "$TARGET/BACKUP_COMPLETE"
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+$RETENTION_DAYS" -exec rm -rf {} +
printf 'Atlas backup complete: %s\n' "$TARGET"
