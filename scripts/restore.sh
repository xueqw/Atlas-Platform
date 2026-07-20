#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Reuse only pure checksum/index helpers. Never call the acceptance project
# guard: this script is intentionally the separately-confirmed production path.
# shellcheck source=scripts/acceptance/lib.sh
source "$SCRIPT_DIR/acceptance/lib.sh"

BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/atlas}"
AGENT_CODE_ROOT="${AGENT_CODE_ROOT:-/var/lib/atlas/agent-code}"
MINIO_ROOT_USER="${MINIO_ROOT_USER:-atlas}"
MINIO_BUCKET="${MINIO_BUCKET:-atlas}"
MINIO_ROOT_PASSWORD_FILE="${MINIO_ROOT_PASSWORD_FILE:-$ROOT/secrets/minio_root_password}"
MINIO_MC_IMAGE="${MINIO_MC_IMAGE:-minio/mc:RELEASE.2025-04-16T18-13-26Z}"
ATLAS_COMPOSE_PROJECT="${ATLAS_COMPOSE_PROJECT:-}"

usage() {
  echo "usage: BACKUP_ROOT=/var/backups/atlas $0 BACKUP_DIR [--apply]" >&2
  exit 2
}

fail() {
  echo "restore refused: $*" >&2
  exit 1
}

