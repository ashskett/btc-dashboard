import os
import json
import base64
import time
import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from config import ACCOUNT_ID, QUOTE_CURRENCIES, MAX_SKEW

load_dotenv()

API_KEY = os.getenv("THREECOMMAS_API_KEY")
API_SECRET = os.getenv("THREECOMMAS_API_SECRET")  # path to RSA private key PEM file
BASE_URL = "https://api.3commas.io/public/api"


def _load_private_key():
    path = API_SECRET.strip() if API_SECRET else "/root/grid-engine/3commas_private.pem"
    if not os.path.exists(path):
        path = "/root/grid-engine/3commas_private.pem"
    with open(path, "rb") as f:
        pem = f.read()
    return serialization.load_pem_private_key(pem, password=None)

# ─────────────────────────────────────────────────────────────────────────────
# INVENTORY TARGET SETTINGS
#
# Three-layer system — each layer has a distinct job:
#
#   TAPER_ZONE   │ Soft ramp: skew increases gradually as ratio drifts toward
#                │ the band edge, preventing cliff-edge tilt flips caused by
#                │ small price moves oscillating across the boundary each cycle.
#                │
#   LOWER/UPPER  │ Band edges: inside here skew=0 (neutral grid tilt).
#   BAND         │ UPPER_BAND sits below engine.py MAX_BTC so the grid starts
#                │ tilting toward selling BEFORE the hard stop fires.
#                │
#   MIN/MAX_BTC  │ Hard stops in engine.py (0.20 / 0.80) — all bots off.
#   (engine.py)  │ Staggered from band edges so there is a warning zone.
#
# Stagger layout:
#   0.20 MIN_BTC ←── hard stop ──→ 0.45 LOWER_BAND  (25% warning gap)
#   0.62 UPPER_BAND ←── taper ──→ 0.80 MAX_BTC      (18% warning gap)
# ─────────────────────────────────────────────────────────────────────────────

_CACHE_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_cache.json")
_CACHE_MAX_AGE = 7200   # use cached value for up to 2 hours on API failure


def _load_cache():
    try:
        data = json.load(open(_CACHE_FILE))
        age  = time.time() - data.get("ts", 0)
        if age < _CACHE_MAX_AGE:
            return data["btc_ratio"], data["skew"]
        print(f"Inventory cache too old ({age/3600:.1f}h) — cannot use")
    except Exception:
        pass
    return None, None


def cache_age_seconds() -> float:
    """Return age of the inventory cache in seconds, or infinity if missing."""
    try:
        data = json.load(open(_CACHE_FILE))
        return time.time() - data.get("live_ts", data.get("ts", 0))
    except Exception:
        return float("inf")


def _save_cache(btc_ratio: float, skew: float, btc_qty: float = 0.0,
                usdc_qty: float = 0.0, btc_price: float = 0.0):
    try:
        json.dump({
            "btc_ratio": btc_ratio, "skew": skew, "ts": time.time(),
            "live_ts": time.time(),  # timestamp of last SUCCESSFUL live fetch
            "btc_qty": btc_qty, "usdc_qty": usdc_qty, "btc_price": btc_price,
        }, open(_CACHE_FILE, "w"))
    except Exception:
        pass


def portfolio_snapshot() -> dict | None:
    """
    Return the most recent portfolio snapshot from the inventory cache.
    Returns None if the cache is missing or stale.

    Keys: btc_qty, usdc_qty, btc_price, portfolio_usd, btc_ratio, ts
    No API call — uses whatever was fetched during the last calculate_inventory() cycle.
    """
    try:
        data = json.load(open(_CACHE_FILE))
        age  = time.time() - data.get("ts", 0)
        if age > _CACHE_MAX_AGE:
            return None
        btc_qty   = float(data.get("btc_qty",   0))
        usdc_qty  = float(data.get("usdc_qty",  0))
        btc_price = float(data.get("btc_price", 0))
        if btc_price == 0:
            return None
        return {
            "btc_qty":       round(btc_qty,  8),
            "usdc_qty":      round(usdc_qty, 2),
            "btc_price":     round(btc_price, 2),
            "portfolio_usd": round(btc_qty * btc_price + usdc_qty, 2),
            "btc_ratio":     round(data.get("btc_ratio", 0), 4),
            "ts":            data["ts"],
        }
    except Exception:
        return None


