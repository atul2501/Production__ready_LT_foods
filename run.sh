#!/usr/bin/env bash
# Runs the whole service - API + worker + email ingester - and keeps it running: any
# service that crashes or exits is restarted automatically (with backoff), so one bad
# moment (Gmail dropping the connection, Postgres restarting, OOM) doesn't leave the
# system half-down. Works in Git Bash on Windows and on Linux/EC2.
#
#   ./run.sh            start everything in the foreground (Ctrl+C stops all)
#   ./run.sh stop       stop a running instance (e.g. from another terminal)
#   ./run.sh status     show what's running
#   PORT=9000 ./run.sh  API on a different port (default 8000)
set -uo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8001}"
RUN_DIR=.run
LOG_DIR=logs
RUN_LOG="$LOG_DIR/run.log"
STOP_FLAG="$RUN_DIR/stopping"
SERVICES=(api worker email)
# A service that stayed up at least this long is considered healthy again, so its restart
# delay resets to the minimum instead of staying at the backed-off value.
HEALTHY_AFTER_SECONDS=60
MIN_RESTART_DELAY=2
MAX_RESTART_DELAY=60
# The worker finishes its in-flight jobs on SIGTERM; after this long it is force-killed.
# Safe either way - a killed job's lease is reclaimed by the reaper and retried.
STOP_GRACE_SECONDS=30

log() {
  local line="$(date '+%Y-%m-%d %H:%M:%S') [run.sh] $*"
  echo "$line"
  echo "$line" >> "$RUN_LOG"
}

is_alive() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

read_pid() {
  [ -f "$1" ] && cat "$1" 2>/dev/null
}

# Run in a background subshell; exec makes the service itself the process behind $!, so
# the pid file and signals point at python, not at an intermediate shell.
run_service() {
  case "$1" in
    api)    exec "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" ;;
    worker) exec "$PY" worker_main.py ;;
    email)  exec "$PY" email_ingest_main.py ;;
  esac
}

# Uses the venv's python directly rather than sourcing its activate script - a
# Windows-created activate has CRLF line endings that MSYS2/Git Bash can't parse, and
# calling the interpreter by path needs no activation anyway.
find_venv_python() {
  if [ -x .venv/Scripts/python.exe ]; then
    PY=.venv/Scripts/python.exe        # Windows venv layout
  elif [ -x .venv/bin/python ]; then
    PY=.venv/bin/python                # Linux/macOS venv layout
  else
    echo "No .venv found - create it first: python -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
  if ! "$PY" -c "import alembic, uvicorn" 2>/dev/null; then
    echo "Dependencies missing in .venv - run: $PY -m pip install -r requirements.txt" >&2
    exit 1
  fi
}

# Runs one service forever: start it, wait for it to exit, restart it after a delay that
# doubles on each quick crash (2s, 4s, ... 60s) so a service that can't start (bad config,
# DB down) doesn't spin, but recovers on its own once the cause is fixed.
supervise() {
  local name=$1
  local delay=$MIN_RESTART_DELAY

  while [ ! -f "$STOP_FLAG" ]; do
    local started=$SECONDS
    run_service "$name" &
    local child=$!
    echo "$child" > "$RUN_DIR/$name.pid"
    log "$name started (pid $child)"

    local code=0
    wait "$child" || code=$?
    rm -f "$RUN_DIR/$name.pid"
    [ -f "$STOP_FLAG" ] && break

    if (( SECONDS - started >= HEALTHY_AFTER_SECONDS )); then
      delay=$MIN_RESTART_DELAY
    fi
    log "$name went down (exit code $code) - restarting in ${delay}s"
    sleep "$delay"
    delay=$(( delay * 2 > MAX_RESTART_DELAY ? MAX_RESTART_DELAY : delay * 2 ))
  done
}

stop_all() {
  mkdir -p "$RUN_DIR"
  touch "$STOP_FLAG"   # tells every supervisor loop not to restart what we're about to stop

  local name pid
  for name in "${SERVICES[@]}"; do
    pid=$(read_pid "$RUN_DIR/$name.supervisor.pid")
    is_alive "$pid" && kill "$pid" 2>/dev/null
  done

  local pids=()
  for name in "${SERVICES[@]}"; do
    pid=$(read_pid "$RUN_DIR/$name.pid")
    if is_alive "$pid"; then
      kill -TERM "$pid" 2>/dev/null
      pids+=("$pid")
    fi
  done

  local waited=0
  while (( waited < STOP_GRACE_SECONDS )); do
    local any_alive=0
    for pid in "${pids[@]}"; do
      is_alive "$pid" && any_alive=1
    done
    (( any_alive )) || break
    sleep 1
    waited=$(( waited + 1 ))
  done
  for pid in "${pids[@]}"; do
    if is_alive "$pid"; then
      log "pid $pid did not stop within ${STOP_GRACE_SECONDS}s - force killing"
      kill -9 "$pid" 2>/dev/null
    fi
  done

  rm -f "$RUN_DIR"/*.pid "$STOP_FLAG"
}

status() {
  local name pid running=0
  for name in "${SERVICES[@]}"; do
    pid=$(read_pid "$RUN_DIR/$name.pid")
    if is_alive "$pid"; then
      echo "$name: running (pid $pid)"
      running=1
    else
      echo "$name: not running"
    fi
  done
  return $(( running ? 0 : 1 ))
}

already_running() {
  local name pid
  for name in "${SERVICES[@]}"; do
    pid=$(read_pid "$RUN_DIR/$name.supervisor.pid")
    is_alive "$pid" && return 0
  done
  return 1
}

mkdir -p "$RUN_DIR" "$LOG_DIR" data/pdfs

case "${1:-start}" in
  stop)
    log "stop requested"
    stop_all
    log "all services stopped"
    exit 0
    ;;
  status)
    status
    exit $?
    ;;
  start)
    ;;
  *)
    echo "usage: $0 [start|stop|status]" >&2
    exit 2
    ;;
esac

# Refuse to start a second copy - two email ingesters polling the same Gmail inbox is
# what got connections dropped before (see logs: "EOF occurred in violation of protocol").
if already_running; then
  echo "Already running - use './run.sh status' or './run.sh stop'." >&2
  exit 1
fi
rm -f "$RUN_DIR"/*.pid "$STOP_FLAG"

find_venv_python

log "applying database migrations"
# Retried rather than fatal: on a server reboot Postgres may still be starting up. Bounded
# so a real config error (wrong DATABASE_URL/password) fails loudly instead of looping forever.
MIGRATION_ATTEMPTS=24   # x 5s = 2 minutes
attempt=1
until "$PY" -m alembic upgrade head; do
  if (( attempt >= MIGRATION_ATTEMPTS )); then
    log "migrations still failing after $MIGRATION_ATTEMPTS attempts - check Postgres is running and DATABASE_URL in .env"
    exit 1
  fi
  log "migrations failed (attempt $attempt/$MIGRATION_ATTEMPTS) - retrying in 5s"
  attempt=$(( attempt + 1 ))
  sleep 5
done

on_signal() {
  trap - INT TERM
  log "shutdown requested - stopping all services"
  stop_all
  log "all services stopped"
  exit 0
}
trap on_signal INT TERM

for name in "${SERVICES[@]}"; do
  supervise "$name" &
  echo $! > "$RUN_DIR/$name.supervisor.pid"
done

log "all services running - API: http://localhost:$PORT/docs  (Ctrl+C or './run.sh stop' to stop)"

# Supervisors never exit on their own; this just keeps the script in the foreground until
# a stop signal arrives. Looping because `wait` returns early whenever a trap fires.
while true; do
  wait
  sleep 1
done
