from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import json, os, base64, time, subprocess, signal, sys, secrets
import requests as req
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from engine_log import is_logging_enabled, set_logging_enabled, read_log, clear_log

load_dotenv()

app = Flask(__name__, static_folder='.')
CORS(app)

# ── Auth token ────────────────────────────────────────────
_DASHBOARD_SECRET = None

def _ensure_secret():
    global _DASHBOARD_SECRET
    token = os.getenv("DASHBOARD_SECRET", "").strip()
    if token:
        _DASHBOARD_SECRET = token
        return
    token = secrets.token_hex(24)
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        lines = open(env_path).readlines() if os.path.exists(env_path) else []
        lines.append(f"DASHBOARD_SECRET={token}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
    except Exception as e:
        print(f"[auth] Warning: could not write token to .env: {e}")
    _DASHBOARD_SECRET = token
    print(f"\n{'='*64}")
    print(f"  DASHBOARD_SECRET generated and saved to .env")
    print(f"  Token: {token}")
    print(f"  Browser URL: http://165.232.101.253:5050/?token={token}")
    print(f"{'='*64}\n")

_ensure_secret()

_PUBLIC_PATHS = {"/", "/ping"}

@app.before_request
def check_token():
    if request.path in _PUBLIC_PATHS:
        return None
    if not _DASHBOARD_SECRET:
        return None
    token = (
        request.headers.get("X-Dashboard-Token") or
        request.args.get("token") or
        ""
    )
    if token != _DASHBOARD_SECRET:
        return jsonify({"error": "unauthorized"}), 401

STATUS_FILE  = "engine_status.json"
API_KEY      = os.getenv("THREECOMMAS_API_KEY")
API_SECRET   = os.getenv("THREECOMMAS_API_SECRET")  # path to RSA private key PEM file
ACCOUNT_ID   = os.getenv("THREECOMMAS_ACCOUNT_ID")
BASE_3C      = "https://api.3commas.io/public/api"


def _load_private_key():
    path = API_SECRET.strip() if API_SECRET else "/root/grid-engine/3commas_private.pem"
    if not os.path.exists(path):
        # Fall back to server default if configured path doesn't exist (e.g. stale Mac path in .env)
        path = "/root/grid-engine/3commas_private.pem"
    with open(path, "rb") as f:
        pem = f.read()
    return serialization.load_pem_private_key(pem, password=None)


def signed_request(method, path, body=None, params=None):
    payload     = json.dumps(body) if body else ""
    sign_target = ("/public/api" + path + payload).encode()
    private_key = _load_private_key()
    sig = base64.b64encode(
        private_key.sign(sign_target, padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    headers = {"Apikey": API_KEY, "Signature": sig, "Content-Type": "application/json"}
    return req.request(method, BASE_3C + path, headers=headers, data=payload, params=params, timeout=10)


# ── Health check (unauthenticated) ───────────────────────
@app.route("/ping")
def ping():
    return jsonify({"ok": True})


# ── Serve dashboard HTML ──────────────────────────────────
@app.route("/")
def index():
    return send_from_directory('.', 'dashboard.html')


# ── Engine status ─────────────────────────────────────────
@app.route("/status")
def status():
    if not os.path.exists(STATUS_FILE):
        return jsonify({"engine_running": False, "no_status_file": True})
    try:
        with open(STATUS_FILE) as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"engine_running": False, "parse_error": str(e)})


# ── Live BTC price via Coinbase ───────────────────────────
@app.route("/price")
def price():
    try:
        r = req.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Candles via ccxt ──────────────────────────────────────
@app.route("/candles")
def candles():
    try:
        import ccxt, time
        exchange = ccxt.coinbase()

        # Coinbase via ccxt only supports these granularities
        # Map UI timeframes to valid ccxt strings
        TF_MAP = {
            '5m':  '5m',
            '15m': '15m',
            '1h':  '1h',
            '4h':  '6h',   # Coinbase has no 4h — use 6h as nearest
            '1d':  '1d',
        }
        tf_raw = request.args.get("tf", "1h")
        tf     = TF_MAP.get(tf_raw, '1h')
        limit  = min(int(request.args.get("limit", 150)), 300)

        data = exchange.fetch_ohlcv("BTC/USDC", timeframe=tf, limit=limit)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Bots list ─────────────────────────────────────────────
@app.route("/bots")
def get_bots():
    try:
        ids = [b.strip() for b in os.getenv("GRID_BOT_IDS","").split(",") if b.strip()]
        bots = []
        for bid in ids:
            r = signed_request("GET", f"/ver1/grid_bots/{bid}")
            bots.append(r.json() if r.status_code == 200 else {"id": bid, "error": r.status_code})
        return jsonify(bots)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Bot start / stop ──────────────────────────────────────
@app.route("/bots/<bot_id>/start", methods=["POST"])
def start_bot(bot_id):
    try:
        r = signed_request("POST", f"/ver1/grid_bots/{bot_id}/enable")
        return jsonify({"ok": True, "code": r.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/bots/<bot_id>/stop", methods=["POST"])
def stop_bot(bot_id):
    try:
        r = signed_request("POST", f"/ver1/grid_bots/{bot_id}/disable")
        return jsonify({"ok": True, "code": r.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Bot fills (completed grid cycles) ─────────────────────
_fills_cache = {"data": None, "ts": 0.0}

@app.route("/bots/fills")
def bot_fills():
    global _fills_cache
    now = time.time()
    if _fills_cache["data"] is not None and now - _fills_cache["ts"] < 300:
        return jsonify(_fills_cache["data"])
    try:
        ids = [b.strip() for b in os.getenv("GRID_BOT_IDS","").split(",") if b.strip()]
        fills = []
        for i, bid in enumerate(ids[:3]):
            r = signed_request("GET", f"/ver1/grid_bots/{bid}/profits", params={"limit": 100})
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, list):
                continue
            for item in data:
                # Pick a representative price from the grid_lines (prefer the sell side)
                price = 0.0
                gls = item.get("grid_lines") or []
                for gl in gls:
                    if (gl.get("side") or "").lower() == "sell":
                        price = float(gl.get("price") or 0)
                        break
                if not price:
                    for gl in gls:
                        price = float(gl.get("price") or 0)
                        if price:
                            break
                fills.append({
                    "bot_id":     bid,
                    "bot_index":  i,
                    "time":       item.get("created_at"),
                    "price":      price,
                    "profit_usd": float(item.get("profit_usd") or item.get("usd_profit") or 0),
                })
        fills.sort(key=lambda x: x["time"] or "")
        _fills_cache = {"data": fills, "ts": now}
        return jsonify(fills)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Inventory mode override ───────────────────────────────
@app.route("/inventory/mode", methods=["POST"])
def set_inventory_mode():
    try:
        mode = request.json.get("mode", "NORMAL")
        with open("inventory_override.json", "w") as f:
            json.dump({"mode": mode}, f)
        return jsonify({"ok": True, "mode": mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Trendlines save/load ─────────────────────────────────
TRENDLINES_FILE = "trendlines.json"

@app.route("/trendlines/save", methods=["POST"])
def save_trendlines():
    try:
        data = request.json
        with open(TRENDLINES_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return jsonify({"ok": True, "count": len(data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/trendlines/load")
def load_trendlines():
    try:
        if os.path.exists(TRENDLINES_FILE):
            with open(TRENDLINES_FILE) as f:
                return jsonify(json.load(f))
        return jsonify([])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Inventory override ───────────────────────────────────
@app.route("/inventory/override/state")
def get_override_state():
    """Return current override state so the dashboard can restore UI on reload."""
    override_file = "inventory_override.json"
    try:
        if os.path.exists(override_file):
            data = json.load(open(override_file))
            return jsonify(data)
        return jsonify({"manual": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/inventory/override", methods=["POST", "DELETE"])
def inventory_override():
    try:
        override_file = "inventory_override.json"
        if request.method == "DELETE":
            if os.path.exists(override_file):
                data = json.load(open(override_file))
                data.pop("btc_ratio", None)
                data.pop("skew", None)
                data.pop("manual", None)
                with open(override_file, "w") as f:
                    json.dump(data, f)
            return jsonify({"ok": True, "cleared": True})

        body = request.json
        btc_ratio = float(body.get("btc_ratio", 0.5))
        skew      = float(body.get("skew", 0.0))

        # Clamp to safe ranges
        btc_ratio = max(0.0, min(1.0, btc_ratio))
        skew      = max(-0.25, min(0.25, skew))

        # Read existing file (may have mode set already)
        existing = {}
        if os.path.exists(override_file):
            try: existing = json.load(open(override_file))
            except: pass

        existing.update({"btc_ratio": btc_ratio, "skew": skew, "manual": True})
        with open(override_file, "w") as f:
            json.dump(existing, f, indent=2)

        return jsonify({"ok": True, "btc_ratio": btc_ratio, "skew": skew})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Trendline override ────────────────────────────────────
@app.route("/trendline", methods=["POST"])
def set_trendline():
    try:
        level = float(request.json.get("level", 66000))
        # Persist so engine.py can read it on next cycle
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        lines = open(env_path).readlines() if os.path.exists(env_path) else []
        new_lines = [l for l in lines if not l.startswith("TRENDLINE_LEVEL=")]
        new_lines.append(f"TRENDLINE_LEVEL={level}\n")
        with open(env_path, "w") as f:
            f.writelines(new_lines)
        return jsonify({"ok": True, "level": level})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Bot ID config ────────────────────────────────────────
@app.route("/config/bots", methods=["GET", "POST"])
def config_bots():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if request.method == "GET":
        return jsonify({"bot_ids": os.getenv("GRID_BOT_IDS", "")})
    body    = request.get_json(force=True, silent=True) or {}
    new_ids = body.get("bot_ids", "").strip()
    if not new_ids:
        return jsonify({"ok": False, "msg": "bot_ids required"}), 400
    try:
        lines   = open(env_path).readlines() if os.path.exists(env_path) else []
        updated = False
        for i, line in enumerate(lines):
            if line.startswith("GRID_BOT_IDS="):
                lines[i] = f"GRID_BOT_IDS={new_ids}\n"
                updated = True
                break
        if not updated:
            lines.append(f"GRID_BOT_IDS={new_ids}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
        os.environ["GRID_BOT_IDS"] = new_ids
        return jsonify({"ok": True, "msg": "Bot IDs saved — restart engine to apply"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ── Breakout state management ─────────────────────────────
@app.route("/breakout/clear", methods=["POST"])
def breakout_clear():
    state_file = os.path.join(os.path.dirname(__file__), "breakout_state.json")
    try:
        import json as _json
        _json.dump(
            {"consec_up": 0, "consec_down": 0, "active": None, "fire_price": None},
            open(state_file, "w")
        )
        return jsonify({"ok": True, "msg": "Breakout state cleared"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ── Logging endpoints ────────────────────────────────────
@app.route("/log/status")
def log_status():
    return jsonify({"enabled": is_logging_enabled()})

@app.route("/log/toggle", methods=["POST"])
def log_toggle():
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", not is_logging_enabled())
    set_logging_enabled(enabled)
    return jsonify({"enabled": enabled})

@app.route("/log/entries")
def log_entries():
    limit = int(request.args.get("limit", 500))
    return jsonify(read_log(limit))

@app.route("/log/clear", methods=["POST"])
def log_clear():
    clear_log()
    return jsonify({"ok": True})


# ── Engine process management ────────────────────────────
_engine_proc   = None
_engine_output = []   # rolling buffer of last 200 lines
_engine_lock   = __import__("threading").Lock()


def _drain_output(proc):
    """Background thread: drain engine stdout into _engine_output buffer."""
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        with _engine_lock:
            _engine_output.append(line)
            if len(_engine_output) > 200:
                _engine_output.pop(0)
    # process has exited — read any remaining bytes
    rest = proc.stdout.read()
    if rest:
        for line in rest.decode("utf-8", errors="replace").splitlines():
            with _engine_lock:
                _engine_output.append(line)
                if len(_engine_output) > 200:
                    _engine_output.pop(0)


def _engine_running():
    global _engine_proc
    if _engine_proc is None: return False
    if _engine_proc.poll() is not None:
        _engine_proc = None
        return False
    return True

@app.route("/engine/status")
def engine_status():
    return jsonify({"running": _engine_running()})

@app.route("/engine/output")
def engine_output():
    with _engine_lock:
        return jsonify({"lines": list(_engine_output)})

@app.route("/engine/start", methods=["POST"])
def engine_start():
    global _engine_proc
    if _engine_running():
        return jsonify({"ok": True, "msg": "Already running"})
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        python_bin = sys.executable
        with _engine_lock:
            _engine_output.clear()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        _engine_proc = subprocess.Popen(
            [python_bin, "-u", os.path.join(script_dir, "engine.py")],
            cwd=script_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        __import__("threading").Thread(
            target=_drain_output, args=(_engine_proc,), daemon=True
        ).start()
        time.sleep(1.5)
        if _engine_proc.poll() is not None:
            with _engine_lock:
                tail = "\n".join(_engine_output[-20:])
            return jsonify({"ok": False, "msg": f"Engine exited — output:\n{tail}"}), 500
        return jsonify({"ok": True, "msg": f"Engine started (pid {_engine_proc.pid})"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/engine/stop", methods=["POST"])
def engine_stop():
    global _engine_proc
    if not _engine_running():
        return jsonify({"ok": True, "msg": "Not running"})
    try:
        _engine_proc.terminate()
        _engine_proc.wait(timeout=5)
        _engine_proc = None
        return jsonify({"ok": True, "msg": "Engine stopped"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


if __name__ == "__main__":
    print("Dashboard running → open http://localhost:5050 in your browser")
    app.run(host="0.0.0.0", port=5050, debug=False)
