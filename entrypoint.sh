#!/usr/bin/env bash
# Entrypoint: bootstrap superuser, start PocketBase (GUI + job API), then the worker.
set -e

PB=/opt/pb/pocketbase

# Idempotent superuser bootstrap; skipped when env vars are not provided
# (you can then create it manually via the admin UI at /_/).
if [ -n "${PB_SUPERUSER_EMAIL}" ] && [ -n "${PB_PASSWORD}" ]; then
    "${PB}" superuser upsert "${PB_SUPERUSER_EMAIL}" "${PB_PASSWORD}" >/dev/null 2>&1 \
        || echo "[entrypoint] superuser upsert skipped"
fi

"${PB}" serve --hooksDir /app/pb_hooks --http "0.0.0.0:${PB_PORT:-8090}" &
PB_PID=$!
trap 'kill "${PB_PID}" 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
    if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PB_PORT:-8090}/api/health', timeout=2)" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

exec python /app/worker.py
