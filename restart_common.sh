#!/usr/bin/env bash

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
LOG_DIR="$ROOT_DIR/logs"
RUN_DIR="$ROOT_DIR/.run"

mkdir -p "$LOG_DIR" "$RUN_DIR"

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

start_backend() {
  echo "backend: starting on http://$BACKEND_HOST:$BACKEND_PORT"
  (
    cd "$ROOT_DIR/backend"
    nohup env PYTHONPATH=. python3 -m uvicorn app.main:app \
      --host "$BACKEND_HOST" \
      --port "$BACKEND_PORT" \
      > "$LOG_DIR/backend.log" 2>&1 &
    local pid="$!"
    echo "$pid" > "$RUN_DIR/backend.pid"
    disown "$pid" 2>/dev/null || true
  )
}

start_frontend() {
  echo "frontend: starting on http://$FRONTEND_HOST:$FRONTEND_PORT"
  (
    cd "$ROOT_DIR/frontend"
    nohup npm run dev -- \
      --host "$FRONTEND_HOST" \
      --port "$FRONTEND_PORT" \
      > "$LOG_DIR/frontend.log" 2>&1 &
    local pid="$!"
    echo "$pid" > "$RUN_DIR/frontend.pid"
    disown "$pid" 2>/dev/null || true
  )
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempts="${3:-30}"

  for _ in $(seq 1 "$attempts"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "$label: ready at $url"
      return 0
    fi
    sleep 1
  done

  echo "$label: did not become ready at $url; check logs"
  return 1
}

restart_backend() {
  stop_port "$BACKEND_PORT" "backend"
  start_backend
  wait_for_url "http://$BACKEND_HOST:$BACKEND_PORT/api/reference-topologies" "backend"
}

restart_frontend() {
  stop_port "$FRONTEND_PORT" "frontend"
  start_frontend
  wait_for_url "http://$FRONTEND_HOST:$FRONTEND_PORT" "frontend"
}

print_backend_summary() {
  echo "Backend restarted:"
  echo "  API:     http://$BACKEND_HOST:$BACKEND_PORT"
  echo "  Logs:    $LOG_DIR/backend.log"
}

print_frontend_summary() {
  echo "Frontend restarted:"
  echo "  UI:      http://$FRONTEND_HOST:$FRONTEND_PORT"
  echo "  Logs:    $LOG_DIR/frontend.log"
}

print_full_summary() {
  echo "Tool restarted:"
  echo "  UI:      http://$FRONTEND_HOST:$FRONTEND_PORT"
  echo "  API:     http://$BACKEND_HOST:$BACKEND_PORT"
  echo "  Logs:    $LOG_DIR/backend.log"
  echo "           $LOG_DIR/frontend.log"
}
