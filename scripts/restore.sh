#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 BACKUP_DIR [--apply]" >&2
  exit 2
fi

BACKUP_DIR="$(cd "$1" && pwd)"
cd "$BACKUP_DIR"
test -f BACKUP_COMPLETE
shasum -a 256 -c SHA256SUMS

if [[ "${2:-}" != "--apply" ]]; then
  echo "Backup verification passed. Re-run with --apply and ATLAS_RESTORE_CONFIRM=RESTORE."
  exit 0
fi
if [[ "${ATLAS_RESTORE_CONFIRM:-}" != "RESTORE" ]]; then
  echo "Refusing restore without ATLAS_RESTORE_CONFIRM=RESTORE" >&2
  exit 3
fi
if [[ "$EUID" -ne 0 ]]; then
  echo "restore.sh --apply must run as root" >&2
  exit 4
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose); else COMPOSE=(docker-compose); fi
MINIO_ID="$("${COMPOSE[@]}" ps -q minio)"
REDIS_ID="$("${COMPOSE[@]}" ps -q redis)"
MINIO_DATA="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$MINIO_ID")"
REDIS_DATA="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$REDIS_ID")"
"${COMPOSE[@]}" exec -T postgres pg_restore -U atlas -d atlas --clean --if-exists < "$BACKUP_DIR/postgres.dump"
"${COMPOSE[@]}" stop minio redis
find "$MINIO_DATA" -mindepth 1 -delete
find "$REDIS_DATA" -mindepth 1 -delete
tar -C "$MINIO_DATA" -xzf "$BACKUP_DIR/minio-data.tar.gz"
tar -C "$REDIS_DATA" -xzf "$BACKUP_DIR/redis-data.tar.gz"
"${COMPOSE[@]}" start minio redis

if [[ -f "$BACKUP_DIR/agent-code.tar.gz" ]]; then
  AGENT_CODE_ROOT="${AGENT_CODE_ROOT:-/var/lib/atlas/agent-code}"
  mkdir -p "$AGENT_CODE_ROOT"
  tar -C "$AGENT_CODE_ROOT" -xzf "$BACKUP_DIR/agent-code.tar.gz"
fi
echo "Database, MinIO, Redis and Agent code restored."
