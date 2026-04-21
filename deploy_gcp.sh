#!/bin/bash
# ========================================
# Paper Trading - Google Cloud VM Deploy Script
# Run this inside your GCP VM via SSH
# ========================================

set -e

APP_DIR="/home/debian/PaperTrading"
REPO="https://github.com/27Krishnan/PaperTrading.git"
PYTHON_BIN="python3.11"

echo "===== Paper Trading GCP Deploy ====="
echo "Date: $(date)"

# 1. Install system dependencies
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3.11 python3.11-venv python3.11-dev git curl libgl1 libglib2.0-0 > /dev/null 2>&1

# 2. Clone or pull repo
if [ -d "$APP_DIR/.git" ]; then
    echo "[2/7] Pulling latest code..."
    cd "$APP_DIR"
    git fetch origin
    git reset --hard origin/main
else
    echo "[2/7] Cloning repository..."
    cd /home/debian
    git clone "$REPO" PaperTrading
    cd "$APP_DIR"
fi

# 3. Setup virtual environment
echo "[3/7] Setting up Python virtual environment..."
cd "$APP_DIR"
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt > /dev/null 2>&1

# Force the app to use port 8000 if .env exists
if [ -f "$APP_DIR/.env" ]; then
    if grep -q '^APP_PORT=' "$APP_DIR/.env"; then
        sed -i 's/^APP_PORT=.*/APP_PORT=8000/' "$APP_DIR/.env"
    else
        echo "APP_PORT=8000" >> "$APP_DIR/.env"
    fi
fi

# 4. Create systemd service for 24/7 running
echo "[4/7] Creating systemd service..."
cat > /tmp/papertrading.service << 'EOF'
[Unit]
Description=Paper Trading FastAPI Server
After=network.target

[Service]
Type=simple
User=debian
WorkingDirectory=/home/debian/PaperTrading
Environment="PATH=/home/debian/PaperTrading/venv/bin"
ExecStart=/home/debian/PaperTrading/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=always
RestartSec=10
StandardOutput=append:/home/debian/PaperTrading/logs/papertrading.log
StandardError=append:/home/debian/PaperTrading/logs/papertrading.log

[Install]
WantedBy=multi-user.target
EOF

sudo mv /tmp/papertrading.service /etc/systemd/system/
sudo systemctl daemon-reload

# 5. Configure firewall
echo "[5/7] Opening port 8000..."
sudo iptables -C INPUT -p tcp --dport 8000 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT

# 6. Start service
echo "[6/7] Starting Paper Trading service..."
sudo systemctl enable papertrading
sudo systemctl restart papertrading

# 7. Status
echo "[7/7] Service status:"
sudo systemctl status papertrading --no-pager -l | head -15

echo ""
echo "===== Deploy Complete ====="
PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || true)
if [ -n "$PUBLIC_IP" ]; then
    echo "Access on the VM: http://localhost:8000"
    echo "Public access (requires a GCP firewall rule for tcp:8000): http://$PUBLIC_IP:8000"
else
    echo "Access on the VM: http://localhost:8000"
    echo "Public access requires the VM external IP and a GCP firewall rule for tcp:8000"
fi
echo "If you want a Cloudflare tunnel URL, start one separately:"
echo "  cloudflared tunnel --url http://localhost:8000"
echo "Logs: tail -f $APP_DIR/logs/papertrading.log"
echo "Manage: sudo systemctl {start|stop|restart|status} papertrading"
