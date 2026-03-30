"""
flash_move.py — Detect abnormally fast price moves (macro events, Trump posts, etc.)

Grid bots are designed for oscillation — they get destroyed by sudden directional
moves because they fill through the entire grid in one direction before the engine
can react. A flash move detector catches these within a single engine cycle.

DETECTION
---------
Two layers (either triggers):

1. CYCLE-TO-CYCLE: price moved > FLASH_ATR_MULT × ATR since last engine cycle
   (~2 min). At current ATR ~$565, 1.5× = $848 in 2 minutes. This is abnormal
   and almost always caused by a macro event (Trump post, Fed announcement, etc.)

2. CANDLE BODY: any of the last 3 five-minute candles has a body > CANDLE_ATR_MULT
   × 1H ATR. Catches flash moves that happened between cycles.

RESPONSE
--------
- Stop ALL bots immediately (capital protection)
- Log direction (UP/DOWN) and magnitude
- Enter cooldown for COOLDOWN_CYCLES engine cycles (~6 min default)
- During cooldown: bots stay off, engine logs countdown

RECOVERY
--------
After cooldown expires, check if price has stabilised:
- Last 3 five-minute candle bodies all < RECOVERY_ATR_MULT × ATR
- If stable: clear flash move state, let normal engine logic resume
- If still volatile: extend cooldown by 1 cycle

STATE
-----
Persisted in flash_move_state.json:
  last_price      — price at end of previous engine cycle
  active          — "UP" | "DOWN" | null
  fire_price      — price when flash move detected
  fired_at        — unix timestamp
  cooldown_remaining — cycles left in cooldown
  magnitude       — absolute $ move that triggered
"""

import os
import json
import time

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flash_move_state.json")

# ── Tuning constants ──────────────────────────────────────────────────────────
FLASH_ATR_MULT    = 1.5    # cycle-to-cycle move threshold (×ATR) — ~$848 at current ATR
                           # lowered from 2.0 for 2-min cycles (less time = smaller natural moves)
CANDLE_ATR_MULT   = 1.5    # single 5m candle body threshold (×1H ATR)
CANDLE_LOOKBACK   = 3      # check last N five-minute candles
WICK_ATR_MULT     = 2.0    # lower/upper wick threshold for sweep detection (×1H ATR)
                           # higher than body threshold — wicks are naturally larger than bodies.
                           # Catches liquidity sweeps where body is small but wick is large.
COOLDOWN_CYCLES   = 5      # minimum cycles with bots off (~10 min at 2-min cycles)
RECOVERY_ATR_MULT = 0.3    # all recent candle bodies must be < this × ATR to recover
RECOVERY_CANDLES  = 3      # number of candles that must be calm


def _load_state() -> dict:
    try:
        if os.path.exists(_STATE_FILE):
            return json.load(open(_STATE_FILE))
    except Exception:
        pass
    return {
        "last_price": None,
        "active": None,
        "fire_price": None,
        "fired_at": None,
        "cooldown_remaining": 0,
        "magnitude": 0,
    }


def _save_state(s: dict):
    try:
        json.dump(s, open(_STATE_FILE, "w"), indent=2)
    except Exception as e:
        print(f"Warning: could not save flash_move_state.json: {e}")


