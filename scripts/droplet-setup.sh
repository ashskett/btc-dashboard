#!/bin/bash
# =============================================================================
# Grid Engine — One-Time Droplet Setup
# Run this ONCE on the droplet:
#   bash <(curl -s https://raw.githubusercontent.com/ashskett/btc-dashboard/main/scripts/droplet-setup.sh)
#
# What it does:
#   1. Installs git + dependencies
#   2. Clones the btc-dashboard repo to /root/btc-dashboard
#   3. Copies engine Python files from /root/grid-engine into the repo
#   4. Commits and pushes them so Claude can edit them
#   5. Sets up the webhook server as a systemd service
#   6. Opens firewall port 9001 for the webhook
#   7. Adds Claude's SSH public key to authorized_keys
# =============================================================================
set -e

REPO_URL="https://github.com/ashskett/btc-dashboard.git"
REPO_BRANCH="main"   # change to feature branch if main not yet merged
REPO_DIR="/root/btc-dashboard"
ENGINE_DIR="/root/grid-engine"
WEBHOOK_PORT=9001
CLAUDE_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOz0Hy8wY5c/3WHMdxFB3s7g8TkOiNdgPHkp2cllNiR7 claude-code@btc-dashboard"
RAW_BASE="https://raw.githubusercontent.com/ashskett/btc-dashboard/${REPO_BRANCH}"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║     Grid Engine — Auto-Deploy Setup              ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# 1. System deps
echo "▶ Installing dependencies..."
apt-get install -y git curl 2>/dev/null | grep -E "^(Setting up|Already)" || true

# 2. Add Claude SSH key
echo "▶ Adding Claude Code SSH key..."
mkdir -p ~/.ssh && chmod 700 ~/.ssh
if ! grep -q "claude-code@btc-dashboard" ~/.ssh/authorized_keys 2>/dev/null; then
    echo "$CLAUDE_PUBKEY" >> ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    echo "  Added."
else
    echo "  Already present."
fi

# 3. Clone or update the repo
echo "▶ Cloning repo (branch: $REPO_BRANCH)..."
if [ -d "$REPO_DIR/.git" ]; then
    echo "  Repo already cloned — pulling latest..."
    git -C "$REPO_DIR" fetch origin
    git -C "$REPO_DIR" checkout "$REPO_BRANCH"
    git -C "$REPO_DIR" pull origin "$REPO_BRANCH"
else
    git clone -b "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
fi

# 4. Copy engine Python files INTO the repo (so Claude can edit them)
echo "▶ Copying engine files into repo..."
mkdir -p "$REPO_DIR/engine"

# List of files to track in git (no secrets, no state, no logs)
FILES=(
    engine.py
    engine_state.py
    engine_log.py
    dashboard_server.py
    dashboard.html
    breakout.py
    regime.py
    grid_logic.py
    inventory.py
    threecommas.py
    market_data.py
    indicators.py
    session.py
    status.py
    config.py
    requirements.txt
    test_connection.py
    liquidity.py
    run.sh
    start.sh
    deploy.sh
)

COPIED=0
for f in "${FILES[@]}"; do
    if [ -f "$ENGINE_DIR/$f" ]; then
        cp "$ENGINE_DIR/$f" "$REPO_DIR/engine/$f"
        COPIED=$((COPIED+1))
    fi
done
echo "  Copied $COPIED files."

# 5. Configure git and push engine files to repo
echo "▶ Committing engine files to git..."
cd "$REPO_DIR"
git config user.email "droplet@grid-engine"
git config user.name "Grid Engine Droplet"

# Switch to or create main branch
git checkout main 2>/dev/null || git checkout -b main

git add engine/ .gitignore 2>/dev/null || true

if git diff --cached --quiet; then
    echo "  Nothing new to commit."
else
    git commit -m "Add engine Python source files from droplet"
    git push origin main
    echo "  Pushed engine files to GitHub."
fi

# 6. Install webhook service
echo "▶ Setting up webhook service..."
# Download directly from GitHub in case local clone is behind
curl -fsSL "${RAW_BASE}/scripts/webhook_server.py" -o /root/webhook_server.py || \
    cp "$REPO_DIR/scripts/webhook_server.py" /root/webhook_server.py

cat > /etc/systemd/system/grid-webhook.service << SERVICE
[Unit]
Description=Grid Engine Auto-Deploy Webhook
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/grid-engine
Environment=WEBHOOK_SECRET=grid-engine-deploy
Environment=WEBHOOK_PORT=${WEBHOOK_PORT}
ExecStart=/root/grid-engine/venv/bin/python3 /root/webhook_server.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable grid-webhook
systemctl restart grid-webhook
echo "  Webhook service started."

# 7. Open firewall port for webhook
echo "▶ Opening port $WEBHOOK_PORT..."
ufw allow $WEBHOOK_PORT/tcp 2>/dev/null || true
iptables -I INPUT -p tcp --dport $WEBHOOK_PORT -j ACCEPT 2>/dev/null || true

# 8. Set up nginx reverse proxy for dashboard (port 80 → 5050)
echo "▶ Setting up nginx reverse proxy..."
apt-get install -y nginx apache2-utils 2>/dev/null | grep -E "^(Setting up|Already)" || true

# Install nginx site config
curl -fsSL "${RAW_BASE}/scripts/nginx-grid-engine.conf" \
    -o /etc/nginx/sites-available/grid-engine || \
    cp "$REPO_DIR/scripts/nginx-grid-engine.conf" /etc/nginx/sites-available/grid-engine

# Enable site, disable default
ln -sf /etc/nginx/sites-available/grid-engine /etc/nginx/sites-enabled/grid-engine
rm -f /etc/nginx/sites-enabled/default

# Create htpasswd file if it doesn't exist yet
if [ ! -f /etc/nginx/.htpasswd ]; then
    # Default: user=admin, password=grid — CHANGE THIS after setup
    htpasswd -bc /etc/nginx/.htpasswd admin grid
    echo "  Created default credentials: admin / grid (change with: htpasswd /etc/nginx/.htpasswd admin)"
else
    echo "  htpasswd already exists — skipping."
fi

ufw allow 80/tcp 2>/dev/null || true
nginx -t && systemctl enable nginx && systemctl reload nginx
echo "  nginx proxy active — dashboard at http://165.232.101.253"

# 9. Test it
sleep 1
echo "▶ Testing webhook..."
RESP=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:$WEBHOOK_PORT/deploy)
if [ "$RESP" = "200" ]; then
    echo "  Webhook responding OK (HTTP $RESP)"
else
    echo "  Warning: got HTTP $RESP — check: journalctl -u grid-webhook -n 20"
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Setup complete!                                  ║"
echo "╠══════════════════════════════════════════════════╣"
echo "║  Dashboard: http://165.232.101.253                ║"
echo "║    Login:   admin / grid  (CHANGE THIS!)          ║"
echo "║  Webhook: http://165.232.101.253:$WEBHOOK_PORT/deploy      ║"
echo "║  Secret:  grid-engine-deploy                     ║"
echo "║  Logs:    journalctl -u grid-webhook -f           ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "IMPORTANT: Change the dashboard password:"
echo "  htpasswd /etc/nginx/.htpasswd admin"
echo ""
echo "NEXT: Add the GitHub webhook at:"
echo "  https://github.com/ashskett/btc-dashboard/settings/hooks/new"
echo "  Payload URL: http://165.232.101.253:$WEBHOOK_PORT/deploy"
echo "  Content-type: application/json"
echo "  Secret: grid-engine-deploy"
echo "  Events: Just the push event"
echo ""
