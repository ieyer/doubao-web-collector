#!/usr/bin/env bash
# Doubao web collector launcher: ensures a virtual display for headed
# browsers, then starts the API service.
set -e
cd "$(dirname "$0")"
export DISPLAY="${DISPLAY:-:98}"
if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
  Xvfb "$DISPLAY" -screen 0 1400x1300x24 >/tmp/collector-xvfb.log 2>&1 &
  sleep 2
fi
exec .venv/bin/python -m uvicorn app.main:app --host "$(python3 -c "import json;print(json.load(open('config.json')).get('host','0.0.0.0'))")" --port "$(python3 -c "import json;print(json.load(open('config.json')).get('port',8790))")"
