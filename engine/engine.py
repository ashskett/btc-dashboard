from inventory import calculate_inventory, portfolio_snapshot
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
    increment_active_cycles,
    breakout_inner_ready,
)
from session import get_session
from grid_logic import (
    get_grid_center,
    get_grid_state,
    update_grid_center,
    drift_detected,
    calculate_grid_parameters,
)
from dashboard import show_dashboard
from market_data import get_btc_data, get_btc_data_short
from indicators import add_indicators
from regime import detect_regime, trend_strength, compression_exit_fast
from threecommas import stop_bot, start_bot, redeploy_all_bots
from price_targets import check_targets, update_target
from threecommas_dca import (
    create_dca_bot,
    enable_dca_bot,
    disable_dca_bot,
    panic_sell_dca_bot,
    estimate_max_exposure,
)

# ── One-shot server fix (remove after first run) ──────────────────────────
# If fix_server.py is present (deployed by webhook), spawn it as a fully
# detached process, then remove it so it only ever runs once.  fix_server.py
# kills the old Flask process by port, copies the new webhook_server.py into
# place, restarts the webhook, and starts fresh Flask — all without needing SSH.
import subprocess as _subprocess, sys as _sys
_fix_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_server.py")
if os.path.exists(_fix_path):
    try:
        _subprocess.Popen(
            [_sys.executable, _fix_path, str(os.getpid())],
            start_new_session=True,
            close_fds=True,
        )
        os.remove(_fix_path)
        print("[engine] fix_server.py spawned — server restart in ~5s", flush=True)
    except Exception as _fe:
        print(f"[engine] fix_server.py spawn failed: {_fe}", flush=True)
# ── End one-shot fix ──────────────────────────────────────────────────────

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
_prev_trendline = None   # last accepted trendline value for spike detection

def get_active_trendline(current_price=None):
    """Read the currently active drawn trendline from trendlines.json.
    Returns the projected price level at the current time, or None if not set.

    Validation guards:
      - Rejects if the level is >20% away from current price (stale/corrupt line)
      - Rejects if the level jumped >25% vs the previous accepted value (spike)
    """
    global _prev_trendline
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

        # Guard 1: reject if >20% from current price (stale trendline from months ago)
        if current_price and abs(level - current_price) / current_price > 0.20:
            print(f"Warning: trendline {level:,.0f} is >20% from price {current_price:,.0f} "
                  f"— ignoring (possible stale/corrupt data)")
            return None

        # Guard 2: reject if spike vs previous accepted value (transient corrupt read)
        if _prev_trendline and abs(level - _prev_trendline) / _prev_trendline > 0.25:
            print(f"Warning: trendline jumped {level:,.0f} vs prev {_prev_trendline:,.0f} "
                  f"({abs(level-_prev_trendline)/_prev_trendline:.0%}) — ignoring spike")
            return _prev_trendline   # hold previous value

        _prev_trendline = level
        return round(level, 2)
    except Exception as e:
        print(f"Warning: could not read active trendline: {e}")
        return None
GRID_BOTS = [bot.strip() for bot in os.getenv("GRID_BOT_IDS", "").split(",") if bot.strip()]

MAX_BTC = 0.80   # hard stop — staggered above inventory.py UPPER_BAND (0.72)
MIN_BTC = 0.20   # hard stop — staggered below inventory.py LOWER_BAND (0.55)


_last_run_ts = 0
_prev_regime  = None   # regime from previous cycle — detects transitions for redeploy

