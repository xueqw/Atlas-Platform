#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/acceptance/lib.sh
source "$SCRIPT_DIR/lib.sh"
configure_acceptance_environment
require_command docker
require_command python3
require_secret_files

BACKUP_ROOT="${BACKUP_ROOT:-$ATLAS_ACCEPTANCE_ROOT/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
GIT_SHA="$(git -C "$ATLAS_ROOT" rev-parse HEAD 2>/dev/null || printf unknown)"
GIT_SHORT="${GIT_SHA:0:12}"
BACKUP_ID="$STAMP-$GIT_SHORT"
TARGET="$BACKUP_ROOT/$BACKUP_ID"
TEMP="$BACKUP_ROOT/.$BACKUP_ID.tmp.$$"
LOCK_DIR="$BACKUP_ROOT/.backup-lock-$ATLAS_ACCEPTANCE_PROJECT"
STARTED_AT="$(utc_now)"

install -d -m 700 "$BACKUP_ROOT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "another backup is active for $ATLAS_ACCEPTANCE_PROJECT" >&2
  exit 45
fi

cleanup() {
  [[ -z "${TEMP:-}" ]] || rm -rf "$TEMP"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT

if [[ -e "$TARGET" ]]; then
  echo "backup target already exists: $TARGET" >&2
  exit 46
fi

install -d -m 700 "$TEMP/minio"

# PostgreSQL custom format is portable across target hosts running the same or
# a newer PostgreSQL major version. Owner and ACL records are intentionally
# omitted so an isolated restore cannot acquire production identities.
compose exec -T postgres pg_dump \
  -U atlas -d atlas --format=custom --no-owner --no-privileges > "$TEMP/postgres.dump"
compose exec -T postgres pg_restore --list < "$TEMP/postgres.dump" >/dev/null

# Back up S3 objects, not MinIO's private volume layout. This works with
# rootless/remote Docker and avoids copying a live server's internal metadata.
docker run --rm \
  --network "$(compose_network)" \
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
    mc mb --ignore-existing "atlas/$MINIO_BUCKET" >/dev/null
    mc mirror --overwrite --preserve "atlas/$MINIO_BUCKET" /backup >/dev/null
  '

build_object_index "$TEMP/minio" "$TEMP/minio-index.tsv"
cp "$ATLAS_ROOT/docker-compose.yml" "$TEMP/docker-compose.yml"

POSTGRES_SHA="$(sha256_file "$TEMP/postgres.dump")"
POSTGRES_BYTES="$(wc -c < "$TEMP/postgres.dump" | tr -d ' ')"
MINIO_INDEX_SHA="$(sha256_file "$TEMP/minio-index.tsv")"
MINIO_OBJECT_COUNT="$(wc -l < "$TEMP/minio-index.tsv" | tr -d ' ')"
MINIO_TOTAL_BYTES="$(awk -F '\t' '{total += $2} END {print total + 0}' "$TEMP/minio-index.tsv")"
POSTGRES_VERSION="$(compose exec -T postgres psql -At -U atlas -d atlas -c 'SHOW server_version')"
PGVECTOR_VERSION="$(compose exec -T postgres psql -At -U atlas -d atlas -c "SELECT extversion FROM pg_extension WHERE extname='vector'" || true)"
COMPLETED_AT="$(utc_now)"

MANIFEST_BACKUP_ID="$BACKUP_ID" \
MANIFEST_SOURCE_PROJECT="$ATLAS_ACCEPTANCE_PROJECT" \
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
python3 - "$TEMP/manifest.json" <<'PY'
import json
import os
import pathlib
import sys

manifest = {
    "schema_version": 1,
    "backup_id": os.environ["MANIFEST_BACKUP_ID"],
    "source_project": os.environ["MANIFEST_SOURCE_PROJECT"],
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
        "bucket": os.environ["MANIFEST_MINIO_BUCKET"],
        "object_count": int(os.environ["MANIFEST_MINIO_OBJECT_COUNT"]),
        "total_bytes": int(os.environ["MANIFEST_MINIO_TOTAL_BYTES"]),
        "index_sha256": os.environ["MANIFEST_MINIO_INDEX_SHA"],
    },
    "redis": {
        "included": False,
        "reason": "Redis is a non-authoritative TTL/hot-state tier; authorization tickets must not be restored.",
    },
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

{
  for filename in postgres.dump minio-index.tsv docker-compose.yml manifest.json; do
    printf '%s  %s\n' "$(sha256_file "$TEMP/$filename")" "$filename"
  done
} > "$TEMP/SHA256SUMS"

printf '%s\n' "$BACKUP_ID" > "$TEMP/COMPLETE"
mv "$TEMP" "$TARGET"
TEMP=""

# Retention only applies to completed timestamped directories in this
# acceptance-owned root; it never traverses arbitrary paths or production data.
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
  -name '20????????T??????Z-*' -mtime "+$RETENTION_DAYS" -exec rm -rf {} +

printf '%s\n' "$TARGET"
