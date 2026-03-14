from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import json, os, base64, time, subprocess, signal, sys
import requests as req
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from engine_log import is_logging_enabled, set_logging_enabled, read_log, clear_log

load_dotenv()

app = Flask(__name__, static_folder='.')
CORS(app)

STATUS_FILE  = "engine_status.json"
API_KEY      = os.getenv("THREECOMMAS_API_KEY")
API_SECRET   = os.getenv("THREECOMMAS_API_SECRET")  # path to RSA private key PEM file
ACCOUNT_ID   = os.getenv("THREECOMMAS_ACCOUNT_ID")
BASE_3C      = "https://api.3commas.io/public/api"


def _load_private_key():
    path = API_SECRET.strip() if API_SECRET else "/root/grid-engine/3commas_private.pem"
    with open(path, "rb") as f:
        pem = f.read()
    return serialization.load_pem_private_key(pem, password=None)


def signed_request(method, path, body=None):
    payload     = json.dumps(body) if body else ""
    sign_target = ("/public/api" + path + payload).encode()
    private_key = _load_private_key()
    sig = base64.b64encode(
        private_key.sign(sign_target, padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    headers = {"Apikey": API_KEY, "Signature": sig, "Content-Type": "application/json"}
    return req.request(method, BASE_3C + path, headers=headers, data=payload, timeout=10)


# ── Serve dashboard HTML ──────────────────────────────────
@app.route("/")
def index():
    return send_from_directory('.', 'dashboard.html')


# ── Engine status ─────────────────────────────────────────
@app.route("/status")
def status():
    try:
        with open(STATUS_FILE) as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
_engine_proc = None

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

@app.route("/engine/start", methods=["POST"])
def engine_start():
    global _engine_proc
    if _engine_running():
        return jsonify({"ok": True, "msg": "Already running"})
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        python_bin = sys.executable
        _engine_proc = subprocess.Popen(
            [python_bin, os.path.join(script_dir, "engine.py")],
            cwd=script_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        time.sleep(0.5)
        if _engine_proc.poll() is not None:
            return jsonify({"ok": False, "msg": "Engine exited immediately — check logs"}), 500
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
