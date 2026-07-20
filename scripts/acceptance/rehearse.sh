#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/acceptance/lib.sh
source "$SCRIPT_DIR/lib.sh"
configure_acceptance_environment

SOURCE_PROJECT="$ATLAS_ACCEPTANCE_PROJECT"
TARGET_PROJECT="${ATLAS_ACCEPTANCE_RESTORE_PROJECT:-atlas-acceptance-restore-$(date -u +%Y%m%d%H%M%S)}"
case "$TARGET_PROJECT" in
  atlas-acceptance-[a-z0-9]* ) ;;
  * ) echo "invalid isolated restore project: $TARGET_PROJECT" >&2; exit 52 ;;
esac
if [[ "$TARGET_PROJECT" == "$SOURCE_PROJECT" ]]; then
  echo "restore rehearsal target must differ from source" >&2
  exit 53
fi

target_manage() {
  env \
    -u ATLAS_ACCEPTANCE_ROOT \
    -u POSTGRES_PASSWORD_FILE \
    -u REDIS_PASSWORD_FILE \
    -u MINIO_ROOT_PASSWORD_FILE \
    ATLAS_ACCEPTANCE_PROJECT="$TARGET_PROJECT" \
    "$SCRIPT_DIR/manage.sh" "$@"
}

target_restore() {
  env \
    -u ATLAS_ACCEPTANCE_ROOT \
    -u POSTGRES_PASSWORD_FILE \
    -u REDIS_PASSWORD_FILE \
    -u MINIO_ROOT_PASSWORD_FILE \
    ATLAS_ACCEPTANCE_PROJECT="$TARGET_PROJECT" \
    "$SCRIPT_DIR/restore.sh" "$@"
}

echo "[preflight] reserving a clean isolated restore target: $TARGET_PROJECT" >&2
target_manage init
target_manage clean

echo "[1/6] starting isolated source project: $SOURCE_PROJECT" >&2
ATLAS_ACCEPTANCE_PROJECT="$SOURCE_PROJECT" "$SCRIPT_DIR/manage.sh" up

echo "[2/6] writing PostgreSQL, pgvector, MinIO and ephemeral Redis sentinels" >&2
ATLAS_ACCEPTANCE_PROJECT="$SOURCE_PROJECT" "$SCRIPT_DIR/manage.sh" seed

echo "[3/6] creating logical/object backup" >&2
BACKUP_OUTPUT="$(ATLAS_ACCEPTANCE_PROJECT="$SOURCE_PROJECT" "$SCRIPT_DIR/backup.sh")"
BACKUP_DIR="$(printf '%s\n' "$BACKUP_OUTPUT" | tail -n 1)"
if [[ ! -f "$BACKUP_DIR/COMPLETE" ]]; then
  echo "backup did not produce a completion marker" >&2
  exit 54
fi

echo "[4/6] stopping source so recovery cannot read source services" >&2
ATLAS_ACCEPTANCE_PROJECT="$SOURCE_PROJECT" "$SCRIPT_DIR/manage.sh" down

echo "[5/6] creating fresh isolated restore project: $TARGET_PROJECT" >&2
target_manage clean
target_manage up

echo "[6/6] restoring PostgreSQL/MinIO while deliberately resetting Redis" >&2
REPORT="$(target_restore "$BACKUP_DIR" | tail -n 1)"
python3 - "$REPORT" <<'PY'
import json
import pathlib
import sys

report = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert report["result"] == "pass", report
assert report["postgres"]["restored"] is True, report
assert report["postgres"]["sentinel"] == "postgres-durable", report
assert report["minio"]["content_index_match"] is True, report
assert report["minio"]["object_count"] >= 1, report
assert report["redis"]["restored"] is False, report
assert report["redis"]["empty_after_restore"] is True, report
PY

echo "restore rehearsal passed; source remains stopped and target remains available for inspection" >&2
printf '%s\n' "$REPORT"
