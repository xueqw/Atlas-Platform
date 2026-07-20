#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Reuse only the side-effect-free checksum, object-index and clock helpers.
# Do not call configure_acceptance_environment or assert_acceptance_project here.
# shellcheck source=scripts/acceptance/lib.sh
source "$SCRIPT_DIR/acceptance/lib.sh"

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/atlas}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
AGENT_CODE_ROOT="${AGENT_CODE_ROOT:-/var/lib/atlas/agent-code}"
MINIO_ROOT_USER="${MINIO_ROOT_USER:-atlas}"
MINIO_BUCKET="${MINIO_BUCKET:-atlas}"
MINIO_ROOT_PASSWORD_FILE="${MINIO_ROOT_PASSWORD_FILE:-$ROOT/secrets/minio_root_password}"
MINIO_MC_IMAGE="${MINIO_MC_IMAGE:-minio/mc:RELEASE.2025-04-16T18-13-26Z}"
ATLAS_COMPOSE_PROJECT="${ATLAS_COMPOSE_PROJECT:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
GIT_SHA="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf unknown)"
GIT_SHORT="${GIT_SHA:0:12}"
BACKUP_ID="$STAMP-$GIT_SHORT"
TARGET="$BACKUP_ROOT/$BACKUP_ID"
TEMP="$BACKUP_ROOT/.$BACKUP_ID.tmp.$$"
LOCK_DIR="$BACKUP_ROOT/.backup-lock"
STARTED_AT="$(utc_now)"

fail() {
  echo "backup failed: $*" >&2
  exit 1
}

