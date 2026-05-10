#!/bin/sh
set -e

# Meridian をローカルのみで起動（外部には FastAPI が公開）
MERIDIAN_HOST=127.0.0.1 MERIDIAN_PORT=3456 meridian &

echo "Waiting for Meridian..."
until curl -sf http://127.0.0.1:3456/health > /dev/null 2>&1; do
  sleep 1
done
echo "Meridian ready."

exec uvicorn main:app --host 0.0.0.0 --port "${SERVER_PORT:-8001}"
