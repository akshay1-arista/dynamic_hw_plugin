#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-5401}"
FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${FRONTEND_PORT:-5400}"
PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1}"
HEALTHCHECK_HOST="${HEALTHCHECK_HOST:-127.0.0.1}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv-production}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SYSTEMD_ENV_FILE="${SYSTEMD_ENV_FILE:-/etc/default/dynamic-hw-topology}"
BACKEND_SERVICE_NAME="${BACKEND_SERVICE_NAME:-dynamic-hw-topology-backend}"
FRONTEND_SERVICE_NAME="${FRONTEND_SERVICE_NAME:-dynamic-hw-topology-frontend}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$USER}}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn "$SERVICE_USER")}"
API_BASE_URL="${API_BASE_URL:-}"
FRONTEND_ORIGIN="${FRONTEND_ORIGIN:-http://${PUBLIC_HOST}:${FRONTEND_PORT}}"
CORS_ALLOWED_ORIGINS="${CORS_ALLOWED_ORIGINS:-$FRONTEND_ORIGIN}"
TEMPLATE_DIR="$ROOT_DIR/deploy/systemd"
FRONTEND_DIST_DIR="${FRONTEND_DIST_DIR:-$ROOT_DIR/frontend/dist}"
FRONTEND_NODE_MIN_VERSION="${FRONTEND_NODE_MIN_VERSION:-20.19.0}"
AUTO_INSTALL_SYSTEM_DEPS="${AUTO_INSTALL_SYSTEM_DEPS:-1}"
PACKAGE_MANAGER="${PACKAGE_MANAGER:-}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}

trap cleanup EXIT

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 1
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo or as root so it can install systemd units." >&2
    exit 1
  fi
}

stop_port() {
  local port="$1"
  local label="$2"
  local pids

  pids="$(lsof -ti "tcp:$port" 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    echo "$label: no process on port $port"
    return
  fi

  echo "$label: stopping process(es) on port $port: $pids"
  kill $pids 2>/dev/null || true
  sleep 1

  pids="$(lsof -ti "tcp:$port" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "$label: force stopping process(es) on port $port: $pids"
    kill -9 $pids 2>/dev/null || true
  fi
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempts="${3:-45}"

  for _ in $(seq 1 "$attempts"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "$label: ready at $url"
      return 0
    fi
    sleep 1
  done

  echo "$label: did not become ready at $url; check logs" >&2
  return 1
}

detect_package_manager() {
  if [[ -n "$PACKAGE_MANAGER" ]]; then
    return 0
  fi

  if command_exists apt-get; then
    PACKAGE_MANAGER="apt-get"
    return 0
  fi

  if command_exists dnf; then
    PACKAGE_MANAGER="dnf"
    return 0
  fi

  echo "Unable to auto-install system dependencies: supported package managers are apt-get and dnf." >&2
  return 1
}

install_system_packages() {
  local packages=("$@")

  if [[ "${#packages[@]}" -eq 0 ]]; then
    return 0
  fi

  detect_package_manager

  case "$PACKAGE_MANAGER" in
    apt-get)
      echo "system: installing packages via apt-get: ${packages[*]}"
      DEBIAN_FRONTEND=noninteractive apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
      ;;
    dnf)
      echo "system: installing packages via dnf: ${packages[*]}"
      dnf install -y "${packages[@]}"
      ;;
    *)
      echo "Unsupported package manager: $PACKAGE_MANAGER" >&2
      return 1
      ;;
  esac
}

python_venv_ready() {
  if ! command_exists "$PYTHON_BIN"; then
    return 1
  fi

  "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import venv
PY
}

normalize_semver() {
  local version="${1#v}"
  local major minor patch

  IFS='.' read -r major minor patch <<<"$version"
  major="${major:-0}"
  minor="${minor:-0}"
  patch="${patch:-0}"

  printf '%d %d %d\n' "$major" "$minor" "$patch"
}

node_version() {
  if ! command_exists node; then
    return 1
  fi

  node --version 2>/dev/null
}

node_version_ready() {
  local current required
  local current_major current_minor current_patch
  local required_major required_minor required_patch

  current="$(node_version)" || return 1
  required="$FRONTEND_NODE_MIN_VERSION"

  read -r current_major current_minor current_patch <<<"$(normalize_semver "$current")"
  read -r required_major required_minor required_patch <<<"$(normalize_semver "$required")"

  if (( current_major > required_major )); then
    return 0
  fi
  if (( current_major < required_major )); then
    return 1
  fi
  if (( current_minor > required_minor )); then
    return 0
  fi
  if (( current_minor < required_minor )); then
    return 1
  fi
  if (( current_patch >= required_patch )); then
    return 0
  fi
  return 1
}

frontend_can_build_locally() {
  command_exists npm && node_version_ready
}

append_unique() {
  local value="$1"
  shift
  local existing
  for existing in "$@"; do
    if [[ "$existing" == "$value" ]]; then
      return 0
    fi
  done
  return 1
}

