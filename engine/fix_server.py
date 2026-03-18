#!/usr/bin/env python3
"""
One-shot server fix script.

Run by engine.py on startup (via subprocess.Popen with start_new_session=True).
Detaches from Flask, kills old Flask by port, copies the new webhook_server.py,
restarts the webhook process, then starts fresh Flask with the code now on disk.

engine.py removes this file immediately after spawning it, so it only runs once.
"""
import os, sys, subprocess, time, shutil

GRID_DIR = "/root/grid-engine"


def _find_python():
    """Return the venv Python executable, fall back to sys.executable."""
    for candidate in [
        os.path.join(GRID_DIR, "venv/bin/python"),
        os.path.join(GRID_DIR, "venv/bin/python3"),
    ]:
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def main():
    engine_pid = int(sys.argv[1]) if len(sys.argv) > 1 else None

    # Brief pause so engine.py finishes its own startup cleanup
    time.sleep(2)

    # 1. Update webhook_server.py and restart the webhook process
    wh_src = os.path.join(GRID_DIR, "webhook_server.py")
    wh_dst = "/root/webhook_server.py"
    if os.path.exists(wh_src):
        try:
            shutil.copy2(wh_src, wh_dst)
            subprocess.run("pkill -f 'python.*webhook_server' 2>/dev/null || true", shell=True)
            time.sleep(1)
            subprocess.Popen(
                [sys.executable, wh_dst],
                start_new_session=True,
                close_fds=True,
                stdout=open(os.path.join(GRID_DIR, "webhook.log"), "a"),
                stderr=subprocess.STDOUT,
            )
            print("[fix_server] Webhook updated and restarted", flush=True)
        except Exception as e:
            print(f"[fix_server] Webhook update failed: {e}", flush=True)

    # 2. Kill engine.py (our parent) — prevents duplicate engines after Flask restart
    if engine_pid:
        try:
            os.kill(engine_pid, 9)
        except Exception:
            pass

    # 3. Kill anything on port 5050 (old Flask)
    subprocess.run("lsof -ti:5050 | xargs kill -9 2>/dev/null || true", shell=True)
    time.sleep(2)

    # 4. Start new Flask from the files now on disk
    python_bin = _find_python()
    log_path = os.path.join(GRID_DIR, "flask_restart.log")
    try:
        subprocess.Popen(
            [python_bin, "-u", os.path.join(GRID_DIR, "dashboard_server.py")],
            cwd=GRID_DIR,
            start_new_session=True,
            close_fds=True,
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        print(f"[fix_server] New Flask started (log: {log_path})", flush=True)
    except Exception as e:
        print(f"[fix_server] Failed to start Flask: {e}", flush=True)


if __name__ == "__main__":
    main()
