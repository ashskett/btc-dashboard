#!/usr/bin/env bash
# deploy.sh — pull latest engine files from git and restart the engine
#
# Run on the droplet (one-liner):
#   bash <(curl -fsSL https://raw.githubusercontent.com/ashskett/btc-dashboard/claude/grid-engine-chat-review-hEEGu/scripts/deploy.sh)

set -euo pipefail

BRANCH="claude/grid-engine-chat-review-hEEGu"
REPO="https://github.com/ashskett/btc-dashboard"
ENGINE_DIR="/root/grid-engine"
TMP_DIR="/tmp/btc-dashboard-deploy"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║      BTC Grid Engine — Deploy        ║"
echo "╚══════════════════════════════════════╝"
echo "Branch : $BRANCH"
echo "Target : $ENGINE_DIR"
echo ""

# ── 1. Pull repo ──────────────────────────────────────────────────────────────
if [ -d "$TMP_DIR/.git" ]; then
  echo "▸ Updating existing clone..."
  git -C "$TMP_DIR" fetch origin "$BRANCH"
  git -C "$TMP_DIR" checkout "$BRANCH"
  git -C "$TMP_DIR" reset --hard "origin/$BRANCH"
else
  echo "▸ Cloning repo..."
  rm -rf "$TMP_DIR"
  git clone --depth=1 -b "$BRANCH" "$REPO" "$TMP_DIR"
fi

# ── 2. Copy engine Python files ───────────────────────────────────────────────
echo "▸ Copying Python engine files..."
for f in "$TMP_DIR/engine/"*.py; do
  fname=$(basename "$f")
  cp "$f" "$ENGINE_DIR/$fname"
  echo "  ✓ $fname"
done

# ── 3. Copy dashboard HTML ────────────────────────────────────────────────────
echo "▸ Copying dashboard.html..."
cp "$TMP_DIR/engine/dashboard.html" "$ENGINE_DIR/dashboard.html"
echo "  ✓ dashboard.html"

# ── 4. Install systemd service (first deploy only) ───────────────────────────
SERVICE_FILE="/etc/systemd/system/grid-engine.service"
if [ ! -f "$SERVICE_FILE" ]; then
  echo "▸ Installing systemd service (first time)..."
  cp "$TMP_DIR/scripts/grid-engine.service" "$SERVICE_FILE"
  systemctl daemon-reload
  systemctl enable grid-engine
  echo "  ✓ grid-engine.service installed + enabled (auto-starts on reboot)"
else
  # Update in case service file changed
  cp "$TMP_DIR/scripts/grid-engine.service" "$SERVICE_FILE"
  systemctl daemon-reload
  echo "  ✓ systemd service updated"
fi

# ── 5. Restart the engine ─────────────────────────────────────────────────────
echo "▸ Restarting engine..."

# Prefer systemd if already managing the service
if systemctl is-active --quiet grid-engine 2>/dev/null; then
  systemctl restart grid-engine
  sleep 2
  if systemctl is-active --quiet grid-engine; then
    echo "  ✓ Restarted via systemd"
  else
    echo "  ✗ systemd restart failed — check: journalctl -u grid-engine -n 50"
    exit 1
  fi
else
  # Fall back to tmux
  echo "  (systemd service not active — using tmux)"

  # Kill any existing engine process in the tmux session
  if tmux has-session -t grid 2>/dev/null; then
    tmux send-keys -t grid C-c ''
    sleep 1
    tmux send-keys -t grid '' Enter
    sleep 1
  else
    tmux new-session -d -s grid
  fi

  START_CMD="cd $ENGINE_DIR && source venv/bin/activate && python dashboard_server.py"
  tmux send-keys -t grid "$START_CMD" Enter
  sleep 3

  # Quick health check
  if curl -sf http://localhost:5050/ping >/dev/null 2>&1; then
    echo "  ✓ Engine started in tmux session 'grid'"
  else
    echo "  ⚠ Engine may still be starting — check: tmux attach -t grid"
  fi

  echo ""
  echo "  Tip: run the following to switch to systemd and stop using tmux:"
  echo "  systemctl start grid-engine && systemctl enable grid-engine"
fi

# ── 6. Health check ───────────────────────────────────────────────────────────
echo ""
echo "▸ Health check..."
sleep 1
if curl -sf http://localhost:5050/ping >/dev/null 2>&1; then
  echo "  ✓ Dashboard responding at http://localhost:5050"
else
  echo "  ⚠ Dashboard not yet responding — may still be starting"
fi

echo ""
echo "╔══════════════════════════════════════╗"
echo "║          Deploy complete ✓           ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  Dashboard : http://165.232.101.253:5050"
echo "  Engine log: journalctl -u grid-engine -f   (if using systemd)"
echo "            : tmux attach -t grid             (if using tmux)"
echo ""
