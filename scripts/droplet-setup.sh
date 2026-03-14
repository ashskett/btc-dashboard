#!/bin/bash
# One-time setup script: run this on the droplet to enable auto-deploy from git
# Usage: bash droplet-setup.sh

set -e

REPO_URL="http://127.0.0.1:41939/git/ashskett/btc-dashboard"  # update if needed
DEPLOY_DIR="/root/grid-engine"
WEBHOOK_PORT=9000
CLAUDE_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOz0Hy8wY5c/3WHMdxFB3s7g8TkOiNdgPHkp2cllNiR7 claude-code@btc-dashboard"

echo "=== Grid Engine Auto-Deploy Setup ==="

# 1. Add Claude's SSH key
echo "Adding Claude Code SSH key..."
mkdir -p ~/.ssh
if ! grep -q "claude-code@btc-dashboard" ~/.ssh/authorized_keys 2>/dev/null; then
    echo "$CLAUDE_PUBKEY" >> ~/.ssh/authorized_keys
    echo "  SSH key added."
else
    echo "  SSH key already present."
fi
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys

# 2. Install webhook listener if not present
if ! command -v webhook &>/dev/null; then
    echo "Installing webhook..."
    apt-get install -y webhook 2>/dev/null || pip3 install webhook-listener 2>/dev/null || true
fi

# 3. Create the deploy script
cat > /root/grid-engine/auto-deploy.sh << 'DEPLOY'
#!/bin/bash
# Auto-deploy: pull latest dashboard from git and restart dashboard server
set -e
cd /root/grid-engine

echo "[$(date)] Auto-deploy triggered" >> /root/grid-engine/deploy.log

# Pull latest HTML files if git repo exists
if [ -d "/root/btc-dashboard/.git" ]; then
    cd /root/btc-dashboard
    git pull origin main 2>&1 | tee -a /root/grid-engine/deploy.log
    # Copy dashboard files to grid-engine
    cp *.html /root/grid-engine/dashboard/ 2>/dev/null || true
    cd /root/grid-engine
fi

# Restart dashboard server if running
if systemctl is-active --quiet dashboard 2>/dev/null; then
    systemctl restart dashboard
    echo "[$(date)] Dashboard service restarted" >> /root/grid-engine/deploy.log
elif pgrep -f dashboard_server.py > /dev/null; then
    pkill -f dashboard_server.py || true
    sleep 1
    cd /root/grid-engine && source venv/bin/activate && nohup python3 dashboard_server.py &
    echo "[$(date)] Dashboard server restarted" >> /root/grid-engine/deploy.log
fi

echo "[$(date)] Deploy complete" >> /root/grid-engine/deploy.log
DEPLOY
chmod +x /root/grid-engine/auto-deploy.sh

# 4. Set up simple HTTP webhook listener using Python
cat > /root/grid-engine/webhook_server.py << 'WEBHOOK'
#!/usr/bin/env python3
"""Simple webhook server for auto-deploy on git push"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import subprocess, json, hmac, hashlib, os

SECRET = os.environ.get('WEBHOOK_SECRET', 'grid-engine-deploy')

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/deploy':
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)

        # Optional: verify secret
        sig = self.headers.get('X-Hub-Signature-256', '')
        if sig:
            expected = 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                self.send_response(403)
                self.end_headers()
                return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Deploy triggered')

        # Run deploy in background
        subprocess.Popen(['/root/grid-engine/auto-deploy.sh'])
        print(f"Deploy triggered from {self.client_address[0]}")

    def log_message(self, *args):
        pass  # Suppress logs

if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', 9000), WebhookHandler)
    print(f"Webhook server running on port 9000")
    server.serve_forever()
WEBHOOK
chmod +x /root/grid-engine/webhook_server.py

# 5. Create systemd service for webhook
cat > /etc/systemd/system/grid-webhook.service << 'SERVICE'
[Unit]
Description=Grid Engine Auto-Deploy Webhook
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/grid-engine
ExecStart=/root/grid-engine/venv/bin/python3 /root/grid-engine/webhook_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable grid-webhook
systemctl start grid-webhook

echo ""
echo "=== Setup Complete ==="
echo "Webhook listener running on port 9000"
echo "Endpoint: http://165.232.101.253:9000/deploy"
echo ""
echo "To test: curl -X POST http://165.232.101.253:9000/deploy"
echo "Logs: tail -f /root/grid-engine/deploy.log"
