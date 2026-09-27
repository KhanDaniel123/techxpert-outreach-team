#!/bin/bash
# Local dev: creates venv on first run, installs deps, starts the app.
cd "$(dirname "$0")"
if [ ! -d venv ]; then python3 -m venv venv; fi
./venv/bin/pip install -q -r requirements.txt
FERNET_KEY="${FERNET_KEY:-}" CRON_SECRET="${CRON_SECRET:-dev-cron-secret}" ./venv/bin/python app.py
