#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/acceptance/lib.sh
source "$SCRIPT_DIR/lib.sh"
configure_acceptance_environment
require_command docker
require_command python3
require_secret_files

if [[ $# -ne 1 ]]; then
  echo "usage: ATLAS_ACCEPTANCE_PROJECT=atlas-acceptance-restore $0 BACKUP_DIR" >&2
  exit 2
fi

BACKUP_DIR="$(cd "$1" && pwd)"
test -f "$BACKUP_DIR/COMPLETE"
test -f "$BACKUP_DIR/manifest.json"
test -f "$BACKUP_DIR/SHA256SUMS"
test -f "$BACKUP_DIR/postgres.dump"
test -f "$BACKUP_DIR/minio-index.tsv"
verify_top_level_checksums "$BACKUP_DIR"

read -r BACKUP_ID SOURCE_PROJECT SOURCE_BUCKET < <(
  python3 - "$BACKUP_DIR/manifest.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if manifest.get("schema_version") != 1:
    raise SystemExit("unsupported backup manifest schema")
if manifest.get("redis", {}).get("included") is not False:
    raise SystemExit("refusing a backup that includes Redis authoritative state")
print(manifest["backup_id"], manifest["source_project"], manifest["minio"]["bucket"])
PY
)

case "$SOURCE_PROJECT" in
  atlas-acceptance|atlas-acceptance-[a-z0-9]* ) ;;
  * ) echo "backup was not produced by an acceptance project" >&2; exit 47 ;;
esac
if [[ "$ATLAS_ACCEPTANCE_PROJECT" == "$SOURCE_PROJECT" ]]; then
  echo "refusing in-place restore; select a distinct atlas-acceptance-* target project" >&2
  exit 48
fi
if [[ "$(cat "$BACKUP_DIR/COMPLETE")" != "$BACKUP_ID" ]]; then
  echo "backup completion marker does not match manifest" >&2
  exit 49
fi
if [[ "$SOURCE_BUCKET" != "$MINIO_BUCKET" ]]; then
  echo "target bucket differs from manifest bucket" >&2
  exit 50
fi

VERIFY_ROOT="$ATLAS_ACCEPTANCE_ROOT/restore-verify/$BACKUP_ID"
EVIDENCE_ROOT="${ATLAS_ACCEPTANCE_EVIDENCE_ROOT:-$ATLAS_ACCEPTANCE_ROOT/evidence}"
REPORT="$EVIDENCE_ROOT/restore-$BACKUP_ID.json"
rm -rf "$VERIFY_ROOT"
install -d -m 700 "$VERIFY_ROOT/download" "$EVIDENCE_ROOT"

build_object_index "$BACKUP_DIR/minio" "$VERIFY_ROOT/backup-index.tsv"
cmp "$BACKUP_DIR/minio-index.tsv" "$VERIFY_ROOT/backup-index.tsv"

STARTED_AT="$(utc_now)"
STARTED_EPOCH="$(date +%s)"

# The target project must already be healthy. The project-name guard in lib.sh
# and the source != target check above prevent this from touching production.
compose up --detach --wait --wait-timeout "${ATLAS_ACCEPTANCE_WAIT_SECONDS:-120}" postgres redis minio

compose exec -T postgres pg_restore \
  -U atlas -d atlas \
  --clean --if-exists --exit-on-error --single-transaction \
  --no-owner --no-privileges < "$BACKUP_DIR/postgres.dump"
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U atlas -d atlas -c 'ANALYZE' >/dev/null

docker run --rm \
  --network "$(compose_network)" \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --env MC_CONFIG_DIR=/tmp/.mc \
  --env MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  --env MINIO_BUCKET="$MINIO_BUCKET" \
  --volume "$MINIO_ROOT_PASSWORD_FILE:/run/secrets/minio_password:ro" \
  --volume "$BACKUP_DIR/minio:/backup:ro" \
  --entrypoint /bin/sh "$MINIO_MC_IMAGE" -ec '
    password="$(cat /run/secrets/minio_password)"
    mc alias set atlas http://minio:9000 "$MINIO_ROOT_USER" "$password" >/dev/null
    mc mb --ignore-existing "atlas/$MINIO_BUCKET" >/dev/null
    mc rm --recursive --force "atlas/$MINIO_BUCKET" >/dev/null 2>&1 || true
    mc mirror --overwrite --preserve /backup "atlas/$MINIO_BUCKET" >/dev/null
  '

