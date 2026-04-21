#!/bin/bash
# Start a fresh Cloudflare quick tunnel for the app on port 8000.

set -e

APP_URL="${APP_URL:-http://localhost:8000}"
LOG_DIR="${LOG_DIR:-logs}"
LOG_FILE="$LOG_DIR/cloudflared.log"

echo "===== Start Cloudflare Quick Tunnel ====="
echo "Target app: $APP_URL"

if ! command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared is not installed."
    echo "Install it on the VM, then rerun this script."
    echo "Suggested install:"
    echo "  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb"
    echo "  sudo dpkg -i /tmp/cloudflared.deb"
    exit 1
fi

mkdir -p "$LOG_DIR"

if pgrep -f "cloudflared tunnel --url" >/dev/null 2>&1; then
    echo "Stopping existing quick tunnel..."
    pkill -f "cloudflared tunnel --url" || true
    sleep 2
fi

nohup cloudflared tunnel --url "$APP_URL" > "$LOG_FILE" 2>&1 &
TUNNEL_PID=$!
echo "cloudflared started with PID: $TUNNEL_PID"
echo "Waiting for public URL..."

for _ in $(seq 1 20); do
    URL=$(grep -o 'https://[-a-z0-9]*\.trycloudflare\.com' "$LOG_FILE" | head -1 || true)
    if [ -n "$URL" ]; then
        echo ""
        echo "Public URL: $URL"
        echo "Logs: tail -f $LOG_FILE"
        exit 0
    fi
    sleep 1
done

echo "Tunnel started, but the URL was not detected yet."
echo "Check logs: tail -f $LOG_FILE"
