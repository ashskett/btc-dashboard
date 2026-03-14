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
#   0.20 MIN_BTC ←── hard stop ──→ 0.55 LOWER_BAND  (35% warning gap)
#   0.72 UPPER_BAND ←── taper ──→ 0.80 MAX_BTC       ( 8% warning gap)
# ─────────────────────────────────────────────────────────────────────────────

TARGET_BTC = 0.65   # ideal BTC allocation
LOWER_BAND = 0.55   # below here: grid tilts to buy
UPPER_BAND = 0.72   # above here: grid tilts to sell  ← was 0.75, now staggered from MAX_BTC
TAPER_ZONE = 0.03   # ramp width on each side of the band edge


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
    if LOWER_BAND <= btc_ratio <= UPPER_BAND:
        return 0.0

    if btc_ratio < LOWER_BAND:
        distance  = LOWER_BAND - btc_ratio           # positive, grows as ratio falls
        taper     = min(distance / TAPER_ZONE, 1.0)  # 0→1 over TAPER_ZONE width
        full_skew = btc_ratio - TARGET_BTC            # negative here
    else:
        distance  = btc_ratio - UPPER_BAND            # positive, grows as ratio rises
        taper     = min(distance / TAPER_ZONE, 1.0)
        full_skew = btc_ratio - TARGET_BTC            # positive here

    return max(-MAX_SKEW, min(MAX_SKEW, full_skew * taper))


def calculate_inventory():
    """
    Fetch live BTC and quote balances from 3Commas.

    Returns (btc_ratio, skew)

    btc_ratio = BTC value / total portfolio value
    skew = inventory pressure outside the neutral band
    """

    if not API_KEY or not API_SECRET:
        raise ValueError(
            "THREECOMMAS_API_KEY and THREECOMMAS_API_SECRET must both be set in your .env file."
        )

    if not ACCOUNT_ID:
        raise ValueError(
            "THREECOMMAS_ACCOUNT_ID is not set in your .env file."
        )

    # Step 1: trigger balance sync (fire-and-continue — don't abort if this fails)
    try:
        _signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/load_balances")
    except Exception as e:
        print(f"Warning: load_balances failed ({e}) — proceeding with cached data")
    time.sleep(2)  # reduced from 3s — 2s is sufficient for 3Commas to update

    # Step 2: fetch per-currency breakdown
    r = _signed_request("POST", f"/ver1/accounts/{ACCOUNT_ID}/pie_chart_data")

    if r.status_code == 204 or not r.text.strip():
        print("Warning: 3Commas returned empty balance data — using neutral inventory (50/50)")
        return 0.5, 0.0

    assets = r.json()

    if not isinstance(assets, list):
        raise ValueError(f"Unexpected pie_chart_data response: {str(assets)[:300]}")

    btc = 0.0
    quote_usd = 0.0

    for asset in assets:

        currency = asset.get("code", "").upper()
        amount = float(asset.get("amount", 0) or 0)
        usd_value = float(asset.get("usd_value", 0) or 0)

        if currency == "BTC":
            btc += amount

        elif currency in QUOTE_CURRENCIES:
            quote_usd += usd_value

    # Convert BTC to USD
    price_r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    price_r.raise_for_status()

    btc_price = float(price_r.json()["data"]["amount"])

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
    if btc_ratio < LOWER_BAND - TAPER_ZONE:
        zone = "BELOW TAPER"
    elif btc_ratio < LOWER_BAND:
        zone = "LOWER TAPER"
    elif btc_ratio > UPPER_BAND + TAPER_ZONE:
        zone = "ABOVE TAPER"
    elif btc_ratio > UPPER_BAND:
        zone = "UPPER TAPER"
    else:
        zone = "IN BAND"

    print(
        f"Inventory → BTC: {btc_ratio:.2%} | "
        f"Target: {TARGET_BTC:.0%} | "
        f"Band: {LOWER_BAND:.0%}–{UPPER_BAND:.0%} | "
        f"Zone: {zone} | "
        f"Skew: {skew:+.4f}"
    )

    return btc_ratio, skew