# ── Inventory band settings ───────────────────────────────────────────────────
# Defaults — overridden by inventory_settings.json when present.
# Edit via the dashboard Inventory → Band Settings panel, or POST /inventory/settings.
_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_settings.json")

_DEFAULT_SETTINGS = {
    "target_btc":  0.40,   # ideal BTC allocation
    "lower_band":  0.30,   # below here: grid tilts to buy
    "upper_band":  0.47,   # above here: grid tilts to sell
    "taper_zone":  0.03,   # ramp width on each side of the band edge
}

# Module-level fallbacks — kept for backwards compatibility with any code that
# imports these names directly.  Always use get_inventory_settings() at call time
# so dashboard changes take effect without an engine restart.
TARGET_BTC = _DEFAULT_SETTINGS["target_btc"]
LOWER_BAND = _DEFAULT_SETTINGS["lower_band"]
UPPER_BAND = _DEFAULT_SETTINGS["upper_band"]
TAPER_ZONE = _DEFAULT_SETTINGS["taper_zone"]


def get_inventory_settings() -> dict:
    """Return current band settings, merging file overrides over defaults."""
    try:
        if os.path.exists(_SETTINGS_FILE):
            saved = json.load(open(_SETTINGS_FILE))
            return {**_DEFAULT_SETTINGS, **saved}
    except Exception as e:
        print(f"Warning: could not load inventory_settings.json: {e}")
    return dict(_DEFAULT_SETTINGS)


def save_inventory_settings(settings: dict) -> dict:
    """
    Validate and persist band settings.  Returns the saved dict.
    Raises ValueError on bad values so the caller can return a 400.
    """
    target = float(settings.get("target_btc", _DEFAULT_SETTINGS["target_btc"]))
    lower  = float(settings.get("lower_band",  _DEFAULT_SETTINGS["lower_band"]))
    upper  = float(settings.get("upper_band",  _DEFAULT_SETTINGS["upper_band"]))
    taper  = float(settings.get("taper_zone",  _DEFAULT_SETTINGS["taper_zone"]))

    if not (0.0 < lower < upper < 1.0):
        raise ValueError(f"lower_band ({lower}) must be < upper_band ({upper}), both in (0,1)")
    if not (lower <= target <= upper):
        raise ValueError(f"target_btc ({target}) must be within [lower_band, upper_band]")
    if not (0.001 <= taper <= 0.15):
        raise ValueError(f"taper_zone ({taper}) must be between 0.001 and 0.15")

    data = {"target_btc": target, "lower_band": lower, "upper_band": upper, "taper_zone": taper}
    json.dump(data, open(_SETTINGS_FILE, "w"), indent=2)
    return data


def _signed_request(method: str, path: str, body=None) -> requests.Response:
    """
    Sign and execute a 3Commas API request using RSA (Self-generated key).
    Sign target: /public/api + path + json_body
    """
    payload = json.dumps(body) if body else ""
    sign_target = ("/public/api" + path + payload).encode()

    private_key = _load_private_key()
    sig = base64.b64encode(
        private_key.sign(sign_target, padding.PKCS1v15(), hashes.SHA256())
    ).decode()

    headers = {
        "Apikey": API_KEY,
        "Signature": sig,
        "Content-Type": "application/json",
    }

    r = requests.request(method, BASE_URL + path, headers=headers, data=payload)
    r.raise_for_status()
    return r


