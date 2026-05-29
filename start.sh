#!/bin/bash

# Start Pauco Gestion App on Railway
echo "[START] Launching App Gestion (wsgi:app)"

# Navigate to app_client directory
cd "$(dirname "$0")/app_client" || exit 1

# Default to port 8000 if PORT env var not set
PORT=${PORT:-8000}

# Activate virtual environment from root
if [ -f ../.venv/bin/activate ]; then
    source ../.venv/bin/activate
fi

exec gunicorn wsgi:app \
    --workers 2 \
    --threads 4 \
    --timeout 120 \
    --bind 0.0.0.0:$PORT
