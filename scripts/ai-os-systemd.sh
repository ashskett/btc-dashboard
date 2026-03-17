#!/bin/bash
# =============================================================================
# AI OS — Convert from tmux to systemd service
# Run once on droplet: bash <(curl -s https://raw.githubusercontent.com/ashskett/btc-dashboard/main/scripts/ai-os-systemd.sh)
# =============================================================================
set -e

cat > /etc/systemd/system/ai-os.service << SERVICE
[Unit]
Description=Ash's AI OS API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/ai-os
EnvironmentFile=/root/ai-os/.env
ExecStart=/root/ai-os/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

# Stop tmux session if running
tmux kill-session -t ai-os 2>/dev/null || true

systemctl daemon-reload
systemctl enable ai-os
systemctl start ai-os

sleep 2
if systemctl is-active --quiet ai-os; then
    echo "ai-os service is running"
    echo "Logs: journalctl -u ai-os -f"
else
    echo "WARNING: service failed to start — check: journalctl -u ai-os -n 30"
fi
