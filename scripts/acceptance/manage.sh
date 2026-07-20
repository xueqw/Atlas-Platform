#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/acceptance/lib.sh
source "$SCRIPT_DIR/lib.sh"
configure_acceptance_environment

usage() {
  cat <<'EOF'
Usage: scripts/acceptance/manage.sh COMMAND [ARGS]

Commands:
  init                 create acceptance-only secret files
  up                   initialize and start PostgreSQL, Redis and MinIO
  wait                 wait for all three services to become healthy
  status               show Compose service state
  urls                 print local acceptance endpoints (no credentials)
  seed                 write deterministic PG/MinIO/Redis restore sentinels
  backup               create a verified backup and print its directory
  restore BACKUP_DIR   restore into the current acceptance project
  rehearse             run source backup -> isolated target restore
  down                 stop services without deleting volumes
  clean                delete this acceptance project's containers and volumes

Stateful commands reject any project not named atlas-acceptance[-...].
Set ATLAS_ACCEPTANCE_PROJECT to select another isolated acceptance project.
EOF
}

initialize_secrets() {
  require_command openssl
  install -d -m 700 "$ATLAS_ACCEPTANCE_ROOT/secrets"
  local secret
  for secret in "$POSTGRES_PASSWORD_FILE" "$REDIS_PASSWORD_FILE" "$MINIO_ROOT_PASSWORD_FILE"; do
    if [[ ! -s "$secret" ]]; then
      umask 077
      openssl rand -hex 24 > "$secret"
    fi
    chmod 600 "$secret"
  done
}

wait_for_services() {
  require_secret_files
  compose up --detach --wait --wait-timeout "${ATLAS_ACCEPTANCE_WAIT_SECONDS:-120}" postgres redis minio
}

seed_recovery_sentinels() {
  require_secret_files
  wait_for_services
  compose exec -T postgres psql -v ON_ERROR_STOP=1 -U atlas -d atlas <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS atlas_acceptance_sentinel (
  key text PRIMARY KEY,
  payload text NOT NULL,
  embedding vector(3) NOT NULL
);
INSERT INTO atlas_acceptance_sentinel (key, payload, embedding)
VALUES ('restore-sentinel', 'postgres-durable', '[1,0,0]')
ON CONFLICT (key) DO UPDATE
SET payload = EXCLUDED.payload, embedding = EXCLUDED.embedding;
SQL

  install -d -m 700 "$ATLAS_ACCEPTANCE_ROOT/seed"
  printf 'atlas-minio-restore-sentinel\n' > "$ATLAS_ACCEPTANCE_ROOT/seed/sentinel.txt"
  docker run --rm \
    --network "$(compose_network)" \
    --user "$(id -u):$(id -g)" \
    --env HOME=/tmp \
    --env MC_CONFIG_DIR=/tmp/.mc \
    --env MINIO_ROOT_USER="$MINIO_ROOT_USER" \
    --env MINIO_BUCKET="$MINIO_BUCKET" \
    --volume "$MINIO_ROOT_PASSWORD_FILE:/run/secrets/minio_password:ro" \
    --volume "$ATLAS_ACCEPTANCE_ROOT/seed:/seed:ro" \
    --entrypoint /bin/sh "$MINIO_MC_IMAGE" -ec '
      password="$(cat /run/secrets/minio_password)"
      mc alias set atlas http://minio:9000 "$MINIO_ROOT_USER" "$password" >/dev/null
      mc mb --ignore-existing "atlas/$MINIO_BUCKET" >/dev/null
      mc cp /seed/sentinel.txt "atlas/$MINIO_BUCKET/workspaces/atlas-acceptance/acceptance/sentinel.txt" >/dev/null
    '

  # Expansion is intentionally performed by the Redis container's shell.
  # shellcheck disable=SC2016
  compose exec -T redis sh -ec '
    REDISCLI_AUTH="$(cat /run/secrets/redis_password)"
    export REDISCLI_AUTH
    redis-cli SETEX atlas:acceptance:ephemeral-ticket 3600 must-not-be-restored >/dev/null
  '
  echo "acceptance restore sentinels written"
}

command="${1:-}"
case "$command" in
  init)
    initialize_secrets
    ;;
  up)
    initialize_secrets
    wait_for_services
    ;;
  wait)
    wait_for_services
    ;;
  status)
    compose ps
    ;;
  urls)
    printf 'PostgreSQL: 127.0.0.1:%s\nRedis:      127.0.0.1:%s\nMinIO API:  http://127.0.0.1:%s\nMinIO UI:   http://127.0.0.1:%s\n' \
      "$POSTGRES_HOST_PORT" "$REDIS_HOST_PORT" "$MINIO_HOST_PORT" "$MINIO_CONSOLE_HOST_PORT"
    ;;
  seed)
    seed_recovery_sentinels
    ;;
  backup)
    require_secret_files
    exec "$SCRIPT_DIR/backup.sh"
    ;;
  restore)
    [[ $# -eq 2 ]] || { usage >&2; exit 2; }
    require_secret_files
    exec "$SCRIPT_DIR/restore.sh" "$2"
    ;;
  rehearse)
    initialize_secrets
    exec "$SCRIPT_DIR/rehearse.sh"
    ;;
  down)
    compose down --remove-orphans
    ;;
  clean)
    compose down --volumes --remove-orphans
    rm -rf "$ATLAS_ACCEPTANCE_ROOT/restore-verify"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
