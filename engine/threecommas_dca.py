"""
threecommas_dca.py — 3Commas DCA bot API integration.

DCA (Dollar-Cost Average) bots are directional. They place a base order
when launched, then place safety orders at fixed % drops below entry,
accumulating a position. They exit everything at a take-profit % above
average cost.

Grid bots profit from oscillation. DCA bots profit from directional moves.
They are complementary:
  - Grid bots catch the chop
  - DCA bot catches the trend leg (73k → 82k)

The engine launches a DCA bot when a price target fires with dca_enabled=true.
It is disabled (not panic-sold) when the target clears — 3Commas will handle
the take-profit exit automatically.

Key 3Commas DCA endpoints used:
  POST /ver1/bots/create_bot         — create (does NOT auto-start)
  POST /ver1/bots/{id}/enable        — activate (creates first deal)
  POST /ver1/bots/{id}/disable       — pause (no new deals; existing deals live)
  POST /ver1/bots/{id}/panic_sell    — emergency: sell all open deals at market
  GET  /ver1/bots/{id}/show          — status, P&L, active deal count
  GET  /ver1/deals?bot_id={id}       — list active deals

Capital note:
  The DCA bot draws from the same Coinbase account as the grid bots.
  Allocate a fixed USDC budget per target (dca_base_order_usd) and size
  safety orders proportionally. Total max exposure =
    base_order + safety_order × Σ(volume_mult^i for i in range(safety_count))
"""

import os
import json
import base64
import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()

API_KEY    = os.getenv("THREECOMMAS_API_KEY")
API_SECRET = os.getenv("THREECOMMAS_API_SECRET")
ACCOUNT_ID = os.getenv("THREECOMMAS_ACCOUNT_ID")
BASE_URL   = "https://api.3commas.io/public/api"


def _load_private_key():
    path = API_SECRET.strip() if API_SECRET else "/root/grid-engine/3commas_private.pem"
    if not os.path.exists(path):
        path = "/root/grid-engine/3commas_private.pem"
    with open(path, "rb") as f:
        pem = f.read()
    return serialization.load_pem_private_key(pem, password=None)