def run():
    global _last_run_ts, _prev_regime
    now = time.time()
    if now - _last_run_ts < 240:
        print(f"Skipping — last cycle was {int(now - _last_run_ts)}s ago (min 240s between runs)")
        return
    _last_run_ts = now
    print("Checking market...")

    state = EngineState()
    _bo_state  = {}      # populated in breakout section; needed in finally block
    _pt_state  = None    # active price target (if any); needed in finally block
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
        TRENDLINE = get_active_trendline(current_price=state.price)
        _trendline_active = TRENDLINE is not None
        if TRENDLINE is None:
            print("Note: no active trendline set — using price as neutral fallback")
            TRENDLINE = state.price

        # ===============================
        # GRID CENTER / REGIME / SESSION
        # ===============================
        _saved_grid = get_grid_state()
        state.center = _saved_grid["grid_center"]
        # grid_width_at_deploy: locked to the mid-tier grid_width at the time
        # of the last redeploy. Used for drift detection so that a temporary
        # ATR dip can't narrow the threshold and cause a premature recentre.
        # Falls back to current state.grid_width once it's calculated below.
        state.deploy_grid_width = _saved_grid.get("grid_width_at_deploy")
        state.regime = detect_regime(df, TRENDLINE)
        state.session = get_session()

        # No-trendline override: when no real trendline is set the engine uses
        # price as a neutral fallback, giving gap_ratio=0 and no directional context.
        # Firing COMPRESSION (which stops inner+mid) with zero context is too aggressive —
        # we have no evidence the market is genuinely dead. Default to RANGE.
        if state.regime == "COMPRESSION" and not _trendline_active:
            print("No active trendline — overriding COMPRESSION to RANGE "
                  "(no directional context; draw a trendline to enable compression logic)")
            state.regime = "RANGE"

        # Weekend override: structural low volatility on Sat/Sun looks like COMPRESSION
        # to the BB/ATR indicators, but this is expected thin-market behaviour — not a
        # genuine dead market. COMPRESSION was designed to protect against the latter.
        # Overriding to RANGE keeps inner+mid running through normal weekend chop.
        if state.regime == "COMPRESSION" and state.session.startswith("WKD_"):
            print(f"Weekend session ({state.session}) — overriding COMPRESSION to RANGE "
                  f"(structural low vol, not a dead market)")
            state.regime = "RANGE"

        # Fast compression exit — if the 1H regime is COMPRESSION, fetch 5m candles
        # and check for momentum that the 1H indicators haven't yet detected.
        # BB width and ATR lag by 1-3 hours; 5m data catches the move in <5 minutes.
        if state.regime == "COMPRESSION":
            try:
                df_5m = get_btc_data_short(timeframe='5m', limit=30)
                if compression_exit_fast(df_5m, state.atr):
                    print("COMPRESSION fast-exit triggered by 5m momentum — overriding to RANGE")
                    state.regime = "RANGE"
            except Exception as _e:
                print(f"Warning: 5m fast-exit check failed: {_e} — staying in COMPRESSION")

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
        # Asymmetric inner-tier tilt when price is grinding up inside RANGE.
        # gap_ratio > 3.0 (trending_up) in RANGE = price well above trendline
        # but no confirmed regime break. Without tilt the inner grid is symmetric
        # and goes idle when price exits the upper boundary.
        # 0.12 shifts the inner grid up by 12% of its width (~$340 at current ATR).
        _trend_tilt = 0.12 if (state.regime == "RANGE" and state.trending_up) else 0.0
        grid = calculate_grid_parameters(
            state.price,
            state.atr,
            state.regime,
            state.session,
            state.skew,
            df,
            trend_tilt=_trend_tilt,
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

            # Track grid centre drift during active breakout.
            # Bots are NOT redeployed here (breakout still active), but keeping
            # the centre current means the eventual exhaustion/recovery redeploy
            # fires at the right level rather than one that may be several ATRs stale.
            _drift_gw = state.deploy_grid_width or state.grid_width
            if drift_detected(state.price, state.center, _drift_gw, tilt=state.tilt or 0):
                print(f"  Centre drift during {_active_dir} breakout — "
                      f"advancing centre ${state.center:,.0f} → ${state.price:,.0f} "
                      f"(bots held; no redeploy until breakout clears)")
                update_grid_center(state.price, grid_width=state.grid_width)
                state.center = state.price
                state.deploy_grid_width = state.grid_width

            if breakout_exhausting(df):
                print(f"BREAKOUT EXHAUSTING — momentum stalling at ${state.price:,.0f}  "
                      f"(moved ${_price_change:+,.0f} from fire price)")
                print("Triggering grid redeploy at new price level")

                if DRY_RUN:
                    update_grid_center(state.price, grid_width=state.grid_width)
                    print(f"[SIMULATION] Would redeploy grid centered at ${state.price:,.0f}")
                    for i, bot_id in enumerate(GRID_BOTS[:3]):
                        tier = state.tiers[i] if i < len(state.tiers) else state.tiers[-1]
                        print(f"  [SIM] Bot {bot_id} ({tier['name']}): "
                              f"${tier['grid_low']:,.0f}–${tier['grid_high']:,.0f}")
                elif _can_act():
                    _record_action()
                    redeploy_all_bots(GRID_BOTS, state.tiers)
                    update_grid_center(state.price, grid_width=state.grid_width)
                    clear_breakout_state()
                else:
                    print(f"Rate limit reached — skipping exhaustion redeploy")
                return   # redeploy done (or skipped) — don't fall through to bot-stop logic

            # During active UP breakout: inner+mid off, outer stays running.
            # After INNER_REENTRY_CYCLES cycles with momentum fading (price still
            # elevated), bring inner back online — mid stays off, outer stays on.
            # Full exhaustion still fires later and triggers the normal grid redeploy.
            # During active DOWN breakout: all bots off (capital protection).
            if _active_dir == "UP":
                increment_active_cycles()
                _cycles = _bo_state.get("cycles_active", 0) + 1  # +1 = value after increment
                _inner_ready = breakout_inner_ready(df)
                if _inner_ready:
                    print(f"BREAKOUT_UP active ({_cycles} cycles) — momentum fading, "
                          f"restarting inner bot (mid still off, outer running)")
                    for i, bot in enumerate(GRID_BOTS[:3]):
                        tier_name = ["inner", "mid", "outer"][i]
                        # inner (i=0) ON, mid (i=1) OFF, outer (i=2) ON
                        _act(bot, i != 1, f"{tier_name} (breakout UP, inner reentry)")
                else:
                    print(f"BREAKOUT_UP active ({_cycles} cycles) — inner+mid paused, outer running")
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

        # ===============================
        # PRICE TARGETS (user-defined trigger levels)
        # ===============================
        # Check before fresh breakout detection — if a target is active we skip
        # the auto-detector entirely (prevents a DOWN false-fire on a dip during
        # an expected upward move). Drift detection is also bypassed while a
        # target is active; the outer bot's 3×ATR range handles the move.
        _pt_state = check_targets(state.price, state.atr)
        if _pt_state:
            _pt_label  = _pt_state.get("label", "unnamed")
            _pt_dir    = _pt_state.get("direction", "UP")
            _pt_trig   = _pt_state.get("trigger_price", 0)
            _pt_tp     = _pt_state.get("price_target")
            _pt_fp     = _pt_state.get("fired_price", state.price)

            move_pct   = (_pt_fp - _pt_trig) / _pt_trig * 100 if _pt_trig else 0
            to_target  = ((_pt_tp - state.price) / state.price * 100) if _pt_tp else None

            print(f"[Target] ACTIVE: '{_pt_label}'  trigger=${_pt_trig:,.0f}  "
                  f"fire=${_pt_fp:,.0f}  now=${state.price:,.0f}"
                  + (f"  → target=${_pt_tp:,.0f} ({to_target:+.1f}%)" if _pt_tp else ""))

            if _pt_dir == "UP":
                print(f"  [Target] inner+mid off, outer running")
                for i, bot in enumerate(GRID_BOTS[:3]):
                    tier_name = ["inner", "mid", "outer"][i]
                    _act(bot, i >= 2, f"{tier_name} (target: {_pt_label})")

                # ── DCA bot launch ─────────────────────────────────────────
                # Sweep guard: hold DCA launch for DCA_LAUNCH_HOLD_SECS after the
                # target fires.  A liquidity sweep that triggers the level and
                # reverses within a few minutes will clear the target before the
                # hold expires, so the DCA bot is never launched on fake moves.
                DCA_LAUNCH_HOLD_SECS = 360   # 6 minutes
                _fired_at   = _pt_state.get("fired_at") or 0
                _hold_secs  = max(0, DCA_LAUNCH_HOLD_SECS - (time.time() - _fired_at))
                if _hold_secs > 0:
                    print(f"  DCA launch held — sweep guard active ({_hold_secs:.0f}s remaining)")

                # Only on the first cycle after firing (dca_bot_id not yet set).
                # DCA bot launches if either dca_tp_steps (multi-level TP) is set
                # or a price_target is set (single TP derived from the absolute $target).
                _tp_steps = _pt_state.get("dca_tp_steps") or []
                _has_tp   = bool(_tp_steps) or bool(_pt_tp)
                if _pt_state.get("dca_enabled") and not _pt_state.get("dca_bot_id") and _has_tp and _hold_secs == 0:
                    bo_usd   = float(_pt_state.get("dca_base_order_usd", 500))
                    so_usd   = round(bo_usd * 0.5, 2)
                    so_count = int(_pt_state.get("dca_safety_count", 5))
                    so_step  = float(_pt_state.get("dca_safety_step_pct", 1.5))
                    so_mult  = float(_pt_state.get("dca_safety_volume_mult", 1.2))
                    max_exp  = estimate_max_exposure(bo_usd, so_usd, so_count, so_mult)

                    # TP config: prefer explicit steps; fall back to single % from price_target
                    if _tp_steps:
                        tp_desc = " | ".join(f"{s['profit_pct']}%→close {s['close_pct']}%" for s in _tp_steps)
                    else:
                        tp_pct  = round((_pt_tp - state.price) / state.price * 100, 2)
                        tp_desc = f"{tp_pct:.1f}%"

                    if DRY_RUN:
                        print(f"  [SIM] Would create DCA bot '{_pt_label}' | "
                              f"base=${bo_usd} SO=${so_usd}×{so_count} "
                              f"step={so_step}% mult={so_mult}× | "
                              f"TP={tp_desc} | max_exposure=${max_exp:,.0f}")
                    elif _can_act():
                        _record_action()
                        try:
                            bot_data = create_dca_bot(
                                label=_pt_label,
                                base_order_usd=bo_usd,
                                safety_order_usd=so_usd,
                                take_profit_pct=tp_pct if not _tp_steps else 2.0,
                                take_profit_steps=_tp_steps if _tp_steps else None,
                                safety_order_count=so_count,
                                safety_order_step_pct=so_step,
                                safety_order_volume_mult=so_mult,
                            )
                            dca_id = str(bot_data.get("id", ""))
                            if dca_id:
                                enable_dca_bot(dca_id)
                                update_target(_pt_state["id"], {"dca_bot_id": dca_id})
                                print(f"  DCA bot launched: id={dca_id} "
                                      f"base=${bo_usd} TP={tp_pct:.1f}% max_exp=${max_exp:,.0f}")
                        except Exception as _dca_err:
                            print(f"  Warning: DCA bot launch failed: {_dca_err}")
                    else:
                        print(f"  Rate limit reached — DCA bot launch deferred to next cycle")

            else:  # DOWN target
                print(f"  [Target] all bots off (capital protection)")
                for bot in GRID_BOTS:
                    _act(bot, False, f"target DOWN: {_pt_label}")

            return   # skip fresh breakout detection AND drift while target is live

        # Fresh breakout detection
        _direction = breakout_detected(df, regime=state.regime, gap_ratio=state.gap_ratio)
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
        # Use the grid_width that was current when the bots were last deployed,
        # not the current ATR-derived width. This prevents a temporary ATR dip
        # from narrowing the threshold and triggering a premature recentre.
        _drift_gw = state.deploy_grid_width or state.grid_width
        print(f"  Drift check: deploy_gw=${_drift_gw:,.0f}  current_gw=${state.grid_width:,.0f}"
              f"  dist=${abs(state.price - (state.center + (state.tilt or 0))):,.0f}"
              f"  threshold=${_drift_gw * 0.85:,.0f}")
        if drift_detected(state.price, state.center, _drift_gw, tilt=state.tilt or 0):
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
                update_grid_center(state.price, grid_width=state.grid_width)
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
                update_grid_center(state.price, grid_width=state.grid_width)
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
        # State                   │ inner │  mid  │ outer │ Rationale
        # ────────────────────────┼───────┼───────┼───────┼──────────────────────────────────────
        # RANGE                   │  ON   │  ON   │  ON   │ Normal — all bots trade
        # TREND_UP                │  ON   │  ON   │  ON   │ Ride the move — grid profits on pullbacks
        # trending_up + TREND_UP  │  OFF  │  ON   │  ON   │ Price running hard — inner gets burned
        # TREND_DOWN              │  OFF  │  OFF  │  ON   │ Outer catches the bounce
        # trending_down           │  OFF  │  OFF  │  ON   │ Same — strong dump, wait with outer
        # COMPRESSION             │  OFF  │  OFF  │  ON   │ Outer wide enough for low-vol oscillations
        # Note: trending_up in RANGE regime = price above support, NOT a trend — all bots ON

        if state.regime == "COMPRESSION":
            print("COMPRESSION — inner+mid off, outer running (wide range catches low-vol oscillations)")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 2, tier_name)   # only outer (index 2) runs

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

        elif state.trending_up and state.regime != "RANGE":
            # Price running hard above trendline AND regime confirms directional move.
            # In RANGE regime, high gap_ratio just means price is sitting comfortably
            # above a support trendline — not actually trending. Inner should run there.
            print(f"TRENDING UP (gap={state.gap_ratio:.2f}×ATR, regime={state.regime}) — inner off, mid+outer running")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 1, tier_name)  # mid (index 1) and outer (index 2) run

        else:
            # RANGE or TREND_UP — all bots run
            if state.regime == "TREND_UP":
                print("TREND_UP — all bots running")
            elif state.compression:
                print("Mild compression — all bots running (compression not confirmed)")

            # Regime-change redeploy: if we're transitioning from a forced-stop regime
            # (TREND_DOWN / COMPRESSION), bots may be deployed at a stale price level
            # (or manually restarted at the wrong center).  Force a full redeploy at
            # current price on the first RANGE/TREND_UP cycle after the transition.
            _stopped_regimes = ("TREND_DOWN", "COMPRESSION")
            if _prev_regime in _stopped_regimes and not DRY_RUN:
                print(f"Regime transition {_prev_regime} → {state.regime} — "
                      f"forcing grid redeploy at ${state.price:,.0f} "
                      f"(bots may be at stale center from stopped period)")
                if _can_act():
                    _record_action()
                    redeploy_all_bots(GRID_BOTS, state.tiers)
                    update_grid_center(state.price, grid_width=state.grid_width)
                else:
                    print(f"  Rate limit reached — falling back to start_bot on regime transition")
                    for i, bot in enumerate(GRID_BOTS):
                        tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                        _act(bot, True, tier_name)
            elif _prev_regime in _stopped_regimes and DRY_RUN:
                print(f"[SIMULATION] Regime transition {_prev_regime} → {state.regime} — "
                      f"would force redeploy at ${state.price:,.0f}")
                for i, bot in enumerate(GRID_BOTS):
                    tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                    _act(bot, True, tier_name)
            else:
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
                "grid_width":          state.grid_width,
                "deploy_grid_width":   getattr(state, "deploy_grid_width", None),
                "center":         state.center,
                "trendline":      TRENDLINE if _trendline_active else None,
                "trendline_gap":  round(state.price - TRENDLINE, 2) if _trendline_active else None,
                "btc_ratio":      round(state.btc_ratio, 4) if state.btc_ratio is not None else None,
                "skew":           round(state.skew, 4) if state.skew is not None else None,
                "inventory_mode": state.inventory_mode,
                "compression":    bool(state.compression),
                "trending_up":    bool(getattr(state, "trending_up",   False)),
                "trending_down":  bool(getattr(state, "trending_down",  False)),
                "gap_ratio":      round(getattr(state, "gap_ratio", 0.0), 3),
                "dry_run":        DRY_RUN,
                "tiers":          state.tiers,
                # Breakout state
                "breakout_active":        _bo_state.get("active"),
                "breakout_fire_price":    _bo_state.get("fire_price"),
                "breakout_cycles_active": _bo_state.get("cycles_active", 0),
                "proximity_alert":     _prox,
                # Price target state
                "price_target_active":  bool(_pt_state),
                "price_target_label":   _pt_state.get("label")   if _pt_state else None,
                "price_target_trigger": _pt_state.get("trigger_price") if _pt_state else None,
                "price_target_tp":      _pt_state.get("price_target")  if _pt_state else None,
                "price_target_dca_id":  _pt_state.get("dca_bot_id")    if _pt_state else None,
            }
            write_status(log_data)
            write_log_entry(log_data)

            # ── Portfolio snapshot (balance-based P&L tracking) ──────────────
            # Appends one line to portfolio_log.jsonl each cycle.
            # Uses the raw balances already fetched by calculate_inventory() —
            # no extra API calls. This is the only accurate P&L source because
            # 3Commas bot P&L resets and orphans positions on every stop/start.
            snap = portfolio_snapshot()
            if snap:
                snap["dt"] = log_data.get("dt", "")
                snap["regime"] = log_data.get("regime", "")
                snap["bots_on"] = [t["name"] for t in log_data.get("tiers", [])
                                   if log_data.get(f"bot_{t['name']}_on")]
                _pf_log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "portfolio_log.jsonl")
                try:
                    with open(_pf_log, "a") as f:
                        f.write(json.dumps(snap) + "\n")
                except Exception as _e:
                    print(f"Warning: could not write portfolio_log.jsonl: {_e}")

        # Track regime for transition detection on next cycle
        if state.regime is not None:
            _prev_regime = state.regime

if __name__ == "__main__":
    schedule.every(5).minutes.do(run)

    # Run once immediately on startup
    run()

    print("Engine running...")

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nEngine stopped safely.")