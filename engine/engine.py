from inventory import calculate_inventory
from engine_state import EngineState
import os
import json
import time
import schedule
from status import write_status
from engine_log import write_log_entry
from dotenv import load_dotenv

from breakout import (
    breakout_detected,
    breakout_exhausting,
    proximity_alert,
    get_breakout_state,
    clear_breakout_state,
)
from session import get_session
from grid_logic import (
    get_grid_center,
    update_grid_center,
    drift_detected,
    calculate_grid_parameters,
)
from dashboard import show_dashboard
from market_data import get_btc_data
from indicators import add_indicators
from regime import detect_regime, trend_strength
from threecommas import stop_bot, start_bot, redeploy_all_bots

DRY_RUN = False
MAX_ACTIONS_PER_HOUR = 3

# Rate limiting — track bot actions in a rolling 1-hour window
from collections import deque
import datetime

_action_timestamps: deque = deque()


def _can_act() -> bool:
    """Return True if we are under the MAX_ACTIONS_PER_HOUR limit."""
    now = datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(hours=1)
    # Drop timestamps older than 1 hour
    while _action_timestamps and _action_timestamps[0] < cutoff:
        _action_timestamps.popleft()
    return len(_action_timestamps) < MAX_ACTIONS_PER_HOUR


def _record_action():
    """Record a bot action timestamp."""
    _action_timestamps.append(datetime.datetime.utcnow())

load_dotenv()

# Trendline is now driven by drawn trendlines — read active one at runtime
def get_active_trendline():
    """Read the currently active drawn trendline from trendlines.json.
    Returns the projected price level at the current time, or None if not set."""
    try:
        path = os.path.join(os.path.dirname(__file__), "trendlines.json")
        if not os.path.exists(path):
            return None
        trendlines = json.load(open(path))
        active = next((tl for tl in trendlines if tl.get("active")), None)
        if not active:
            return None
        t1, p1 = active["t1"], active["p1"]
        t2, p2 = active["t2"], active["p2"]
        dt = t2 - t1
        slope = 0 if dt == 0 else (p2 - p1) / dt
        now = time.time()
        level = p1 + slope * (now - t1)
        return round(level, 2)
    except Exception as e:
        print(f"Warning: could not read active trendline: {e}")
        return None
GRID_BOTS = [bot.strip() for bot in os.getenv("GRID_BOT_IDS", "").split(",") if bot.strip()]

MAX_BTC = 0.80   # hard stop — staggered above inventory.py UPPER_BAND (0.72)
MIN_BTC = 0.20   # hard stop — staggered below inventory.py LOWER_BAND (0.55)


_last_run_ts = 0