bootstrap_system_dependencies() {
  local packages=()
  local frontend_needs_build=0

  if [[ "$AUTO_INSTALL_SYSTEM_DEPS" != "1" ]]; then
    echo "system: auto-install disabled; using host-provided dependencies"
    return 0
  fi

  if ! command_exists systemctl; then
    echo "Missing required command: systemctl" >&2
    echo "This deployment script requires a systemd-based host." >&2
    exit 1
  fi

  if ! command_exists install; then
    if ! append_unique coreutils "${packages[@]}"; then
      packages+=("coreutils")
    fi
  fi

  if ! command_exists curl; then
    if ! append_unique curl "${packages[@]}"; then
      packages+=("curl")
    fi
  fi

  if ! command_exists lsof; then
    if ! append_unique lsof "${packages[@]}"; then
      packages+=("lsof")
    fi
  fi

  if ! command_exists "$PYTHON_BIN"; then
    if [[ "$PYTHON_BIN" != "python3" ]]; then
      echo "Missing required command: $PYTHON_BIN" >&2
      echo "Auto-install only supports the default PYTHON_BIN=python3." >&2
      exit 1
    fi

    if ! append_unique python3 "${packages[@]}"; then
      packages+=("python3")
    fi
  fi

  if [[ ! -f "$FRONTEND_DIST_DIR/index.html" ]] && ! command_exists npm; then
    frontend_needs_build=1
    if ! append_unique nodejs "${packages[@]}"; then
      packages+=("nodejs")
    fi
    if ! append_unique npm "${packages[@]}"; then
      packages+=("npm")
    fi
  fi

  if ! python_venv_ready; then
    case "${PACKAGE_MANAGER:-auto}" in
      apt-get|auto)
        if ! append_unique python3-venv "${packages[@]}"; then
          packages+=("python3-venv")
        fi
        ;;
      dnf)
        if ! append_unique python3 "${packages[@]}"; then
          packages+=("python3")
        fi
        ;;
    esac
  fi

  if [[ "${#packages[@]}" -gt 0 ]]; then
    install_system_packages "${packages[@]}"
  fi

  require_command "$PYTHON_BIN"
  require_command curl
  require_command lsof
  require_command install
  require_command systemctl

  if ! python_venv_ready; then
    echo "Python venv support is still unavailable after installing dependencies." >&2
    exit 1
  fi

  if [[ "$frontend_needs_build" -eq 1 ]] && ! command_exists npm; then
    echo "Frontend build is required, but npm is still unavailable after dependency installation." >&2
    exit 1
  fi
}

prepare_backend() {
  echo "backend: preparing virtual environment"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -r "$ROOT_DIR/backend/requirements.txt"
}

prepare_frontend() {
  if frontend_can_build_locally; then
    echo "frontend: installing packages"
    (
      cd "$ROOT_DIR/frontend"
      npm ci
    )

    if [[ -n "$API_BASE_URL" ]]; then
      echo "frontend: building with API base $API_BASE_URL"
    else
      echo "frontend: building with same-origin /api proxy"
    fi
    (
      cd "$ROOT_DIR/frontend"
      VITE_API_BASE_URL="$API_BASE_URL" npm run build
    )
    return
  fi

  if command_exists npm && ! node_version_ready; then
    echo "frontend: local node $(node_version) is too old; need >= $FRONTEND_NODE_MIN_VERSION for this Vite build"
  fi

  if [[ -f "$FRONTEND_DIST_DIR/index.html" ]]; then
    echo "frontend: using prebuilt assets from $FRONTEND_DIST_DIR"
    return
  fi

  if ! command_exists npm; then
    echo "frontend: npm not found and no prebuilt assets exist at $FRONTEND_DIST_DIR" >&2
    echo "frontend: install npm or commit/build frontend/dist before deploying" >&2
    exit 1
  fi

  echo "frontend: local node $(node_version) does not satisfy the required version >= $FRONTEND_NODE_MIN_VERSION" >&2
  echo "frontend: upgrade node on the host or commit/build frontend/dist before deploying" >&2
  exit 1
}

