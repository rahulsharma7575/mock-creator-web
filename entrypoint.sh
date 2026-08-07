#!/usr/bin/env bash
# Entrypoint: bootstrap superuser, start PocketBase (GUI + job API), then the worker.
# v2.1 — fails loudly if PocketBase does not come up (container shows restarting + logs).
set -e

PB=/opt/pb/pocketbase
PORT="${PB_PORT:-8090}"
BOOT_LOG=/tmp/pb_boot.log
PBPID_BOOT=/tmp/pb_boot.pid

echo "[entrypoint] mock-creator v2.1 — PocketBase 0.39.10 + worker (hub image)"

# Idempotent superuser bootstrap; skipped when env vars are not provided
# (you can then create it manually via the admin UI at /_/).
if [ -n "${PB_SUPERUSER_EMAIL}" ] && [ -n "${PB_PASSWORD}" ]; then
    "${PB}" superuser upsert "${PB_SUPERUSER_EMAIL}" "${PB_PASSWORD}" >/dev/null 2>&1 \
        || echo "[entrypoint] superuser upsert skipped"
fi

"${PB}" serve --hooksDir /app/pb_hooks --http "0.0.0.0:${PORT}" > "${BOOT_LOG}" 2>&1 &
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