def _signed_request(method: str, path: str, body=None) -> requests.Response:
    payload     = json.dumps(body) if body else ""
    sign_target = ("/public/api" + path + payload).encode()
    private_key = _load_private_key()
    sig = base64.b64encode(
        private_key.sign(sign_target, padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    headers = {
        "Apikey":       API_KEY,
        "Signature":    sig,
        "Content-Type": "application/json",
    }
    r = requests.request(method, BASE_URL + path, headers=headers, data=payload, timeout=15)
    return r


# ── Public API ─────────────────────────────────────────────────────────────────

def get_dca_bots() -> list:
    """List all DCA bots on the account."""
    r = _signed_request("GET", f"/ver1/bots?account_id={ACCOUNT_ID}&limit=50&strategy=long")
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def get_dca_bot(bot_id: str) -> dict:
    """Get DCA bot status, stats, and active deal count."""
    r = _signed_request("GET", f"/ver1/bots/{bot_id}/show")
    r.raise_for_status()
    return r.json()


def create_dca_bot(
    label: str,
    base_order_usd: float,
    safety_order_usd: float,
    take_profit_pct: float,
    safety_order_count: int = 5,
    safety_order_step_pct: float = 1.5,
    safety_order_volume_mult: float = 1.2,
    pair: str = "USDC_BTC",
    take_profit_steps: list = None,
) -> dict:
    """
    Create a new DCA bot and return the API response dict (contains 'id').

    Does NOT enable the bot — call enable_dca_bot() after to start trading.

    take_profit_pct: % above average cost to close all deals (used when no steps).
    take_profit_steps: optional list of up to 4 partial-close targets, e.g.:
        [{"amount_percentage": 50, "profit_percentage": 3},
         {"amount_percentage": 50, "profit_percentage": 6}]
        amount_percentage values must sum to 100.

    safety_order_step_pct: % drop between each safety order.
    safety_order_volume_mult (martingale): each SO is this × larger than previous.
    """
    body = {
        "account_id":                      int(ACCOUNT_ID),
        "pairs":                           [pair],
        "base_order_volume":               str(round(base_order_usd, 2)),
        "base_order_volume_type":          "quote_currency",
        "safety_order_volume":             str(round(safety_order_usd, 2)),
        "safety_order_volume_type":        "quote_currency",
        "safety_order_step_percentage":    str(round(safety_order_step_pct, 2)),
        "martingale_volume_coefficient":   str(round(safety_order_volume_mult, 2)),
        "martingale_step_coefficient":     "1.0",
        "max_safety_orders":               safety_order_count,
        "active_safety_orders_count":      min(safety_order_count, 3),
        "name":                            label,
        "strategy_list":                   [{"strategy": "manual", "options": {}}],
        "leverage_type":                   "not_specified",
    }
    if take_profit_steps and len(take_profit_steps) > 0:
        # Step mode: each step closes a portion of the position at a different profit %.
        # API fields: amount_percentage (share to close) + profit_percentage (target %).
        # amount_percentage values must sum to 100.
        body["take_profit_type"]  = "step"
        body["take_profit_steps"] = [
            {
                "amount_percentage": round(s["close_pct"], 2),
                "profit_percentage": round(s["profit_pct"], 2),
            }
            for s in take_profit_steps
        ]
    else:
        body["take_profit_type"] = "total"
        body["take_profit"]      = str(round(take_profit_pct, 2))
    r = _signed_request("POST", "/ver1/bots/create_bot", body=body)
    if not r.ok:
        raise ValueError(f"3Commas {r.status_code}: {r.text[:500]}")
    return r.json()


def enable_dca_bot(bot_id: str) -> bool:
    """Enable bot — creates its first deal immediately."""
    r = _signed_request("POST", f"/ver1/bots/{bot_id}/enable")
    ok = r.status_code in (200, 201)
    if not ok:
        print(f"Warning: enable_dca_bot {bot_id} returned {r.status_code}: {r.text[:200]}")
    return ok


def disable_dca_bot(bot_id: str) -> bool:
    """
    Disable bot — no new deals started. Existing deals continue until
    they hit take-profit or are manually closed. Use this for a clean exit.
    """
    r = _signed_request("POST", f"/ver1/bots/{bot_id}/disable")
    ok = r.status_code in (200, 201)
    if not ok:
        print(f"Warning: disable_dca_bot {bot_id} returned {r.status_code}: {r.text[:200]}")
    return ok


def panic_sell_dca_bot(bot_id: str) -> bool:
    """
    Emergency exit — stops the bot AND market-sells all open deal positions
    immediately. Use when the thesis has fully invalidated and you want out now.
    """
    r = _signed_request("POST", f"/ver1/bots/{bot_id}/panic_sell")
    ok = r.status_code in (200, 201)
    if not ok:
        print(f"Warning: panic_sell_dca_bot {bot_id} returned {r.status_code}: {r.text[:200]}")
    return ok


def get_dca_deals(bot_id: str) -> list:
    """Return list of active deals for a DCA bot."""
    r = _signed_request("GET", f"/ver1/deals?bot_id={bot_id}&scope=active&limit=50")
    if r.status_code != 200:
        return []
    data = r.json()
    return data if isinstance(data, list) else []


def get_dca_completed_deals(bot_id: str, from_iso: str = None, to_iso: str = None, limit: int = 1000) -> list:
    """
    Return completed deals for a DCA bot, optionally filtered by close date.

    from_iso / to_iso: ISO 8601 strings, e.g. "2026-01-01T00:00:00Z".
    Returns deals sorted newest-first (3Commas default).
    Each deal has: final_profit_percentage, usd_final_profit, closed_at, pair, id.
    """
    params = f"bot_id={bot_id}&scope=completed&limit={limit}"
    if from_iso:
        params += f"&closed_at_from={from_iso}"
    if to_iso:
        params += f"&closed_at_to={to_iso}"
    r = _signed_request("GET", f"/ver1/deals?{params}")
    if r.status_code != 200:
        return []
    data = r.json()
    return data if isinstance(data, list) else []


def update_dca_bot(
    bot_id: str,
    base_order_usd: float = None,
    safety_order_usd: float = None,
    take_profit_pct: float = None,
    safety_order_count: int = None,
    safety_order_step_pct: float = None,
    safety_order_volume_mult: float = None,
    take_profit_steps: list = None,
) -> dict:
    """
    Update DCA bot parameters. Fetches current config and merges in any
    provided overrides — omitted args keep their existing values.
    """
    current = get_dca_bot(bot_id)
    pairs = current.get("pairs") or ["USDC_BTC"]
    pair = pairs[0] if isinstance(pairs, list) else pairs
    so_count = safety_order_count if safety_order_count is not None else int(current.get("max_safety_orders", 5))
    body = {
        "account_id":                    int(ACCOUNT_ID),
        "pairs":                         [pair],
        "base_order_volume":             str(round(base_order_usd          if base_order_usd          is not None else float(current.get("base_order_volume",             500)),  2)),
        "base_order_volume_type":        "quote_currency",
        "safety_order_volume":           str(round(safety_order_usd        if safety_order_usd        is not None else float(current.get("safety_order_volume",           100)),  2)),
        "safety_order_volume_type":      "quote_currency",
        "take_profit_type":              "total",
        "take_profit":                   str(round(take_profit_pct if take_profit_pct is not None else float(current.get("take_profit", 2.0)), 2)),
        "safety_order_step_percentage":  str(round(safety_order_step_pct   if safety_order_step_pct   is not None else float(current.get("safety_order_step_percentage",  1.5)),  2)),
        "martingale_volume_coefficient": str(round(safety_order_volume_mult if safety_order_volume_mult is not None else float(current.get("martingale_volume_coefficient", 1.2)), 2)),
        "martingale_step_coefficient":   "1.0",
        "max_safety_orders":             so_count,
        "active_safety_orders_count":    min(so_count, 3),
        "name":                          current.get("name", "DCA Bot"),
        "strategy_list":                 [{"strategy": "manual", "options": {}}],
        "leverage_type":                 "not_specified",
    }
    if take_profit_steps is not None:
        body["take_profit_steps"] = take_profit_steps
    r = _signed_request("PATCH", f"/ver1/bots/{bot_id}/update", body=body)
    if not r.ok:
        raise ValueError(f"3Commas {r.status_code}: {r.text[:500]}")
    return r.json()


def delete_dca_bot(bot_id: str) -> bool:
    """Delete a DCA bot (bot must be disabled first with no active deals)."""
    r = _signed_request("POST", f"/ver1/bots/{bot_id}/delete")
    if r.status_code not in (200, 201, 204):
        raise ValueError(f"3Commas {r.status_code}: {r.text[:300]}")
    return True


def estimate_max_exposure(
    base_order_usd: float,
    safety_order_usd: float,
    safety_order_count: int,
    safety_order_volume_mult: float,
) -> float:
    """
    Calculate maximum possible USDC exposure if all safety orders fill.
    Useful for capital planning before launching a DCA bot.
    """
    total = base_order_usd
    so = safety_order_usd
    for _ in range(safety_order_count):
        total += so
        so *= safety_order_volume_mult
    return round(total, 2)