# Redis is deliberately reset instead of restored. This prevents stale TTL
# state, sessions and one-time authorization tickets from becoming valid again.
# Expansion is intentionally performed by the Redis container's shell.
# shellcheck disable=SC2016
REDIS_SIZE="$(compose exec -T redis sh -ec '
  REDISCLI_AUTH="$(cat /run/secrets/redis_password)"
  export REDISCLI_AUTH
  redis-cli FLUSHALL >/dev/null
  redis-cli --raw DBSIZE
' | tr -d '\r')"
if [[ "$REDIS_SIZE" != "0" ]]; then
  echo "Redis was not empty after restore: $REDIS_SIZE" >&2
  exit 51
fi

# Download the restored bucket and compare a content-addressed object index.
docker run --rm \
  --network "$(compose_network)" \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --env MC_CONFIG_DIR=/tmp/.mc \
  --env MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  --env MINIO_BUCKET="$MINIO_BUCKET" \
  --volume "$MINIO_ROOT_PASSWORD_FILE:/run/secrets/minio_password:ro" \
  --volume "$VERIFY_ROOT/download:/verify" \
  --entrypoint /bin/sh "$MINIO_MC_IMAGE" -ec '
    password="$(cat /run/secrets/minio_password)"
    mc alias set atlas http://minio:9000 "$MINIO_ROOT_USER" "$password" >/dev/null
    mc mirror --overwrite --preserve "atlas/$MINIO_BUCKET" /verify >/dev/null
  '
build_object_index "$VERIFY_ROOT/download" "$VERIFY_ROOT/restored-index.tsv"
cmp "$BACKUP_DIR/minio-index.tsv" "$VERIFY_ROOT/restored-index.tsv"

POSTGRES_OK="$(compose exec -T postgres psql -At -v ON_ERROR_STOP=1 -U atlas -d atlas -c 'SELECT 1')"
PGVECTOR_VERSION="$(compose exec -T postgres psql -At -v ON_ERROR_STOP=1 -U atlas -d atlas -c "SELECT extversion FROM pg_extension WHERE extname='vector'")"
POSTGRES_SENTINEL="$(compose exec -T postgres psql -At -U atlas -d atlas -c "SELECT payload FROM atlas_acceptance_sentinel WHERE key='restore-sentinel'" 2>/dev/null || true)"
[[ "$POSTGRES_OK" == "1" ]]
[[ -n "$PGVECTOR_VERSION" ]]

COMPLETED_AT="$(utc_now)"
RTO_SECONDS="$(( $(date +%s) - STARTED_EPOCH ))"
OBJECT_COUNT="$(wc -l < "$BACKUP_DIR/minio-index.tsv" | tr -d ' ')"

RESTORE_BACKUP_ID="$BACKUP_ID" \
RESTORE_SOURCE_PROJECT="$SOURCE_PROJECT" \
RESTORE_TARGET_PROJECT="$ATLAS_ACCEPTANCE_PROJECT" \
RESTORE_STARTED_AT="$STARTED_AT" \
RESTORE_COMPLETED_AT="$COMPLETED_AT" \
RESTORE_RTO_SECONDS="$RTO_SECONDS" \
RESTORE_OBJECT_COUNT="$OBJECT_COUNT" \
RESTORE_PGVECTOR_VERSION="$PGVECTOR_VERSION" \
RESTORE_POSTGRES_SENTINEL="$POSTGRES_SENTINEL" \
python3 - "$REPORT" <<'PY'
import json
import os
import pathlib
import sys

report = {
    "schema_version": 1,
    "restore_id": f"{os.environ['RESTORE_BACKUP_ID']}:{os.environ['RESTORE_TARGET_PROJECT']}",
    "backup_id": os.environ["RESTORE_BACKUP_ID"],
    "source_project": os.environ["RESTORE_SOURCE_PROJECT"],
    "target_project": os.environ["RESTORE_TARGET_PROJECT"],
    "started_at": os.environ["RESTORE_STARTED_AT"],
    "completed_at": os.environ["RESTORE_COMPLETED_AT"],
    "rto_seconds": int(os.environ["RESTORE_RTO_SECONDS"]),
    "postgres": {
        "restored": True,
        "pgvector_version": os.environ["RESTORE_PGVECTOR_VERSION"],
        "sentinel": os.environ["RESTORE_POSTGRES_SENTINEL"] or None,
    },
    "minio": {
        "restored": True,
        "object_count": int(os.environ["RESTORE_OBJECT_COUNT"]),
        "content_index_match": True,
    },
    "redis": {
        "restored": False,
        "empty_after_restore": True,
    },
    "result": "pass",
}
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

printf '%s\n' "$REPORT"
