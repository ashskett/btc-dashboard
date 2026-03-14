"""
price_targets.py — User-defined breakout trigger levels.

Lets you pre-set a price level that, when crossed, tells the engine
"I believe a big move is coming — keep the outer bot running and don't
let the automatic breakout detector stop it on a dip."

Trigger behaviour (tunable per target):
  confirm_closes      How many consecutive 1H closes above the trigger
                      before it fires. Default 2 — filters single-candle
                      wicks and brief spikes without meaningful delay.
                      Set to 1 for immediate fire on first close above.

  rearm_cooldown_h    After a reversal clears the target, how many hours
                      before it can fire again. Default 4. Prevents the
                      "price bounces around the level all day" thrash scenario
                      where the target fires and clears repeatedly.

Schema per target:
    {
        "id":                   "abc12345",
        "label":                "ATH retest → $82k",
        "trigger_price":        73000,
        "direction":            "UP",         # "UP" or "DOWN"
        "price_target":         82000,        # optional: where you expect the move to end
        "reversal_atr_mult":    2.0,          # clear if price reverses 2×ATR from fire price
        "confirm_closes":       2,            # consecutive closes needed before firing
        "rearm_cooldown_h":     4,            # hours before re-arm after reversal
        "active":               true,
        "fired":                false,
        "fired_at":             null,
        "fired_price":          null,
        "cleared_at":           null,         # set on reversal; used for cooldown guard
        "consec_above":         0,            # internal: running count of closes above trigger
        "dca_enabled":          false,
        "dca_base_order_usd":   500,
        "dca_safety_count":     5,
        "dca_safety_step_pct":  1.5,
        "dca_safety_volume_mult": 1.2,
        "dca_bot_id":           null
    }

Engine behaviour when a target fires (UP):
  - outer ON (wide range captures oscillations on the way up)
  - inner + mid OFF (too tight — will get filled on the wrong side of a fast move)
  - DCA bot launched if dca_enabled=true (accumulates on dips toward price_target)
  - Normal breakout detector bypassed (won't false-fire DOWN on a dip)
  - Drift detection bypassed (outer's 3×ATR range handles the move without redeployment)

Cleared automatically when:
  - price reaches price_target (target achieved) → active=false, won't re-fire
  - price reverses > reversal_atr_mult × ATR below fire price → cooldown before re-arm

Or manually via:
  POST /targets/<id>/clear  — re-arms immediately (overrides cooldown)
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
      - If not yet fired:
          Increment or reset consec_above counter based on whether
          price is above/below trigger.
          Fire once consec_above >= confirm_closes AND cooldown elapsed.
      - If already fired:
          Check for completion (price_target hit) or reversal (> rev_mult×ATR drop).
          On reversal: clear + record cleared_at for cooldown.

    Returns the first active+fired target, or None.
    """
    targets = load_targets()
    changed = False
    active_target = None

    for t in targets:
        if not t.get("active"):
            continue

        direction     = t.get("direction", "UP")
        trigger       = float(t.get("trigger_price", 0))
        fired         = t.get("fired", False)
        confirm_n     = int(t.get("confirm_closes", 2))
        cooldown_h    = float(t.get("rearm_cooldown_h", 4))
        consec_above  = int(t.get("consec_above", 0))

        if fired:
            # ── Already active: check for completion or reversal ──────────
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
                      f"${price:,.0f} hit target ${price_target:,.0f} — disarming")
                t.update({"fired": False, "fired_at": None, "fired_price": None,
                          "consec_above": 0, "active": False})
                changed = True

            elif reversed_:
                print(f"[Target] '{t['label']}' REVERSED — "
                      f"${price:,.0f} < fire ${fire_price:,.0f} − {rev_mult}×ATR "
                      f"— cooling down {cooldown_h:.0f}h before re-arm")
                t.update({"fired": False, "fired_at": None, "fired_price": None,
                          "consec_above": 0, "cleared_at": time.time()})
                changed = True

            else:
                if active_target is None:
                    active_target = t

        else:
            # ── Not fired: accumulate confirmation closes ─────────────────
            above = (direction == "UP" and price >= trigger) or \
                    (direction == "DOWN" and price <= trigger)

            if above:
                consec_above += 1
            else:
                if consec_above > 0:
                    consec_above = 0
                    changed = True

            if consec_above != int(t.get("consec_above", 0)):
                t["consec_above"] = consec_above
                changed = True

            # Check cooldown
            cleared_at = t.get("cleared_at")
            in_cooldown = bool(
                cleared_at and
                (time.time() - cleared_at) < cooldown_h * 3600
            )

            if consec_above >= confirm_n and not in_cooldown:
                cooldown_remaining = 0
                if cleared_at:
                    cooldown_remaining = max(0, cooldown_h * 3600 - (time.time() - cleared_at))

                print(f"[Target] FIRED: '{t['label']}' — "
                      f"{consec_above} closes {'above' if direction=='UP' else 'below'} "
                      f"${trigger:,.0f} (needed {confirm_n}) — price=${price:,.0f}")
                t.update({"fired": True, "fired_at": time.time(), "fired_price": price,
                          "consec_above": 0})
                changed = True
                if active_target is None:
                    active_target = t

            elif consec_above > 0 and confirm_n > 1:
                remaining = confirm_n - consec_above
                if in_cooldown:
                    cooldown_secs = cooldown_h * 3600 - (time.time() - cleared_at)
                    print(f"[Target] '{t['label']}' — {consec_above}/{confirm_n} closes "
                          f"above ${trigger:,.0f} (cooldown: {cooldown_secs/3600:.1f}h remaining)")
                else:
                    print(f"[Target] '{t['label']}' — {consec_above}/{confirm_n} closes "
                          f"above ${trigger:,.0f}, need {remaining} more")

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
    confirm_closes: int = 2,
    rearm_cooldown_h: float = 4.0,
    dca_enabled: bool = False,
    dca_base_order_usd: float = 500,
    dca_safety_count: int = 5,
    dca_safety_step_pct: float = 1.5,
    dca_safety_volume_mult: float = 1.2,
) -> dict:
    targets = load_targets()
    target = {
        "id":                     str(uuid.uuid4())[:8],
        "label":                  label,
        "trigger_price":          trigger_price,
        "direction":              direction,
        "price_target":           price_target,
        "reversal_atr_mult":      reversal_atr_mult,
        "confirm_closes":         confirm_closes,
        "rearm_cooldown_h":       rearm_cooldown_h,
        "active":                 True,
        "fired":                  False,
        "fired_at":               None,
        "fired_price":            None,
        "cleared_at":             None,
        "consec_above":           0,
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
    targets = load_targets()
    for t in targets:
        if t.get("id") == target_id:
            immutable = {"id", "fired_at", "fired_price", "cleared_at"}
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
    """Clear fired/cooldown state immediately — re-arms the target."""
    targets = load_targets()
    for t in targets:
        if t.get("id") == target_id:
            t.update({"fired": False, "fired_at": None, "fired_price": None,
                      "cleared_at": None, "consec_above": 0})
            save_targets(targets)
            return True
    return False
