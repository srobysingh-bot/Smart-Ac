#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starting SmartCool AC Optimizer..."

# Expose HA Supervisor token to the app
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export HA_BASE_URL="http://supervisor/core"

# Data directory for SQLite
mkdir -p /data

cd /app
exec python3 -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port 8099 \
    --workers 1 \
    --log-level info \
    --access-log
