#!/usr/bin/env python3
"""
Webhook server for auto-deploy on GitHub push.
Listens on port 9001 for:
  POST /deploy        — deploys grid engine (btc-dashboard repo)
  POST /deploy-ai-os  — deploys AI OS API (ai-os repo)
"""
import os
import hmac
import hashlib
import subprocess
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "grid-engine-deploy")
REPO_DIR       = "/root/btc-dashboard"
ENGINE_DIR     = "/root/grid-engine"
LOG_FILE       = "/root/grid-engine/deploy.log"

AI_OS_REPO_DIR = "/root/ai-os"
AI_OS_LOG_FILE = "/root/ai-os/deploy.log"

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
    log.info("=== Grid Engine Deploy started ===")

    # 1. Pull latest code from whichever branch is currently checked out
    branch = subprocess.run(
        "git rev-parse --abbrev-ref HEAD", shell=True, cwd=REPO_DIR,
        capture_output=True, text=True
    ).stdout.strip() or "main"
    rc = run(f"git pull origin {branch}", cwd=REPO_DIR)
    if rc != 0:
        log.error("git pull failed — aborting deploy")
        return

    # 2. Sync engine Python files (never overwrite secrets/state)
    run(f"rsync -av --exclude='.env' --exclude='*.pem' --exclude='*.json' "
        f"--exclude='*.log' --exclude='*.jsonl' --exclude='__pycache__' "
        f"--exclude='venv' "
        f"{REPO_DIR}/engine/ {ENGINE_DIR}/")

    # 3. Sync dashboard.html if present in repo
    dash = f"{REPO_DIR}/engine/dashboard.html"
    if os.path.exists(dash):
        run(f"cp {dash} {ENGINE_DIR}/dashboard.html")

    # 4. Restart engine (sends Ctrl-C to tmux grid session then restarts)
    restart = (
        "tmux send-keys -t grid C-c Enter; sleep 2; "
        "tmux send-keys -t grid "
        "'cd /root/grid-engine && source venv/bin/activate && python dashboard_server.py' Enter"
    )
    run(restart)

    log.info("=== Grid Engine Deploy complete ===")


def deploy_ai_os():
    log.info("=== AI OS Deploy started ===")

    if not os.path.isdir(AI_OS_REPO_DIR):
        log.error(f"{AI_OS_REPO_DIR} does not exist — run ai-os-setup.sh first")
        return

    # 1. Pull latest from main
    branch = subprocess.run(
        "git rev-parse --abbrev-ref HEAD", shell=True, cwd=AI_OS_REPO_DIR,
        capture_output=True, text=True
    ).stdout.strip() or "main"
    rc = run(f"git pull origin {branch}", cwd=AI_OS_REPO_DIR)
    if rc != 0:
        log.error("git pull failed for ai-os — aborting")
        return

    # 2. Install/update Python dependencies
    run(f"{AI_OS_REPO_DIR}/venv/bin/pip install -r {AI_OS_REPO_DIR}/requirements.txt -q")

    # 3. Restart AI OS in tmux session ai-os
    restart = (
        "tmux send-keys -t ai-os C-c Enter; sleep 2; "
        "tmux send-keys -t ai-os "
        "'cd /root/ai-os && source venv/bin/activate && "
        "uvicorn main:app --host 0.0.0.0 --port 8080' Enter"
    )
    run(restart)

    log.info("=== AI OS Deploy complete ===")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/deploy", "/", "/deploy-ai-os"):
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
        if self.path == "/deploy-ai-os":
            threading.Thread(target=deploy_ai_os, daemon=True).start()
        else:
            threading.Thread(target=deploy, daemon=True).start()

    def log_message(self, fmt, *args):
        log.info(f"{self.client_address[0]} - {fmt % args}")


if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", 9001))
    log.info(f"Webhook server listening on :{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
