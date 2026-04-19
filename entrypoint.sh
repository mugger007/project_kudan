#!/usr/bin/env bash
set -euo pipefail

APP_USER="${APP_USER:-appuser}"
APP_DIR="/app"
HEALTH_PORT="${HEALTH_PORT:-8080}"

log() {
  printf '%s | entrypoint | %s\n' "$(date -u +'%Y-%m-%d %H:%M:%S')" "$*"
}

prepare_filesystem() {
  mkdir -p /data "${APP_DIR}/logs"
  chown -R "${APP_USER}:${APP_USER}" /data "${APP_DIR}/logs" || true
}

main() {
  prepare_filesystem

  export DB_PATH="${DB_PATH:-/data/kudan.db}"
  export HEALTH_HOST="${HEALTH_HOST:-0.0.0.0}"
  export HEALTH_PORT

  if id "${APP_USER}" >/dev/null 2>&1; then
    log "Launching Kudan as non-root user ${APP_USER}."
    exec gosu "${APP_USER}" python "${APP_DIR}/main.py"
  fi

  log "User ${APP_USER} not found; launching as current user."
  exec python "${APP_DIR}/main.py"
}

main "$@"
