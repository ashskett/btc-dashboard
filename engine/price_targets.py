"""
price_targets.py — User-defined breakout trigger levels.

Lets you pre-set a price level that, when crossed, tells the engine
"I believe a big move is coming — keep the outer bot running and don't
let the automatic breakout detector stop it on a dip."

Compared to the automatic breakout detector (4 consecutive closes + ATR move),
price targets fire the instant price crosses the trigger. No momentum
confirmation needed — you've already made the call.

Config file: breakout_targets.json (sits alongside engine.py on the droplet)

Schema per target:
    {
        "id":                   "abc12345",       # auto-generated
        "label":                "ATH retest → $82k",
        "trigger_price":        73000,            # price level to watch
        "direction":            "UP",             # "UP" or "DOWN"
        "price_target":         82000,            # optional: where the move ends
        "reversal_atr_mult":    2.0,              # clear if price reverses 2×ATR from fire price
        "active":               true,             # arm/disarm without deleting
        "fired":                false,            # set to true when price crosses trigger
        "fired_at":             null,             # unix timestamp of crossing
        "fired_price":          null,             # exact price when it crossed
        "dca_enabled":          false,            # launch DCA bot when trigger fires
        "dca_base_order_usd":   500,              # DCA base order size in USDC
        "dca_safety_count":     5,                # number of safety orders
        "dca_safety_step_pct":  1.5,              # % drop between safety orders
        "dca_safety_volume_mult": 1.2,            # safety order volume multiplier
        "dca_bot_id":           null              # set by engine after DCA bot is created
    }

Engine behaviour when a target fires (UP):
  - outer ON (wide range captures oscillations on the way up)
  - inner + mid OFF (too tight — will get filled on the wrong side of a fast move)
  - DCA bot launched if dca_enabled=true (accumulates on dips toward price_target)
  - Normal breakout detector is bypassed (won't false-fire DOWN on a dip)
  - Drift detection bypassed (outer's 3×ATR range handles the move without redeployment)

Cleared automatically when:
  - price reaches price_target (target achieved)
  - price reverses more than reversal_atr_mult × ATR below the fire price (thesis invalidated)

Or manually via: POST /targets/<id>/clear  (re-arms the target for next crossing)
"""

import os
import json
import time
import uuid

_TARGETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "breakout_targets.json")


# ── File I/O ──────────────────────────────────────────────────────────────────

def load_targets() -> list:
    try:
        if not os.path.exists(_TARGETS_FILE):
            return []
        return json.load(open(_TARGETS_FILE))
    except Exception as e:
        print(f"Warning: could not load breakout_targets.json: {e}")
        return []


def save_targets(targets: list):
    try:
        json.dump(targets, open(_TARGETS_FILE, "w"), indent=2)
    except Exception as e:
        print(f"Warning: could not save breakout_targets.json: {e}")


# ── Core: check all targets against current price ─────────────────────────────

def check_targets(price: float, atr: float) -> dict | None:
    """
    Check all active targets against current price.

    For each armed (active=true) target:
      - If not yet fired: check if price crossed the trigger → mark fired
      - If already fired: check for completion (price_target hit) or
        reversal (price retreated > reversal_atr_mult × ATR) → clear if so

    Returns the first active+fired target after processing, or None.
    """
    targets = load_targets()
    changed = False
    active_target = None

    for t in targets:
        if not t.get("active"):
            continue

        direction = t.get("direction", "UP")
        trigger   = float(t.get("trigger_price", 0))
        fired     = t.get("fired", False)

        if fired:
            fire_price   = float(t.get("fired_price") or trigger)
            price_target = t.get("price_target")
            rev_mult     = float(t.get("reversal_atr_mult", 2.0))

            completed = bool(
                (direction == "UP"   and price_target and price >= price_target) or
                (direction == "DOWN" and price_target and price <= price_target)
            )
            reversed_ = bool(
                (direction == "UP"   and price < fire_price - atr * rev_mult) or
                (direction == "DOWN" and price > fire_price + atr * rev_mult)
            )

            if completed:
                print(f"[Target] '{t['label']}' REACHED — "
                      f"${price:,.0f} ≥ target ${price_target:,.0f} — disarming")
                t["fired"]      = False
                t["fired_at"]   = None
                t["fired_price"] = None
                t["active"]     = False   # disarm: won't re-fire unless manually re-enabled
                changed = True

            elif reversed_:
                print(f"[Target] '{t['label']}' REVERSED — "
                      f"${price:,.0f} pulled back >{rev_mult}×ATR from fire ${fire_price:,.0f} — clearing")
                t["fired"]       = False
                t["fired_at"]    = None
                t["fired_price"] = None
                changed = True

            else:
                # Still active — this is the target the engine should act on
                if active_target is None:
                    active_target = t

        else:
            # Not yet fired — check for trigger cross
            crossed = bool(
                (direction == "UP"   and price >= trigger) or
                (direction == "DOWN" and price <= trigger)
            )
            if crossed:
                print(f"[Target] FIRED: '{t['label']}' — "
                      f"${price:,.0f} crossed ${trigger:,.0f} ({direction})")
                t["fired"]       = True
                t["fired_at"]    = time.time()
                t["fired_price"] = price
                changed = True
                if active_target is None:
                    active_target = t

    if changed:
        save_targets(targets)

    return active_target


# ── CRUD ──────────────────────────────────────────────────────────────────────

def add_target(
    label: str,
    trigger_price: float,
    direction: str = "UP",
    price_target: float = None,
    reversal_atr_mult: float = 2.0,
    dca_enabled: bool = False,
    dca_base_order_usd: float = 500,
    dca_safety_count: int = 5,
    dca_safety_step_pct: float = 1.5,
    dca_safety_volume_mult: float = 1.2,
) -> dict:
    """Add a new price target. Returns the new target dict (with generated id)."""
    targets = load_targets()
    target = {
        "id":                     str(uuid.uuid4())[:8],
        "label":                  label,
        "trigger_price":          trigger_price,
        "direction":              direction,
        "price_target":           price_target,
        "reversal_atr_mult":      reversal_atr_mult,
        "active":                 True,
        "fired":                  False,
        "fired_at":               None,
        "fired_price":            None,
        "dca_enabled":            dca_enabled,
        "dca_base_order_usd":     dca_base_order_usd,
        "dca_safety_count":       dca_safety_count,
        "dca_safety_step_pct":    dca_safety_step_pct,
        "dca_safety_volume_mult": dca_safety_volume_mult,
        "dca_bot_id":             None,
    }
    targets.append(target)
    save_targets(targets)
    return target


def update_target(target_id: str, updates: dict) -> dict | None:
    """Patch fields on an existing target. Returns updated target or None."""
    targets = load_targets()
    for t in targets:
        if t.get("id") == target_id:
            immutable = {"id", "fired_at", "fired_price"}
            for k, v in updates.items():
                if k not in immutable:
                    t[k] = v
            save_targets(targets)
            return t
    return None


def delete_target(target_id: str) -> bool:
    targets = load_targets()
    new = [t for t in targets if t.get("id") != target_id]
    if len(new) == len(targets):
        return False
    save_targets(new)
    return True


def clear_target(target_id: str) -> bool:
    """Clear fired state (re-arms the target so it can fire again)."""
    targets = load_targets()
    for t in targets:
        if t.get("id") == target_id:
            t["fired"]       = False
            t["fired_at"]    = None
            t["fired_price"] = None
            save_targets(targets)
            return True
    return False