render_template() {
  local template_path="$1"
  local output_path="$2"

  TEMPLATE_PATH="$template_path" OUTPUT_PATH="$output_path" \
  APP_ROOT="$ROOT_DIR" \
  VENV_PATH="$VENV_DIR" \
  ENV_FILE_PATH="$SYSTEMD_ENV_FILE" \
  SERVICE_USER_VALUE="$SERVICE_USER" \
  SERVICE_GROUP_VALUE="$SERVICE_GROUP" \
  BACKEND_HOST_VALUE="$BACKEND_HOST" \
  BACKEND_PORT_VALUE="$BACKEND_PORT" \
  FRONTEND_HOST_VALUE="$FRONTEND_HOST" \
  FRONTEND_PORT_VALUE="$FRONTEND_PORT" \
  PUBLIC_HOST_VALUE="$PUBLIC_HOST" \
  HEALTHCHECK_HOST_VALUE="$HEALTHCHECK_HOST" \
  CORS_ALLOWED_ORIGINS_VALUE="$CORS_ALLOWED_ORIGINS" \
  BACKEND_SERVICE_NAME_VALUE="$BACKEND_SERVICE_NAME" \
  FRONTEND_SERVICE_NAME_VALUE="$FRONTEND_SERVICE_NAME" \
  "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import os

content = Path(os.environ["TEMPLATE_PATH"]).read_text()
for key, value in {
    "__APP_ROOT__": os.environ["APP_ROOT"],
    "__VENV_DIR__": os.environ["VENV_PATH"],
    "__ENV_FILE__": os.environ["ENV_FILE_PATH"],
    "__SERVICE_USER__": os.environ["SERVICE_USER_VALUE"],
    "__SERVICE_GROUP__": os.environ["SERVICE_GROUP_VALUE"],
    "__BACKEND_HOST__": os.environ["BACKEND_HOST_VALUE"],
    "__BACKEND_PORT__": os.environ["BACKEND_PORT_VALUE"],
    "__FRONTEND_HOST__": os.environ["FRONTEND_HOST_VALUE"],
    "__FRONTEND_PORT__": os.environ["FRONTEND_PORT_VALUE"],
    "__PUBLIC_HOST__": os.environ["PUBLIC_HOST_VALUE"],
    "__HEALTHCHECK_HOST__": os.environ["HEALTHCHECK_HOST_VALUE"],
    "__CORS_ALLOWED_ORIGINS__": os.environ["CORS_ALLOWED_ORIGINS_VALUE"],
    "__BACKEND_SERVICE_NAME__": os.environ["BACKEND_SERVICE_NAME_VALUE"],
    "__FRONTEND_SERVICE_NAME__": os.environ["FRONTEND_SERVICE_NAME_VALUE"],
}.items():
    content = content.replace(key, value)
Path(os.environ["OUTPUT_PATH"]).write_text(content)
PY
}

install_service_units() {
  local backend_unit="$TMP_DIR/${BACKEND_SERVICE_NAME}.service"
  local frontend_unit="$TMP_DIR/${FRONTEND_SERVICE_NAME}.service"

  echo "systemd: rendering service units"
  render_template "$TEMPLATE_DIR/dynamic-hw-topology-backend.service.template" "$backend_unit"
  render_template "$TEMPLATE_DIR/dynamic-hw-topology-frontend.service.template" "$frontend_unit"

  echo "systemd: installing units into $SYSTEMD_UNIT_DIR"
  install -m 0644 "$backend_unit" "$SYSTEMD_UNIT_DIR/${BACKEND_SERVICE_NAME}.service"
  install -m 0644 "$frontend_unit" "$SYSTEMD_UNIT_DIR/${FRONTEND_SERVICE_NAME}.service"
}

restart_services() {
  echo "systemd: reloading units"
  systemctl daemon-reload
  systemctl enable "${BACKEND_SERVICE_NAME}.service" "${FRONTEND_SERVICE_NAME}.service"
  systemctl stop "${FRONTEND_SERVICE_NAME}.service" "${BACKEND_SERVICE_NAME}.service" >/dev/null 2>&1 || true

  stop_port "$BACKEND_PORT" "backend"
  stop_port "$FRONTEND_PORT" "frontend"

  echo "backend: starting on http://$PUBLIC_HOST:$BACKEND_PORT"
  systemctl restart "${BACKEND_SERVICE_NAME}.service"
  wait_for_url "http://$HEALTHCHECK_HOST:$BACKEND_PORT/api/reference-topologies" "backend"

  echo "frontend: starting on http://$PUBLIC_HOST:$FRONTEND_PORT"
  systemctl restart "${FRONTEND_SERVICE_NAME}.service"
  wait_for_url "http://$HEALTHCHECK_HOST:$FRONTEND_PORT" "frontend"
}

print_summary() {
  echo
  echo "Production deployment completed:"
  echo "  Frontend: http://$PUBLIC_HOST:$FRONTEND_PORT"
  echo "  Backend:  http://$PUBLIC_HOST:$BACKEND_PORT"
  echo "  Frontend service: ${FRONTEND_SERVICE_NAME}.service"
  echo "  Backend service:  ${BACKEND_SERVICE_NAME}.service"
  echo "  Status:  systemctl status ${BACKEND_SERVICE_NAME}.service ${FRONTEND_SERVICE_NAME}.service"
  echo "  Logs:    journalctl -u ${BACKEND_SERVICE_NAME}.service -u ${FRONTEND_SERVICE_NAME}.service -f"
  echo "  Optional overrides: $SYSTEMD_ENV_FILE"
}

require_root
bootstrap_system_dependencies
prepare_backend
prepare_frontend
install_service_units
restart_services
print_summary