def _calculate_skew(btc_ratio: float) -> float:
    """
    Compute grid tilt skew from current BTC ratio.

    Inside the band:  skew = 0.0  (neutral)
    Outside the band: skew ramps linearly over TAPER_ZONE then holds at full value.

    Example with LOWER_BAND=0.55, TAPER_ZONE=0.03:
      ratio=0.55  → skew= 0.000  (band edge, taper begins)
      ratio=0.53  → skew=-0.067  (2/3 through taper)
      ratio=0.52  → skew=-0.100  (full skew, taper complete)
      ratio=0.40  → skew=-0.250  (clamped at MAX_SKEW)

    Eliminates the 0.10 cliff-edge jump that previously caused the grid
    tilt to flip on a ~$50 BTC price move crossing the band boundary.
    """
    s = get_inventory_settings()
    target = s["target_btc"]
    lower  = s["lower_band"]
    upper  = s["upper_band"]
    taper_zone = s["taper_zone"]

    if lower <= btc_ratio <= upper:
        return 0.0

    if btc_ratio < lower:
        distance  = lower - btc_ratio
        taper     = min(distance / taper_zone, 1.0)
        full_skew = btc_ratio - target            # negative here
    else:
        distance  = btc_ratio - upper
        taper     = min(distance / taper_zone, 1.0)
        full_skew = btc_ratio - target            # positive here

    return max(-MAX_SKEW, min(MAX_SKEW, full_skew * taper))


def calculate_inventory():
    """
    Fetch live BTC and quote balances from 3Commas.

    Returns (btc_ratio, skew).  On API failure, falls back to the last
    cached value (up to 2 hours old) before returning neutral (0.5, 0.0).

    Large-swing sanity check:
    If the live result differs from the cached value by > 0.12 in a single
    cycle, 3Commas may be returning a stale snapshot (observed during heavy
    fill activity when the balance cache hasn't caught up yet).  We wait 10s
    and re-fetch once.  If the re-fetch is closer to the cached value, we
    use it — a genuine large change would produce consistent results on both
    fetches.  Threshold 0.12 = ~2× the largest single-cycle drift that is
    explainable by normal price movement alone.
    """
    try:
        result = _calculate_inventory_live()
        btc_ratio = result[0]

        # Sanity check against cache
        cached_ratio, _ = _load_cache()
        if cached_ratio is not None and abs(btc_ratio - cached_ratio) > 0.12:
            print(f"WARNING: Large inventory swing detected "
                  f"({cached_ratio:.2%} → {btc_ratio:.2%}, "
                  f"Δ={btc_ratio - cached_ratio:+.2%}). "
                  f"Re-fetching in 10s to confirm...")
            time.sleep(10)
            try:
                result2 = _calculate_inventory_live()
                btc2 = result2[0]
                print(f"Re-fetch: {btc2:.2%}  (Δ from cache: {btc2 - cached_ratio:+.2%})")
                if abs(btc2 - cached_ratio) < abs(btc_ratio - cached_ratio):
                    print(f"Re-fetch closer to cache — using re-fetch value ({btc2:.2%})")
                    result = result2
                else:
                    print(f"Both fetches confirm large swing — accepting {btc_ratio:.2%}")
            except Exception as e2:
                print(f"Re-fetch failed ({e2}) — using first result ({btc_ratio:.2%})")

        # result = (btc_ratio, skew, btc_qty, usdc_qty, btc_price)
        _save_cache(result[0], result[1], result[2], result[3], result[4])
        return result[0], result[1]
    except Exception as e:
        cached_ratio, cached_skew = _load_cache()
        if cached_ratio is not None:
            print(f"Inventory API error ({e}) — using cached value "
                  f"(btc_ratio={cached_ratio:.2%}, skew={cached_skew:+.4f})")
            return cached_ratio, cached_skew
        print(f"Inventory API error ({e}) — no usable cache, falling back to neutral (50/50)")
        return 0.5, 0.0


