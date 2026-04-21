#!/bin/bash
# ============================================
# Paper Trading - Auto-Diagnose & Fix Script
# Just paste this ENTIRE block into your GCP VM SSH terminal
# ============================================

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  Paper Trading - Auto Diagnose & Fix${NC}"
echo -e "${YELLOW}============================================${NC}"
echo ""

# 1. Find the app directory
echo -e "${GREEN}[1/8] Finding app directory...${NC}"
APP_DIR=$(find /home -name "main.py" -path "*/PaperTrading/*" 2>/dev/null | head -1 | xargs dirname)
if [ -z "$APP_DIR" ]; then
    APP_DIR="/home/debian/PaperTrading"
fi
echo "  -> App directory: $APP_DIR"

if [ ! -d "$APP_DIR" ]; then
    echo -e "${RED}  ERROR: App directory not found!${NC}"
    exit 1
fi

cd "$APP_DIR"

# Force the app config to use only port 8000
echo -e "${GREEN}[0] Enforcing APP_PORT=8000 in .env...${NC}"
if [ -f "$APP_DIR/.env" ]; then
    if grep -q '^APP_PORT=' "$APP_DIR/.env"; then
        sed -i 's/^APP_PORT=.*/APP_PORT=8000/' "$APP_DIR/.env"
    else
        echo "APP_PORT=8000" >> "$APP_DIR/.env"
    fi
    echo -e "${GREEN}  -> .env updated to APP_PORT=8000${NC}"
else
    echo -e "${YELLOW}  -> .env not found, skipping port override${NC}"
fi

# 2. Check git status
echo -e "${GREEN}[2/8] Checking git version...${NC}"
CURRENT_COMMIT=$(git log --oneline -1 2>/dev/null || echo "Not a git repo")
echo "  -> Current commit: $CURRENT_COMMIT"

# 3. Pull latest code
echo -e "${GREEN}[3/8] Pulling latest code from GitHub...${NC}"
git fetch origin main 2>/dev/null
LATEST_COMMIT=$(git log origin/main --oneline -1 2>/dev/null || echo "Cannot fetch")
echo "  -> Latest on GitHub: $LATEST_COMMIT"

if [ "$CURRENT_COMMIT" != "$LATEST_COMMIT" ]; then
    echo -e "${YELLOW}  -> Updating code...${NC}"
    git reset --hard origin/main 2>/dev/null || git pull origin main 2>/dev/null
    echo -e "${GREEN}  -> Updated!${NC}"
else
    echo -e "${GREEN}  -> Already up to date.${NC}"
fi

# 4. Check if AUTO-INIT fix is present
echo -e "${GREEN}[4/8] Checking Owners tab fix...${NC}"
AUTO_INIT_COUNT=$(grep -c "AUTO-INIT" "$APP_DIR/dashboard/templates/index.html" 2>/dev/null || echo "0")
if [ "$AUTO_INIT_COUNT" -gt "0" ]; then
    echo -e "${GREEN}  -> Fix is PRESENT (AUTO-INIT found)${NC}"
else
    echo -e "${RED}  -> Fix is MISSING!${NC}"
fi

# 5. Check running process
echo -e "${GREEN}[5/8] Checking running process...${NC}"
APP_PID=$(pgrep -f "uvicorn.*main:app" 2>/dev/null | head -1)
if [ -n "$APP_PID" ]; then
    echo -e "${GREEN}  -> App is RUNNING (PID: $APP_PID)${NC}"
    ps -p $APP_PID -o pid,etime,cmd --no-headers 2>/dev/null
else
    echo -e "${RED}  -> App is NOT running!${NC}"
fi

OLD_PORT_PIDS=$(pgrep -f "uvicorn.*2583" 2>/dev/null || true)
if [ -n "$OLD_PORT_PIDS" ]; then
    echo -e "${YELLOW}  -> Found stale app process(es) on port 2583: $OLD_PORT_PIDS${NC}"
fi

