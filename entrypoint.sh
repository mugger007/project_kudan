#!/usr/bin/env bash
set -euo pipefail

APP_USER="${APP_USER:-appuser}"
APP_DIR="/app"
HEALTH_PORT="${HEALTH_PORT:-8080}"

log() {
  printf '%s | entrypoint | %s\n' "$(date -u +'%Y-%m-%d %H:%M:%S')" "$*"
}

start_vpn_if_enabled() {
  if [[ "${VPN_ENABLED:-false}" != "true" ]]; then
    log "VPN disabled; proceeding without OpenVPN."
    return
  fi

  OVPN_EXE="${OPENVPN_EXECUTABLE:-openvpn}"
  OVPN_CFG="${OPENVPN_CONFIG_FILE:-}"
  OVPN_AUTH="${OPENVPN_AUTH_FILE:-}"

  if [[ -z "${OVPN_CFG}" ]]; then
    log "VPN_ENABLED=true but OPENVPN_CONFIG_FILE is empty."
    exit 1
  fi

  if [[ ! -f "${OVPN_CFG}" ]]; then
    log "OpenVPN config file not found: ${OVPN_CFG}"
    exit 1
  fi

  if ! command -v "${OVPN_EXE}" >/dev/null 2>&1 && [[ ! -x "${OVPN_EXE}" ]]; then
    log "OpenVPN executable not found: ${OVPN_EXE}"
    exit 1
  fi

  if [[ "$(basename "${OVPN_EXE}" | tr '[:upper:]' '[:lower:]')" == *"openvpn-gui"* ]]; then
    log "Starting OpenVPN GUI connection using config ${OVPN_CFG}."
    if ! "${OVPN_EXE}" --command connect "${OVPN_CFG}"; then
      log "Initial openvpn-gui connect failed; bot will rely on internal reconnect loop."
    fi
    return
  fi

  log "Starting OpenVPN daemon with config ${OVPN_CFG}."
  OVPN_CMD=("${OVPN_EXE}" --config "${OVPN_CFG}" --daemon)
  if [[ -n "${OVPN_AUTH}" ]]; then
    OVPN_CMD+=(--auth-user-pass "${OVPN_AUTH}")
  fi
  if ! "${OVPN_CMD[@]}"; then
    log "Initial OpenVPN daemon start failed; bot will rely on internal reconnect loop."
  fi
}

prepare_filesystem() {
  mkdir -p /data "${APP_DIR}/logs"
  chown -R "${APP_USER}:${APP_USER}" /data "${APP_DIR}/logs" || true
}

main() {
  prepare_filesystem
  start_vpn_if_enabled

  export DB_PATH="${DB_PATH:-/data/kudan.db}"
  export HEALTH_HOST="${HEALTH_HOST:-0.0.0.0}"
  export HEALTH_PORT

  if [[ "${VPN_ENABLED:-false}" == "true" ]]; then
    log "Launching Kudan as root (VPN mode may require elevated networking)."
    exec python "${APP_DIR}/main.py"
  fi

  if id "${APP_USER}" >/dev/null 2>&1; then
    log "Launching Kudan as non-root user ${APP_USER}."
    exec gosu "${APP_USER}" python "${APP_DIR}/main.py"
  fi

  log "User ${APP_USER} not found; launching as current user."
  exec python "${APP_DIR}/main.py"
}

main "$@"
