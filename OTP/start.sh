#!/bin/sh
# ============================================================
# start.sh — OTP Bypass MCP launcher for /workspace/otp-bypass
# Your AI assistant app runs THIS via the MCP config:
#   name: otp-bypass | transport: stdio | command: sh /workspace/otp-bypass/start.sh
#   workingDirectory: /workspace/otp-bypass
# ============================================================
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$DIR"

# --- stop mode: start.sh stop ---
if [ "$1" = "stop" ]; then
    if [ -f lab.pid ]; then
        kill "$(cat lab.pid)" 2>/dev/null && echo "lab stopped" || echo "lab not running"
        rm -f lab.pid
    else
        echo "lab not running"
    fi
    exit 0
fi

# 1) install Python dependencies once (skip when already present)
if ! python3 -c "import mcp, flask, requests" >/dev/null 2>&1; then
    echo "[start.sh] installing dependencies (one time) ..." >&2
    pip3 install --quiet mcp flask requests \
        || pip3 install --break-system-packages --quiet mcp flask requests \
        || { echo "[start.sh] FATAL: pip install failed" >&2; exit 1; }
fi

# 2) auto-start the embedded vulnerable lab if nothing answers on :5000
if ! python3 - <<'PYEOF' >/dev/null 2>&1
import socket
s = socket.socket(); s.settimeout(1)
try:
    s.connect(("127.0.0.1", 5000)); raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PYEOF
then
    echo "[start.sh] starting embedded OTP lab on http://127.0.0.1:5000 ..." >&2
    nohup python3 "$DIR/otp_bypass.py" --lab > "$DIR/lab.log" 2>&1 &
    echo $! > "$DIR/lab.pid"
    i=0
    while [ "$i" -lt 40 ]; do
        if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/login', timeout=1)" >/dev/null 2>&1; then
            break
        fi
        i=$((i + 1)); sleep 0.25
    done
    echo "[start.sh] lab is up (log: $DIR/lab.log)" >&2
fi

# 3) make sure a global config exists (default target = the local lab)
if [ ! -f "$DIR/config.json" ]; then
    printf '{"target": "http://127.0.0.1:5000", "email": "admin@lab.local", "password": "admin123"}' > "$DIR/config.json"
fi

# 4) run the MCP server on stdio — this is what the AI assistant talks to
exec python3 "$DIR/otp_bypass.py"
