#!/usr/bin/env bash
# Runs the whole service - API + worker + email ingester - in the background and keeps it
# running: any service that crashes or exits is restarted automatically (with backoff), so
# one bad moment (Gmail dropping the connection, Postgres restarting, OOM) doesn't leave
# the system half-down. Works in Git Bash / MSYS2 on Windows and on Linux/EC2.
#
#   ./run.sh            start everything in the background (returns immediately)
#   ./run.sh stop       stop everything
#   ./run.sh restart    stop, then start
#   ./run.sh status     show what's running
#   ./run.sh logs       follow logs/run.log + logs/app.log (Ctrl+C stops following only)
#   PORT=9000 ./run.sh  API on a different port (default 8001)
#
# Logs:
#   logs/app.log  - everything the app does (JSON, one line per event) - LOG_FILE in .env
#   logs/run.log  - service starts/stops/crashes/restarts, plus any error output a service
#                   prints outside the app log (crash tracebacks, uvicorn startup errors)
set -uo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8001}"
RUN_DIR=.run
LOG_DIR=logs
RUN_LOG="$LOG_DIR/run.log"
MAIN_PID_FILE="$RUN_DIR/main.pid"
STOP_FLAG="$RUN_DIR/stopping"
SERVICES=(api worker email)
# A service that stayed up at least this long is considered healthy again, so its restart
# delay resets to the minimum instead of staying at the backed-off value.
HEALTHY_AFTER_SECONDS=60
MIN_RESTART_DELAY=2
MAX_RESTART_DELAY=60
# Linux only: the worker finishes its in-flight jobs on SIGTERM; after this long it is
# force-killed. Safe either way - a killed job's lease is reclaimed and the job retried.
STOP_GRACE_SECONDS=30

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
  *) IS_WINDOWS=0 ;;
esac

# Always appended to run.log; also echoed when a person is watching (a terminal), but not
# in the background process, whose stdout already *is* run.log - that would double lines.
log() {
  local line="$(date '+%Y-%m-%d %H:%M:%S') [run.sh] $*"
  echo "$line" >> "$RUN_LOG"
  [ -t 1 ] && echo "$line"
  return 0
}

is_alive() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

read_pid() {
  [ -f "$1" ] && cat "$1" 2>/dev/null
}

# Command-line pattern identifying each service's process (used on Windows, see below).
service_pattern() {
  case "$1" in
    api)    echo "uvicorn main:app" ;;
    worker) echo "worker_main.py" ;;
    email)  echo "email_ingest_main.py" ;;
  esac
}

# --- Windows process handling -------------------------------------------------------
# On Windows the services are native processes (.venv\Scripts\python.exe is a launcher
# that starts the real C:\PythonXY\python.exe as its child). MSYS/Git Bash `kill` does not
# reliably terminate those, and MSYS pids differ between Git Bash and MSYS2 terminals - so
# instead of trusting pid files, services are found by what they actually are: this
# project's venv python running one of the three service commands. That is also what
# catches copies left behind by an earlier crashed or killed run.

# Prints "<windows pid> <service>" for every running service launcher of this project.
win_service_pids() {
  VENV_PY="$(cygpath -w "$PWD/.venv/Scripts/python.exe")" \
    powershell.exe -NoProfile -NonInteractive -Command '
      Get-CimInstance Win32_Process |
        Where-Object { $_.ExecutablePath -eq $env:VENV_PY } |
        ForEach-Object {
          $c = $_.CommandLine
          if ($c -like "*uvicorn main:app*") { "$($_.ProcessId) api" }
          elseif ($c -like "*worker_main.py*") { "$($_.ProcessId) worker" }
          elseif ($c -like "*email_ingest_main.py*") { "$($_.ProcessId) email" }
        }' 2>/dev/null | tr -d '\r'
}

# Kills a Windows process and all of its children (the launcher + the real python).
win_kill_tree() {
  taskkill //F //T //PID "$1" >/dev/null 2>&1
}

win_winpid() {
  cat "/proc/$1/winpid" 2>/dev/null
}
# -------------------------------------------------------------------------------------

