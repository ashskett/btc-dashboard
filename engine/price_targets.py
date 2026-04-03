"""
price_targets.py — User-defined breakout + support-failure trigger levels.

TWO DETECTION MODES
===================

1. "breakout" (default / existing behaviour)
   direction UP  — fires when N consecutive 1H closes are above trigger
   direction DOWN — fires when N consecutive 1H closes are below trigger
   On fire: outer ON, inner+mid OFF; DCA bot launched if dca_enabled=true

2. "support_failure" (DOWN only)
   Four-phase state machine — designed to ignore liquidity sweeps (wicks
   that snap back) and only fire on a confirmed, retested break:

   WATCHING → BROKEN → RETESTING → fires SmartTrade
              ↑ N body-closes below support (close price, not low)
                        ↑ price bounces back to within retest_tolerance_pct of support
                                   ↑ closes below support again after the retest

   On fire: 3Commas SmartTrade (spot sell X% of BTC + TP + SL)
   Cancelled/reversed: price recovers above support + reversal_atr_mult×ATR

SCHEMA FIELDS
=============
  # Core (shared)
  id, label, trigger_price, direction, price_target
  reversal_atr_mult, confirm_closes, rearm_cooldown_h
  active, fired, fired_at, fired_price, cleared_at, consec_above

  # Mode selector
  detection_mode          "breakout" | "support_failure"

  # Support-failure state (support_failure mode only)
  sf_phase                "watching" | "broken" | "retesting"
  sf_retest_high          float — highest close seen since break (tracks retest)
  sf_broken_at            float — unix ts when phase became broken

  # Smart trade config (support_failure mode)
  smart_trade_enabled     bool
  smart_trade_sell_pct    float  — % of current BTC to sell (e.g. 25 = sell 25%)
  smart_trade_tp_pct      float  — take-profit below entry (e.g. 3.0 = 3% below)
  smart_trade_sl_pct      float  — stop-loss above entry (e.g. 1.5 = 1.5% above)
  smart_trade_id          str|null — active 3Commas SmartTrade ID once launched
  retest_tolerance_pct    float  — how close to level counts as a retest (default 0.5)

  # DCA config (breakout mode, UP direction)
  dca_enabled, dca_base_order_usd, dca_safety_count
  dca_safety_step_pct, dca_safety_volume_mult, dca_tp_steps, dca_bot_id
  dca_trailing_enabled, dca_trailing_deviation_pct
  dca_dual_entry             bool   — if True, split into scout (small/fast) + retest (larger/later)
  dca_scout_pct              float  — % of base_order for scout bot (e.g. 30 = 30%)
  dca_scout_buffer_cycles    int    — engine cycles to wait before scout fires (e.g. 2 = 10 min)
  dca_retest_tolerance_pct   float  — price must pull back to within this % of trigger for retest bot
  dca_scout_bot_id           str|null — 3Commas bot ID for scout
  dca_retest_bot_id          str|null — 3Commas bot ID for retest
  dca_stop_loss_pct          float|null — if set, engine calls panic_sell when price drops this %
                                below fired_price. Closes the deal, frees capital back to grid.
                                e.g. 3.0 = stop loss 3% below entry. null = no stop loss.
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


# ── Support-failure phase logic ────────────────────────────────────────────────

def _advance_support_failure(t: dict, close_price: float, high_price: float) -> bool:
    """
    Advance a support_failure target through its 4-phase state machine.
    Uses CLOSE prices only — wicks below support that snap back are ignored.

    Returns True if the target should fire this cycle.
    """
    trigger          = float(t["trigger_price"])
    confirm_n        = int(t.get("confirm_closes", 1))
    tolerance_pct    = float(t.get("retest_tolerance_pct", 0.5))
    tolerance_abs    = trigger * tolerance_pct / 100.0
    phase            = t.get("sf_phase", "watching")
    consec_below     = int(t.get("consec_above", 0))  # reused counter

    changed = False

    if phase == "watching":
        # Count consecutive body-closes below support
        if close_price < trigger:
            consec_below += 1
            t["consec_above"] = consec_below
            changed = True
            print(f"[SFail] '{t['label']}' — close ${close_price:,.0f} below "
                  f"${trigger:,.0f} ({consec_below}/{confirm_n})")
            if consec_below >= confirm_n:
                t["sf_phase"]     = "broken"
                t["sf_broken_at"] = time.time()
                t["sf_retest_high"] = close_price
                t["consec_above"] = 0
                print(f"[SFail] '{t['label']}' → BROKEN  (${close_price:,.0f})")
        else:
            if consec_below > 0:
                t["consec_above"] = 0
                changed = True

    elif phase == "broken":
        # Track highest close since breaking — watching for retest
        prev_high = float(t.get("sf_retest_high") or close_price)
        if close_price > prev_high:
            t["sf_retest_high"] = close_price
            changed = True

        retest_high = float(t.get("sf_retest_high", close_price))

        # Retest condition: price closed back up to within tolerance of support
        if retest_high >= trigger - tolerance_abs:
            t["sf_phase"] = "retesting"
            changed = True
            print(f"[SFail] '{t['label']}' → RETESTING  "
                  f"(high ${retest_high:,.0f} reached ${trigger:,.0f} ± ${tolerance_abs:,.0f})")

        # Price recovered fully above support without retesting properly → reset
        elif close_price > trigger:
            t["sf_phase"]       = "watching"
            t["consec_above"]   = 0
            t["sf_retest_high"] = None
            changed = True
            print(f"[SFail] '{t['label']}' → WATCHING (price recovered ${close_price:,.0f} > ${trigger:,.0f})")

    elif phase == "retesting":
        # If price closes below support again after the retest → FIRE
        if close_price < trigger:
            print(f"[SFail] '{t['label']}' CONFIRMED — failed retest, "
                  f"close ${close_price:,.0f} below ${trigger:,.0f} — FIRING")
            t["sf_phase"] = "watching"  # reset for next time
            t["sf_retest_high"] = None
            return True   # ← signal to caller: fire now

        # Price recovered back above support and is holding — break was fake
        elif close_price > trigger * 1.005:
            t["sf_phase"]       = "watching"
            t["consec_above"]   = 0
            t["sf_retest_high"] = None
            changed = True
            print(f"[SFail] '{t['label']}' → WATCHING "
                  f"(retest succeeded — support held at ${close_price:,.0f})")

    return False


# ── Core: check all targets against current price ─────────────────────────────

def check_targets(price: float, atr: float,
                  last_close: float = None, last_high: float = None) -> dict | None:
    """
    Check all active targets against current price.

    price      — current BTC price (used for reversal/completion checks)
    atr        — current ATR (used for reversal threshold)
    last_close — most recent 1H close (used for body-close confirmation)
    last_high  — most recent 1H high (used for retest detection)

    For each armed (active=true) target:
      breakout mode:
        - Count consecutive closes above/below trigger; fire on threshold
      support_failure mode (DOWN only):
        - 4-phase state machine; see _advance_support_failure()

    Returns the first active+fired target, or None.
    """
    # Fall back to price if close/high not supplied (keeps backward compat)
    close = last_close if last_close is not None else price
    high  = last_high  if last_high  is not None else price

    targets = load_targets()
    changed = False
    active_target = None

    for t in targets:
        if not t.get("active"):
            continue

        direction  = t.get("direction", "UP")
        trigger    = float(t.get("trigger_price", 0))
        fired      = t.get("fired", False)
        mode       = t.get("detection_mode", "breakout")
        confirm_n  = int(t.get("confirm_closes", 2))
        cooldown_h = float(t.get("rearm_cooldown_h", 4))

        # ── Already fired: check completion or reversal ────────────────────
        if fired:
            fire_price   = float(t.get("fired_price") or trigger)
            price_target = t.get("price_target")
            rev_mult     = float(t.get("reversal_atr_mult", 1.2))

            # Timeout: how long a fired target may remain active before auto-clearing.
            # Default 2h. Override per-target via "auto_clear_h" field.
            # Applies regardless of SmartTrade state — the SmartTrade may have
            # closed at 3Commas (TP/SL hit) without the engine polling it.
            # The engine's SmartTrade status poller in engine.py is the primary
            # completion path; this is a safety net for edge cases where polling
            # fails or a SmartTrade was never configured.
            _fired_ts        = t.get("fired_at") or 0
            _auto_clear_h    = float(t.get("auto_clear_h", 2.0))
            _auto_clear_secs = _auto_clear_h * 3600
            _timed_out       = bool(
                _fired_ts and (time.time() - _fired_ts) > _auto_clear_secs
            )

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
                          "consec_above": 0, "active": False,
                          "smart_trade_id": None})
                changed = True

            elif reversed_:
                print(f"[Target] '{t['label']}' REVERSED — "
                      f"${price:,.0f} > fire ${fire_price:,.0f} + {rev_mult}×ATR "
                      f"— cooling down {cooldown_h:.0f}h before re-arm")
                t.update({"fired": False, "fired_at": None, "fired_price": None,
                          "consec_above": 0, "cleared_at": time.time(),
                          "smart_trade_id": None})
                changed = True

            elif _timed_out:
                # Fired too long ago — auto-clear and re-arm with cooldown.
                # Engine's SmartTrade poller is the primary completion path;
                # this catches cases where polling failed or no SmartTrade was set.
                _age_h = (time.time() - _fired_ts) / 3600
                _st_id = t.get("smart_trade_id") or "none"
                print(f"[Target] '{t['label']}' TIMEOUT — "
                      f"fired {_age_h:.1f}h ago (limit {_auto_clear_h:.0f}h), "
                      f"auto-clearing (SmartTrade={_st_id})")
                t.update({"fired": False, "fired_at": None, "fired_price": None,
                          "consec_above": 0, "cleared_at": time.time(),
                          "smart_trade_id": None})
                changed = True

            else:
                if active_target is None:
                    active_target = t
            continue

        # ── Not fired: accumulate confirmation ────────────────────────────

        # Support-failure mode: dedicated state machine
        if mode == "support_failure" and direction == "DOWN":
            should_fire = _advance_support_failure(t, close, high)
            changed = True  # phase may have advanced

            if should_fire:
                # Check cooldown
                cleared_at = t.get("cleared_at")
                in_cooldown = bool(
                    cleared_at and
                    (time.time() - cleared_at) < cooldown_h * 3600
                )
                if not in_cooldown:
                    t.update({"fired": True, "fired_at": time.time(),
                              "fired_price": close, "consec_above": 0})
                    changed = True
                    print(f"[SFail] FIRED: '{t['label']}' @ ${close:,.0f}")
                    if active_target is None:
                        active_target = t
                else:
                    secs = cooldown_h * 3600 - (time.time() - cleared_at)
                    print(f"[SFail] '{t['label']}' would fire but in cooldown "
                          f"({secs/3600:.1f}h remaining)")
            continue

        # Breakout mode: original consecutive-close logic
        consec_above = int(t.get("consec_above", 0))
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

        cleared_at  = t.get("cleared_at")
        in_cooldown = bool(
            cleared_at and
            (time.time() - cleared_at) < cooldown_h * 3600
        )

        if consec_above >= confirm_n and not in_cooldown:
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
                      f"{'above' if direction=='UP' else 'below'} ${trigger:,.0f} "
                      f"(cooldown: {cooldown_secs/3600:.1f}h remaining)")
            else:
                print(f"[Target] '{t['label']}' — {consec_above}/{confirm_n} closes "
                      f"{'above' if direction=='UP' else 'below'} ${trigger:,.0f}, "
                      f"need {remaining} more")

    if changed:
        save_targets(targets)

    return active_target


# ── CRUD ──────────────────────────────────────────────────────────────────────

def add_target(
    label: str,
    trigger_price: float,
    direction: str = "UP",
    price_target: float = None,
    reversal_atr_mult: float = 1.2,
    confirm_closes: int = 2,
    rearm_cooldown_h: float = 4.0,
    detection_mode: str = "breakout",
    retest_tolerance_pct: float = 0.5,
    # DCA (breakout UP)
    dca_enabled: bool = False,
    dca_base_order_usd: float = 500,
    dca_safety_count: int = 5,
    dca_safety_step_pct: float = 1.5,
    dca_safety_volume_mult: float = 1.2,
    dca_tp_steps: list = None,
    dca_trailing_enabled: bool = False,
    dca_trailing_deviation_pct: float = 1.0,
    dca_dual_entry: bool = False,
    dca_scout_pct: float = 30.0,
    dca_scout_buffer_cycles: int = 5,
    dca_retest_tolerance_pct: float = 0.5,
    dca_stop_loss_pct: float = None,
    # SmartTrade (support_failure DOWN)
    smart_trade_enabled: bool = False,
    smart_trade_sell_pct: float = 25.0,
    smart_trade_tp_pct: float = 3.0,
    smart_trade_sl_pct: float = 1.5,
    smart_trade_tp_steps: list = None,
    smart_trade_dual_entry: bool = False,
    smart_trade_scout_pct: float = 30.0,
    smart_trade_retest_tolerance_pct: float = 0.5,
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
        # Mode
        "detection_mode":         detection_mode,
        "retest_tolerance_pct":   retest_tolerance_pct,
        # Support-failure state
        "sf_phase":               "watching",
        "sf_retest_high":         None,
        "sf_broken_at":           None,
        # DCA
        "dca_enabled":            dca_enabled,
        "dca_base_order_usd":     dca_base_order_usd,
        "dca_safety_count":       dca_safety_count,
        "dca_safety_step_pct":    dca_safety_step_pct,
        "dca_safety_volume_mult": dca_safety_volume_mult,
        "dca_tp_steps":           dca_tp_steps or [],
        "dca_trailing_enabled":   dca_trailing_enabled,
        "dca_trailing_deviation_pct": dca_trailing_deviation_pct,
        "dca_dual_entry":         dca_dual_entry,
        "dca_scout_pct":          dca_scout_pct,
        "dca_scout_buffer_cycles": dca_scout_buffer_cycles,
        "dca_retest_tolerance_pct": dca_retest_tolerance_pct,
        "dca_stop_loss_pct":      dca_stop_loss_pct,
        "dca_bot_id":             None,
        "dca_scout_bot_id":       None,
        "dca_retest_bot_id":      None,
        # SmartTrade
        "smart_trade_enabled":    smart_trade_enabled,
        "smart_trade_sell_pct":   smart_trade_sell_pct,
        "smart_trade_tp_pct":     smart_trade_tp_pct,
        "smart_trade_sl_pct":     smart_trade_sl_pct,
        "smart_trade_tp_steps":   smart_trade_tp_steps or [],
        "smart_trade_dual_entry": smart_trade_dual_entry,
        "smart_trade_scout_pct":  smart_trade_scout_pct,
        "smart_trade_retest_tolerance_pct": smart_trade_retest_tolerance_pct,
        "smart_trade_id":         None,
        "smart_trade_scout_id":   None,
        "smart_trade_retest_id":  None,
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
    """Clear fired/cooldown/phase state immediately — re-arms the target."""
    targets = load_targets()
    for t in targets:
        if t.get("id") == target_id:
            t.update({"fired": False, "fired_at": None, "fired_price": None,
                      "cleared_at": None, "consec_above": 0,
                      "sf_phase": "watching", "sf_retest_high": None,
                      "sf_broken_at": None, "smart_trade_id": None,
                      "smart_trade_scout_id": None, "smart_trade_retest_id": None,
                      "dca_scout_bot_id": None, "dca_retest_bot_id": None,
                      "dca_scout_cycles_active": 0, "dca_fail_count": 0,
                      "dca_last_attempt_ts": None})
            save_targets(targets)
            return True
    return False
