#!/bin/bash
# deploy.sh — push all engine files to the server
SERVER="root@165.232.101.253"
REMOTE="/root/grid-engine"

echo "Deploying to $SERVER..."
rsync -av --exclude='venv' --exclude='__pycache__' --exclude='*.bak' \
    ~/grid-engine/ $SERVER:$REMOTE/

echo "Done."
