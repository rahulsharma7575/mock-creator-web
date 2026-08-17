#!/usr/bin/env bash
# Entrypoint: bootstrap superuser, start PocketBase (GUI + job API), then the worker.
# v3.0 — explicit --dir so the SQLite DB ALWAYS lives in /app/pb_data (the bind mount).
#       Previously PB resolved ./pb_data from the cwd; any cwd change or fresh
#       container layer made it look like "data was lost" on redeploy.
set -e

PB=/opt/pb/pocketbase
PORT="${PB_PORT:-8090}"
PB_DIR="${PB_DIR:-/app/pb_data}"
PUBLIC_DIR="${PUBLIC_DIR:-/app/pb_public}"
BOOT_LOG=/tmp/pb_boot.log
PBPID_BOOT=/tmp/pb_boot.pid

echo "[entrypoint] mock-creator v3.0 — PocketBase 0.39.10 + worker (hub image)"
echo "[entrypoint] PocketBase data dir : ${PB_DIR}   <-- must be bind-mounted (./pb_data) for persistence"
echo "[entrypoint] PocketBase public dir: ${PUBLIC_DIR}"

# Fail loudly if the data dir is not usable (e.g. container ran without the volume).
mkdir -p "${PB_DIR}"
if [ ! -w "${PB_DIR}" ]; then
    echo "[entrypoint] FATAL: ${PB_DIR} is not writable - check the ./pb_data bind mount" >&2
    exit 1
fi
if [ -d "${PB_DIR}" ] && [ -f "${PB_DIR}/data.db" ]; then
    echo "[entrypoint] data.db present ($(du -h "${PB_DIR}/data.db" | cut -f1)) - resuming existing data"
elif [ -d "${PB_DIR}" ] && [ -f "${PB_DIR}/data.s.db" ]; then
    echo "[entrypoint] data.s.db present ($(du -h "${PB_DIR}/data.s.db" | cut -f1)) - resuming existing data"
else
    echo "[entrypoint] NOTE: ${PB_DIR} has no existing database - starting fresh (first boot?)"
fi

# Idempotent superuser bootstrap; skipped when env vars are not provided
# (you can then create it manually via the admin UI at /_/).
if [ -n "${PB_SUPERUSER_EMAIL}" ] && [ -n "${PB_PASSWORD}" ]; then
    "${PB}" superuser upsert --dir "${PB_DIR}" "${PB_SUPERUSER_EMAIL}" "${PB_PASSWORD}" >/dev/null 2>&1 \
        || echo "[entrypoint] superuser upsert skipped"
fi

"${PB}" serve --dir "${PB_DIR}" --publicDir "${PUBLIC_DIR}" --hooksDir /app/pb_hooks --httpMaxBodySize 134217728 --http "0.0.0.0:${PORT}" > "${BOOT_LOG}" 2>&1 &
PB_PID=$!
echo "${PB_PID}" > "${PBPID_BOOT}"
trap 'kill "${PB_PID}" 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
    if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/api/health', timeout=2)" >/dev/null 2>&1; then
        echo "[entrypoint] PocketBase is up on :${PORT}"
# Stream PocketBase's own log (incl. hook-loader errors) to the container log
tail -f "${BOOT_LOG}" >&2 &
LOGGER_PID=$!
trap 'kill "${PB_PID}" "${LOGGER_PID}" 2>/dev/null || true' EXIT
        break
    fi
    sleep 1
done

if ! kill -0 "${PB_PID}" 2>/dev/null; then
    echo "[entrypoint] FATAL: PocketBase exited during boot:" >&2
    tail -n 40 "${BOOT_LOG}" >&2 || true
    exit 1
fi

if ! python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/api/health', timeout=2)" >/dev/null 2>&1; then
    echo "[entrypoint] FATAL: PocketBase did not answer /api/health after 60s:" >&2
    tail -n 40 "${BOOT_LOG}" >&2 || true
    exit 1
fi

exec python /app/worker.py
