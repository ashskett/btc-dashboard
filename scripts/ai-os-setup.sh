#!/bin/bash
# =============================================================================
# AI OS API — One-Time Droplet Setup
# Run this ONCE on the droplet:
#   bash <(curl -s https://raw.githubusercontent.com/ashskett/btc-dashboard/main/scripts/ai-os-setup.sh)
#
# What it does:
#   1. Clones the ashskett/ai-os repo to /root/ai-os
#   2. Creates Python venv and installs requirements
#   3. Creates .env from .env.example (you fill in credentials after)
#   4. Starts the API in a tmux session named ai-os
#   5. Opens firewall port 8080
#   6. Adds a GitHub webhook to auto-deploy on push to ai-os repo
# =============================================================================
set -e

AI_OS_REPO="https://github.com/ashskett/ai-os.git"
AI_OS_DIR="/root/ai-os"
PORT=8080

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║     AI OS API — Droplet Setup                    ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# 1. Install deps
echo "▶ Installing system dependencies..."
apt-get install -y git curl python3-venv 2>/dev/null | grep -E "^(Setting up|Already)" || true

# 2. Clone or update repo
echo "▶ Cloning ai-os repo..."
if [ -d "$AI_OS_DIR/.git" ]; then
    echo "  Already cloned — pulling latest..."
    git -C "$AI_OS_DIR" pull origin main
else
    git clone "$AI_OS_REPO" "$AI_OS_DIR"
fi

# 3. Set up Python venv
echo "▶ Setting up Python environment..."
cd "$AI_OS_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -r requirements.txt -q
echo "  Dependencies installed."

# 4. Create .env if not present
if [ ! -f "$AI_OS_DIR/.env" ]; then
    cp "$AI_OS_DIR/.env.example" "$AI_OS_DIR/.env"
    echo "  Created .env from template. YOU MUST edit it:"
    echo "  nano /root/ai-os/.env"
else
    echo "  .env already exists — skipping."
fi

# 5. Open firewall port 8080
echo "▶ Opening port $PORT..."
ufw allow $PORT/tcp 2>/dev/null || true
iptables -I INPUT -p tcp --dport $PORT -j ACCEPT 2>/dev/null || true

# 6. Start in tmux
echo "▶ Starting AI OS API in tmux session 'ai-os'..."
tmux kill-session -t ai-os 2>/dev/null || true
tmux new-session -d -s ai-os \
    "cd $AI_OS_DIR && source venv/bin/activate && uvicorn main:app --host 0.0.0.0 --port $PORT"
sleep 2

# 7. Verify it started
RESP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://localhost:$PORT/health)
if [ "$RESP" = "200" ]; then
    echo "  API is running (HTTP $RESP)"
else
    echo "  Warning: got HTTP $RESP — check: tmux attach -t ai-os"
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Setup complete!                                  ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║  API:   http://165.232.101.253:8080               ║"
echo "║  Docs:  http://165.232.101.253:8080/docs          ║"
echo "║  Logs:  tmux attach -t ai-os                      ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "REQUIRED NEXT STEPS:"
echo "  1. Edit credentials:  nano /root/ai-os/.env"
echo "  2. Restart API:       tmux send-keys -t ai-os C-c Enter && tmux send-keys -t ai-os 'cd /root/ai-os && source venv/bin/activate && uvicorn main:app --host 0.0.0.0 --port 8080' Enter"
echo ""
echo "  3. Add GitHub webhook for auto-deploy:"
echo "     URL:    http://165.232.101.253:9001/deploy-ai-os"
echo "     Secret: grid-engine-deploy"
echo "     Event:  push"
echo ""