validate_settings() {
  [[ "$BACKUP_ROOT" == /* && "$BACKUP_ROOT" != "/" ]] || fail "BACKUP_ROOT must be an absolute non-root path"
  [[ "$AGENT_CODE_ROOT" == /* && "$AGENT_CODE_ROOT" != "/" ]] || fail "AGENT_CODE_ROOT must be an absolute non-root path"
  [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || fail "BACKUP_RETENTION_DAYS must be a non-negative integer"
  [[ "$MINIO_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail "invalid MINIO_BUCKET"
  [[ "$ATLAS_COMPOSE_PROJECT" =~ ^[a-z0-9][a-z0-9-]{1,62}$ ]] || \
    fail "ATLAS_COMPOSE_PROJECT must explicitly name the production Compose project"
  [[ -s "$MINIO_ROOT_PASSWORD_FILE" ]] || fail "MinIO password file is missing or empty: $MINIO_ROOT_PASSWORD_FILE"
  require_command docker
  require_command python3
  require_command tar
}

prod_compose() {
  docker compose --project-name "$ATLAS_COMPOSE_PROJECT" --file "$ROOT/docker-compose.yml" "$@"
}

require_compose_service() {
  local service="$1" container_id project_label
  container_id="$(prod_compose ps -q "$service")"
  [[ -n "$container_id" ]] || fail "Compose service is not running: $service"
  project_label="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$container_id")"
  [[ "$project_label" == "$ATLAS_COMPOSE_PROJECT" ]] || \
    fail "service $service belongs to unexpected Compose project: $project_label"
}

safe_remove_temp() {
  local candidate="${1:-}"
  [[ -n "$candidate" && "$candidate" == "$BACKUP_ROOT"/.*.tmp.* ]] || return 0
  [[ -d "$candidate" ]] && rm -rf -- "$candidate"
}

cleanup() {
  safe_remove_temp "${TEMP:-}"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

validate_agent_archive() {
  python3 - "$1" <<'PY'
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
with tarfile.open(archive, "r:gz") as handle:
    for member in handle.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe agent-code archive path: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise SystemExit(f"unsupported agent-code archive entry: {member.name}")
PY
}

validate_settings
install -d -m 700 "$BACKUP_ROOT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  fail "another production backup is active: $LOCK_DIR"
fi
trap cleanup EXIT

[[ ! -e "$TARGET" ]] || fail "backup target already exists: $TARGET"
install -d -m 700 "$TEMP/minio" "$TEMP/config"
for service in postgres redis minio; do
  require_compose_service "$service"
done

# PostgreSQL is backed up through its logical API, never by copying a live
# volume. Custom format supports pg_restore validation and transactional restore.
prod_compose exec -T postgres pg_dump \
  -U atlas -d atlas --format=custom --no-owner --no-privileges > "$TEMP/postgres.dump"
prod_compose exec -T postgres pg_restore --list < "$TEMP/postgres.dump" >/dev/null

# MinIO is exported through the S3-compatible object API. Copying /data would
# capture private live-server metadata and is intentionally forbidden.
MINIO_ID="$(prod_compose ps -q minio)"
MINIO_NETWORK="$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$MINIO_ID" | head -n 1)"
[[ -n "$MINIO_NETWORK" ]] || fail "could not determine the MinIO Compose network"
docker run --rm \
  --network "$MINIO_NETWORK" \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --env MC_CONFIG_DIR=/tmp/.mc \
  --env MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  --env MINIO_BUCKET="$MINIO_BUCKET" \
  --volume "$MINIO_ROOT_PASSWORD_FILE:/run/secrets/minio_password:ro" \
  --volume "$TEMP/minio:/backup" \
  --entrypoint /bin/sh "$MINIO_MC_IMAGE" -ec '
    password="$(cat /run/secrets/minio_password)"
    mc alias set atlas http://minio:9000 "$MINIO_ROOT_USER" "$password" >/dev/null
    mc stat "atlas/$MINIO_BUCKET" >/dev/null
    mc mirror --overwrite --preserve "atlas/$MINIO_BUCKET" /backup >/dev/null
  '
build_object_index "$TEMP/minio" "$TEMP/minio-index.tsv"

AGENT_CODE_INCLUDED=false
if [[ -d "$AGENT_CODE_ROOT" ]]; then
  if find "$AGENT_CODE_ROOT" -type l -print -quit | grep -q .; then
    fail "agent-code contains symbolic links; refusing a non-self-contained backup"
  fi
  tar -C "$AGENT_CODE_ROOT" -czf "$TEMP/agent-code.tar.gz" .
  validate_agent_archive "$TEMP/agent-code.tar.gz"
  AGENT_CODE_INCLUDED=true
fi

# Keep only non-sensitive deployment inputs. Runtime .env files and secrets are
# explicitly excluded and must be restored from the secret manager.
cp "$ROOT/docker-compose.yml" "$TEMP/config/docker-compose.yml"
if [[ -f "$ROOT/compose.env.example" ]]; then
  cp "$ROOT/compose.env.example" "$TEMP/config/compose.env.example"
fi

POSTGRES_SHA="$(sha256_file "$TEMP/postgres.dump")"
POSTGRES_BYTES="$(wc -c < "$TEMP/postgres.dump" | tr -d ' ')"
MINIO_INDEX_SHA="$(sha256_file "$TEMP/minio-index.tsv")"
MINIO_OBJECT_COUNT="$(wc -l < "$TEMP/minio-index.tsv" | tr -d ' ')"
MINIO_TOTAL_BYTES="$(awk -F '\t' '{total += $2} END {print total + 0}' "$TEMP/minio-index.tsv")"
POSTGRES_VERSION="$(prod_compose exec -T postgres psql -At -U atlas -d atlas -c 'SHOW server_version')"
PGVECTOR_VERSION="$(prod_compose exec -T postgres psql -At -U atlas -d atlas -c "SELECT extversion FROM pg_extension WHERE extname='vector'" || true)"
COMPLETED_AT="$(utc_now)"

MANIFEST_BACKUP_ID="$BACKUP_ID" \
MANIFEST_STARTED_AT="$STARTED_AT" \
MANIFEST_COMPLETED_AT="$COMPLETED_AT" \
MANIFEST_GIT_SHA="$GIT_SHA" \
MANIFEST_POSTGRES_VERSION="$POSTGRES_VERSION" \
MANIFEST_PGVECTOR_VERSION="$PGVECTOR_VERSION" \
MANIFEST_POSTGRES_BYTES="$POSTGRES_BYTES" \
MANIFEST_POSTGRES_SHA="$POSTGRES_SHA" \
MANIFEST_MINIO_BUCKET="$MINIO_BUCKET" \
MANIFEST_MINIO_OBJECT_COUNT="$MINIO_OBJECT_COUNT" \
MANIFEST_MINIO_TOTAL_BYTES="$MINIO_TOTAL_BYTES" \
MANIFEST_MINIO_INDEX_SHA="$MINIO_INDEX_SHA" \
MANIFEST_AGENT_CODE_INCLUDED="$AGENT_CODE_INCLUDED" \
MANIFEST_SOURCE_COMPOSE_PROJECT="$ATLAS_COMPOSE_PROJECT" \
python3 - "$TEMP/manifest.json" <<'PY'
import json
import os
import pathlib
import sys

manifest = {
    "schema_version": 2,
    "backup_id": os.environ["MANIFEST_BACKUP_ID"],
    "kind": "atlas-production-logical-backup",
    "source_compose_project": os.environ["MANIFEST_SOURCE_COMPOSE_PROJECT"],
    "started_at": os.environ["MANIFEST_STARTED_AT"],
    "completed_at": os.environ["MANIFEST_COMPLETED_AT"],
    "git_sha": os.environ["MANIFEST_GIT_SHA"],
    "postgres": {
        "format": "custom",
        "server_version": os.environ["MANIFEST_POSTGRES_VERSION"],
        "pgvector_version": os.environ["MANIFEST_PGVECTOR_VERSION"],
        "bytes": int(os.environ["MANIFEST_POSTGRES_BYTES"]),
        "sha256": os.environ["MANIFEST_POSTGRES_SHA"],
    },
    "minio": {
        "transport": "s3-object-api",
        "bucket": os.environ["MANIFEST_MINIO_BUCKET"],
        "object_count": int(os.environ["MANIFEST_MINIO_OBJECT_COUNT"]),
        "total_bytes": int(os.environ["MANIFEST_MINIO_TOTAL_BYTES"]),
        "index_sha256": os.environ["MANIFEST_MINIO_INDEX_SHA"],
    },
    "redis": {
        "included": False,
        "restore_policy": "flushall",
        "reason": "Redis contains non-authoritative TTL state, sessions and authorization tickets.",
    },
    "agent_code": {"included": os.environ["MANIFEST_AGENT_CODE_INCLUDED"] == "true"},
    "config": {"sensitive_values_included": False, "directory": "config"},
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

{
  for filename in postgres.dump minio-index.tsv manifest.json; do
    printf '%s  %s\n' "$(sha256_file "$TEMP/$filename")" "$filename"
  done
  if [[ -f "$TEMP/agent-code.tar.gz" ]]; then
    printf '%s  %s\n' "$(sha256_file "$TEMP/agent-code.tar.gz")" agent-code.tar.gz
  fi
  while IFS= read -r -d '' config_file; do
    relative="${config_file#"$TEMP/"}"
    printf '%s  %s\n' "$(sha256_file "$config_file")" "$relative"
  done < <(find "$TEMP/config" -type f -print0 | LC_ALL=C sort -z)
} > "$TEMP/SHA256SUMS"

printf '%s\n' "$BACKUP_ID" > "$TEMP/COMPLETE"
mv "$TEMP" "$TARGET"
TEMP=""

# Retention is restricted to completed backups matching the production schema.
# Temporary/foreign directories are never removed.
while IFS= read -r -d '' expired; do
  expired_id="$(basename "$expired")"
  [[ "$expired_id" =~ ^20[0-9]{6}T[0-9]{6}Z-([0-9a-f]{7,40}|unknown)$ ]] || continue
  [[ -f "$expired/COMPLETE" && "$(cat "$expired/COMPLETE")" == "$expired_id" ]] || continue
  rm -rf -- "$expired"
done < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
  -name '20????????T??????Z-*' -mtime "+$RETENTION_DAYS" -print0)

printf 'Atlas production backup complete: %s\n' "$TARGET"
