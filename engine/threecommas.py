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
    if not os.path.exists(path):
        path = "/root/grid-engine/3commas_private.pem"
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
    time.sleep(0.5)  # avoid 3Commas rate limit when stopping multiple bots in sequence

    if r.status_code in (200, 201, 204):
        print(f"  ✓ Bot {bot_id} stopped ({r.status_code})")
    else:
        print(f"  ✗ stop_bot({bot_id}) failed with {r.status_code}: {r.text}")

    return r


def start_bot(bot_id):
    """Enable a grid bot."""
    print(f"  Starting bot {bot_id}...")
    r = _signed_request("POST", f"/ver1/grid_bots/{bot_id}/enable")
    time.sleep(0.5)  # avoid 3Commas rate limit when starting multiple bots in sequence

    if r.status_code in (200, 201, 204):
        print(f"  ✓ Bot {bot_id} started ({r.status_code})")
    else:
        print(f"  ✗ start_bot({bot_id}) failed with {r.status_code}: {r.text}")

    return r


_BUDGET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tier_budgets.json")

# Default budget: % of total portfolio allocated to each tier
_DEFAULT_BUDGETS = [
    {"name": "inner",  "pct": 30},
    {"name": "mid",    "pct": 20},
    {"name": "outer",  "pct": 15},
]


def load_tier_budgets() -> list:
    try:
        if os.path.exists(_BUDGET_FILE):
            return json.load(open(_BUDGET_FILE))
    except Exception as e:
        print(f"Warning: could not load tier_budgets.json: {e}")
    return list(_DEFAULT_BUDGETS)


def save_tier_budgets(budgets: list):
    try:
        json.dump(budgets, open(_BUDGET_FILE, "w"), indent=2)
    except Exception as e:
        print(f"Warning: could not save tier_budgets.json: {e}")


