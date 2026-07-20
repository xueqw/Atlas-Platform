#!/usr/bin/env bash

set -euo pipefail

ACCEPTANCE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATLAS_ROOT="$(cd "$ACCEPTANCE_SCRIPT_DIR/../.." && pwd)"
ATLAS_ACCEPTANCE_PROJECT="${ATLAS_ACCEPTANCE_PROJECT:-atlas-acceptance}"
ATLAS_ACCEPTANCE_ROOT="${ATLAS_ACCEPTANCE_ROOT:-$ATLAS_ROOT/.acceptance/$ATLAS_ACCEPTANCE_PROJECT}"
MINIO_ROOT_USER="${MINIO_ROOT_USER:-atlas}"
MINIO_BUCKET="${MINIO_BUCKET:-atlas}"
MINIO_MC_IMAGE="${MINIO_MC_IMAGE:-minio/mc:RELEASE.2025-04-16T18-13-26Z}"

assert_acceptance_project() {
  case "$ATLAS_ACCEPTANCE_PROJECT" in
    atlas-acceptance|atlas-acceptance-[a-z0-9]* ) ;;
    * )
      echo "refusing stateful operation for non-acceptance Compose project: $ATLAS_ACCEPTANCE_PROJECT" >&2
      echo "project must be atlas-acceptance or start with atlas-acceptance-" >&2
      exit 40
      ;;
  esac
  if [[ ! "$ATLAS_ACCEPTANCE_PROJECT" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "invalid acceptance project name: $ATLAS_ACCEPTANCE_PROJECT" >&2
    exit 41
  fi
}

configure_acceptance_environment() {
  assert_acceptance_project
  export POSTGRES_HOST_PORT="${POSTGRES_HOST_PORT:-15432}"
  export REDIS_HOST_PORT="${REDIS_HOST_PORT:-16379}"
  export MINIO_HOST_PORT="${MINIO_HOST_PORT:-19000}"
  export MINIO_CONSOLE_HOST_PORT="${MINIO_CONSOLE_HOST_PORT:-19001}"
  export POSTGRES_PASSWORD_FILE="${POSTGRES_PASSWORD_FILE:-$ATLAS_ACCEPTANCE_ROOT/secrets/postgres_password}"
  export REDIS_PASSWORD_FILE="${REDIS_PASSWORD_FILE:-$ATLAS_ACCEPTANCE_ROOT/secrets/redis_password}"
  export MINIO_ROOT_PASSWORD_FILE="${MINIO_ROOT_PASSWORD_FILE:-$ATLAS_ACCEPTANCE_ROOT/secrets/minio_root_password}"
  export MINIO_ROOT_USER MINIO_BUCKET
}

compose() {
  docker compose --project-name "$ATLAS_ACCEPTANCE_PROJECT" --file "$ATLAS_ROOT/docker-compose.yml" "$@"
}

compose_network() {
  printf '%s_default\n' "$ATLAS_ACCEPTANCE_PROJECT"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "required command is missing: $1" >&2
    exit 42
  fi
}

require_secret_files() {
  local secret
  for secret in "$POSTGRES_PASSWORD_FILE" "$REDIS_PASSWORD_FILE" "$MINIO_ROOT_PASSWORD_FILE"; do
    if [[ ! -s "$secret" ]]; then
      echo "required acceptance secret is missing or empty: $secret" >&2
      exit 43
    fi
  done
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

build_object_index() {
  local root="$1"
  local output="$2"
  local scratch="${output}.tmp"
  local file relative size digest
  : > "$scratch"
  while IFS= read -r -d '' file; do
    relative="${file#"$root"/}"
    size="$(wc -c < "$file" | tr -d ' ')"
    digest="$(sha256_file "$file")"
    printf '%s\t%s\t%s\n' "$digest" "$size" "$relative" >> "$scratch"
  done < <(find "$root" -type f -print0)
  LC_ALL=C sort "$scratch" > "$output"
  rm -f "$scratch"
}

verify_top_level_checksums() {
  local backup_dir="$1"
  local expected filename actual
  while read -r expected filename; do
    [[ -n "$expected" && -n "$filename" ]] || continue
    actual="$(sha256_file "$backup_dir/$filename")"
    if [[ "$actual" != "$expected" ]]; then
      echo "checksum mismatch: $filename" >&2
      exit 44
    fi
  done < "$backup_dir/SHA256SUMS"
}

utc_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}