# Run in a background subshell; exec makes the service itself the process behind $!.
# stdout is dropped because it's the same JSON the app already writes to LOG_FILE
# (logs/app.log); stderr goes to run.log so a crash traceback or a uvicorn startup error
# is never lost.
run_service() {
  case "$1" in
    api)    exec "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" >/dev/null 2>>"$RUN_LOG" ;;
    worker) exec "$PY" worker_main.py >/dev/null 2>>"$RUN_LOG" ;;
    email)  exec "$PY" email_ingest_main.py >/dev/null 2>>"$RUN_LOG" ;;
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

# Service stdout is discarded (see run_service), so the app log file is the only record
# of what the app does - make sure there is one even if .env doesn't set LOG_FILE. Only
# set when .env lacks it: a real env var would override the .env value.
ensure_app_log_file() {
  if ! grep -qE '^[[:space:]]*LOG_FILE=' .env 2>/dev/null; then
    export LOG_FILE="$LOG_DIR/app.log"
  fi
}

port_is_free() {
  "$PY" -c "import socket, sys; socket.socket().bind(('0.0.0.0', int(sys.argv[1])))" "$PORT" 2>/dev/null
}

run_migrations() {
  log "applying database migrations"
  # Retried rather than fatal: on a server reboot Postgres may still be starting up.
  # Bounded so a real config error (wrong DATABASE_URL/password) fails loudly.
  local attempts=24 attempt=1   # x 5s = 2 minutes
  until "$PY" -m alembic upgrade head; do
    if (( attempt >= attempts )); then
      log "migrations still failing after $attempts attempts - check Postgres is running and DATABASE_URL in .env"
      return 1
    fi
    log "migrations failed (attempt $attempt/$attempts) - retrying in 5s"
    attempt=$(( attempt + 1 ))
    sleep 5
  done
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

# Stops supervisors first (so nothing gets restarted), then the services themselves.
stop_services() {
  touch "$STOP_FLAG"

  local name pid
  for name in "${SERVICES[@]}"; do
    pid=$(read_pid "$RUN_DIR/$name.supervisor.pid")
    is_alive "$pid" && kill "$pid" 2>/dev/null
  done

  if (( IS_WINDOWS )); then
    local winpid _
    while read -r winpid _; do
      [ -n "$winpid" ] && win_kill_tree "$winpid"
    done < <(win_service_pids)
    return
  fi

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
}

# Prints one "  <service>: running (...)" / "not running" line per service. Returns 0 if
# anything at all is running (including leftovers), 1 if nothing is.
status() {
  local name any=0
  if (( IS_WINDOWS )); then
    local found
    found=$(win_service_pids)
    for name in "${SERVICES[@]}"; do
      local pids
      pids=$(awk -v s="$name" '$2 == s { printf "%s%s", sep, $1; sep = ", " }' <<< "$found")
      if [ -n "$pids" ]; then
        local count
        count=$(awk -v s="$name" '$2 == s' <<< "$found" | wc -l)
        if (( count > 1 )); then
          echo "  $name: running $count COPIES (windows pids $pids) - run './run.sh stop'"
        else
          echo "  $name: running (windows pid $pids)"
        fi
        any=1
      else
        echo "  $name: not running"
      fi
    done
  else
    local pid
    for name in "${SERVICES[@]}"; do
      pid=$(read_pid "$RUN_DIR/$name.pid")
      if is_alive "$pid"; then
        echo "  $name: running (pid $pid)"
        any=1
      else
        echo "  $name: not running"
      fi
    done
  fi
  (( any ))
}

is_running() {
  is_alive "$(read_pid "$MAIN_PID_FILE")" && return 0
  if (( IS_WINDOWS )); then
    [ -n "$(win_service_pids)" ] && return 0
  fi
  local name
  for name in "${SERVICES[@]}"; do
    is_alive "$(read_pid "$RUN_DIR/$name.supervisor.pid")" && return 0
  done
  return 1
}

# The background process: supervises the three services until told to stop.
daemon() {
  echo $$ > "$MAIN_PID_FILE"
  (( IS_WINDOWS )) && win_winpid $$ > "$RUN_DIR/main.winpid"
  find_venv_python
  ensure_app_log_file

  on_signal() {
    trap - INT TERM HUP
    log "shutdown requested - stopping all services"
    stop_services
    rm -f "$RUN_DIR"/*.pid "$STOP_FLAG"
    log "all services stopped"
    exit 0
  }
  trap on_signal INT TERM HUP

  local name
  for name in "${SERVICES[@]}"; do
    supervise "$name" &
    echo $! > "$RUN_DIR/$name.supervisor.pid"
  done
  log "all services running - API: http://localhost:$PORT/docs"

  # Supervisors never exit on their own; this keeps the daemon alive until a stop signal.
  # Looping because `wait` returns early whenever a trap fires.
  while true; do
    wait
    sleep 1
  done
}

start() {
  if is_running; then
    echo "Already running:"
    status
    echo "Use './run.sh restart' to restart or './run.sh stop' to stop."
    return 1
  fi
  rm -f "$RUN_DIR"/*.pid "$RUN_DIR"/*.winpid "$STOP_FLAG"

  # Done here in the foreground so a broken venv, busy port or database shows up
  # immediately in the terminal instead of only inside a log file.
  find_venv_python
  if ! port_is_free; then
    echo "Port $PORT is already in use by another program - stop it, or start on another port: PORT=9000 ./run.sh" >&2
    return 1
  fi
  run_migrations || return 1

  log "starting in background"
  nohup "$0" __daemon >> "$RUN_LOG" 2>&1 < /dev/null &
  disown

  # Give the services a moment so status reflects reality (and an instant crash shows up).
  sleep 5
  echo
  status || true
  echo
  echo "Running in the background. API: http://localhost:$PORT/docs"
  echo "Logs:   logs/app.log (app)   logs/run.log (starts/stops/crashes)"
  echo "        ./run.sh logs     to follow both"
  echo "Stop:   ./run.sh stop"
}

stop() {
  if ! is_running; then
    echo "Not running."
    rm -f "$RUN_DIR"/*.pid "$RUN_DIR"/*.winpid "$STOP_FLAG"
    return 0
  fi
  echo "Stopping..."
  log "stop requested"

  local main_pid
  main_pid=$(read_pid "$MAIN_PID_FILE")
  if (( IS_WINDOWS )); then
    # Take down the background process first so nothing restarts what's stopped next.
    touch "$STOP_FLAG"
    local main_winpid
    main_winpid=$(read_pid "$RUN_DIR/main.winpid")
    [ -n "$main_winpid" ] && win_kill_tree "$main_winpid"
    is_alive "$main_pid" && kill -9 "$main_pid" 2>/dev/null
  elif is_alive "$main_pid"; then
    kill -TERM "$main_pid" 2>/dev/null
    local waited=0
    while is_alive "$main_pid" && (( waited < STOP_GRACE_SECONDS + 10 )); do
      sleep 1
      waited=$(( waited + 1 ))
    done
  fi
  # Also catches services left behind by an earlier run that died without cleaning up.
  stop_services
  is_alive "$main_pid" && kill -9 "$main_pid" 2>/dev/null
  rm -f "$RUN_DIR"/*.pid "$RUN_DIR"/*.winpid "$STOP_FLAG"
  log "all services stopped"

  if (( IS_WINDOWS )) && [ -n "$(win_service_pids)" ]; then
    echo "Some services could not be stopped:" >&2
    status >&2
    return 1
  fi
  echo "Stopped."
}

mkdir -p "$RUN_DIR" "$LOG_DIR" data/pdfs
touch "$RUN_LOG"

case "${1:-start}" in
  start)    start ;;
  stop)     stop ;;
  restart)  stop && start ;;
  status)
    if is_running; then echo "Running:"; else echo "Not running:"; fi
    status
    ;;
  logs)     tail -n 50 -F "$RUN_LOG" "$LOG_DIR/app.log" ;;
  __daemon) daemon ;;
  *)
    echo "usage: $0 [start|stop|restart|status|logs]" >&2
    exit 2
    ;;
esac
