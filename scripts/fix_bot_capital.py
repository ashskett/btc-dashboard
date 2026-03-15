#!/usr/bin/env python3
"""
One-shot script to fix ERR_805Y insufficient-funds errors on all three grid bots.

For each bot it:
  1. Fetches current config + investment holdings
  2. Calculates the max qty_per_grid that available BTC/USDC can fund
  3. Stops the bot, PATCHes the new qty_per_grid, restarts it

Run from /root/grid-engine with the venv active:
    source venv/bin/activate && python scripts/fix_bot_capital.py
"""
import os, sys, json, time, base64, requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()
API_KEY    = os.getenv("THREECOMMAS_API_KEY")
API_SECRET = os.getenv("THREECOMMAS_API_SECRET")
BASE_URL   = "https://api.3commas.io/public/api"
BOT_IDS    = [b.strip() for b in os.getenv("GRID_BOT_IDS", "").split(",") if b.strip()]

if not API_KEY or not API_SECRET:
    sys.exit("ERROR: THREECOMMAS_API_KEY / THREECOMMAS_API_SECRET not set in .env")
if not BOT_IDS:
    sys.exit("ERROR: GRID_BOT_IDS not set in .env")


def _load_key():
    path = API_SECRET.strip()
    if not os.path.exists(path):
        path = "/root/grid-engine/3commas_private.pem"
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _req(method, path, body=None):
    payload = json.dumps(body) if body else ""
    target  = ("/public/api" + path + payload).encode()
    sig     = base64.b64encode(_load_key().sign(target, padding.PKCS1v15(), hashes.SHA256())).decode()
    headers = {"Apikey": API_KEY, "Signature": sig, "Content-Type": "application/json"}
    return requests.request(method, BASE_URL + path, headers=headers, data=payload, timeout=15)


def get_bot(bot_id):
    r = _req("GET", f"/ver1/grid_bots/{bot_id}")
    if r.status_code != 200:
        raise RuntimeError(f"get_bot({bot_id}) failed {r.status_code}: {r.text[:200]}")
    return r.json()


def stop_bot(bot_id):
    r = _req("POST", f"/ver1/grid_bots/{bot_id}/disable")
    time.sleep(1)
    return r.status_code in (200, 201, 204)


def start_bot(bot_id):
    r = _req("POST", f"/ver1/grid_bots/{bot_id}/enable")
    time.sleep(0.5)
    return r.status_code in (200, 201, 204)


def fix_bot(bot_id):
    print(f"\n── Bot {bot_id} ──────────────────────────────")

    bot = get_bot(bot_id)
    name   = bot.get("name", bot_id)
    lower  = float(bot.get("lower_price", 0))
    upper  = float(bot.get("upper_price", 0))
    grids  = int(bot.get("grids_quantity", 1))
    old_qty = float(bot.get("quantity_per_grid") or 0)

    btc_held  = float(bot.get("investment_base_currency")  or 0)
    usdc_held = float(bot.get("investment_quote_currency") or 0)
    enabled   = bool(bot.get("is_enabled", False))

    mid_price   = (lower + upper) / 2
    sell_levels = max(1, round((upper - mid_price) / (upper - lower) * grids))
    buy_levels  = max(1, grids - sell_levels)

    candidates = []
    if btc_held  > 0: candidates.append(btc_held  / sell_levels)
    if usdc_held > 0: candidates.append(usdc_held / (buy_levels * mid_price))

    if not candidates:
        print(f"  {name}: no capital detected — skipping")
        return

    new_qty = min(candidates)

    print(f"  Name        : {name}")
    print(f"  Range       : ${lower:,.2f} – ${upper:,.2f}  |  {grids} levels")
    print(f"  Capital     : {btc_held:.4f} BTC  +  ${usdc_held:,.2f} USDC")
    print(f"  Sell levels : {sell_levels}  |  Buy levels : {buy_levels}")
    print(f"  qty_per_grid: {old_qty:.6f}  →  {new_qty:.6f} BTC", end="")

    if new_qty >= old_qty * 0.95:
        print("  (no change needed)")
        return
    print(f"  ({(1-new_qty/old_qty)*100:.0f}% reduction)")

    # Stop if running
    if enabled:
        print(f"  Stopping...")
        if not stop_bot(bot_id):
            print("  WARNING: stop returned unexpected status — continuing anyway")
        time.sleep(2)

    # PATCH new qty_per_grid (keep all other params)
    patch = {
        "name":              name,
        "upper_price":       upper,
        "lower_price":       lower,
        "grids_quantity":    grids,
        "quantity_per_grid": round(new_qty, 8),
        "grid_type":         bot.get("grid_type", "arithmetic"),
        "ignore_warnings":   True,
    }
    for key in ("upper_stop_loss_enabled", "lower_stop_loss_enabled",
                "upper_stop_loss_action",  "lower_stop_loss_action",
                "upper_stop_loss_price",   "lower_stop_loss_price"):
        if bot.get(key) is not None:
            patch[key] = bot[key]

    r = _req("PATCH", f"/ver1/grid_bots/{bot_id}/manual", body=patch)
    if r.status_code not in (200, 201):
        print(f"  ERROR: PATCH failed {r.status_code}: {r.text[:300]}")
        if enabled:
            print("  Attempting to restart anyway...")
            start_bot(bot_id)
        return

    print(f"  PATCH ok  →  restarting...")
    time.sleep(1)
    if start_bot(bot_id):
        print(f"  ✓ Done")
    else:
        print(f"  WARNING: start returned unexpected status")


if __name__ == "__main__":
    print("fix_bot_capital.py — adjusting qty_per_grid to match available capital")
    for bot_id in BOT_IDS:
        try:
            fix_bot(bot_id)
        except Exception as e:
            print(f"  ERROR on {bot_id}: {e}")
    print("\nDone. Check the 3Commas app — Funds warnings should clear within ~30s.")