def _calculate_inventory_live():
    """Inner implementation — raises on any failure."""

    if not API_KEY or not API_SECRET:
        raise ValueError(
            "THREECOMMAS_API_KEY and THREECOMMAS_API_SECRET must both be set in your .env file."
        )

    if not ACCOUNT_ID:
        raise ValueError(
            "THREECOMMAS_ACCOUNT_ID is not set in your .env file."
        )

    # Step 1: trigger balance sync — retry up to 3× with backoff.
    # If load_balances keeps failing, 3Commas serves its OWN stale cache via
    # pie_chart_data, which can be hours old.  Retrying gives the API a chance
    # to accept the request even under transient 5xx / rate-limit conditions.
    load_ok = False
    for attempt in range(1, 4):
        try:
            _signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/load_balances")
            load_ok = True
            break
        except Exception as e:
            wait = attempt * 4
            if attempt < 3:
                print(f"Warning: load_balances attempt {attempt}/3 failed ({e}) "
                      f"— retrying in {wait}s")
                time.sleep(wait)
            else:
                print(f"Warning: load_balances failed after 3 attempts ({e}) "
                      f"— pie_chart_data may return stale 3Commas balance")
    if load_ok:
        time.sleep(8)  # give 3Commas time to refresh its balance cache
    else:
        time.sleep(3)  # shorter wait — balance won't be fresh anyway

    # Step 2: fetch per-currency breakdown.
    # 3Commas occasionally returns 204/empty immediately after load_balances
    # before its internal cache has updated — retry up to 3× with backoff.
    assets = None
    for attempt in range(1, 4):
        r = _signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/pie_chart_data")
        if r.status_code == 204 or not r.text.strip():
            if attempt < 3:
                wait = attempt * 3
                print(f"Warning: 3Commas returned empty balance data "
                      f"(attempt {attempt}/3) — retrying in {wait}s")
                time.sleep(wait)
            else:
                raise RuntimeError("3Commas returned empty balance data after 3 attempts")
        else:
            assets = r.json()
            break

    if assets is None or not isinstance(assets, list):
        raise ValueError(f"Unexpected pie_chart_data response: {str(assets)[:300]}")

    btc = 0.0
    btc_value = 0.0
    quote_usd = 0.0

    for asset in assets:

        currency = asset.get("code", "").upper()
        amount = float(asset.get("amount", 0) or 0)
        usd_value = float(asset.get("usd_value", 0) or 0)

        if currency == "BTC":
            btc += amount
            btc_value += usd_value  # use 3Commas valuation (reflects total incl. locked in bots)

        elif currency in QUOTE_CURRENCIES:
            quote_usd += usd_value

    # Fetch live price for cache/display purposes only (not used in ratio calculation)
    price_r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    price_r.raise_for_status()

    btc_price = float(price_r.json()["data"]["amount"])

    # If 3Commas returned zero usd_value for BTC, fall back to live price calculation
    if btc_value == 0 and btc > 0:
        btc_value = btc * btc_price

    total = btc_value + quote_usd

    if total == 0:
        return 0.5, 0.0

    btc_ratio = btc_value / total

    # ─────────────────────────────────────────────────────────────────────────
    # INVENTORY SKEW — tapering ramp via _calculate_skew()
    # Skew is 0 inside the band, ramps smoothly over TAPER_ZONE outside it.
    # ─────────────────────────────────────────────────────────────────────────

    skew = _calculate_skew(btc_ratio)

    # Zone label for debug output
    s = get_inventory_settings()
    lb, ub, tz = s["lower_band"], s["upper_band"], s["taper_zone"]
    if btc_ratio < lb - tz:
        zone = "BELOW TAPER"
    elif btc_ratio < lb:
        zone = "LOWER TAPER"
    elif btc_ratio > ub + tz:
        zone = "ABOVE TAPER"
    elif btc_ratio > ub:
        zone = "UPPER TAPER"
    else:
        zone = "IN BAND"

    print(
        f"Inventory → BTC: {btc:.6f} (${btc_value:,.0f}) | "
        f"USDC: ${quote_usd:,.0f} | "
        f"Ratio: {btc_ratio:.2%} | "
        f"Target: {s['target_btc']:.0%} | "
        f"Band: {lb:.0%}–{ub:.0%} | "
        f"Zone: {zone} | "
        f"Skew: {skew:+.4f}"
    )

    return btc_ratio, skew, btc, quote_usd, btc_price