[[ $# -eq 1 || $# -eq 2 ]] || usage
[[ $# -eq 1 || "$2" == "--apply" ]] || usage
[[ "$BACKUP_ROOT" == /* && "$BACKUP_ROOT" != "/" ]] || fail "BACKUP_ROOT must be an absolute non-root path"
[[ "$AGENT_CODE_ROOT" == /* && "$AGENT_CODE_ROOT" != "/" ]] || fail "AGENT_CODE_ROOT must be an absolute non-root path"
[[ "$ATLAS_COMPOSE_PROJECT" =~ ^[a-z0-9][a-z0-9-]{1,62}$ ]] || \
  fail "ATLAS_COMPOSE_PROJECT must explicitly name the target Compose project"
[[ -d "$1" ]] || fail "backup directory does not exist: $1"
BACKUP_ROOT_REAL="$(cd "$BACKUP_ROOT" && pwd -P)"
BACKUP_DIR="$(cd "$1" && pwd -P)"
case "$BACKUP_DIR/" in
  "$BACKUP_ROOT_REAL"/*/) ;;
  *) fail "backup must be a direct child of BACKUP_ROOT ($BACKUP_ROOT_REAL)" ;;
esac
[[ "$(dirname "$BACKUP_DIR")" == "$BACKUP_ROOT_REAL" ]] || fail "nested backup paths are not allowed"
BACKUP_ID="$(basename "$BACKUP_DIR")"
[[ "$BACKUP_ID" =~ ^20[0-9]{6}T[0-9]{6}Z-([0-9a-f]{7,40}|unknown)$ ]] || fail "invalid backup directory name"

require_command docker
require_command python3
[[ -s "$MINIO_ROOT_PASSWORD_FILE" ]] || fail "MinIO password file is missing or empty: $MINIO_ROOT_PASSWORD_FILE"

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

for required in COMPLETE manifest.json SHA256SUMS postgres.dump minio-index.tsv config/docker-compose.yml; do
  [[ -f "$BACKUP_DIR/$required" ]] || fail "required backup file is missing: $required"
done
if find "$BACKUP_DIR" -type l -print -quit | grep -q .; then
  fail "backup contains symbolic links"
fi
[[ "$(cat "$BACKUP_DIR/COMPLETE")" == "$BACKUP_ID" ]] || fail "COMPLETE marker does not match backup ID"

# Strictly validate the manifest and checksum filenames before opening any
# payload. This prevents traversal through a forged checksum or manifest file.
python3 - "$BACKUP_DIR" "$BACKUP_ID" "$MINIO_BUCKET" <<'PY'
import json
import pathlib
import re
import sys
import tarfile

root = pathlib.Path(sys.argv[1])
backup_id = sys.argv[2]
target_bucket = sys.argv[3]
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
if manifest.get("schema_version") != 2 or manifest.get("kind") != "atlas-production-logical-backup":
    raise SystemExit("unsupported production backup schema")
if manifest.get("backup_id") != backup_id:
    raise SystemExit("manifest backup ID mismatch")
source_project = manifest.get("source_compose_project", "")
if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", source_project):
    raise SystemExit("invalid source Compose project in manifest")
if manifest.get("postgres", {}).get("format") != "custom":
    raise SystemExit("PostgreSQL backup is not custom format")
if manifest.get("minio", {}).get("transport") != "s3-object-api":
    raise SystemExit("MinIO backup was not produced through the object API")
if manifest.get("minio", {}).get("bucket") != target_bucket:
    raise SystemExit("target MinIO bucket differs from backup manifest")
if manifest.get("redis", {}).get("included") is not False:
    raise SystemExit("refusing a backup that includes Redis state")

allowed = {"postgres.dump", "minio-index.tsv", "manifest.json", "agent-code.tar.gz"}
allowed.update(
    str(path.relative_to(root)) for path in (root / "config").glob("*") if path.is_file()
)
checksum_lines = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
seen = set()
for line in checksum_lines:
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._/-]+)", line)
    if not match:
        raise SystemExit("invalid SHA256SUMS record")
    name = match.group(2)
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or name not in allowed:
        raise SystemExit(f"unsafe or unexpected checksum target: {name}")
    if name in seen or not (root / name).is_file():
        raise SystemExit(f"duplicate or missing checksum target: {name}")
    seen.add(name)
required = {"postgres.dump", "minio-index.tsv", "manifest.json", "config/docker-compose.yml"}
if not required.issubset(seen):
    raise SystemExit("SHA256SUMS does not cover every required payload")

archive = root / "agent-code.tar.gz"
if manifest.get("agent_code", {}).get("included") != archive.exists():
    raise SystemExit("agent-code manifest/payload mismatch")
if archive.exists():
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle.getmembers():
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(f"unsafe agent-code archive path: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise SystemExit(f"unsupported agent-code archive entry: {member.name}")
PY

verify_top_level_checksums "$BACKUP_DIR"
for service in postgres redis minio; do
  require_compose_service "$service"
done
VERIFY_ROOT="$(mktemp -d "$BACKUP_ROOT_REAL/.restore-verify-$BACKUP_ID.XXXXXX")"
AGENT_STAGE=""
AGENT_OLD=""
cleanup() {
  [[ -z "${VERIFY_ROOT:-}" || ! -d "$VERIFY_ROOT" ]] || rm -rf -- "$VERIFY_ROOT"
  [[ -z "${AGENT_STAGE:-}" || ! -d "$AGENT_STAGE" ]] || rm -rf -- "$AGENT_STAGE"
}
trap cleanup EXIT

build_object_index "$BACKUP_DIR/minio" "$VERIFY_ROOT/source-index.tsv"
cmp "$BACKUP_DIR/minio-index.tsv" "$VERIFY_ROOT/source-index.tsv"
prod_compose exec -T postgres pg_restore --list < "$BACKUP_DIR/postgres.dump" >/dev/null

if [[ "${2:-}" != "--apply" ]]; then
  echo "Backup verification passed: $BACKUP_ID"
  echo "No data was changed. To restore, re-run with --apply and:"
  echo "ATLAS_COMPOSE_PROJECT=<exact-compose-project>"
  echo "ATLAS_RESTORE_TARGET=<same-exact-compose-project>"
  echo "ATLAS_RESTORE_CONFIRM=RESTORE:<same-exact-compose-project>:$BACKUP_ID"
  exit 0
fi

RESTORE_TARGET="${ATLAS_RESTORE_TARGET:-}"
[[ "$RESTORE_TARGET" =~ ^[a-z0-9][a-z0-9-]{1,62}$ ]] || \
  fail "set ATLAS_RESTORE_TARGET to an explicit environment name (for example production or isolated-dr-20260716)"
[[ "$RESTORE_TARGET" == "$ATLAS_COMPOSE_PROJECT" ]] || \
  fail "ATLAS_RESTORE_TARGET must exactly match ATLAS_COMPOSE_PROJECT ($ATLAS_COMPOSE_PROJECT)"
[[ "${ATLAS_RESTORE_CONFIRM:-}" == "RESTORE:$RESTORE_TARGET:$BACKUP_ID" ]] || \
  fail "set ATLAS_RESTORE_CONFIRM=RESTORE:$RESTORE_TARGET:$BACKUP_ID for this target and exact backup"

# PostgreSQL restore is all-or-nothing. Owner/ACL state is not imported.
prod_compose exec -T postgres pg_restore \
  -U atlas -d atlas \
  --clean --if-exists --exit-on-error --single-transaction \
  --no-owner --no-privileges < "$BACKUP_DIR/postgres.dump"
prod_compose exec -T postgres psql -v ON_ERROR_STOP=1 -U atlas -d atlas -c 'ANALYZE' >/dev/null

# Restore through S3 and then download every object to recompute the content
# index. No MinIO private volume is read or written by this script.
MINIO_ID="$(prod_compose ps -q minio)"
MINIO_NETWORK="$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$MINIO_ID" | head -n 1)"
[[ -n "$MINIO_NETWORK" ]] || fail "could not determine the MinIO Compose network"
install -d -m 700 "$VERIFY_ROOT/download"
docker run --rm \
  --network "$MINIO_NETWORK" \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --env MC_CONFIG_DIR=/tmp/.mc \
  --env MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  --env MINIO_BUCKET="$MINIO_BUCKET" \
  --volume "$MINIO_ROOT_PASSWORD_FILE:/run/secrets/minio_password:ro" \
  --volume "$BACKUP_DIR/minio:/backup:ro" \
  --volume "$VERIFY_ROOT/download:/verify" \
  --entrypoint /bin/sh "$MINIO_MC_IMAGE" -ec '
    password="$(cat /run/secrets/minio_password)"
    mc alias set atlas http://minio:9000 "$MINIO_ROOT_USER" "$password" >/dev/null
    mc mb --ignore-existing "atlas/$MINIO_BUCKET" >/dev/null
    mc rm --recursive --force "atlas/$MINIO_BUCKET" >/dev/null 2>&1 || true
    mc mirror --overwrite --preserve /backup "atlas/$MINIO_BUCKET" >/dev/null
    mc mirror --overwrite --preserve "atlas/$MINIO_BUCKET" /verify >/dev/null
  '
build_object_index "$VERIFY_ROOT/download" "$VERIFY_ROOT/restored-index.tsv"
cmp "$BACKUP_DIR/minio-index.tsv" "$VERIFY_ROOT/restored-index.tsv"

# Redis is never restored. Flush every database and prove that neither sessions,
# TTL memory nor one-time authorization tickets survived the recovery.
# shellcheck disable=SC2016
REDIS_KEYSPACE="$(prod_compose exec -T redis sh -ec '
  REDISCLI_AUTH="$(cat /run/secrets/redis_password)"
  export REDISCLI_AUTH
  redis-cli FLUSHALL SYNC >/dev/null
  redis-cli --raw DBSIZE
  redis-cli --raw INFO keyspace
' | tr -d '\r')"
[[ "$(printf '%s\n' "$REDIS_KEYSPACE" | head -n 1)" == "0" ]] || fail "Redis DBSIZE is not zero after FLUSHALL"
if printf '%s\n' "$REDIS_KEYSPACE" | tail -n +2 | grep -Eq '^db[0-9]+:'; then
  fail "Redis keyspace is not empty after FLUSHALL"
fi

if [[ -f "$BACKUP_DIR/agent-code.tar.gz" ]]; then
  AGENT_PARENT="$(dirname "$AGENT_CODE_ROOT")"
  AGENT_NAME="$(basename "$AGENT_CODE_ROOT")"
  install -d -m 750 "$AGENT_PARENT"
  AGENT_STAGE="$AGENT_PARENT/.${AGENT_NAME}.restore.$BACKUP_ID.$$"
  AGENT_OLD="$AGENT_PARENT/.${AGENT_NAME}.previous.$BACKUP_ID.$$"
  install -d -m 750 "$AGENT_STAGE"
  tar -C "$AGENT_STAGE" --no-same-owner --no-same-permissions -xzf "$BACKUP_DIR/agent-code.tar.gz"
  if [[ -e "$AGENT_CODE_ROOT" ]]; then
    mv "$AGENT_CODE_ROOT" "$AGENT_OLD"
  fi
  if ! mv "$AGENT_STAGE" "$AGENT_CODE_ROOT"; then
    [[ ! -e "$AGENT_CODE_ROOT" && -e "$AGENT_OLD" ]] && mv "$AGENT_OLD" "$AGENT_CODE_ROOT"
    fail "could not atomically install restored agent-code"
  fi
  AGENT_STAGE=""
  [[ ! -d "$AGENT_OLD" ]] || rm -rf -- "$AGENT_OLD"
  AGENT_OLD=""
fi

echo "Atlas restore complete: PostgreSQL and MinIO restored; Redis reset and verified empty."
echo "Non-sensitive configuration is retained in $BACKUP_DIR/config for manual comparison only."
