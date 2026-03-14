import requests
import os
import json
import time
import base64
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

load_dotenv()

API_KEY    = os.getenv("THREECOMMAS_API_KEY")
API_SECRET = os.getenv("THREECOMMAS_API_SECRET")  # path to RSA private key PEM file
BASE_URL   = "https://api.3commas.io/public/api"


def _load_private_key():
    path = API_SECRET.strip() if API_SECRET else "/root/grid-engine/3commas_private.pem"
    with open(path, "rb") as f:
        pem = f.read()
    return serialization.load_pem_private_key(pem, password=None)


def _signed_request(method, path, body=None):
    """
    Make a signed request to the 3Commas API using RSA (Self-generated key).
    Signs: path + json_body using RSASSA-PKCS1-v1_5 with SHA-256.
    Signature is Base64-encoded (RFC 2045).
    """
    payload     = json.dumps(body) if body else ""
    sign_target = ("/public/api" + path + payload).encode()

    private_key = _load_private_key()
    signature_bytes = private_key.sign(sign_target, padding.PKCS1v15(), hashes.SHA256())
    sig = base64.b64encode(signature_bytes).decode()

    headers = {
        "Apikey":       API_KEY,
        "Signature":    sig,
        "Content-Type": "application/json",
    }

    url = BASE_URL + path
    resp = requests.request(method, url, headers=headers, data=payload, timeout=15)
    return resp


def get_bot(bot_id):
    """Fetch current bot config from 3Commas. Returns dict or raises."""
    r = _signed_request("GET", f"/ver1/grid_bots/{bot_id}")
    if r.status_code != 200:
        raise RuntimeError(f"get_bot({bot_id}) failed: {r.status_code} {r.text}")
    return r.json()


def stop_bot(bot_id):
    """Disable a grid bot (cancels open orders, keeps config intact)."""
    print(f"  Stopping bot {bot_id}...")
    r = _signed_request("POST", f"/ver1/grid_bots/{bot_id}/disable")

    if r.status_code in (200, 201, 204):
        print(f"  ✓ Bot {bot_id} stopped ({r.status_code})")
    else:
        print(f"  ✗ stop_bot({bot_id}) failed with {r.status_code}: {r.text}")

    return r


def start_bot(bot_id):
    """Enable a grid bot."""
    print(f"  Starting bot {bot_id}...")
    r = _signed_request("POST", f"/ver1/grid_bots/{bot_id}/enable")

    if r.status_code in (200, 201, 204):
        print(f"  ✓ Bot {bot_id} started ({r.status_code})")
    else:
        print(f"  ✗ start_bot({bot_id}) failed with {r.status_code}: {r.text}")

    return r


def redeploy_bot(bot_id, tier):
    """
    Stop, update parameters for a single tier, then restart.

    tier dict keys used:
        grid_low    — lower_price
        grid_high   — upper_price
        levels      — grids_quantity
        name        — appended to bot name for clarity
    """
    print(f"  Redeploying bot {bot_id} ({tier['name']} tier)...")

    # 1. Fetch current config so we can preserve pair, quantity, currency settings
    try:
        current = get_bot(bot_id)
    except Exception as e:
        print(f"  ✗ Could not fetch bot config: {e}")
        return False

    # 2. Stop the bot first (required before editing range)
    stop_bot(bot_id)
    time.sleep(2)  # brief pause to let 3Commas cancel open orders

    # 3. Build the PATCH payload — only change range/levels, preserve everything else
    lower = round(tier["grid_low"],  2)
    upper = round(tier["grid_high"], 2)
    grids = int(tier["levels"])

    # Preserve quantity_per_grid from current config (set by user in 3Commas UI)
    qty = current.get("quantity_per_grid", current.get("investment_quote_currency"))

    patch_body = {
        "name":             current.get("name", f"Grid {tier['name']}"),
        "upper_price":      upper,
        "lower_price":      lower,
        "grids_quantity":   grids,
        "quantity_per_grid": float(qty) if qty else 100.0,
        "grid_type":        current.get("grid_type", "arithmetic"),
        "ignore_warnings":  True,  # don't abort if price is near boundary
    }

    # Preserve stop-loss settings if they were configured
    if current.get("upper_stop_loss_enabled"):
        patch_body["upper_stop_loss_enabled"] = True
        patch_body["upper_stop_loss_action"]  = current.get("upper_stop_loss_action", "stop_bot")
        # Set stop-loss just outside the new range
        patch_body["upper_stop_loss_price"]   = round(upper * 1.02, 2)

    if current.get("lower_stop_loss_enabled"):
        patch_body["lower_stop_loss_enabled"] = True
        patch_body["lower_stop_loss_action"]  = current.get("lower_stop_loss_action", "stop_bot")
        patch_body["lower_stop_loss_price"]   = round(lower * 0.98, 2)

    print(f"    Range: ${lower:,.2f} – ${upper:,.2f} | {grids} levels")

    # 4. PATCH the bot with new parameters
    path = f"/ver1/grid_bots/{bot_id}/manual"
    r = _signed_request("PATCH", path, body=patch_body)

    if r.status_code not in (200, 201):
        print(f"  ✗ redeploy_bot PATCH failed: {r.status_code} {r.text}")
        return False

    print(f"  ✓ Bot {bot_id} parameters updated")

    # 5. Re-enable the bot
    time.sleep(1)
    start_bot(bot_id)
    return True


def redeploy_all_bots(bot_ids, tiers):
    """
    Redeploy all bots with their respective tier parameters.
    bot_ids: list of 3Commas bot ID strings
    tiers:   list of tier dicts from calculate_grid_parameters()
    """
    results = []
    for i, bot_id in enumerate(bot_ids[:3]):
        tier = tiers[i] if i < len(tiers) else tiers[-1]
        ok = redeploy_bot(bot_id, tier)
        results.append((bot_id, tier["name"], ok))
        if i < len(bot_ids) - 1:
            time.sleep(1)  # stagger calls

    print("\n  Redeploy summary:")
    for bot_id, tier_name, ok in results:
        status = "✓" if ok else "✗"
        print(f"    {status} Bot {bot_id} ({tier_name})")

    return all(ok for _, _, ok in results)