# 6. Check service manager
echo -e "${GREEN}[6/8] Checking service manager...${NC}"
if systemctl is-active --quiet papertrading 2>/dev/null; then
    echo -e "${GREEN}  -> systemd service: ACTIVE${NC}"
elif command -v pm2 &>/dev/null && pm2 list 2>/dev/null | grep -q "paper"; then
    echo -e "${GREEN}  -> PM2 process: ACTIVE${NC}"
    pm2 list 2>/dev/null
else
    echo -e "${RED}  -> No service manager found${NC}"
fi

# 7. Check cloudflared tunnel
echo -e "${GREEN}[7/8] Checking Cloudflare tunnel...${NC}"
if pgrep -f "cloudflared" >/dev/null 2>&1; then
    echo -e "${GREEN}  -> cloudflared: RUNNING${NC}"
    pgrep -fa cloudflared 2>/dev/null
else
    echo -e "${YELLOW}  -> cloudflared: NOT running${NC}"
fi

# 8. Restart the app
echo -e "${GREEN}[8/8] Restarting app...${NC}"
STALE_UVICORN_PIDS=$(pgrep -f "uvicorn.*main:app" 2>/dev/null || true)
if [ -n "$STALE_UVICORN_PIDS" ]; then
    echo -e "${YELLOW}  -> Stopping existing uvicorn instance(s): $STALE_UVICORN_PIDS${NC}"
    echo "$STALE_UVICORN_PIDS" | xargs -r kill 2>/dev/null || true
    sleep 2
fi
if systemctl is-active --quiet papertrading 2>/dev/null; then
    sudo systemctl restart papertrading 2>/dev/null
    echo -e "${GREEN}  -> Restarted via systemd${NC}"
elif command -v pm2 &>/dev/null && pm2 list 2>/dev/null | grep -q "paper"; then
    pm2 restart paper 2>/dev/null
    echo -e "${GREEN}  -> Restarted via PM2${NC}"
elif [ -n "$APP_PID" ]; then
    kill $APP_PID 2>/dev/null
    sleep 2
    cd "$APP_DIR"
    source venv/bin/activate 2>/dev/null
    nohup uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2 > /dev/null 2>&1 &
    echo -e "${GREEN}  -> Restarted manually (new PID: $!)${NC}"
else
    echo -e "${YELLOW}  -> Starting app fresh...${NC}"
    cd "$APP_DIR"
    source venv/bin/activate 2>/dev/null
    nohup uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2 > /dev/null 2>&1 &
    echo -e "${GREEN}  -> Started (PID: $!)${NC}"
fi

# Final check
echo ""
sleep 3
echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  FINAL STATUS${NC}"
echo -e "${YELLOW}============================================${NC}"

# Verify app is responding
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/status 2>/dev/null)
if [ "$RESPONSE" = "200" ]; then
    echo -e "${GREEN}✅ App is responding on port 8000${NC}"
    curl -s http://localhost:8000/api/status 2>/dev/null | python3 -m json.tool 2>/dev/null || echo ""
else
    echo -e "${RED}❌ App is NOT responding (HTTP $RESPONSE)${NC}"
fi

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || true)

echo ""
echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  ACCESS INFO${NC}"
if pgrep -f "cloudflared" >/dev/null 2>&1; then
    echo -e "${GREEN}  cloudflared is running, but this script cannot safely guess the current trycloudflare URL.${NC}"
    echo -e "${YELLOW}  Check the cloudflared startup logs for the live public URL.${NC}"
else
    echo -e "${YELLOW}  No active Cloudflare tunnel detected.${NC}"
fi
if [ -n "$PUBLIC_IP" ]; then
    echo -e "${YELLOW}  VM URL: http://$PUBLIC_IP:8000${NC}"
fi
echo -e "${YELLOW}  Note: GCP also needs an ingress firewall rule allowing tcp:8000.${NC}"
echo -e "${YELLOW}============================================${NC}"
