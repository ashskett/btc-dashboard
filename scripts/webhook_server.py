#!/usr/bin/env python3
"""
Webhook server for auto-deploy on GitHub push.
Listens on port 9001 for POST /deploy from GitHub.
Runs: git pull → rsync engine files → restart engine.
"""
import os
import sys
import hmac
import hashlib
import subprocess
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "grid-engine-deploy")
DEPLOY_BRANCH  = os.environ.get("DEPLOY_BRANCH", "claude/grid-engine-chat-review-hEEGu")
REPO_DIR = "/root/btc-dashboard"
ENGINE_DIR = "/root/grid-engine"
LOG_FILE = "/root/grid-engine/deploy.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("webhook")


def run(cmd, cwd=None):
    result = subprocess.run(
        cmd, shell=True, cwd=cwd,
        capture_output=True, text=True
    )
    if result.stdout:
        log.info(result.stdout.strip())
    if result.stderr:
        log.warning(result.stderr.strip())
    return result.returncode


def deploy():
    log.info("=== Deploy started ===")
    log.info(f"Branch: {DEPLOY_BRANCH}")

    # 1. Fetch and checkout the configured deploy branch
    run(f"git fetch origin {DEPLOY_BRANCH}", cwd=REPO_DIR)
    rc = run(f"git checkout {DEPLOY_BRANCH}", cwd=REPO_DIR)
    if rc != 0:
        # Branch may not exist locally yet — create tracking branch
        rc = run(f"git checkout -b {DEPLOY_BRANCH} origin/{DEPLOY_BRANCH}", cwd=REPO_DIR)
    if rc != 0:
        log.error("git checkout failed — aborting deploy")
        return
    rc = run(f"git pull origin {DEPLOY_BRANCH}", cwd=REPO_DIR)
    if rc != 0:
        log.error("git pull failed — aborting deploy")
        return

    # 2. Sync engine Python files (never overwrite secrets/state)
    run(f"rsync -av --exclude='.env' --exclude='*.pem' --exclude='*.json' "
        f"--exclude='*.log' --exclude='*.jsonl' --exclude='__pycache__' "
        f"--exclude='venv' "
        f"{REPO_DIR}/engine/ {ENGINE_DIR}/")

    # 2b. Update this webhook server script itself
    run(f"cp {REPO_DIR}/scripts/webhook_server.py /root/webhook_server.py")

    # 3. Sync dashboard.html if present in repo
    dash = f"{REPO_DIR}/engine/dashboard.html"
    if os.path.exists(dash):
        run(f"cp {dash} {ENGINE_DIR}/dashboard.html")

    # 4. Restart Flask — kill by port first (works regardless of tmux state),
    #    then start fresh in the tmux grid session (or background if tmux absent).
    #    Using lsof+kill ensures the old process is gone before we start the new one,
    #    which prevents "Address already in use" races that caused silent restart failures.
    restart = (
        # Kill whatever is holding port 5050 (Flask), regardless of tmux
        "lsof -ti:5050 | xargs kill -9 2>/dev/null; sleep 2; "
        # Ensure tmux grid session exists
        "tmux new-session -d -s grid 2>/dev/null; "
        # Start Flask inside tmux grid session
        "tmux send-keys -t grid "
        "'cd /root/grid-engine && source venv/bin/activate && python dashboard_server.py' Enter"
    )
    run(restart)

    log.info("=== Deploy complete ===")

    # 5. Restart this webhook process so the updated script takes effect immediately.
    #    os.execv replaces the current process image — safe because the HTTP response
    #    was already sent before this thread started.
    log.info("Restarting webhook server with updated script...")
    import time as _time
    _time.sleep(1)
    os.execv(sys.executable, [sys.executable, "/root/webhook_server.py"])


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/deploy", "/"):
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # Verify GitHub signature if present
        sig_header = self.headers.get("X-Hub-Signature-256", "")
        if sig_header:
            expected = "sha256=" + hmac.new(
                WEBHOOK_SECRET.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                log.warning(f"Invalid signature from {self.client_address[0]}")
                self.send_response(403)
                self.end_headers()
                return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Deploy triggered\n")

        import threading
        threading.Thread(target=deploy, daemon=True).start()

    def log_message(self, fmt, *args):
        log.info(f"{self.client_address[0]} - {fmt % args}")


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", 9001))
    log.info(f"Webhook server listening on :{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
