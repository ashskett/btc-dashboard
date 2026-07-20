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

    # Calculate qty_per_grid from budget if provided.
    # A grid of N lines places only N-1 live orders — the line nearest price
    # sits neutral (the base position, no order). Sizing qty against `grids`
    # therefore under-deploys by (N-1)/N: negligible at 10 levels (~10%) but a
    # big 33% shortfall once the fee guard squeezes a tier to 3 levels in low
    # vol. Size against the live-order count so the full budget is deployed
    # regardless of level count.
    active_orders = max(grids - 1, 1)
    if budget_usd and budget_usd > 0 and mid_price > 0:
        qty = budget_usd / (active_orders * mid_price)
        print(f"    Budget: ${budget_usd:,.0f} → qty_per_grid={qty:.6f} BTC "
              f"(${budget_usd/active_orders:,.0f}/order × {active_orders} live orders "
              f"of {grids} lines)")
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


def execute_smart_trade(target: dict, current_price: float, btc_ratio: float,
                        sell_pct_override: float = None, note_suffix: str = "") -> dict:
    """
    Open a 3Commas SmartTrade (spot SELL) on support failure confirmation.

    Sells smart_trade_sell_pct% of current BTC holdings at market.
    Supports multi-step TP levels (smart_trade_tp_steps) or single TP fallback.
    Sets SL smart_trade_sl_pct% above entry (if support recovers, exit).

    sell_pct_override: for dual entry, pass the reduced sell % (e.g. 7.5 for scout of 25%)
    note_suffix: e.g. " (Scout)" or " (Retest)" for dual entry labelling

    Returns the SmartTrade API response dict.
    """
    account_id  = int(os.getenv("THREECOMMAS_ACCOUNT_ID", 0))
    sell_pct    = (sell_pct_override if sell_pct_override is not None
                   else float(target.get("smart_trade_sell_pct", 25.0))) / 100.0
    sl_pct      = float(target.get("smart_trade_sl_pct",  1.5))
    sl_price    = round(current_price * (1 + sl_pct / 100.0), 2)

    # Build TP steps — multi-step if configured, else single legacy TP
    tp_steps_cfg = target.get("smart_trade_tp_steps") or []
    if tp_steps_cfg:
        tp_steps = []
        for step in tp_steps_cfg:
            step_price = round(current_price * (1 - float(step["profit_pct"]) / 100.0), 2)
            tp_steps.append({
                "order_type": "limit",
                "price":      {"value": str(step_price), "type": "last"},
                "volume":     float(step["close_pct"]),
            })
        tp_desc = " / ".join(f"{s['profit_pct']}%@{s['close_pct']}%" for s in tp_steps_cfg)
    else:
        tp_pct   = float(target.get("smart_trade_tp_pct", 3.0))
        tp_price = round(current_price * (1 - tp_pct / 100.0), 2)
        tp_steps = [{
            "order_type": "limit",
            "price":      {"value": str(tp_price), "type": "last"},
            "volume":     100,
        }]
        tp_desc = f"{tp_pct:.1f}% below"

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
            "steps":   tp_steps,
        },
        "stop_loss": {
            "enabled":    True,
            "order_type": "market",
            "price":      {"value": str(sl_price), "type": "last"},
            "conditional": {"price": {"type": "last"}},
        },
        "note": f"Support failure: {target.get('label', '')}{note_suffix} @ ${current_price:,.0f}",
    }

    print(f"  SmartTrade SELL{note_suffix}: {sell_pct*100:.1f}% BTC | "
          f"entry ~${current_price:,.0f} | TP [{tp_desc}] | "
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


# ── Auto-remediation guards (Ash 2026-07-17: system should protect itself) ──
REDEPLOY_MIN_GAP_SECS = 480     # (B) min gap between NORMAL redeploys — anti-cascade
FORCED_TRADE_FRAC     = 0.12    # (A) abort a NORMAL redeploy if any tier would deploy
                                #     base this far (frac of range) from current holdings
                                #     → it would force a big base market buy/sell (~$9k@$75k)
_HERE_TC = os.path.dirname(os.path.abspath(__file__))


def _last_redeploy_ts():
    try:
        return float(json.load(open(os.path.join(_HERE_TC, "grid_state.json")))
                     .get("last_redeploy_ts") or 0)
    except Exception:
        return 0.0


def _tc_notify(msg):
    try:
        import notify
        notify.notify_critical(msg)
    except Exception:
        pass


def _forced_trade_check(tiers, price, cur_ratio):
    """(A) Return the worst tier whose deployed base fraction is > FORCED_TRADE_FRAC
    from current holdings — i.e. enabling it would market buy/sell a big base chunk.
    None = safe (every tier deploys ≈ at holdings, no forced trade)."""
    worst = None
    for t in tiers[:3]:
        lo = float(t.get("grid_low", 0)); hi = float(t.get("grid_high", 0))
        w = hi - lo
        if w <= 0 or not (lo < price < hi):
            continue
        f = (hi - price) / w
        off = abs(f - cur_ratio)
        if off > FORCED_TRADE_FRAC and (worst is None or off > worst[1]):
            worst = (t.get("name"), off, f)
    return worst


def _size_tiers_to_holdings(tiers, price, cur_ratio):
    """Shift each tier's range so the base it acquires on enable ≈ what we ALREADY
    hold, instead of market-buying a big centred (~50%) base in one shot.

    A 3Commas grid bot's base ≈ the fraction of its range ABOVE price (it must
    hold BTC to back the sell rungs), and it MARKET-buys that base on enable. A
    centred grid therefore slams the account toward ~50% BTC on every redeploy —
    the churn that bought ~$24k at one price on the 2026-06-29 weekend exit. By
    placing price higher in the range (above-fraction = current ratio), the bot
    deploys at ~current holdings and acquires/sheds the rest ORGANICALLY via its
    own limit orders. Width is preserved; only applied when meaningfully off
    centre and within the NORMAL inventory band (intensive modes own the extremes).
    Mutates tiers in place; caller saves the shifted ranges to grid_state."""
    # Deploy at ≈ current holdings so a redeploy triggers NO market order — not a
    # buy AND not a sell. Two landmines this closes:
    #   • floor was 0.25 → a low-ratio redeploy deployed CENTRED and market-BOUGHT
    #     ~50% base (~$29k risk, 2026-07-05).
    #   • ceiling was 0.75 AND clamp 0.70 → an over-weight redeploy (ratio 86% on a
    #     drift recentre) deployed CENTRED/clamped-down and market-SOLD ~0.32 BTC
    #     at the day's LOW (~$450 opportunity cost, 2026-07-17). Shedding must go
    #     through SELL_ONLY (bounce-guarded), NEVER a blind redeploy sell.
    # So: apply across the whole realistic band and deploy at cur_ratio, clamped
    # only at degenerate extremes [0.10, 0.90] (a fully one-sided grid). The clamp
    # can only ever nudge a BUY at <10% or a SELL above 90% — both far outside
    # normal operation and tiny. Intensive/ride modes never pass size_base=True.
    if not (0.02 <= cur_ratio <= 0.98) or abs(cur_ratio - 0.5) <= 0.08:
        return False
    target_f = max(0.10, min(0.90, cur_ratio))   # base fraction to deploy at ≈ holdings
    shifted = False
    for tier in tiers[:3]:
        lo = float(tier.get("grid_low", 0)); hi = float(tier.get("grid_high", 0))
        width = hi - lo
        if width <= 0 or not (lo < price < hi):
            continue
        f_now = (hi - price) / width
        new_hi = round(price + target_f * width, 2)
        new_lo = round(new_hi - width, 2)
        tier["grid_high"] = new_hi
        tier["grid_low"]  = new_lo
        if "center" in tier:
            tier["center"] = round((new_hi + new_lo) / 2, 2)
        shifted = True
        _verb = "buys" if target_f > f_now else "sheds"
        print(f"    [base-size] {tier.get('name')}: holdings {cur_ratio:.0%} BTC → "
              f"deploy base ~{target_f:.0%} (was ~{f_now:.0%} centred) — "
              f"range ${new_lo:,.0f}–${new_hi:,.0f}; grid {_verb} the rest via "
              f"limit orders — NO market buy or sell on this redeploy")
    return shifted


def redeploy_all_bots(bot_ids, tiers, size_base=False):
    """
    Redeploy all bots with their respective tier parameters.
    bot_ids: list of 3Commas bot ID strings
    tiers:   list of tier dicts from calculate_grid_parameters()
    size_base: if True, shift ranges so the deployed base ≈ current holdings
               (avoids a big base MARKET buy/sell). Only for NORMAL full-grid
               redeploys — NOT intensive/ride/exhaust modes which set base on
               purpose. Internally gated to the normal inventory band anyway.

    Applies capital budgets from tier_budgets.json — each tier gets a fixed %
    of total portfolio value. This prevents 3Commas from auto-allocating all
    available capital to whichever bot starts first.

    Returns True if the bots were (re)deployed, False if the redeploy was SKIPPED
    by a guard (cooldown or forced-trade). Callers MUST gate their follow-up
    update_grid_center/_mark_all_bots_started on the return, or state desyncs.
    """
    # ── (B) Cascade cooldown — NORMAL redeploys only. Intensive/safety redeploys
    # (size_base=False: SELL_ONLY/BUY_ONLY/ride/exhaust) always proceed. Stops the
    # drift→mode→mode churn (3 redeploys in 3 min, 2026-07-17).
    if size_base:
        _gap = time.time() - _last_redeploy_ts()
        if 0 < _gap < REDEPLOY_MIN_GAP_SECS:
            print(f"  Redeploy SKIPPED (cooldown) — only {_gap:.0f}s since last "
                  f"(< {REDEPLOY_MIN_GAP_SECS}s); avoiding cascade churn")
            return False

    # Fetch total portfolio value for budget calculation
    # Three attempts in priority order to avoid the death spiral where
    # low deployed qty → low estimated portfolio → low budget → even lower qty.
    budgets = load_tier_budgets()
    portfolio_usd = 0.0

    # Attempt 1: cached portfolio snapshot (fastest, usually fresh)
    try:
        from inventory import portfolio_snapshot
        snap = portfolio_snapshot()
        if snap:
            portfolio_usd = snap.get("portfolio_usd", 0)
            if portfolio_usd > 0:
                print(f"  Portfolio (cached): ${portfolio_usd:,.0f}")
    except Exception as e:
        print(f"  Warning: portfolio snapshot failed: {e}")

    # Attempt 2: live fetch from 3Commas account balance
    if portfolio_usd <= 0:
        try:
            from inventory import calculate_inventory, _calculate_inventory_live
            result = _calculate_inventory_live()
            if result and len(result) >= 5:
                btc_qty, usdc_qty, btc_price = result[2], result[3], result[4]
                portfolio_usd = btc_qty * btc_price + usdc_qty
                print(f"  Portfolio (live fetch): ${portfolio_usd:,.0f} "
                      f"({btc_qty:.4f} BTC + ${usdc_qty:,.0f} USDC)")
        except Exception as e:
            print(f"  Warning: live portfolio fetch failed: {e}")

    # If both fetches failed, do NOT invent a portfolio value. The old behaviour
    # used a hardcoded $60k floor, which under-deployed whenever the real
    # portfolio was larger (e.g. 95% of $60k against a real $95k balance reads as
    # ~60% allocated — the "allocation keeps resetting to 60%" bug). Instead skip
    # capital re-sizing for this redeploy: grid ranges still update, but each
    # bot's existing qty_per_grid is preserved (budget_usd=None) so we never
    # deploy a wrong amount. Sizing self-corrects on the next good fetch.
    _skip_sizing = portfolio_usd <= 0
    if _skip_sizing:
        print("  WARNING: could not determine portfolio value — SKIPPING capital "
              "re-sizing this redeploy (preserving each bot's existing qty_per_grid). "
              "Grid ranges still update.")
    else:
        print(f"  Portfolio: ${portfolio_usd:,.0f}")
        for b in budgets:
            pct = b.get("pct", 0)
            print(f"    {b['name']}: {pct}% = ${portfolio_usd * pct / 100:,.0f}")

    # ── Base-sizing: avoid a big base-position MARKET buy/sell on this redeploy ──
    if size_base and not _skip_sizing:
        try:
            from inventory import portfolio_snapshot as _ps
            _snap = _ps()
            _price = float(_snap.get("btc_price") or 0) if _snap else 0
            _btc   = float(_snap.get("btc_qty") or 0) if _snap else 0
            _ratio = (_btc * _price) / portfolio_usd if (portfolio_usd > 0 and _price) else None
            if _ratio is not None and _price > 0:
                if not _size_tiers_to_holdings(tiers, _price, _ratio):
                    print(f"    [base-size] holdings {_ratio:.0%} BTC near centred — "
                          f"deploying normally (no shift needed)")
                # ── (A) Forced-trade hard guard — the last line of defence. After
                # sizing, EVERY tier should deploy ≈ at holdings. If one still would
                # force a big base market buy/sell (a skipped/degenerate tier, a
                # future regression), ABORT the whole redeploy — keep the current
                # grid, never dump/slam base at the redeploy-moment price.
                _bad = _forced_trade_check(tiers, _price, _ratio)
                if _bad:
                    _nm, _off, _f = _bad
                    _usd = _off * portfolio_usd
                    print(f"  Redeploy ABORTED (forced-trade guard) — '{_nm}' tier would "
                          f"deploy base {_f:.0%} vs holdings {_ratio:.0%} → ~${_usd:,.0f} "
                          f"market trade. Keeping current grid.")
                    _tc_notify(f"Griddy GUARD: redeploy aborted — would force ~${_usd:,.0f} "
                               f"base trade (tier '{_nm}' at {_f:.0%} vs holdings {_ratio:.0%}). "
                               f"Grid left as-is; check the sizing logic.")
                    return False
        except Exception as _bse:
            print(f"    [base-size] skipped (error: {_bse})")

    results = []
    for i, bot_id in enumerate(bot_ids[:3]):
        tier = tiers[i] if i < len(tiers) else tiers[-1]
        tier_name = tier.get("name", f"tier{i}")

        # Find matching budget (skipped entirely if portfolio is unknown — see above)
        budget_usd = None
        if not _skip_sizing:
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
