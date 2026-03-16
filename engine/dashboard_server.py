from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import json, os, base64, time, subprocess, signal, sys, secrets, threading
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

_PUBLIC_PATHS = {"/", "/ping", "/deploy", "/account/balance/raw", "/macro", "/macro/mobile"}

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
    # Debug: /ping?debug=balance returns raw 3Commas pie_chart_data
    if request.args.get("debug") == "balance":
        try:
            signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/load_balances")
            time.sleep(2)
            r = signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/pie_chart_data")
            return jsonify({"status": r.status_code, "data": r.json() if r.ok else r.text[:500]})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    # Debug: /ping?debug=version returns PUBLIC_PATHS to confirm code version
    if request.args.get("debug") == "version":
        return jsonify({"public_paths": list(_PUBLIC_PATHS), "pid": os.getpid()})
    # Debug: /ping?debug=state returns engine state files for diagnosis
    if request.args.get("debug") == "state":
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
    # Debug: /ping?debug=log&limit=N returns last N engine log entries
    if request.args.get("debug") == "log":
        limit = int(request.args.get("limit", 30))
        return jsonify(read_log(limit))
    # Debug: /ping?debug=dcabots returns raw DCA bot list from 3Commas
    if request.args.get("debug") == "dcabots":
        try:
            from threecommas_dca import get_dca_bots
            bots = get_dca_bots()
            # Return only fields useful for status diagnosis
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

@app.route("/macro")
def macro_desktop():
    return send_from_directory('.', 'btc_macro_dashboard.html')

@app.route("/macro/mobile")
def macro_mobile():
    return send_from_directory('.', 'btc_macro_dashboard_mobile.html')


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


def _aggregate_candles(daily, period):
    """Aggregate daily OHLCV into weekly or monthly candles."""
    import datetime
    from collections import defaultdict
    buckets = defaultdict(list)
    for c in daily:
        dt = datetime.datetime.utcfromtimestamp(c[0] / 1000)
        if period == '1w':
            anchor = dt - datetime.timedelta(days=dt.weekday())  # Monday
        else:  # '1M'
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
            TF_MS = {'1m': 60_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000,
                     '6h': 21_600_000, '1d': 86_400_000}
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
        for item in pie:
            code = (item.get("code") or item.get("currency_code") or "").upper()
            # API returns "usd_value" (string)
            val  = float(item.get("usd_value") or item.get("current_value_usd") or 0)
            if code == "BTC":
                btc_usd = val
            elif code in ("USDC", "USDT", "USD"):
                usdc_usd += val

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
            "usdc_usd":       usdc_usd,
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

        t = add_target(
            label=label,
            trigger_price=float(trigger_price),
            direction=direction,
            price_target=float(body["price_target"]) if body.get("price_target") else None,
            reversal_atr_mult=float(body.get("reversal_atr_mult", 2.0)),
            confirm_closes=int(body.get("confirm_closes", 2)),
            rearm_cooldown_h=float(body.get("rearm_cooldown_h", 4.0)),
            dca_enabled=bool(body.get("dca_enabled", False)),
            dca_base_order_usd=float(body.get("dca_base_order_usd", 500)),
            dca_safety_count=int(body.get("dca_safety_count", 5)),
            dca_safety_step_pct=float(body.get("dca_safety_step_pct", 1.5)),
            dca_safety_volume_mult=float(body.get("dca_safety_volume_mult", 1.2)),
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
    "dashboard_server.py",
    "threecommas.py",
    "threecommas_dca.py",
    "grid_logic.py",
    "session.py",
    "engine.py",
    "regime.py",
    "market_data.py",
    "breakout.py",
    "inventory.py",
    "indicators.py",
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

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"status": "deploying", "branch": _DEPLOY_BRANCH, "files": results})


if __name__ == "__main__":
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
        _heal_files = ["dashboard.html", "btc_macro_dashboard.html", "btc_macro_dashboard_mobile.html"]
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
