from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import json, os, base64, time, subprocess, signal, sys, secrets, threading, shutil
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
    # Always read directly from .env file first so restarts pick up the
    # persisted token even when the env var wasn't inherited (e.g. start_new_session).
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        for line in (open(env_path).readlines() if os.path.exists(env_path) else []):
            if line.startswith("DASHBOARD_SECRET="):
                persisted = line.split("=", 1)[1].strip()
                if persisted:
                    _DASHBOARD_SECRET = persisted
                    return
    except Exception:
        pass
    # Fall back to env var (e.g. set externally)
    token = os.getenv("DASHBOARD_SECRET", "").strip()
    if token:
        _DASHBOARD_SECRET = token
        return
    # Generate a new token and write it to .env (replace any existing line)
    token = secrets.token_hex(24)
    try:
        lines = open(env_path).readlines() if os.path.exists(env_path) else []
        lines = [l for l in lines if not l.startswith("DASHBOARD_SECRET=")]
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

_PUBLIC_PATHS = {"/", "/ping", "/deploy", "/account/balance/raw", "/macro", "/macro/mobile", "/mobile", "/notifications", "/pnl-page", "/pnl-page/"}

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


def _fix_pem_line_endings(path: str) -> None:
    """Strip Windows CRLF line endings from PEM file (silently no-ops if already clean)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if b"\r" in raw:
            with open(path, "wb") as f:
                f.write(raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
            print(f"PEM fix: removed CR line endings from {path}")
    except Exception as e:
        print(f"Warning: could not fix PEM line endings for {path}: {e}")


def _load_private_key():
    path = API_SECRET.strip() if API_SECRET else "/root/grid-engine/3commas_private.pem"
    if not os.path.exists(path):
        # Fall back to server default if configured path doesn't exist (e.g. stale Mac path in .env)
        path = "/root/grid-engine/3commas_private.pem"
    _fix_pem_line_endings(path)
    with open(path, "rb") as f:
        pem = f.read()
    return serialization.load_pem_private_key(pem, password=None)


def signed_request(method, path, body=None, params=None):
    payload     = json.dumps(body) if body else ""
    qs          = ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))) if params else ""
    sign_target = ("/public/api" + path + qs + payload).encode()
    private_key = _load_private_key()
    sig = base64.b64encode(
        private_key.sign(sign_target, padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    headers = {"Apikey": API_KEY, "Signature": sig, "Content-Type": "application/json"}
    return req.request(method, BASE_3C + path, headers=headers, data=payload, params=params, timeout=10)


# ── Health check (unauthenticated — intentionally minimal) ───────────────
@app.route("/ping")
def ping():
    # Public: just confirms the server is alive. No debug info exposed.
    debug = request.args.get("debug")
    if debug:
        # Debug modes require auth
        err = check_token()
        if err:
            return err
        if debug == "balance":
            try:
                signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/load_balances")
                time.sleep(2)
                r = signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/pie_chart_data")
                return jsonify({"status": r.status_code, "data": r.json() if r.ok else r.text[:500]})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        if debug == "version":
            return jsonify({"public_paths": list(_PUBLIC_PATHS), "pid": os.getpid()})
        if debug == "state":
            script_dir = os.path.dirname(os.path.abspath(__file__))
            result = {}
            for fname in ["inventory_override.json", "breakout_state.json",
                          "regime_state.json", "grid_state.json", "engine_status.json"]:
                fpath = os.path.join(script_dir, fname)
                try:
                    result[fname] = json.load(open(fpath)) if os.path.exists(fpath) else None
                except Exception as e:
                    result[fname] = {"error": str(e)}
            return jsonify(result)
        if debug == "log":
            limit = int(request.args.get("limit", 30))
            return jsonify(read_log(limit))
        if debug == "dcabots":
            try:
                from threecommas_dca import get_dca_bots
                bots = get_dca_bots()
                slim = [{
                    "id": b.get("id"),
                    "name": b.get("name"),
                    "is_enabled": b.get("is_enabled"),
                    "enabled": b.get("enabled"),
                    "active_deals_count": b.get("active_deals_count"),
                    "pairs": b.get("pairs"),
                } for b in bots]
                return jsonify({"count": len(slim), "bots": slim})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


# ── Serve dashboard HTML ──────────────────────────────────
@app.route("/")
def index():
    return send_from_directory('.', 'dashboard.html')

@app.route("/pnl-page", strict_slashes=False)
def pnl_page():
    return send_from_directory('.', 'pnl.html')

@app.route("/macro")
def macro_desktop():
    return send_from_directory('.', 'btc_macro_dashboard.html')

@app.route("/macro/mobile")
def macro_mobile():
    return send_from_directory('.', 'btc_macro_dashboard_mobile.html')

@app.route("/mobile")
def mobile_dashboard():
    return send_from_directory('.', 'dashboard_mobile.html')


# ── Engine status ─────────────────────────────────────────
@app.route("/status")
def status():
    if not os.path.exists(STATUS_FILE):
        return jsonify({"engine_running": False, "no_status_file": True})
    try:
        with open(STATUS_FILE) as f:
            data = json.load(f)
        # Inject deployed_tiers from grid_state.json so the chart can render
        # lines that match the actual 3Commas order positions (fixed at deploy
        # time), rather than the current-cycle recalculation which drifts with
        # every price tick.
        grid_state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "grid_state.json")
        if os.path.exists(grid_state_file):
            try:
                gs = json.load(open(grid_state_file))
                if gs.get("deployed_tiers"):
                    data["deployed_tiers"] = gs["deployed_tiers"]
            except Exception:
                pass
        return jsonify(data)
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


def _aggregate_candles(candles_in, period):
    """Aggregate OHLCV candles into a larger period (45m, 1w, 1M)."""
    import datetime
    from collections import defaultdict
    buckets = defaultdict(list)
    for c in candles_in:
        if period == '45m':
            # Snap timestamp down to nearest 45-min boundary (seconds)
            ts_s = c[0] // 1000
            key  = (ts_s // 2700) * 2700 * 1000  # 2700s = 45min, result in ms
        elif period == '1w':
            dt = datetime.datetime.utcfromtimestamp(c[0] / 1000)
            anchor = dt - datetime.timedelta(days=dt.weekday())  # Monday
            key = int(datetime.datetime(anchor.year, anchor.month, anchor.day,
                                        tzinfo=datetime.timezone.utc).timestamp() * 1000)
        else:  # '1M'
            dt = datetime.datetime.utcfromtimestamp(c[0] / 1000)
            anchor = datetime.datetime(dt.year, dt.month, 1)
            key = int(datetime.datetime(anchor.year, anchor.month, anchor.day,
                                        tzinfo=datetime.timezone.utc).timestamp() * 1000)
        buckets[key].append(c)
    result = []
    for ts_ms, cs in sorted(buckets.items()):
        result.append([ts_ms, cs[0][1], max(c[2] for c in cs),
                        min(c[3] for c in cs), cs[-1][4], sum(c[5] for c in cs)])
    return result


# ── Candles via ccxt ──────────────────────────────────────
@app.route("/candles")
def candles():
    try:
        import ccxt
        exchange = ccxt.coinbase()

        # Coinbase via ccxt only supports these granularities
        # Map UI timeframes to valid ccxt strings
        TF_MAP = {
            '1m':  '1m',
            '5m':  '5m',
            '15m': '15m',
            '1h':  '1h',
            '4h':  '6h',   # Coinbase has no 4h — use 6h as nearest
            '1d':  '1d',
            # 1w and 1M are not native Coinbase granularities — aggregated below
        }
        tf_raw    = request.args.get("tf", "1h")
        limit     = min(int(request.args.get("limit", 150)), 500)
        before_ms = request.args.get("before")

        # 45m: aggregate 3×15m candles
        if tf_raw == '45m':
            fetch_limit = limit * 3 + 3  # extra buffer for alignment
            if before_ms:
                since_45 = int(before_ms) - fetch_limit * 900_000
                raw15 = exchange.fetch_ohlcv("BTC/USDC", timeframe='15m', since=since_45, limit=fetch_limit)
                raw15 = [c for c in raw15 if c[0] < int(before_ms)]
            else:
                raw15 = exchange.fetch_ohlcv("BTC/USDC", timeframe='15m', limit=fetch_limit)
            data = _aggregate_candles(raw15, '45m')
            return jsonify(data[-limit:])

        # Weekly / Monthly: Coinbase has no native granularity for these.
        # Paginate daily candles (≤300/call) to cover the requested range.
        if tf_raw in ('1w', '1M'):
            import time as _time
            BATCH      = 300
            # Cap: 1w → max 4 years of daily data; 1M → max 6 years
            max_days   = 4 * 365 if tf_raw == '1w' else 6 * 365
            days_per   = 7 if tf_raw == '1w' else 31
            days_needed = min(limit * days_per, max_days)
            end_ms     = int(before_ms) if before_ms else int(_time.time() * 1000)
            start_ms   = end_ms - days_needed * 86_400_000

            # Paginate forward from start_ms until we reach end_ms
            daily, batch_since = [], start_ms
            while batch_since < end_ms:
                batch = exchange.fetch_ohlcv("BTC/USDC", timeframe='1d',
                                             since=batch_since, limit=BATCH)
                if not batch:
                    break
                daily.extend(batch)
                if batch[-1][0] >= end_ms:
                    break
                batch_since = batch[-1][0] + 86_400_000

            # Deduplicate, sort, trim to window
            seen, deduped = set(), []
            for c in daily:
                if c[0] not in seen:
                    seen.add(c[0]); deduped.append(c)
            deduped.sort(key=lambda c: c[0])
            if before_ms:
                deduped = [c for c in deduped if c[0] < int(before_ms)]

            data = _aggregate_candles(deduped, tf_raw)
            return jsonify(data[-limit:])

        tf = TF_MAP.get(tf_raw, '1h')

        if before_ms:
            # Fetch a window of candles ending strictly before the given timestamp.
            # ccxt fetch_ohlcv(since) fetches from that point forward, so we back-calculate.
            TF_MS = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '45m': 2_700_000,
                     '1h': 3_600_000, '6h': 21_600_000, '1d': 86_400_000}
            tf_ms = TF_MS.get(tf, 3_600_000)
            since = int(before_ms) - limit * tf_ms
            data  = exchange.fetch_ohlcv("BTC/USDC", timeframe=tf, since=since, limit=limit)
            # Strip any candles that overlap with what the frontend already has
            data  = [c for c in data if c[0] < int(before_ms)]
        else:
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
def _engine_bot_ids():
    """Return the set of bot IDs managed by this engine (from GRID_BOT_IDS env var)."""
    return {b.strip() for b in os.getenv("GRID_BOT_IDS", "").split(",") if b.strip()}


@app.route("/bots/<bot_id>/start", methods=["POST"])
def start_bot(bot_id):
    if bot_id not in _engine_bot_ids():
        return jsonify({"error": "Bot not managed by this engine"}), 403
    try:
        r = signed_request("POST", f"/ver1/grid_bots/{bot_id}/enable")
        return jsonify({"ok": True, "code": r.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/bots/<bot_id>/stop", methods=["POST"])
def stop_bot(bot_id):
    if bot_id not in _engine_bot_ids():
        return jsonify({"error": "Bot not managed by this engine"}), 403
    try:
        r = signed_request("POST", f"/ver1/grid_bots/{bot_id}/disable")
        return jsonify({"ok": True, "code": r.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Tier budget config ────────────────────────────────────
from threecommas import load_tier_budgets, save_tier_budgets

@app.route("/budgets", methods=["GET"])
def get_budgets():
    return jsonify(load_tier_budgets())

@app.route("/budgets", methods=["POST"])
def set_budgets():
    """Set tier budget percentages. Body: [{"name":"inner","pct":30}, ...]"""
    try:
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, list):
            return jsonify({"ok": False, "msg": "Expected array of {name, pct}"}), 400
        budgets = []
        for item in body:
            name = item.get("name", "").strip()
            pct  = float(item.get("pct", 0))
            if not name:
                continue
            budgets.append({"name": name, "pct": round(pct, 1)})
        total = sum(b["pct"] for b in budgets)
        if total > 100:
            return jsonify({"ok": False, "msg": f"Total budget {total}% exceeds 100%"}), 400
        save_tier_budgets(budgets)
        return jsonify({"ok": True, "budgets": budgets, "total_pct": total,
                        "reserve_pct": round(100 - total, 1)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ── Grid bot capital allocation ───────────────────────────
@app.route("/bots/<bot_id>/capital", methods=["POST"])
def set_grid_bot_capital(bot_id):
    ids = [b.strip() for b in os.getenv("GRID_BOT_IDS","").split(",") if b.strip()]
    if bot_id not in ids:
        return jsonify({"ok": False, "error": "Bot not managed by this engine"}), 403
    try:
        body      = request.get_json(force=True, silent=True) or {}
        total_usd = float(body.get("total_usd", 0))
        btc_price = float(body.get("btc_price", 0))
        if total_usd <= 0:
            return jsonify({"ok": False, "error": "total_usd must be > 0"}), 400

        # Fetch current config
        r = signed_request("GET", f"/ver1/grid_bots/{bot_id}")
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"3Commas {r.status_code}: {r.text[:200]}"}), 502
        current   = r.json()
        levels    = int(current.get("grids_quantity") or 10)
        # quantity_per_grid is in BTC for BTC/USDC bots — convert from USDC total
        if btc_price > 0:
            qty = round(total_usd / (levels * btc_price), 6)  # BTC per grid level
        else:
            qty = round(total_usd / levels, 2)  # fallback: treat as quote currency
        was_on    = bool(current.get("is_enabled", False))

        if was_on:
            signed_request("POST", f"/ver1/grid_bots/{bot_id}/disable")
            time.sleep(2)

        patch = {
            "name":             current.get("name", f"Grid Bot {bot_id}"),
            "upper_price":      float(current.get("upper_price", 0)),
            "lower_price":      float(current.get("lower_price", 0)),
            "grids_quantity":   levels,
            "quantity_per_grid": qty,
            "grid_type":        current.get("grid_type", "arithmetic"),
            "ignore_warnings":  True,
        }
        rp = signed_request("PATCH", f"/ver1/grid_bots/{bot_id}/manual", body=patch)
        if rp.status_code not in (200, 201):
            return jsonify({"ok": False, "error": f"PATCH {rp.status_code}: {rp.text[:300]}"}), 502

        if was_on:
            time.sleep(1)
            signed_request("POST", f"/ver1/grid_bots/{bot_id}/enable")

        return jsonify({"ok": True, "qty_per_grid": qty, "total_usd": total_usd,
                        "levels": levels, "restarted": was_on})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Redistribute total capital across all bots by tier weight ──
@app.route("/account/allocate_total", methods=["POST"])
def allocate_total_capital():
    """
    Distribute a total USD amount across all three grid bots weighted by
    their base grid levels: inner 20, mid 14, outer 10 (ratio 20:14:10).
    Body: { total_usd: float, btc_price: float }
    """
    TIER_LEVELS = [20, 14, 10]   # inner, mid, outer
    ids = [b.strip() for b in os.getenv("GRID_BOT_IDS","").split(",") if b.strip()]
    if len(ids) != 3:
        return jsonify({"ok": False, "error": f"Expected 3 GRID_BOT_IDS, got {len(ids)}"}), 500
    try:
        body      = request.get_json(force=True, silent=True) or {}
        total_usd = float(body.get("total_usd", 0))
        btc_price = float(body.get("btc_price", 0))
        if total_usd <= 0:
            return jsonify({"ok": False, "error": "total_usd must be > 0"}), 400

        total_weight = sum(TIER_LEVELS)
        results = []
        for bot_id, levels in zip(ids, TIER_LEVELS):
            share     = total_usd * levels / total_weight
            r_cur     = signed_request("GET", f"/ver1/grid_bots/{bot_id}")
            if r_cur.status_code != 200:
                return jsonify({"ok": False, "error": f"GET bot {bot_id}: {r_cur.status_code}"}), 502
            current   = r_cur.json()
            g_levels  = int(current.get("grids_quantity") or levels)
            if btc_price > 0:
                qty = round(share / (g_levels * btc_price), 6)
            else:
                qty = round(share / g_levels, 2)
            was_on    = bool(current.get("is_enabled", False))
            if was_on:
                signed_request("POST", f"/ver1/grid_bots/{bot_id}/disable")
                time.sleep(2)
            patch = {
                "name":              current.get("name", f"Grid Bot {bot_id}"),
                "upper_price":       float(current.get("upper_price", 0)),
                "lower_price":       float(current.get("lower_price", 0)),
                "grids_quantity":    g_levels,
                "quantity_per_grid": qty,
                "grid_type":         current.get("grid_type", "arithmetic"),
                "ignore_warnings":   True,
            }
            rp = signed_request("PATCH", f"/ver1/grid_bots/{bot_id}/manual", body=patch)
            if rp.status_code not in (200, 201):
                return jsonify({"ok": False, "error": f"PATCH {bot_id}: {rp.status_code} {rp.text[:200]}"}), 502
            if was_on:
                time.sleep(1)
                signed_request("POST", f"/ver1/grid_bots/{bot_id}/enable")
            results.append({"bot_id": bot_id, "share": round(share, 2), "qty_per_grid": qty,
                             "levels": g_levels, "restarted": was_on})
        return jsonify({"ok": True, "total_usd": total_usd, "bots": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Redeploy bot with current engine tier parameters ─────
@app.route("/bots/<bot_id>/redeploy", methods=["POST"])
def redeploy_bot_endpoint(bot_id):
    """Stop, re-range, and restart a bot using the engine's latest tier calculation."""
    ids = [b.strip() for b in os.getenv("GRID_BOT_IDS","").split(",") if b.strip()]
    if bot_id not in ids:
        return jsonify({"ok": False, "error": "Bot not managed by this engine"}), 403
    try:
        # Load latest engine status to get tier params
        status_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATUS_FILE)
        with open(status_path) as f:
            status = json.load(f)
        tiers = status.get("tiers", [])
        if not tiers:
            return jsonify({"ok": False, "error": "No tier data in engine status yet"}), 503

        # Match tier by position in GRID_BOT_IDS (index 0=inner, 1=mid, 2=outer)
        tier_idx = ids.index(bot_id)
        if tier_idx >= len(tiers):
            return jsonify({"ok": False, "error": f"No tier at index {tier_idx}"}), 400
        tier = tiers[tier_idx]

        lower  = round(float(tier["grid_low"]),  2)
        upper  = round(float(tier["grid_high"]), 2)
        levels = int(tier["levels"])

        # Fetch current 3Commas config to preserve qty_per_grid and other settings
        r = signed_request("GET", f"/ver1/grid_bots/{bot_id}")
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"3Commas GET {r.status_code}: {r.text[:200]}"}), 502
        current  = r.json()
        was_on   = bool(current.get("is_enabled", False))
        orig_qty = float(current.get("quantity_per_grid") or 0)

        # Stop if running
        if was_on:
            signed_request("POST", f"/ver1/grid_bots/{bot_id}/disable")
            time.sleep(2)

        patch = {
            "name":              current.get("name", f"Grid Bot {bot_id}"),
            "upper_price":       upper,
            "lower_price":       lower,
            "grids_quantity":    levels,
            "quantity_per_grid": orig_qty,
            "grid_type":         current.get("grid_type", "arithmetic"),
            "ignore_warnings":   True,
        }
        rp = signed_request("PATCH", f"/ver1/grid_bots/{bot_id}/manual", body=patch)
        if rp.status_code not in (200, 201):
            if was_on:  # try to restore
                signed_request("POST", f"/ver1/grid_bots/{bot_id}/enable")
            return jsonify({"ok": False, "error": f"PATCH {rp.status_code}: {rp.text[:300]}"}), 502

        # Restart
        time.sleep(1)
        signed_request("POST", f"/ver1/grid_bots/{bot_id}/enable")

        # If this was the inner bot (index 0), persist deployed bounds so the
        # OOB check in engine.py uses the actual deployed range, not a recalculated one.
        if tier_idx == 0:
            try:
                from grid_logic import get_grid_state, update_grid_center
                _gs = get_grid_state()
                _inner_gw = tier.get("grid_width") or ((upper - lower) / 2)
                update_grid_center(
                    _gs["grid_center"],
                    grid_width=_gs.get("grid_width_at_deploy"),
                    inner_grid_width=_inner_gw,
                    inner_center=(lower + upper) / 2,
                    inner_grid_high=upper,
                    inner_grid_low=lower,
                )
            except Exception as _ge:
                pass  # non-fatal — bot is running, state update is best-effort

        return jsonify({
            "ok":     True,
            "bot_id": bot_id,
            "tier":   tier["name"],
            "lower":  lower,
            "upper":  upper,
            "levels": levels,
            "step":   tier.get("step"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Force redeploy all bots at current price ──────────────
@app.route("/grid/force-redeploy", methods=["POST"])
def force_redeploy_all():
    """Force stop, re-range, and restart all grid bots using the latest engine
    status tiers AND the budget system. Applies correct capital allocation.
    Resets grid_state.json center to the current price."""
    try:
        status_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATUS_FILE)
        with open(status_path) as f:
            status = json.load(f)

        tiers      = status.get("tiers", [])
        price      = status.get("price")
        grid_width = status.get("grid_width")

        if not tiers or not price:
            return jsonify({"ok": False, "error": "No tier data in engine status yet"}), 503

        ids = [b.strip() for b in os.getenv("GRID_BOT_IDS", "").split(",") if b.strip()]
        if not ids:
            return jsonify({"ok": False, "error": "GRID_BOT_IDS not configured"}), 503

        # Use the budget-aware redeploy from threecommas.py
        from threecommas import redeploy_all_bots
        all_ok = redeploy_all_bots(ids, tiers)

        # Reset grid center to current price so drift detection stays accurate
        from grid_logic import update_grid_center
        inner_gw = None
        inner_high = inner_low = None
        if tiers:
            t0 = tiers[0]
            inner_gw   = t0.get("grid_width") or ((t0.get("grid_high",0) - t0.get("grid_low",0)) / 2)
            inner_high = t0.get("grid_high")
            inner_low  = t0.get("grid_low")
        update_grid_center(price, grid_width=grid_width,
                          inner_grid_width=inner_gw, inner_center=price,
                          inner_grid_high=inner_high, inner_grid_low=inner_low)

        return jsonify({"ok": all_ok, "price": price, "center": price,
                       "msg": "All bots redeployed with budgets" if all_ok else "Some bots failed"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Account balance + capital allocation ─────────────────
_balance_cache = {"data": None, "ts": 0.0}

@app.route("/account/balance")
def account_balance():
    global _balance_cache
    now = time.time()
    if _balance_cache["data"] is not None and now - _balance_cache["ts"] < 60:
        return jsonify(_balance_cache["data"])
    try:
        # Trigger 3Commas balance refresh
        signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/load_balances")
        time.sleep(2)

        # Get pie chart data (per-currency breakdown)
        r = signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/pie_chart_data")
        pie = r.json() if r.ok else []
        if not isinstance(pie, list):
            pie = []

        btc_usd  = 0.0
        usdc_usd = 0.0
        btc_qty  = 0.0
        usdc_qty = 0.0
        for item in pie:
            code = (item.get("code") or item.get("currency_code") or "").upper()
            # API returns "usd_value" (string)
            val  = float(item.get("usd_value") or item.get("current_value_usd") or 0)
            qty  = float(item.get("amount") or item.get("quantity") or 0)
            if code == "BTC":
                btc_usd = val
                btc_qty = qty
            elif code in ("USDC", "USDT", "USD"):
                usdc_usd += val
                usdc_qty += qty

        # Get bot configs to calculate deployed capital
        # investment_quote_currency = USDC held inside the bot
        # investment_base_currency  = BTC held inside the bot (not converted here)
        ids = [b.strip() for b in os.getenv("GRID_BOT_IDS","").split(",") if b.strip()]
        bots_capital = []
        total_deployed = 0.0
        for bid in ids:
            rb = signed_request("GET", f"/ver1/grid_bots/{bid}")
            if rb.status_code == 200:
                b = rb.json()
                deployed = float(b.get("investment_quote_currency") or 0)
                total_deployed += deployed
                bots_capital.append({
                    "id":      bid,
                    "name":    b.get("name", f"Bot {bid}"),
                    "enabled": b.get("is_enabled", False),
                    "deployed": deployed,
                })
            else:
                bots_capital.append({"id": bid, "name": f"Bot {bid}", "deployed": 0.0, "error": rb.status_code})

        total_usd  = btc_usd + usdc_usd
        usdc_idle  = max(0.0, usdc_usd - total_deployed)

        result = {
            "btc_usd":        btc_usd,
            "btc_qty":        btc_qty,
            "usdc_usd":       usdc_usd,
            "usdc_qty":       usdc_qty,
            "total_usd":      total_usd,
            "total_deployed": total_deployed,
            "usdc_idle":      usdc_idle,
            "bots":           bots_capital,
        }
        _balance_cache = {"data": result, "ts": now}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/account/balance/raw")
def account_balance_raw():
    """Debug: return raw pie_chart_data from 3Commas so we can see actual field names."""
    try:
        signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/load_balances")
        time.sleep(2)
        r = signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/pie_chart_data")
        return jsonify({"status": r.status_code, "data": r.json() if r.status_code == 200 else r.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Bot fills (completed grid cycles) ─────────────────────
_fills_cache = {"data": None, "ts": 0.0}
_pnl_cache   = {"data": None, "ts": 0.0}


@app.route("/bots/pnl")
def bot_pnl():
    """
    Returns realized P&L per bot from the bot config fields.
    current_profit_usd  = realized profit from completed grid cycles
    total_profits_count = number of completed grid cycles (fills)
    unrealized_profit_loss = open position paper gain/loss vs grid centre
    Cached for 5 minutes.

    Response: { "<bot_id>": { "realized_usd": float, "fill_count": int,
                               "unrealized_usd": float, "profit_pct": float } }
    """
    global _pnl_cache
    now = time.time()
    if _pnl_cache["data"] is not None and now - _pnl_cache["ts"] < 300:
        return jsonify(_pnl_cache["data"])
    try:
        ids = [b.strip() for b in os.getenv("GRID_BOT_IDS", "").split(",") if b.strip()]
        result = {}
        for bid in ids[:3]:
            r = signed_request("GET", f"/ver1/grid_bots/{bid}")
            if r.status_code != 200:
                result[bid] = {"realized_usd": None, "fill_count": 0, "unrealized_usd": None, "profit_pct": None, "error": r.status_code}
                continue
            b = r.json()
            realized    = float(b.get("current_profit_usd")      or 0)
            unrealized  = float(b.get("unrealized_profit_loss")   or 0)
            fill_count  = int(b.get("total_profits_count")        or 0)
            profit_pct  = float(b.get("profit_percentage")        or 0)
            result[bid] = {
                "realized_usd":   round(realized,   4),
                "unrealized_usd": round(unrealized, 4),
                "fill_count":     fill_count,
                "profit_pct":     round(profit_pct * 100, 4),
            }
        _pnl_cache = {"data": result, "ts": now}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/bots/fills/debug")
def bot_fills_debug():
    """Temporary: expose raw 3Commas profits + market_orders for first bot."""
    ids = [b.strip() for b in os.getenv("GRID_BOT_IDS","").split(",") if b.strip()]
    if not ids:
        return jsonify({"error": "no bots"})
    bid = ids[0]
    r1 = signed_request("GET", f"/ver1/grid_bots/{bid}/profits",  params={"limit": 5})
    r2 = signed_request("GET", f"/ver1/grid_bots/{bid}/market_orders", params={"limit": 5})
    return jsonify({
        "profits_status": r1.status_code, "profits": r1.json() if r1.status_code == 200 else r1.text[:500],
        "orders_status":  r2.status_code, "orders":  r2.json() if r2.status_code == 200 else r2.text[:500],
    })


FILLS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fills_log.jsonl")

def _load_persisted_fills():
    """Load all persisted fills from JSONL file."""
    fills = []
    if os.path.exists(FILLS_LOG):
        with open(FILLS_LOG) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        fills.append(json.loads(line))
                    except Exception:
                        pass
    return fills

def _persist_new_fills(new_fills):
    """Append new fills to JSONL file, deduplicating by order_id."""
    existing_ids = set()
    if os.path.exists(FILLS_LOG):
        with open(FILLS_LOG) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        existing_ids.add(json.loads(line).get("order_id"))
                    except Exception:
                        pass
    added = 0
    with open(FILLS_LOG, "a") as f:
        for fill in new_fills:
            if fill.get("order_id") not in existing_ids:
                f.write(json.dumps(fill) + "\n")
                existing_ids.add(fill.get("order_id"))
                added += 1
    return added


@app.route("/bots/fills")
def bot_fills():
    global _fills_cache
    now = time.time()
    if (request.args.get("nocache") != "1"
            and _fills_cache["data"] is not None
            and now - _fills_cache["ts"] < 120):
        return jsonify(_fills_cache["data"])
    try:
        ids = [b.strip() for b in os.getenv("GRID_BOT_IDS","").split(",") if b.strip()]
        api_fills = []
        for i, bid in enumerate(ids[:3]):
            r = signed_request("GET", f"/ver1/grid_bots/{bid}/market_orders", params={"limit": 200})
            if r.status_code != 200:
                continue
            data = r.json()
            orders = data.get("balancing_orders") or [] if isinstance(data, dict) else []
            for item in orders:
                if item.get("status_string") != "Filled":
                    continue
                price = float(item.get("average_price") or item.get("rate") or 0)
                if not price:
                    continue
                api_fills.append({
                    "order_id":  item.get("order_id"),
                    "bot_id":    bid,
                    "bot_index": i,
                    "time":      item.get("created_at"),
                    "price":     price,
                    "side":      (item.get("order_type") or "").upper(),
                    "qty":       float(item.get("quantity") or 0),
                })
        # Persist any new fills we haven't seen before
        added = _persist_new_fills(api_fills)
        if added:
            print(f"[Fills] Persisted {added} new fills to {FILLS_LOG}")
        # Return ALL persisted fills (historical + current)
        all_fills = _load_persisted_fills()
        all_fills.sort(key=lambda x: x.get("time") or "")
        _fills_cache = {"data": all_fills, "ts": now}
        return jsonify(all_fills)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_daily_profit_cache = {"data": None, "ts": 0}

@app.route("/bots/daily-profit")
def bot_daily_profit():
    """
    Daily realized grid-bot profit from the 3Commas profits endpoint.

    Each profit entry is a completed grid cycle with a timestamp and USD profit.
    We fetch up to 500 entries per bot, aggregate by date, and return per-bot
    daily totals.

    Returns: { "days": [ {"date": "2026-03-24", "inner": 12.50, "mid": 8.20, "outer": 3.10, "total": 23.80} ] }
    """
    global _daily_profit_cache
    now = time.time()
    # Cache for 5 minutes
    if _daily_profit_cache["data"] is not None and now - _daily_profit_cache["ts"] < 300:
        return jsonify(_daily_profit_cache["data"])

    BOT_NAMES = ["inner", "mid", "outer"]

    try:
        ids = [b.strip() for b in os.getenv("GRID_BOT_IDS", "").split(",") if b.strip()]
        from collections import defaultdict
        daily_profit = {i: defaultdict(float) for i in range(3)}

        for i, bid in enumerate(ids[:3]):
            # Fetch up to 500 profit entries (3Commas paginates with offset/limit)
            offset = 0
            while offset < 2000:  # safety cap
                r = signed_request("GET", f"/ver1/grid_bots/{bid}/profits",
                                   params={"limit": 100, "offset": offset})
                if r.status_code != 200:
                    break
                entries = r.json()
                if not entries or not isinstance(entries, list):
                    break
                for entry in entries:
                    ts = entry.get("created_at", "")
                    usd = float(entry.get("usd_profit") or entry.get("profit") or 0)
                    if ts and usd:
                        date_str = ts[:10]
                        daily_profit[i][date_str] += usd
                if len(entries) < 100:
                    break  # no more pages
                offset += 100

        # Build response
        all_dates = sorted(set(
            d for bp in daily_profit.values() for d in bp.keys()
        ))
        days = []
        for d in all_dates:
            row = {"date": d}
            total = 0
            for i, name in enumerate(BOT_NAMES):
                v = round(daily_profit[i].get(d, 0), 2)
                row[name] = v
                total += v
            row["total"] = round(total, 2)
            days.append(row)

        result = {"days": days}
        _daily_profit_cache = {"data": result, "ts": now}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Inventory mode override ───────────────────────────────
@app.route("/inventory/mode", methods=["POST"])
def set_inventory_mode():
    try:
        mode = request.json.get("mode", "NORMAL")
        # Merge with existing file so manual btc_ratio override is preserved
        existing = {}
        if os.path.exists("inventory_override.json"):
            try:
                existing = json.load(open("inventory_override.json"))
            except Exception:
                pass
        existing["mode"] = mode
        with open("inventory_override.json", "w") as f:
            json.dump(existing, f)
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


# ── Inventory band settings ─────────────────────────────
@app.route("/inventory/settings", methods=["GET", "POST"])
def inventory_settings():
    try:
        from inventory import get_inventory_settings, save_inventory_settings
        if request.method == "GET":
            return jsonify(get_inventory_settings())
        data = save_inventory_settings(request.json)
        return jsonify({"ok": True, **data})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Inventory status / debug ─────────────────────────────
@app.route("/inventory/status")
def inventory_status():
    """Return current inventory cache state — useful for diagnosing stale data."""
    try:
        cache_file = os.path.join(os.path.dirname(__file__), "inventory_cache.json")
        now = time.time()
        if not os.path.exists(cache_file):
            return jsonify({"error": "no cache file", "stale": True})
        data = json.load(open(cache_file))
        live_ts  = data.get("live_ts", data.get("ts", 0))
        write_ts = data.get("ts", 0)
        live_age_s  = now - live_ts
        write_age_s = now - write_ts
        stale = live_age_s > 1800  # flag as stale if last live fetch > 30 min ago
        return jsonify({
            "btc_ratio":      data.get("btc_ratio"),
            "skew":           data.get("skew"),
            "btc_qty":        data.get("btc_qty"),
            "usdc_qty":       data.get("usdc_qty"),
            "btc_price":      data.get("btc_price"),
            "live_age_min":   round(live_age_s / 60, 1),
            "write_age_min":  round(write_age_s / 60, 1),
            "stale":          stale,
            "live_ts":        live_ts,
        })
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


# ── DCA bot ID config ────────────────────────────────────
@app.route("/config/dca-bots", methods=["GET", "POST"])
def config_dca_bots():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if request.method == "GET":
        return jsonify({"bot_ids": os.getenv("DCA_BOT_IDS", "")})
    body    = request.get_json(force=True, silent=True) or {}
    new_ids = body.get("bot_ids", "").strip()
    try:
        lines   = open(env_path).readlines() if os.path.exists(env_path) else []
        updated = False
        for i, line in enumerate(lines):
            if line.startswith("DCA_BOT_IDS="):
                lines[i] = f"DCA_BOT_IDS={new_ids}\n"
                updated = True
                break
        if not updated:
            lines.append(f"DCA_BOT_IDS={new_ids}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
        os.environ["DCA_BOT_IDS"] = new_ids
        return jsonify({"ok": True, "bot_ids": new_ids})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ── DCA bots ──────────────────────────────────────────────
from threecommas_dca import (
    get_dca_bots, get_dca_bot, create_dca_bot, enable_dca_bot,
    disable_dca_bot, panic_sell_dca_bot, get_dca_deals,
    get_dca_completed_deals,
    update_dca_bot, delete_dca_bot, estimate_max_exposure,
)

@app.route("/dca/bots")
def dca_bots_list():
    try:
        bots = get_dca_bots()
        return jsonify(bots)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/dca/pnl")
def dca_pnl():
    """
    P&L summary for DCA bots listed in DCA_BOT_IDS env var.

    Query params:
      from  — ISO 8601 start (e.g. 2026-01-01T00:00:00Z)
      to    — ISO 8601 end

    If no dates supplied: returns totals from bot config (finished_deals_profit_usd).
    If dates supplied: fetches completed deals per bot and sums usd_final_profit
    for deals whose closed_at falls in range. Falls back to config totals on API error.

    Response:
    {
      "configured": bool,
      "date_filtered": bool,
      "from": str|null,
      "to": str|null,
      "bots": [{ "id", "name", "realized_usd", "unrealized_usd", "deals", "pair" }],
      "total_realized": float,
      "total_unrealized": float,
      "total_deals": int
    }
    """
    from_iso = request.args.get("from")
    to_iso   = request.args.get("to")
    ids = [b.strip() for b in os.getenv("DCA_BOT_IDS", "").split(",") if b.strip()]
    if not ids:
        return jsonify({
            "configured": False, "date_filtered": False,
            "from": None, "to": None,
            "bots": [], "total_realized": 0, "total_unrealized": 0, "total_deals": 0,
        })

    date_filtered = bool(from_iso or to_iso)
    bots_result   = []
    total_realized   = 0.0
    total_unrealized = 0.0
    total_deals      = 0

    for bid in ids:
        try:
            r = _signed_request_local(bid, from_iso, to_iso, date_filtered)
            bots_result.append(r)
            total_realized   += r["realized_usd"]
            total_unrealized += r["unrealized_usd"]
            total_deals      += r["deals"]
        except Exception as e:
            bots_result.append({"id": bid, "name": "?", "realized_usd": 0,
                                 "unrealized_usd": 0, "deals": 0, "error": str(e)})

    return jsonify({
        "configured": True,
        "date_filtered": date_filtered,
        "from": from_iso,
        "to": to_iso,
        "bots": bots_result,
        "total_realized":   round(total_realized,   2),
        "total_unrealized": round(total_unrealized, 2),
        "total_deals": total_deals,
    })


def _signed_request_local(bot_id, from_iso, to_iso, date_filtered):
    """Fetch P&L for one DCA bot — uses completed deals when date range given."""
    rb = signed_request("GET", f"/ver1/bots/{bot_id}/show")
    if rb.status_code != 200:
        raise RuntimeError(f"Bot fetch {rb.status_code}")
    b = rb.json()
    name  = b.get("name", f"Bot {bot_id}")
    pairs = b.get("pairs", [])
    pair  = pairs[0] if pairs else "?"
    unrealized = float(b.get("active_deals_usd_profit") or 0)

    if date_filtered:
        deals = get_dca_completed_deals(bot_id, from_iso=from_iso, to_iso=to_iso)
        realized = sum(float(d.get("usd_final_profit") or 0) for d in deals)
        deal_count = len(deals)
    else:
        realized   = float(b.get("finished_deals_profit_usd") or 0)
        deal_count = int(b.get("finished_deals_count") or 0)

    return {
        "id":             bot_id,
        "name":           name,
        "pair":           pair,
        "realized_usd":   round(realized,   2),
        "unrealized_usd": round(unrealized, 2),
        "deals":          deal_count,
    }


@app.route("/dca/bots", methods=["POST"])
def dca_bots_create():
    try:
        body = request.get_json(force=True, silent=True) or {}
        bot = create_dca_bot(
            label=body.get("label", "DCA Bot"),
            base_order_usd=float(body.get("base_order_usd", 500)),
            safety_order_usd=float(body.get("safety_order_usd", 100)),
            take_profit_pct=float(body.get("take_profit_pct", 2.0)),
            safety_order_count=int(body.get("safety_order_count", 5)),
            safety_order_step_pct=float(body.get("safety_order_step_pct", 1.5)),
            safety_order_volume_mult=float(body.get("safety_order_volume_mult", 1.2)),
            pair=body.get("pair", "USDC_BTC"),
            take_profit_steps=body.get("take_profit_steps") or None,
        )
        if body.get("start"):
            enable_dca_bot(str(bot["id"]))
        return jsonify({"ok": True, "bot": bot})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/dca/bots/<bot_id>/enable", methods=["POST"])
def dca_bot_enable(bot_id):
    try:
        ok = enable_dca_bot(bot_id)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/dca/bots/<bot_id>/disable", methods=["POST"])
def dca_bot_disable(bot_id):
    try:
        ok = disable_dca_bot(bot_id)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/dca/bots/<bot_id>/panic_sell", methods=["POST"])
def dca_bot_panic(bot_id):
    try:
        ok = panic_sell_dca_bot(bot_id)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/dca/bots/<bot_id>/deals")
def dca_bot_deals(bot_id):
    try:
        deals = get_dca_deals(bot_id)
        return jsonify(deals)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/dca/bots/<bot_id>", methods=["PUT"])
def dca_bot_update(bot_id):
    try:
        body = request.get_json(force=True, silent=True) or {}
        bot = update_dca_bot(
            bot_id=bot_id,
            base_order_usd=float(body["base_order_usd"]) if body.get("base_order_usd") else None,
            safety_order_usd=float(body["safety_order_usd"]) if body.get("safety_order_usd") else None,
            take_profit_pct=float(body["take_profit_pct"]) if body.get("take_profit_pct") else None,
            safety_order_count=int(body["safety_order_count"]) if body.get("safety_order_count") else None,
            safety_order_step_pct=float(body["safety_order_step_pct"]) if body.get("safety_order_step_pct") else None,
            safety_order_volume_mult=float(body["safety_order_volume_mult"]) if body.get("safety_order_volume_mult") else None,
            take_profit_steps=body.get("take_profit_steps") or None,
        )
        return jsonify({"ok": True, "bot": bot})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/dca/bots/<bot_id>", methods=["DELETE"])
def dca_bot_delete(bot_id):
    try:
        delete_dca_bot(bot_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/dca/exposure", methods=["POST"])
def dca_exposure():
    """Estimate max capital exposure for a given DCA config."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        exposure = estimate_max_exposure(
            base_order_usd=float(body.get("base_order_usd", 500)),
            safety_order_usd=float(body.get("safety_order_usd", 100)),
            safety_order_count=int(body.get("safety_order_count", 5)),
            safety_order_volume_mult=float(body.get("safety_order_volume_mult", 1.2)),
        )
        return jsonify({"exposure": exposure})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Price targets ─────────────────────────────────────────
from price_targets import (
    load_targets, add_target, update_target, delete_target, clear_target
)

@app.route("/targets", methods=["GET"])
def get_targets():
    return jsonify(load_targets())


@app.route("/targets", methods=["POST"])
def create_target():
    try:
        body = request.get_json(force=True, silent=True) or {}
        label         = body.get("label", "").strip()
        trigger_price = body.get("trigger_price")
        direction     = body.get("direction", "UP").upper()

        if not label:
            return jsonify({"ok": False, "msg": "label is required"}), 400
        if not trigger_price:
            return jsonify({"ok": False, "msg": "trigger_price is required"}), 400
        if direction not in ("UP", "DOWN"):
            return jsonify({"ok": False, "msg": "direction must be UP or DOWN"}), 400

        raw_steps = body.get("dca_tp_steps")
        dca_tp_steps = None
        if isinstance(raw_steps, list) and raw_steps:
            dca_tp_steps = [
                {"profit_pct": float(s["profit_pct"]), "close_pct": float(s["close_pct"])}
                for s in raw_steps
                if "profit_pct" in s and "close_pct" in s
            ] or None

        t = add_target(
            label=label,
            trigger_price=float(trigger_price),
            direction=direction,
            price_target=float(body["price_target"]) if body.get("price_target") else None,
            reversal_atr_mult=float(body.get("reversal_atr_mult", 2.0)),
            confirm_closes=int(body.get("confirm_closes", 2)),
            rearm_cooldown_h=float(body.get("rearm_cooldown_h", 4.0)),
            detection_mode=body.get("detection_mode", "breakout"),
            retest_tolerance_pct=float(body.get("retest_tolerance_pct", 0.5)),
            dca_enabled=bool(body.get("dca_enabled", False)),
            dca_base_order_usd=float(body.get("dca_base_order_usd", 500)),
            dca_safety_count=int(body.get("dca_safety_count", 5)),
            dca_safety_step_pct=float(body.get("dca_safety_step_pct", 1.5)),
            dca_safety_volume_mult=float(body.get("dca_safety_volume_mult", 1.2)),
            dca_tp_steps=dca_tp_steps,
            dca_trailing_enabled=bool(body.get("dca_trailing_enabled", False)),
            dca_trailing_deviation_pct=float(body.get("dca_trailing_deviation_pct", 1.0)),
            dca_dual_entry=bool(body.get("dca_dual_entry", False)),
            dca_scout_pct=float(body.get("dca_scout_pct", 30.0)),
            dca_scout_buffer_cycles=int(body.get("dca_scout_buffer_cycles", 2)),
            dca_retest_tolerance_pct=float(body.get("dca_retest_tolerance_pct", 0.5)),
            smart_trade_enabled=bool(body.get("smart_trade_enabled", False)),
            smart_trade_sell_pct=float(body.get("smart_trade_sell_pct", 25.0)),
            smart_trade_tp_pct=float(body.get("smart_trade_tp_pct", 3.0)),
            smart_trade_sl_pct=float(body.get("smart_trade_sl_pct", 1.5)),
            smart_trade_tp_steps=body.get("smart_trade_tp_steps"),
            smart_trade_dual_entry=bool(body.get("smart_trade_dual_entry", False)),
            smart_trade_scout_pct=float(body.get("smart_trade_scout_pct", 30.0)),
            smart_trade_retest_tolerance_pct=float(body.get("smart_trade_retest_tolerance_pct", 0.5)),
        )
        return jsonify({"ok": True, "target": t})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/targets/<target_id>", methods=["PUT"])
def patch_target(target_id):
    try:
        body = request.get_json(force=True, silent=True) or {}
        t = update_target(target_id, body)
        if t is None:
            return jsonify({"ok": False, "msg": "target not found"}), 404
        return jsonify({"ok": True, "target": t})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/targets/<target_id>", methods=["DELETE"])
def remove_target(target_id):
    try:
        ok = delete_target(target_id)
        return jsonify({"ok": ok, "msg": "deleted" if ok else "not found"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/targets/<target_id>/clear", methods=["POST"])
def clear_target_route(target_id):
    """Re-arm a fired target without deleting it."""
    try:
        ok = clear_target(target_id)
        return jsonify({"ok": ok, "msg": "cleared (re-armed)" if ok else "not found"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ── SmartTrade monitoring ─────────────────────────────────
@app.route("/smart_trades/<st_id>", methods=["GET"])
def get_smart_trade_route(st_id):
    """Fetch live status of a 3Commas SmartTrade."""
    try:
        from threecommas import get_smart_trade
        data = get_smart_trade(st_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/smart_trades/<st_id>/cancel", methods=["POST"])
def cancel_smart_trade_route(st_id):
    """Cancel an active SmartTrade and clear its ID from the linked target."""
    try:
        from threecommas import cancel_smart_trade
        result = cancel_smart_trade(st_id)
        # Also clear smart_trade_id from any target that references it
        targets = load_targets()
        for t in targets:
            if str(t.get("smart_trade_id")) == str(st_id):
                t["smart_trade_id"] = None
                t.update({"fired": False, "fired_at": None, "fired_price": None,
                           "cleared_at": time.time(), "consec_above": 0,
                           "sf_phase": "watching", "sf_retest_high": None})
        from price_targets import save_targets
        save_targets(targets)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Regime state management ───────────────────────────────
@app.route("/grid/set-deployed-tiers", methods=["POST"])
def grid_set_deployed_tiers():
    """
    Write deployed_tiers into grid_state.json so the chart renders lines
    that match the real 3Commas order positions.
    Body: {"tiers": [{"name":"inner","grid_low":X,"grid_high":Y,"levels":N}, ...]}
    Each tier's grid_levels are auto-generated as evenly-spaced from low to high.
    """
    data = request.get_json(silent=True) or {}
    tiers_in = data.get("tiers")
    if not tiers_in:
        return jsonify({"error": "tiers array required"}), 400

    # Generate evenly-spaced grid_levels for each tier
    built = []
    for t in tiers_in:
        low    = float(t.get("grid_low",  0))
        high   = float(t.get("grid_high", 0))
        levels = int(t.get("levels", 5))
        if high <= low or levels < 2:
            gl = [low, high]
        else:
            step = (high - low) / (levels - 1)
            gl   = [round(low + step * i, 2) for i in range(levels)]
        built.append({
            "name":        t.get("name", ""),
            "grid_low":    round(low,  2),
            "grid_high":   round(high, 2),
            "levels":      levels,
            "grid_levels": gl,
        })

    gs_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grid_state.json")
    existing = {}
    if os.path.exists(gs_file):
        try:
            existing = json.load(open(gs_file))
        except Exception:
            pass
    existing["deployed_tiers"] = built
    with open(gs_file, "w") as f:
        json.dump(existing, f, indent=2)
    return jsonify({"ok": True, "deployed_tiers": built})


@app.route("/regime/clear", methods=["POST"])
def regime_clear():
    """Reset regime_state.json — clears TREND_DOWN lock and trending_up state."""
    state_file = os.path.join(os.path.dirname(__file__), "regime_state.json")
    try:
        import json as _json
        _json.dump(
            {"below_tl_count": 0, "trending_up_active": False, "trend_down_active": False},
            open(state_file, "w")
        )
        return jsonify({"ok": True, "msg": "Regime state cleared"})
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


@app.route("/flash-move/clear", methods=["POST"])
def flash_move_clear():
    """Clear flash move state — resume normal operation."""
    state_file = os.path.join(os.path.dirname(__file__), "flash_move_state.json")
    try:
        import json as _json
        _json.dump(
            {"last_price": None, "active": None, "fire_price": None,
             "fired_at": None, "cooldown_remaining": 0, "magnitude": 0},
            open(state_file, "w")
        )
        return jsonify({"ok": True, "msg": "Flash move state cleared"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/flash-move/state", methods=["GET"])
def flash_move_state():
    """Return current flash move state."""
    state_file = os.path.join(os.path.dirname(__file__), "flash_move_state.json")
    try:
        import json as _json
        if os.path.exists(state_file):
            return jsonify(_json.load(open(state_file)))
        return jsonify({"active": None, "cooldown_remaining": 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/breakout/inject", methods=["POST"])
def breakout_inject():
    """Inject a simulated breakout state for testing.
    Body: { "direction": "UP"|"DOWN", "price": 73000 }
    The next engine cycle will treat this as a live breakout.
    """
    import json as _json, time as _time
    data = request.get_json(silent=True) or {}
    direction = data.get("direction", "UP").upper()
    if direction not in ("UP", "DOWN"):
        return jsonify({"ok": False, "msg": "direction must be UP or DOWN"}), 400
    price = float(data.get("price", 0))
    if price <= 0:
        return jsonify({"ok": False, "msg": "price must be > 0"}), 400

    state_file = os.path.join(os.path.dirname(__file__), "breakout_state.json")
    try:
        state = {
            "consec_up":   0,
            "consec_down": 0,
            "active":      direction,
            "fire_price":  price,
            "fired_at":    _time.time(),
            "simulated":   True,
        }
        _json.dump(state, open(state_file, "w"))
        return jsonify({"ok": True, "msg": f"Injected BREAKOUT_{direction} @ ${price:,.0f}", "state": state})
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


@app.route("/notifications")
def notifications():
    """
    Walk engine_log.jsonl and extract discrete safety/state-change events.
    Returns a list of events, newest first, max 100.
    Each event: {ts, type, severity, msg}
    severity: "critical" | "warning" | "info"
    """
    entries = read_log(1000)
    events  = []
    prev    = {}

    for e in entries:
        ts    = e.get("ts", "")
        price = e.get("price") or 0

        # Fill-flood guard
        if e.get("fill_flood_active") and not prev.get("fill_flood_active"):
            events.append({"ts": ts, "type": "FILL_FLOOD", "severity": "critical",
                           "msg": f"Fill-flood: rapid fills after redeploy — bots paused 30 min  (${price:,.0f})"})

        # Drift / grid recentre
        if e.get("drift_triggered") and not prev.get("drift_triggered"):
            old_center = prev.get("center") or 0
            events.append({"ts": ts, "type": "DRIFT", "severity": "warning",
                           "msg": f"Grid recentred ${old_center:,.0f} → ${price:,.0f}"})

        # Breakout state
        bo      = e.get("breakout_active")
        prev_bo = prev.get("breakout_active")
        if bo and bo != prev_bo:
            if bo in ("UP", "DOWN"):
                sev = "critical" if bo == "DOWN" else "warning"
                events.append({"ts": ts, "type": f"BREAKOUT_{bo}", "severity": sev,
                               "msg": f"Breakout {bo} detected at ${price:,.0f}"})
            elif bo in ("PENDING_UP", "PENDING_DOWN"):
                events.append({"ts": ts, "type": "BREAKOUT_PENDING", "severity": "info",
                               "msg": f"Breakout {bo} — awaiting 1H confirm at ${price:,.0f}"})
        if prev_bo in ("UP", "DOWN") and not bo:
            events.append({"ts": ts, "type": "BREAKOUT_CLEAR", "severity": "info",
                           "msg": f"Breakout {prev_bo} cleared at ${price:,.0f}"})

        # Regime transition
        reg      = e.get("regime")
        prev_reg = prev.get("regime")
        if reg and prev_reg and reg != prev_reg:
            sev = {"TREND_DOWN": "warning", "COMPRESSION": "critical",
                   "TREND_UP": "info", "RANGE": "info"}.get(reg, "info")
            events.append({"ts": ts, "type": "REGIME", "severity": sev,
                           "msg": f"Regime: {prev_reg} → {reg}  (${price:,.0f})"})

        # Trending_up / trending_down changes
        # Dedup: suppress TRENDING events that chop within 30 min (threshold noise)
        TREND_DEDUP_SECS = 1800

        def _last_trend_ts(label):
            from datetime import datetime, timezone
            for ev in reversed(events):
                if ev["type"] == "TRENDING" and label in ev["msg"]:
                    try:
                        return datetime.fromisoformat(
                            ev["ts"].replace("Z", "+00:00")).timestamp()
                    except Exception:
                        pass
            return 0

        def _cur_unix(ts_str):
            from datetime import datetime
            try:
                return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0

        _now = _cur_unix(ts)

        if e.get("trending_down") and not prev.get("trending_down"):
            if _now - _last_trend_ts("Trending DOWN \u2014") > TREND_DEDUP_SECS:
                events.append({"ts": ts, "type": "TRENDING", "severity": "warning",
                               "msg": f"Trending DOWN \u2014 inner+mid off  gap={e.get('gap_ratio',0):.1f}\u00d7ATR  (${price:,.0f})"})
        if not e.get("trending_down") and prev.get("trending_down"):
            if _now - _last_trend_ts("Trending DOWN cleared") > TREND_DEDUP_SECS:
                events.append({"ts": ts, "type": "TRENDING", "severity": "info",
                               "msg": f"Trending DOWN cleared  (${price:,.0f})"})
        if e.get("trending_up") and not prev.get("trending_up"):
            if _now - _last_trend_ts("Trending UP \u2014") > TREND_DEDUP_SECS:
                events.append({"ts": ts, "type": "TRENDING", "severity": "info",
                               "msg": f"Trending UP \u2014 all bots on  gap={e.get('gap_ratio',0):.1f}\u00d7ATR  (${price:,.0f})"})
        if not e.get("trending_up") and prev.get("trending_up"):
            if _now - _last_trend_ts("Trending UP cleared") > TREND_DEDUP_SECS:
                events.append({"ts": ts, "type": "TRENDING", "severity": "info",
                               "msg": f"Trending UP cleared  (${price:,.0f})"})

        # Inventory mode
        mode      = e.get("inventory_mode")
        prev_mode = prev.get("inventory_mode")
        if mode and prev_mode and mode != prev_mode:
            sev = "critical" if mode in ("SELL_ONLY", "BUY_ONLY") else "info"
            events.append({"ts": ts, "type": "INVENTORY", "severity": sev,
                           "msg": f"Inventory: {prev_mode} → {mode}  BTC={e.get('btc_ratio',0):.1%}"})

        # Price target fired
        if e.get("price_target_active") and not prev.get("price_target_active"):
            label = e.get("price_target_label") or "target"
            events.append({"ts": ts, "type": "PRICE_TARGET", "severity": "info",
                           "msg": f"Price target fired: \"{label}\"  (${price:,.0f})"})

        # Price target timeout — support level held below for 2h, bots redeployed
        if e.get("price_target_timeout") and not prev.get("price_target_timeout"):
            label = e.get("price_target_label") or "target"
            events.append({"ts": ts, "type": "TIMEOUT", "severity": "warning",
                           "msg": f"Target timeout: \"{label}\" — 2h below, bots redeployed  (${price:,.0f})"})

        # Price target cleared (was active, now not)
        if prev.get("price_target_active") and not e.get("price_target_active") \
                and not e.get("price_target_timeout"):
            label = prev.get("price_target_label") or "target"
            events.append({"ts": ts, "type": "PRICE_TARGET", "severity": "info",
                           "msg": f"Price target cleared: \"{label}\"  (${price:,.0f})"})

        # DCA bot launched
        if e.get("dca_bot_active") and not prev.get("dca_bot_active"):
            label = e.get("price_target_label") or "target"
            dca_id = e.get("price_target_dca_id") or "?"
            events.append({"ts": ts, "type": "DCA", "severity": "info",
                           "msg": f"DCA bot launched for \"{label}\"  id={dca_id}  (${price:,.0f})"})

        # SmartTrade launched
        if e.get("price_target_st_id") and not prev.get("price_target_st_id"):
            label = e.get("price_target_label") or "target"
            events.append({"ts": ts, "type": "SMART_TRADE", "severity": "warning",
                           "msg": f"SmartTrade opened for \"{label}\"  (${price:,.0f})"})

        # Inner drift (narrow-only recentre)
        if e.get("inner_drift_fired") and not prev.get("inner_drift_fired"):
            events.append({"ts": ts, "type": "INNER_DRIFT", "severity": "info",
                           "msg": f"Narrow bot recentred (inner drift)  (${price:,.0f})"})

        # Bot actions — group all 3 bot transitions into a single event per cycle
        actions      = e.get("bot_actions") or []
        prev_actions = prev.get("bot_actions") or []
        _cur_bots    = {a["bot"]: a for a in actions}
        _prev_bots   = {a["bot"]: a for a in prev_actions}
        _stops, _starts = [], []
        for bot_id, a in _cur_bots.items():
            prev_a = _prev_bots.get(bot_id)
            if prev_a and prev_a["action"] != a["action"]:
                if a["action"] == "stop":
                    _stops.append(a.get("reason", bot_id))
                else:
                    _starts.append(a.get("reason", bot_id))
        # Emit one grouped event instead of three individual ones
        if _stops:
            # Extract the unique reason (strip tier prefix — "inner (fill-flood)" → "fill-flood")
            reasons = list({r.split("(")[-1].rstrip(")").strip() if "(" in r else r for r in _stops})
            reason_str = " / ".join(reasons)
            n = len(_stops)
            events.append({"ts": ts, "type": "BOT_ACTION", "severity": "warning",
                           "msg": f"{n} bot{'s' if n>1 else ''} STOPPED — {reason_str}  (${price:,.0f})"})
        if _starts:
            reasons = list({r.split("(")[-1].rstrip(")").strip() if "(" in r else r for r in _starts})
            reason_str = " / ".join(reasons)
            n = len(_starts)
            events.append({"ts": ts, "type": "BOT_ACTION", "severity": "info",
                           "msg": f"{n} bot{'s' if n>1 else ''} STARTED — {reason_str}  (${price:,.0f})"})

        prev = e

    events.reverse()
    limit = request.args.get("limit", 200, type=int)
    return jsonify(events[:limit])


@app.route("/portfolio/log")
def portfolio_log():
    """Return portfolio snapshots for P&L charting. ?limit=N (default all)."""
    limit  = request.args.get("limit", type=int)
    pf_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio_log.jsonl")
    rows   = []
    if os.path.exists(pf_log):
        with open(pf_log) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    if limit:
        rows = rows[-limit:]
    return jsonify(rows)


# ── Capital events (deposits / withdrawals) ───────────────
_CAPITAL_EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capital_events.json")


def _load_capital_events():
    if not os.path.exists(_CAPITAL_EVENTS_FILE):
        return []
    try:
        with open(_CAPITAL_EVENTS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


@app.route("/capital/events", methods=["GET"])
def capital_events_get():
    return jsonify(_load_capital_events())


@app.route("/capital/events", methods=["POST"])
def capital_events_post():
    data = request.get_json(silent=True) or {}
    amount_usd = data.get("amount_usd")
    if amount_usd is None:
        return jsonify({"error": "amount_usd required"}), 400
    label = data.get("label", "")
    ts    = data.get("ts", time.time())
    events = _load_capital_events()
    events.append({"ts": float(ts), "amount_usd": float(amount_usd), "label": label})
    events.sort(key=lambda e: e["ts"])
    with open(_CAPITAL_EVENTS_FILE, "w") as f:
        json.dump(events, f, indent=2)
    return jsonify({"ok": True, "events": events})


@app.route("/capital/events/<int:idx>", methods=["DELETE"])
def capital_events_delete(idx):
    events = _load_capital_events()
    if idx < 0 or idx >= len(events):
        return jsonify({"error": "index out of range"}), 404
    events.pop(idx)
    with open(_CAPITAL_EVENTS_FILE, "w") as f:
        json.dump(events, f, indent=2)
    return jsonify({"ok": True, "events": events})


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


# ── Self-deploy endpoint ────────────────────────────────────────────────────
_DEPLOY_FILES = [
    "dashboard.html",
    "dashboard_mobile.html",
    "pnl.html",
    "dashboard_server.py",
    "threecommas.py",
    "threecommas_dca.py",
    "grid_logic.py",
    "session.py",
    "engine.py",
    "notify.py",
    "regime.py",
    "market_data.py",
    "breakout.py",
    "inventory.py",
    "indicators.py",
    "price_targets.py",
    "flash_move.py",
]
# Macro dashboard HTML lives in repo root, not engine/
_DEPLOY_FILES_ROOT = [
    "btc_macro_dashboard.html",
    "btc_macro_dashboard_mobile.html",
]
# ── News proxy ───────────────────────────────────────────
_news_cache = {"data": None, "ts": 0.0}
_NEWS_FEEDS = [
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]

@app.route("/news")
def news_proxy():
    """Fetch and parse RSS from crypto news sources. Cached 10 min."""
    global _news_cache
    now = time.time()
    if _news_cache["data"] is not None and now - _news_cache["ts"] < 600:
        return jsonify(_news_cache["data"])
    import xml.etree.ElementTree as ET
    items = []
    for source_name, feed_url in _NEWS_FEEDS:
        try:
            r = req.get(feed_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.text)
            ns   = {"media": "http://search.yahoo.com/mrss/"}
            for item in root.findall(".//item")[:8]:
                title = (item.findtext("title") or "").strip()
                link  = (item.findtext("link")  or "").strip()
                pub   = (item.findtext("pubDate") or "").strip()
                if not title or not link:
                    continue
                # Strip CDATA wrappers from link if present
                if link.startswith("http"):
                    pass
                else:
                    guid = item.findtext("guid") or ""
                    link = guid if guid.startswith("http") else link
                items.append({
                    "title":        title,
                    "url":          link,
                    "published_at": pub,
                    "source":       source_name,
                })
        except Exception:
            continue
    _news_cache = {"data": items, "ts": now}
    return jsonify(items)


_DEPLOY_BRANCH    = "claude/grid-engine-chat-review-hEEGu"
_DEPLOY_BASE      = f"https://raw.githubusercontent.com/ashskett/btc-dashboard/{_DEPLOY_BRANCH}/engine"
_DEPLOY_BASE_ROOT = f"https://raw.githubusercontent.com/ashskett/btc-dashboard/{_DEPLOY_BRANCH}"

@app.route("/deploy", methods=["POST"])
def deploy_endpoint():
    import urllib.request, threading
    token    = (request.args.get("token") or (request.get_json(silent=True) or {}).get("token", ""))
    expected = os.environ.get("DEPLOY_TOKEN", "grid-deploy-2026")
    if token != expected:
        return jsonify({"error": "unauthorized"}), 403

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # ── Pre-deploy backup ─────────────────────────────────────────────────────
    # Save all deployable files + credentials + state JSONs before overwriting.
    # Backups live in /root/grid-engine-backups/<timestamp>/
    # Use /deploy/rollback to restore any backup.
    _backup_ts  = time.strftime("%Y-%m-%d-%H%M%S")
    _backup_dir = os.path.join(os.path.dirname(script_dir), "grid-engine-backups", _backup_ts)
    try:
        os.makedirs(_backup_dir, exist_ok=True)
        # Code files
        for _fname in _DEPLOY_FILES + list(_DEPLOY_FILES_ROOT):
            _src = os.path.join(script_dir, _fname)
            if os.path.exists(_src):
                shutil.copy2(_src, os.path.join(_backup_dir, _fname))
        # Credentials and state (not in git — most critical to preserve)
        for _critical in [".env", "3commas_private.pem",
                          "grid_state.json", "inventory_settings.json",
                          "inventory_override.json", "trendlines.json",
                          "tier_budgets.json", "regime_state.json"]:
            _src = os.path.join(script_dir, _critical)
            if os.path.exists(_src):
                shutil.copy2(_src, os.path.join(_backup_dir, _critical))
        # Write a human-readable manifest
        with open(os.path.join(_backup_dir, "_manifest.json"), "w") as _mf:
            json.dump({
                "timestamp":  _backup_ts,
                "branch":     _DEPLOY_BRANCH,
                "created_by": "pre-deploy auto-backup",
            }, _mf, indent=2)
    except Exception as _be:
        print(f"Warning: pre-deploy backup failed: {_be}")

    results = {}
    for fname in _DEPLOY_FILES:
        url  = f"{_DEPLOY_BASE}/{fname}"
        dest = os.path.join(script_dir, fname)
        try:
            urllib.request.urlretrieve(url, dest + ".new")
            os.replace(dest + ".new", dest)
            results[fname] = "ok"
        except Exception as e:
            results[fname] = f"error: {e}"
    for fname in _DEPLOY_FILES_ROOT:
        url  = f"{_DEPLOY_BASE_ROOT}/{fname}"
        dest = os.path.join(script_dir, fname)
        try:
            urllib.request.urlretrieve(url, dest + ".new")
            os.replace(dest + ".new", dest)
            results[fname] = "ok"
        except Exception as e:
            results[fname] = f"error: {e}"

    # Always update and restart the webhook server so the old main-branch
    # webhook process can't keep overwriting our files.
    _wh_path = "/root/webhook_server.py"
    try:
        _wh_url = f"{_DEPLOY_BASE_ROOT}/scripts/webhook_server.py"
        urllib.request.urlretrieve(_wh_url, _wh_path + ".new")
        os.replace(_wh_path + ".new", _wh_path)
        results["webhook_server.py"] = "ok"
        # Kill old webhook process; new one will be started after restart
        subprocess.run("pkill -f 'python.*webhook_server' || true", shell=True)
        time.sleep(0.5)
        subprocess.Popen(
            [sys.executable, _wh_path],
            start_new_session=True, close_fds=True,
        )
        results["webhook_restart"] = "ok"
    except Exception as e:
        results["webhook_restart"] = f"error: {e}"

    def _restart():
        time.sleep(1.0)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        restart_cmd = (
            "tmux send-keys -t grid C-c Enter ; sleep 2 ; "
            f"tmux send-keys -t grid 'cd {script_dir} && "
            "source venv/bin/activate && python dashboard_server.py' Enter"
        )
        try:
            # start_new_session=True puts the subprocess in its own process
            # session/group so it doesn't receive SIGINT when C-c is sent to
            # the grid tmux pane — without this, the shell gets SIGINT mid-
            # "sleep 2" and the restart command is never sent.
            subprocess.Popen(
                restart_cmd, shell=True,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"[deploy] restart subprocess failed: {e}", flush=True)
        time.sleep(0.5)
        os._exit(0)

    # ── Post-deploy Notion memory log ────────────────────────────────────────
    # Fire-and-forget: log each deploy to the AI OS memory so there's a
    # permanent record of what was deployed and when.
    def _log_deploy_to_notion():
        try:
            ok_files  = [f for f, s in results.items() if s == "ok"]
            fail_files = [f for f, s in results.items() if s != "ok"]
            entry = (
                f"DEPLOY [{_backup_ts}] branch={_DEPLOY_BRANCH} "
                f"ok={len(ok_files)} fail={len(fail_files)} "
                f"backup={_backup_ts}"
            )
            if fail_files:
                entry += f" FAILURES: {', '.join(fail_files)}"
            req.post(
                "https://api.uncrewedmaritime.com/memory/log",
                json={"content": entry, "tags": ["deploy", "grid-engine"]},
                timeout=10,
            )
        except Exception as _le:
            print(f"Warning: deploy Notion log failed: {_le}")

    threading.Thread(target=_log_deploy_to_notion, daemon=True).start()
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"status": "deploying", "branch": _DEPLOY_BRANCH,
                    "files": results, "backup": _backup_ts})


# ── Deploy backup management ──────────────────────────────────────────────────

_BACKUP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "grid-engine-backups")

@app.route("/deploy/backups")
def list_deploy_backups():
    """List all available pre-deploy backups, newest first."""
    try:
        if not os.path.exists(_BACKUP_ROOT):
            return jsonify({"backups": []})
        backups = []
        for name in sorted(os.listdir(_BACKUP_ROOT), reverse=True):
            d = os.path.join(_BACKUP_ROOT, name)
            if not os.path.isdir(d):
                continue
            manifest_path = os.path.join(d, "_manifest.json")
            manifest = {}
            if os.path.exists(manifest_path):
                try:
                    manifest = json.load(open(manifest_path))
                except Exception:
                    pass
            files = [f for f in os.listdir(d) if not f.startswith("_")]
            backups.append({"timestamp": name, "files": files, **manifest})
        return jsonify({"backups": backups, "count": len(backups)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/deploy/rollback", methods=["POST"])
def deploy_rollback():
    """
    Restore a named backup to the engine directory and restart.
    Body: {"timestamp": "2026-03-30-143012"}
    """
    try:
        body = request.get_json(silent=True) or {}
        ts = body.get("timestamp", "").strip()
        if not ts:
            return jsonify({"error": "timestamp required"}), 400
        backup_dir = os.path.join(_BACKUP_ROOT, ts)
        if not os.path.exists(backup_dir):
            return jsonify({"error": f"Backup '{ts}' not found"}), 404

        # Live state files that belong to the running system — never roll back
        # these because they contain user-drawn trendlines, active bot config,
        # and real-time grid state that is independent of the code version.
        _ROLLBACK_SKIP = {
            "trendlines.json",
            "tier_budgets.json",
            "inventory_settings.json",
            "inventory_override.json",
            "breakout_targets.json",
            "grid_state.json",
            "regime_state.json",
            "redeploy_state.json",
            "capital_events.json",
        }

        script_dir = os.path.dirname(os.path.abspath(__file__))
        restored, skipped = [], []
        for fname in os.listdir(backup_dir):
            if fname.startswith("_"):
                continue   # skip _manifest.json
            if fname in _ROLLBACK_SKIP:
                skipped.append(f"{fname}: protected live state — not rolled back")
                continue
            src = os.path.join(backup_dir, fname)
            dst = os.path.join(script_dir, fname)
            try:
                shutil.copy2(src, dst)
                restored.append(fname)
            except Exception as fe:
                skipped.append(f"{fname}: {fe}")

        # Log rollback to Notion
        def _log_rollback():
            try:
                req.post(
                    "https://api.uncrewedmaritime.com/memory/log",
                    json={"content": f"ROLLBACK to {ts} — restored: {', '.join(restored)}",
                          "tags": ["rollback", "grid-engine"]},
                    timeout=10,
                )
            except Exception:
                pass
        threading.Thread(target=_log_rollback, daemon=True).start()

        # Restart engine and server
        def _restart_after_rollback():
            time.sleep(1.0)
            script_dir2 = os.path.dirname(os.path.abspath(__file__))
            restart_cmd = (
                "tmux send-keys -t grid C-c Enter ; sleep 2 ; "
                f"tmux send-keys -t grid 'cd {script_dir2} && "
                "source venv/bin/activate && python dashboard_server.py' Enter"
            )
            subprocess.Popen(restart_cmd, shell=True, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_restart_after_rollback, daemon=True).start()

        return jsonify({"ok": True, "restored_from": ts,
                        "restored": restored, "skipped": skipped})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Startup self-update removed.
    # It created a CDN race condition: GitHub's Fastly CDN can serve stale
    # content for several minutes after a push.  The self-update would then
    # download the old version, overwrite the freshly-deployed file, and
    # restart — silently reverting every deploy.
    #
    # The /deploy endpoint (POST /deploy?token=grid-deploy-2026) is the
    # authoritative update mechanism and is unaffected by this change.

    # Auto-start engine on server startup
    if not _engine_running():
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            _engine_proc = subprocess.Popen(
                [sys.executable, "-u", os.path.join(script_dir, "engine.py")],
                cwd=script_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
            )
            __import__("threading").Thread(
                target=_drain_output, args=(_engine_proc,), daemon=True
            ).start()
            print(f"Engine auto-started (pid {_engine_proc.pid})")
        except Exception as e:
            print(f"Warning: could not auto-start engine: {e}")

    # Startup self-heal: re-download static files from the correct branch 20s after
    # startup. This silently fixes any bad webhook overwrite (e.g. webhook running
    # old code that deployed from main). Only downloads files served from disk
    # (dashboard.html + macro dashboards) — no restart needed since Flask reads them
    # fresh on every request. Python code changes still require a full /deploy.
    def _startup_heal():
        import time as _t, urllib.request as _ur
        _t.sleep(20)
        _sd = os.path.dirname(os.path.abspath(__file__))
        _heal_files = ["dashboard.html", "dashboard_mobile.html", "btc_macro_dashboard.html", "btc_macro_dashboard_mobile.html"]
        for _f in _heal_files:
            _url = f"{_DEPLOY_BASE}/{_f}"
            _dst = os.path.join(_sd, _f)
            try:
                _ur.urlretrieve(_url, _dst + ".heal")
                os.replace(_dst + ".heal", _dst)
            except Exception:
                pass
    threading.Thread(target=_startup_heal, daemon=True).start()

    print("Dashboard running → open http://localhost:5050 in your browser")
    app.run(host="0.0.0.0", port=5050, debug=False)