def redeploy_bot(bot_id, tier, budget_usd=None):
    """
    Stop, update parameters for a single tier, then restart.

    tier dict keys used:
        grid_low    — lower_price
        grid_high   — upper_price
        levels      — grids_quantity
        name        — appended to bot name for clarity

    budget_usd: if provided, calculates qty_per_grid from budget rather
                than preserving whatever 3Commas had. This prevents capital
                creep where 3Commas auto-allocates available funds on enable.
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
    mid_price = (lower + upper) / 2

    # Calculate qty_per_grid from budget if provided
    if budget_usd and budget_usd > 0 and mid_price > 0:
        qty = budget_usd / (grids * mid_price)
        print(f"    Budget: ${budget_usd:,.0f} → qty_per_grid={qty:.6f} BTC "
              f"(${budget_usd/grids:,.0f}/level × {grids} levels)")
    else:
        # Fallback: preserve original qty (legacy behaviour for manual calls)
        qty = float(current.get("quantity_per_grid") or 0) or (100.0 / mid_price)
        print(f"    No budget set — using existing qty_per_grid={qty:.6f} BTC")

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


def set_bot_capital(bot_id: str, total_usd: float) -> dict:
    """
    Update a grid bot's capital by setting quantity_per_grid = total_usd / grids_quantity.
    If the bot is running, stops it first, applies the change, then restarts.
    """
    current = get_bot(bot_id)
    levels = int(current.get("grids_quantity") or 10)
    if levels <= 0:
        raise ValueError(f"Invalid grids_quantity: {levels}")

    qty_per_grid = round(total_usd / levels, 2)
    was_enabled  = bool(current.get("is_enabled", False))

    if was_enabled:
        stop_bot(bot_id)
        time.sleep(2)

    patch_body = {
        "name":             current.get("name", f"Grid Bot {bot_id}"),
        "upper_price":      float(current.get("upper_price", 0)),
        "lower_price":      float(current.get("lower_price", 0)),
        "grids_quantity":   levels,
        "quantity_per_grid": qty_per_grid,
        "grid_type":        current.get("grid_type", "arithmetic"),
        "ignore_warnings":  True,
    }
    r = _signed_request("PATCH", f"/ver1/grid_bots/{bot_id}/manual", body=patch_body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"PATCH failed {r.status_code}: {r.text[:300]}")

    if was_enabled:
        time.sleep(1)
        start_bot(bot_id)

    return {
        "ok":           True,
        "qty_per_grid": qty_per_grid,
        "total_usd":    total_usd,
        "levels":       levels,
        "restarted":    was_enabled,
    }


def execute_smart_trade(target: dict, current_price: float, btc_ratio: float) -> dict:
    """
    Open a 3Commas SmartTrade (spot SELL) on support failure confirmation.

    Sells smart_trade_sell_pct% of current BTC holdings at market.
    Sets TP smart_trade_tp_pct% below entry (limit buyback).
    Sets SL smart_trade_sl_pct% above entry (if support recovers, exit).

    Returns the SmartTrade API response dict.
    """
    account_id  = int(os.getenv("THREECOMMAS_ACCOUNT_ID", 0))
    sell_pct    = float(target.get("smart_trade_sell_pct", 25.0)) / 100.0
    tp_pct      = float(target.get("smart_trade_tp_pct",  3.0))
    sl_pct      = float(target.get("smart_trade_sl_pct",  1.5))

    # Estimate sell quantity from BTC ratio and account size.
    # btc_ratio is 0-1 fraction; we use it to get approximate BTC qty.
    # SmartTrade will reject if insufficient funds — that's fine, it surfaces clearly.
    # Use a reasonable estimate: if btc_ratio=0.65 and we want 25%, that's 0.65*0.25 fraction of portfolio.
    # We don't know exact BTC qty here, so let the SmartTrade use a % of available instead.
    # 3Commas SmartTrade supports "percent" unit type.
    tp_price = round(current_price * (1 - tp_pct / 100.0), 2)
    sl_price = round(current_price * (1 + sl_pct / 100.0), 2)

    body = {
        "account_id": account_id,
        "pair":       "USDC_BTC",
        "instant":    False,
        "leverage":   {"enabled": False},
        "position": {
            "type":       "sell",
            "units":      {"value": str(round(sell_pct * 100, 1)), "type": "percent"},
            "order_type": "market",
        },
        "take_profit": {
            "enabled": True,
            "steps": [{
                "order_type": "limit",
                "price":      {"value": str(tp_price), "type": "last"},
                "volume":     100,
            }],
        },
        "stop_loss": {
            "enabled":    True,
            "order_type": "market",
            "price":      {"value": str(sl_price), "type": "last"},
            "conditional": {"price": {"type": "last"}},
        },
        "note": f"Support failure: {target.get('label', '')} @ ${current_price:,.0f}",
    }

    print(f"  SmartTrade SELL: {sell_pct*100:.0f}% BTC | "
          f"entry ~${current_price:,.0f} | TP ${tp_price:,.0f} ({tp_pct:.1f}% down) | "
          f"SL ${sl_price:,.0f} ({sl_pct:.1f}% up)")

    r = _signed_request("POST", "/v2/smart_trades", body=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"SmartTrade POST failed {r.status_code}: {r.text[:400]}")
    return r.json()


def get_smart_trade(smart_trade_id: str) -> dict:
    """Fetch a SmartTrade by ID."""
    r = _signed_request("GET", f"/v2/smart_trades/{smart_trade_id}")
    if r.status_code != 200:
        raise RuntimeError(f"get_smart_trade({smart_trade_id}) failed: {r.status_code} {r.text[:200]}")
    return r.json()


def cancel_smart_trade(smart_trade_id: str) -> dict:
    """Cancel (close) an active SmartTrade."""
    r = _signed_request("DELETE", f"/v2/smart_trades/{smart_trade_id}")
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"cancel_smart_trade({smart_trade_id}) failed: {r.status_code} {r.text[:200]}")
    return r.json() if r.content else {}


def redeploy_all_bots(bot_ids, tiers):
    """
    Redeploy all bots with their respective tier parameters.
    bot_ids: list of 3Commas bot ID strings
    tiers:   list of tier dicts from calculate_grid_parameters()

    Applies capital budgets from tier_budgets.json — each tier gets a fixed %
    of total portfolio value. This prevents 3Commas from auto-allocating all
    available capital to whichever bot starts first.
    """
    # Fetch total portfolio value for budget calculation
    budgets = load_tier_budgets()
    portfolio_usd = 0.0
    try:
        from inventory import portfolio_snapshot
        snap = portfolio_snapshot()
        if snap:
            portfolio_usd = snap.get("portfolio_usd", 0)
    except Exception as e:
        print(f"  Warning: could not get portfolio snapshot for budgets: {e}")

    if portfolio_usd <= 0:
        # Fallback: sum up what's deployed across all bots
        try:
            total = 0
            for bid in bot_ids[:3]:
                b = get_bot(bid)
                qty = float(b.get("quantity_per_grid") or 0)
                lvl = int(b.get("grids_quantity") or 1)
                up  = float(b.get("upper_price") or 0)
                lo  = float(b.get("lower_price") or 0)
                total += qty * lvl * ((up + lo) / 2)
            portfolio_usd = total * 1.5  # rough estimate (deployed ≈ 65% of total)
        except Exception:
            portfolio_usd = 80000  # last-resort fallback
        print(f"  Using estimated portfolio: ${portfolio_usd:,.0f}")

    print(f"  Portfolio: ${portfolio_usd:,.0f}")
    for b in budgets:
        pct = b.get("pct", 0)
        print(f"    {b['name']}: {pct}% = ${portfolio_usd * pct / 100:,.0f}")

    results = []
    for i, bot_id in enumerate(bot_ids[:3]):
        tier = tiers[i] if i < len(tiers) else tiers[-1]
        tier_name = tier.get("name", f"tier{i}")

        # Find matching budget
        budget_usd = None
        for b in budgets:
            if b["name"] == tier_name:
                budget_usd = portfolio_usd * b["pct"] / 100.0
                break

        ok = redeploy_bot(bot_id, tier, budget_usd=budget_usd)
        results.append((bot_id, tier_name, ok))
        if i < len(bot_ids) - 1:
            time.sleep(1)  # stagger calls

    print("\n  Redeploy summary:")
    for bot_id, tier_name, ok in results:
        status = "✓" if ok else "✗"
        print(f"    {status} Bot {bot_id} ({tier_name})")

    return all(ok for _, _, ok in results)