def detect_flash_move(current_price: float, atr: float, df_5m=None) -> dict:
    """
    Check for a flash move. Call once per engine cycle.

    Returns dict:
        status: "new"      — flash move just detected this cycle
                "active"   — still in cooldown from a previous flash move
                "clear"    — no flash move, normal operation
        direction: "UP" | "DOWN" | None
        magnitude: float (absolute $ move)
        cooldown_remaining: int (cycles left)
    """
    s = _load_state()
    prev_price = s.get("last_price")

    # Always update last_price for next cycle
    result = {"status": "clear", "direction": None, "magnitude": 0, "cooldown_remaining": 0}

    # ── Already in active flash move: manage cooldown ──────────────────────
    if s.get("active"):
        remaining = s.get("cooldown_remaining", 0)
        if remaining > 0:
            # Check if we can recover early (price stabilised)
            stable = True
            if df_5m is not None and len(df_5m) >= RECOVERY_CANDLES:
                for i in range(1, RECOVERY_CANDLES + 1):
                    body = abs(df_5m["close"].iloc[-i] - df_5m["open"].iloc[-i])
                    if body > atr * RECOVERY_ATR_MULT:
                        stable = False
                        break
            else:
                stable = False  # can't verify stability without 5m data

            remaining -= 1
            if remaining <= 0 and stable:
                # Cooldown expired and price is calm — clear
                print(f"FLASH_MOVE cleared — {s['active']} move of ${s.get('magnitude', 0):,.0f} "
                      f"has stabilised after cooldown")
                s.update({"active": None, "fire_price": None, "fired_at": None,
                          "cooldown_remaining": 0, "magnitude": 0, "last_price": current_price})
                _save_state(s)
                return result
            elif remaining <= 0 and not stable:
                # Cooldown expired but still volatile — extend
                remaining = 1
                print(f"FLASH_MOVE extending cooldown — price still volatile")

            s["cooldown_remaining"] = remaining
            s["last_price"] = current_price
            _save_state(s)
            return {
                "status": "active",
                "direction": s["active"],
                "magnitude": s.get("magnitude", 0),
                "cooldown_remaining": remaining,
            }

    # ── Check for new flash move ──────────────────────────────────────────
    triggered = False
    direction = None
    magnitude = 0

    # Layer 1: Cycle-to-cycle price change
    if prev_price is not None and atr > 0:
        move = current_price - prev_price
        abs_move = abs(move)
        threshold = atr * FLASH_ATR_MULT
        if abs_move > threshold:
            triggered = True
            direction = "UP" if move > 0 else "DOWN"
            magnitude = abs_move
            print(f"FLASH_MOVE detected (cycle-to-cycle) — "
                  f"${prev_price:,.0f} → ${current_price:,.0f} "
                  f"(${abs_move:,.0f} = {abs_move/atr:.1f}×ATR, threshold {FLASH_ATR_MULT}×ATR = ${threshold:,.0f})")

    # Layer 2: Large 5m candle body
    if not triggered and df_5m is not None and atr > 0:
        threshold = atr * CANDLE_ATR_MULT
        lookback = min(CANDLE_LOOKBACK, len(df_5m))
        for i in range(1, lookback + 1):
            body = df_5m["close"].iloc[-i] - df_5m["open"].iloc[-i]
            abs_body = abs(body)
            if abs_body > threshold:
                triggered = True
                direction = "UP" if body > 0 else "DOWN"
                magnitude = abs_body
                print(f"FLASH_MOVE detected (5m candle body) — "
                      f"${abs_body:,.0f} body = {abs_body/atr:.1f}×ATR "
                      f"(threshold {CANDLE_ATR_MULT}×ATR = ${threshold:,.0f})")
                break

    # Layer 3: Large wick (liquidity sweep detection)
    # Sweep candles have a large wick but small body — body detection misses them entirely.
    # Lower wick = min(open, close) - low  (tail below the body)
    # Upper wick = high - max(open, close) (tail above the body)
    if not triggered and df_5m is not None and atr > 0:
        wick_threshold = atr * WICK_ATR_MULT
        lookback = min(CANDLE_LOOKBACK, len(df_5m))
        for i in range(1, lookback + 1):
            high   = df_5m["high"].iloc[-i]
            low    = df_5m["low"].iloc[-i]
            close  = df_5m["close"].iloc[-i]
            open_  = df_5m["open"].iloc[-i]
            down_wick = min(open_, close) - low
            up_wick   = high - max(open_, close)
            if down_wick > wick_threshold:
                triggered = True
                direction = "DOWN"
                magnitude = down_wick
                print(f"FLASH_MOVE detected (sweep wick DOWN) — "
                      f"${down_wick:,.0f} lower wick = {down_wick/atr:.1f}×ATR "
                      f"(threshold {WICK_ATR_MULT}×ATR = ${wick_threshold:,.0f})")
                break
            elif up_wick > wick_threshold:
                triggered = True
                direction = "UP"
                magnitude = up_wick
                print(f"FLASH_MOVE detected (sweep wick UP) — "
                      f"${up_wick:,.0f} upper wick = {up_wick/atr:.1f}×ATR "
                      f"(threshold {WICK_ATR_MULT}×ATR = ${wick_threshold:,.0f})")
                break

    if triggered:
        s.update({
            "active": direction,
            "fire_price": current_price,
            "fired_at": time.time(),
            "cooldown_remaining": COOLDOWN_CYCLES,
            "magnitude": magnitude,
            "last_price": current_price,
        })
        _save_state(s)
        return {
            "status": "new",
            "direction": direction,
            "magnitude": magnitude,
            "cooldown_remaining": COOLDOWN_CYCLES,
        }

    # No flash move — update last_price for next cycle
    s["last_price"] = current_price
    _save_state(s)
    return result


def get_flash_move_state() -> dict:
    """Return current flash move state for logging/dashboard."""
    return _load_state()


def clear_flash_move():
    """Manually clear flash move state (e.g. from dashboard)."""
    _save_state({
        "last_price": None,
        "active": None,
        "fire_price": None,
        "fired_at": None,
        "cooldown_remaining": 0,
        "magnitude": 0,
    })