def run():
    global _last_run_ts
    now = time.time()
    if now - _last_run_ts < 240:
        print(f"Skipping — last cycle was {int(now - _last_run_ts)}s ago (min 240s between runs)")
        return
    _last_run_ts = now
    print("Checking market...")

    state = EngineState()
    _bo_state  = {}      # populated in breakout section; needed in finally block
    _prox      = None    # proximity alert direction; needed in finally block
    TRENDLINE  = None    # declared early so finally block can always reference it
    _trendline_active = False

    try:
        # ===============================
        # MARKET DATA
        # ===============================
        df = get_btc_data()
        df = add_indicators(df)

        state.price = df["close"].iloc[-1]
        state.atr = df["atr"].iloc[-1]
        state.volatility_ratio = state.atr / state.price

        # Get active trendline level (slope-projected to now)
        TRENDLINE = get_active_trendline()
        _trendline_active = TRENDLINE is not None
        if TRENDLINE is None:
            print("Note: no active trendline set — using price as neutral fallback")
            TRENDLINE = state.price

        # ===============================
        # GRID CENTER / REGIME / SESSION
        # ===============================
        state.center = get_grid_center()
        state.regime = detect_regime(df, TRENDLINE)
        state.session = get_session()

        # Trend strength — directional, asymmetric thresholds
        # trending_up (3×ATR) and trending_down (1.5×ATR) drive tiered bot decisions
        ts = trend_strength(state.price, TRENDLINE, state.atr)
        state.trending_up   = ts["trending_up"]
        state.trending_down = ts["trending_down"]
        state.gap_ratio     = ts["gap_ratio"]

        # ===============================
        # INVENTORY
        # ===============================
        # Fetches live BTC + quote balances from 3Commas account
        state.btc_ratio, state.skew = calculate_inventory()

        # Check for manual override from dashboard
        OVERRIDE_FILE = "inventory_override.json"
        override = {}
        if os.path.exists(OVERRIDE_FILE):
            try:
                override = json.load(open(OVERRIDE_FILE))
                if override.get("manual"):
                    state.btc_ratio = float(override["btc_ratio"])
                    state.skew      = float(override["skew"])
                    print(f"[OVERRIDE] Inventory: btc_ratio={state.btc_ratio:.2%}, skew={state.skew:+.4f}")
                if override.get("mode"):
                    state.inventory_mode = override["mode"]
            except Exception as e:
                print(f"Warning: could not read inventory override: {e}")

        if not override.get("mode"):
            if state.btc_ratio > MAX_BTC:
                state.inventory_mode = "SELL_ONLY"
            elif state.btc_ratio < MIN_BTC:
                state.inventory_mode = "BUY_ONLY"
            else:
                state.inventory_mode = "NORMAL"

        # ===============================
        # GRID PARAMETERS
        # ===============================
        grid = calculate_grid_parameters(
            state.price,
            state.atr,
            state.regime,
            state.session,
            state.skew,
            df
        )

        state.grid_width = grid["grid_width"]
        state.grid_low = grid["grid_low"]
        state.grid_high = grid["grid_high"]
        state.levels = grid["levels"]
        state.step = grid["step"]
        state.compression = grid["compression"]
        state.tilt = grid.get("tilt")
        state.grid_levels = grid.get("grid_levels")
        state.support = grid.get("support")
        state.resistance = grid.get("resistance")
        state.tiers = grid.get("tiers", [])  # [inner, mid, outer]

        # Log which tier each bot is assigned to
        for i, bot_id in enumerate(GRID_BOTS[:3]):
            tier = state.tiers[i] if i < len(state.tiers) else state.tiers[-1]
            print(f"  Bot {bot_id} → {tier['name']} tier | "
                  f"range ${tier['grid_low']:,.0f}–${tier['grid_high']:,.0f} | "
                  f"{tier['levels']} levels @ ${tier['step']:,.0f} step")

        # ===============================
        # DASHBOARD
        # ===============================
        show_dashboard(
            state.price,
            state.atr,
            state.regime,
            state.grid_width,
            TRENDLINE,
            state.center,
            state.session,
            state.btc_ratio,
            state.skew,
            state.inventory_mode,
            state.compression
        )

        # Helper: start or stop a bot (respects DRY_RUN)
        # Defined here so it's available to the breakout block AND tiered decisions below
        def _act(bot_id, should_run, label):
            if DRY_RUN:
                action = "start" if should_run else "stop"
                print(f"[SIMULATION] Would {action} bot {bot_id} ({label})")
            else:
                if should_run:
                    start_bot(bot_id)
                else:
                    stop_bot(bot_id)

        # ===============================
        # BREAKOUT DETECTION
        # ===============================
        # Proximity warning — price approaching outer grid edge
        # Does not stop bots, just logs a heads-up so the outer range can be widened
        # on the next drift/redeploy cycle
        _bo_state = get_breakout_state()
        _outer_tier = state.tiers[-1] if state.tiers else {}
        _outer_low  = _outer_tier.get("grid_low", 0)
        _outer_high = _outer_tier.get("grid_high", 0)

        _prox = proximity_alert(df, _outer_low, _outer_high)
        if _prox:
            print(f"PROXIMITY ALERT — price approaching outer grid edge ({_prox})")

        # If already in an active breakout, check for exhaustion
        if _bo_state.get("active") in ("UP", "DOWN"):
            _active_dir   = _bo_state["active"]
            _fire_price   = _bo_state.get("fire_price", state.price)
            _price_change = state.price - _fire_price
            print(f"BREAKOUT ACTIVE ({_active_dir}) — fire=${_fire_price:,.0f}  "
                  f"current=${state.price:,.0f}  Δ=${_price_change:+,.0f}")

            if breakout_exhausting(df):
                print(f"BREAKOUT EXHAUSTING — momentum stalling at ${state.price:,.0f}  "
                      f"(moved ${_price_change:+,.0f} from fire price)")
                print("Triggering grid redeploy at new price level")

                if DRY_RUN:
                    update_grid_center(state.price)
                    print(f"[SIMULATION] Would redeploy grid centered at ${state.price:,.0f}")
                    for i, bot_id in enumerate(GRID_BOTS[:3]):
                        tier = state.tiers[i] if i < len(state.tiers) else state.tiers[-1]
                        print(f"  [SIM] Bot {bot_id} ({tier['name']}): "
                              f"${tier['grid_low']:,.0f}–${tier['grid_high']:,.0f}")
                elif _can_act():
                    _record_action()
                    redeploy_all_bots(GRID_BOTS, state.tiers)
                    update_grid_center(state.price)
                    clear_breakout_state()
                else:
                    print(f"Rate limit reached — skipping exhaustion redeploy")

            # During active UP breakout: inner+mid off, outer stays running
            # During active DOWN breakout: all bots off (already stopped at fire)
            if _active_dir == "UP":
                print(f"BREAKOUT_UP active — inner+mid paused, outer running")
                for i, bot in enumerate(GRID_BOTS[:3]):
                    tier_name = ["inner", "mid", "outer"][i]
                    _act(bot, i >= 2, f"{tier_name} (breakout UP)")
            else:
                print(f"BREAKOUT_DOWN active — all bots off (capital protection)")
                for bot in GRID_BOTS:
                    if DRY_RUN:
                        print(f"[SIMULATION] Would keep bot {bot} stopped")
                    else:
                        stop_bot(bot)
            return

        # Fresh breakout detection
        _direction = breakout_detected(df)
        if _direction:
            print(f"BREAKOUT DETECTED — direction: {_direction}")

            if _direction == "UP":
                # Upside breakout: inner+mid off, outer keeps running to capture oscillations
                # on the trend. Grid will redeploy at new level once exhaustion fires.
                print("BREAKOUT UP — stopping inner+mid, keeping outer running")
                if DRY_RUN:
                    print("[SIMULATION] Would stop inner bot (too tight for the move)")
                    print("[SIMULATION] Would stop mid bot")
                    print("[SIMULATION] Outer bot stays running")
                elif _can_act():
                    _record_action()
                    if len(GRID_BOTS) >= 1:
                        stop_bot(GRID_BOTS[0])   # inner
                    if len(GRID_BOTS) >= 2:
                        stop_bot(GRID_BOTS[1])   # mid
                    # outer (GRID_BOTS[2]) intentionally left running
                else:
                    print(f"Rate limit reached — breakout UP bot stops skipped")

            else:
                # Downside breakout: stop everything — capital protection
                print("BREAKOUT DOWN — stopping all bots (capital protection)")
                if DRY_RUN:
                    print("[SIMULATION] Would stop all grid bots")
                elif _can_act():
                    _record_action()
                    for bot in GRID_BOTS:
                        stop_bot(bot)
                else:
                    print(f"Rate limit reached — breakout DOWN bot stops skipped")

            return

        # ===============================
        # GRID DRIFT / REDEPLOYMENT
        # ===============================
        if drift_detected(state.price, state.center, state.grid_width, tilt=state.tilt or 0):
            state.drift_triggered = True
            print("Grid drift detected")
            print("New Grid Parameters")
            print("Center:", state.price)
            print("Low:", state.grid_low)
            print("High:", state.grid_high)
            print("Levels:", state.levels)
            print("Step:", state.step)
            print("Tilt:", state.tilt)
            print("Support:", state.support)
            print("Resistance:", state.resistance)

            if DRY_RUN:
                # In dry run: update center so simulation doesn't re-trigger drift every cycle
                update_grid_center(state.price)
                print("[SIMULATION] Would redeploy grid bots with tiered ranges:")
                for i, bot_id in enumerate(GRID_BOTS[:3]):
                    tier = state.tiers[i] if i < len(state.tiers) else state.tiers[-1]
                    print(f"  [SIM] Bot {bot_id} ({tier['name']}): "
                          f"${tier['grid_low']:,.0f}–${tier['grid_high']:,.0f}, "
                          f"{tier['levels']} levels, ${tier['step']:,.0f} step")
            elif _can_act():
                _record_action()
                # Stop, reprice each bot to its tier range, restart
                redeploy_all_bots(GRID_BOTS, state.tiers)
                # Only advance center AFTER bots successfully redeployed
                update_grid_center(state.price)
            else:
                print(f"Rate limit reached ({MAX_ACTIONS_PER_HOUR}/hr) — skipping drift redeploy")
                print(f"  Bots remain on current ranges — center NOT advanced")

            return

        # ===============================
        # INVENTORY PROTECTION
        # ===============================
        if state.inventory_mode == "SELL_ONLY":
            print("Inventory protection: SELL ONLY")

            for bot in GRID_BOTS:
                if DRY_RUN:
                    print(f"[SIMULATION] Sell-only mode bot {bot}")
                else:
                    stop_bot(bot)

            return

        if state.inventory_mode == "BUY_ONLY":
            print("Inventory protection: BUY ONLY")

            for bot in GRID_BOTS:
                if DRY_RUN:
                    print(f"[SIMULATION] Buy-only mode bot {bot}")
                else:
                    stop_bot(bot)

            return

        # ===============================
        # TIERED BOT DECISIONS
        # ===============================
        # Philosophy: bots stay running unless there is strong confirmed evidence
        # they are fighting the market. The outer bot (Bot 3) acts as a permanent
        # safety net and only shuts down in full compression (no volatility = no profit).
        #
        # Tier mapping: GRID_BOTS[0]=inner  GRID_BOTS[1]=mid  GRID_BOTS[2]=outer
        #
        # State          │ inner │  mid  │ outer │ Rationale
        # ───────────────┼───────┼───────┼───────┼─────────────────────────────────
        # RANGE          │  ON   │  ON   │  ON   │ Normal — all bots trade
        # TREND_UP       │  ON   │  ON   │  ON   │ Ride the move — grid profits on pullbacks
        # trending_up    │  OFF  │  ON   │  ON   │ Price running — inner gets burned through
        # TREND_DOWN     │  OFF  │  OFF  │  ON   │ Outer catches the bounce
        # trending_down  │  OFF  │  OFF  │  ON   │ Same — strong dump, wait with outer
        # COMPRESSION    │  OFF  │  OFF  │  OFF  │ No vol = no profit for any tier

        if state.regime == "COMPRESSION":
            print("COMPRESSION — all bots off (no volatility)")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, False, tier_name)

        elif state.trending_down:
            # Strong downside move — inner and mid OFF, outer ON as safety net
            print(f"TRENDING DOWN (gap={state.gap_ratio:.2f}×ATR) — inner+mid off, outer holding")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 2, tier_name)  # only outer (index 2) runs

        elif state.regime == "TREND_DOWN":
            # Confirmed TREND_DOWN (hysteresis-filtered) — same as trending_down
            print(f"TREND_DOWN — inner+mid off, outer holding")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 2, tier_name)

        elif state.trending_up:
            # Price running hard above trendline — inner too tight, mid+outer ride it
            print(f"TRENDING UP (gap={state.gap_ratio:.2f}×ATR) — inner off, mid+outer running")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 1, tier_name)  # mid (index 1) and outer (index 2) run

        else:
            # RANGE or TREND_UP — all bots run
            if state.regime == "TREND_UP":
                print("TREND_UP — all bots running")
            elif state.compression:
                print("Mild compression — all bots running (compression not confirmed)")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, True, tier_name)

    finally:
        # ===============================
        # DASHBOARD STATUS EXPORT
        # Always runs — even on early return or exception
        # ===============================
        if state.price is not None:
            log_data = {
                "price":          state.price,
                "atr":            round(state.atr, 2) if state.atr else None,
                "regime":         state.regime,
                "drift_triggered": bool(getattr(state, "drift_triggered", False)),
                "session":        state.session,
                "grid_low":       state.grid_low,
                "grid_high":      state.grid_high,
                "grid_width":     state.grid_width,
                "center":         state.center,
                "trendline":      TRENDLINE if _trendline_active else None,
                "trendline_gap":  round(state.price - TRENDLINE, 2) if _trendline_active else None,
                "btc_ratio":      round(state.btc_ratio, 4) if state.btc_ratio else None,
                "skew":           round(state.skew, 4) if state.skew else None,
                "inventory_mode": state.inventory_mode,
                "compression":    bool(state.compression),
                "trending_up":    bool(getattr(state, "trending_up",   False)),
                "trending_down":  bool(getattr(state, "trending_down",  False)),
                "gap_ratio":      round(getattr(state, "gap_ratio", 0.0), 3),
                "dry_run":        DRY_RUN,
                "tiers":          state.tiers,
                # Breakout state
                "breakout_active":     _bo_state.get("active"),
                "breakout_fire_price": _bo_state.get("fire_price"),
                "proximity_alert":     _prox,
            }
            write_status(log_data)
            write_log_entry(log_data)

schedule.every(5).minutes.do(run)

# Run once immediately
run()

print("Engine running...")

try:
    while True:
        schedule.run_pending()
        time.sleep(1)
except KeyboardInterrupt:
    print("\nEngine stopped safely.")