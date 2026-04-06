from inventory import calculate_inventory, portfolio_snapshot, get_inventory_settings
from engine_state import EngineState
import os
import json
import time
import schedule
from status import write_status
from engine_log import write_log_entry
from dotenv import load_dotenv
from notify import notify, notify_critical
from flash_move import detect_flash_move, get_flash_move_state, clear_flash_move

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
    redeploy_allowed,
)
from dashboard import show_dashboard
from market_data import get_btc_data, get_btc_data_short
from indicators import add_indicators
from regime import detect_regime, trend_strength, compression_exit_fast, get_regime_state
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

# MAX_BTC / MIN_BTC are no longer hardcoded here — they are read dynamically
# from inventory_settings.json via get_inventory_settings() each cycle so that
# dashboard changes take effect immediately without an engine restart.

_BOT_OVERRIDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_overrides.json")

def _load_bot_overrides() -> dict:
    """
    Load manual bot overrides from bot_overrides.json.
    Returns dict of {bot_id_str: "stopped"} for each manually locked-off bot.
    Returns {} if file doesn't exist or is unreadable.
    """
    try:
        if os.path.exists(_BOT_OVERRIDES_FILE):
            with open(_BOT_OVERRIDES_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _make_intensive_buy_tiers(price: float, tiers: list) -> list:
    """
    Build buy-biased tier parameters for BUY_ONLY mode.

    Shifts each tier's range entirely below current price so the bot's initial
    orders are buys only (no sell orders sit above price at deployment time).
    Range is compressed to 60% of normal width to create a denser buy cluster.

        grid_high = price × 0.9995  (fractional buffer — avoids placing orders
                                     right on the live price spread)
        grid_low  = grid_high − (original_width × 0.60)

    Levels and step are recalculated proportionally.
    """
    import copy as _copy
    result = []
    for tier in tiers:
        t = _copy.deepcopy(tier)
        orig_width = float(t.get("grid_high", price + 1000)) - float(t.get("grid_low", price - 1000))
        new_width  = round(orig_width * 0.60, 2)
        new_high   = round(price * 0.9995, 2)
        new_low    = round(new_high - new_width, 2)
        n          = max(int(t.get("levels", 5)), 2)
        new_step   = round(new_width / (n - 1), 2)
        t["grid_high"]   = new_high
        t["grid_low"]    = new_low
        t["step"]        = new_step
        t["grid_levels"] = [round(new_low + i * new_step, 2) for i in range(n)]
        result.append(t)
    return result


def _make_intensive_sell_tiers(price: float, tiers: list) -> list:
    """
    Build sell-biased tier parameters for SELL_ONLY mode.

    Shifts each tier's range entirely above current price so the bot's initial
    orders are sells only (no buy orders sit below price at deployment time).
    Range is compressed to 60% of normal width to create a denser sell cluster.

        grid_low  = price × 1.0005  (fractional buffer — avoids placing orders
                                     right on the live price spread)
        grid_high = grid_low + (original_width × 0.60)

    As price rises into the range, sell orders fill and BTC converts to USDC.
    If price falls further, orders remain unexecuted — no forced selling at a loss.
    Levels and step are recalculated proportionally.
    """
    import copy as _copy
    result = []
    for tier in tiers:
        t = _copy.deepcopy(tier)
        orig_width = float(t.get("grid_high", price + 1000)) - float(t.get("grid_low", price - 1000))
        new_width  = round(orig_width * 0.60, 2)
        new_low    = round(price * 1.0005, 2)
        new_high   = round(new_low + new_width, 2)
        n          = max(int(t.get("levels", 5)), 2)
        new_step   = round(new_width / (n - 1), 2)
        t["grid_high"]   = new_high
        t["grid_low"]    = new_low
        t["step"]        = new_step
        t["grid_levels"] = [round(new_low + i * new_step, 2) for i in range(n)]
        result.append(t)
    return result


_TL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trendlines.json")

def _try_auto_activate_trendline(td_last_low: float, current_price: float,
                                  atr: float) -> dict | None:
    """
    After a TREND_DOWN auto-clear, check whether the episode low landed on one
    of the user's inactive drawn trendlines.  If it did, and current price is
    above that trendline (so activating it won't immediately re-trigger
    TREND_DOWN), swap it in as the active trendline.

    Match condition: abs(td_last_low − trendline_projected_at_now) ≤ 1.0 × ATR
    Safety condition: current_price > trendline_projected_at_now

    Returns the activated trendline dict on success, None otherwise.
    """
    try:
        if not os.path.exists(_TL_PATH):
            return None
        trendlines = json.load(open(_TL_PATH))
        now = time.time()

        def _project(tl):
            t1, p1, t2, p2 = tl["t1"], tl["p1"], tl["t2"], tl["p2"]
            dt = t2 - t1
            slope = 0 if dt == 0 else (p2 - p1) / dt
            return p1 + slope * (now - t1)

        threshold = atr * 1.0   # within 1×ATR of the trendline = "landed on it"
        best_match, best_dist = None, float("inf")

        for tl in trendlines:
            if tl.get("active"):
                continue   # skip the currently active one
            projected = _project(tl)
            dist = abs(td_last_low - projected)
            if dist <= threshold and dist < best_dist:
                # Safety: current price must be above the trendline at this moment
                if current_price > projected:
                    best_match = tl
                    best_dist  = dist

        if best_match is None:
            return None

        # Swap active flag: deactivate all, activate the match
        _projected_match = _project(best_match)
        for tl in trendlines:
            tl["active"] = (tl["id"] == best_match["id"])
        with open(_TL_PATH, "w") as f:
            json.dump(trendlines, f)

        print(f"  Trendline auto-activated: '{best_match.get('label','?')}' "
              f"(projected ${_projected_match:,.0f}) matched td_low "
              f"${td_last_low:,.0f} (dist ${best_dist:,.0f}, threshold ${threshold:,.0f})")
        # Clear td_last_low so this doesn't fire again on the next recovery
        try:
            rs = json.load(open(os.path.join(os.path.dirname(
                os.path.abspath(__file__)), "regime_state.json")))
            rs["td_last_low"] = None
            json.dump(rs, open(os.path.join(os.path.dirname(
                os.path.abspath(__file__)), "regime_state.json"), "w"))
        except Exception:
            pass
        return best_match

    except Exception as _e:
        print(f"  Warning: _try_auto_activate_trendline failed: {_e}")
        return None


def _is_weekend_grid_hours() -> bool:
    """
    Return True during the weekend low-volatility window:
        Fri ≥ 21:00 UTC  (NYSE/NASDAQ close)
        Sat & Sun all day
        Mon < 07:00 UTC  (before EU open)
    """
    import datetime as _dt
    now = _dt.datetime.utcnow()
    wd  = now.weekday()   # Mon=0 … Fri=4, Sat=5, Sun=6
    if wd == 4 and now.hour >= 21:  return True   # Friday after US close
    if wd in (5, 6):                return True   # Saturday / Sunday
    if wd == 0 and now.hour < 7:    return True   # Monday before EU open
    return False


def _price_near_level(price: float, tiers: list) -> tuple:
    """
    Check whether price is within 20% of a step of any DEPLOYED grid level.
    Returns (is_near: bool, nearest_level: float|None, threshold: float).

    IMPORTANT: must be called with deployed_tiers (from grid_state.json), NOT
    state.tiers (freshly calculated each cycle centred on current price).
    state.tiers always has a level at exactly current price → guard would
    always fire → weekend redeploy permanently deferred.

    deployed_tiers strips the 'step' field on save, so derive step from
    grid_levels spacing or from (grid_high - grid_low) / (levels - 1).
    """
    if not tiers:
        # No deployed tiers on record — nothing to protect, allow deploy
        return False, None, 0.0

    steps = []
    for t in tiers:
        lvls = t.get("grid_levels", [])
        if len(lvls) >= 2:
            # Derive step from actual level spacing
            steps.append(abs(float(lvls[1]) - float(lvls[0])))
        elif t.get("step"):
            steps.append(float(t["step"]))
        elif t.get("grid_high") and t.get("grid_low") and t.get("levels", 0) > 1:
            steps.append((float(t["grid_high"]) - float(t["grid_low"])) / (int(t["levels"]) - 1))

    if not steps:
        return False, None, 0.0

    min_step  = min(steps)
    threshold = min_step * 0.20   # 20% of tightest step (~$87 at current params)

    nearest_level, nearest_dist = None, float("inf")
    for tier in tiers:
        for lvl in tier.get("grid_levels", []):
            dist = abs(price - float(lvl))
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_level = float(lvl)

    if nearest_level is None:
        return False, None, threshold
    return nearest_dist <= threshold, nearest_level, threshold


def _make_weekend_tiers(price: float, tiers: list) -> list:
    """
    Build tighter tier parameters for the weekend low-volatility window.

    Width is compressed to 65% of the normal ATR-derived width, centred
    symmetrically on current price.  Level count is also reduced by the
    same 0.65 factor (minimum 3) so that the step size — and therefore
    the P&L per fill — stays roughly equal to normal trading days.

    Keeping the same level count with a narrower grid shrinks the step and
    therefore the fill size proportionally (observed: ~£5 vs ~£8 normal).
    Reducing levels compensates: the grid is still tighter/more concentrated
    but fills remain meaningful.

    Example (inner tier, 10 normal levels, W = normal width):
        Normal:  step = W/9
        Weekend: step = 0.65W/6  ≈  W/9.2  → virtually identical fill size

        grid_low  = price − (original_width × 0.65 / 2)
        grid_high = price + (original_width × 0.65 / 2)
        levels    = max(round(original_levels × 0.65), 3)
    """
    import copy as _copy
    result = []
    for tier in tiers:
        t = _copy.deepcopy(tier)
        orig_width = float(t.get("grid_high", price + 1000)) - float(t.get("grid_low", price - 1000))
        new_width  = round(orig_width * 0.65, 2)
        new_low    = round(price - new_width / 2, 2)
        new_high   = round(price + new_width / 2, 2)
        n_orig     = max(int(t.get("levels", 5)), 2)
        n          = max(round(n_orig * 0.65), 3)   # reduce proportionally, floor 3
        new_step   = round(new_width / (n - 1), 2)
        t["grid_high"]   = new_high
        t["grid_low"]    = new_low
        t["levels"]      = n
        t["step"]        = new_step
        t["grid_levels"] = [round(new_low + i * new_step, 2) for i in range(n)]
        result.append(t)
    return result


_last_run_ts = 0
_prev_regime: str | None = None
_prev_trending_down: bool = False
_prev_inventory_mode: str | None = None
_prev_weekend_mode: bool = False

def run():
    global _last_run_ts, _prev_regime, _prev_trending_down, _prev_inventory_mode, _prev_weekend_mode
    now = time.time()
    if now - _last_run_ts < 100:
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

        # ===============================
        # FLASH MOVE DETECTION
        # ===============================
        _flash = detect_flash_move(state.price, state.atr)
        if _flash["status"] == "new":
            print(f"FLASH MOVE {_flash['direction']} — ${_flash['magnitude']:,.0f} move "
                  f"({_flash['magnitude']/state.atr:.1f}×ATR) — stopping all bots, "
                  f"cooldown {_flash['cooldown_remaining']} cycles")
            notify_critical(f"Flash move {_flash['direction']} — ${_flash['magnitude']:,.0f} "
                            f"({_flash['magnitude']/state.atr:.1f}×ATR) — all bots stopped")
            if not DRY_RUN:
                for bot in GRID_BOTS:
                    stop_bot(bot)
            return

        if _flash["status"] == "active":
            print(f"FLASH_MOVE cooldown ({_flash['direction']}) — "
                  f"{_flash['cooldown_remaining']} cycles remaining — bots held off")
            if not DRY_RUN:
                for bot in GRID_BOTS:
                    stop_bot(bot)
            return

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
        if state.regime == "COMPRESSION" and (state.session == "WKD" or state.session.startswith("WKD_")):
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
            # Read min_btc/max_btc dynamically so dashboard changes take effect
            # immediately without an engine restart.
            _inv_s  = get_inventory_settings()
            _min_btc = _inv_s["min_btc"]
            _max_btc = _inv_s["max_btc"]
            if state.btc_ratio > _max_btc:
                state.inventory_mode = "SELL_ONLY"
            elif state.btc_ratio < _min_btc:
                state.inventory_mode = "BUY_ONLY"
            else:
                state.inventory_mode = "NORMAL"
            print(f"Inventory hard stops: min_btc={_min_btc:.0%}, max_btc={_max_btc:.0%} "
                  f"(current ratio {state.btc_ratio:.0%} → {state.inventory_mode})")

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

        # Helper: start or stop a bot (respects DRY_RUN and manual overrides)
        # Defined here so it's available to the breakout block AND tiered decisions below
        _bot_overrides = _load_bot_overrides()

        def _act(bot_id, should_run, label):
            if should_run and _bot_overrides.get(str(bot_id)) == "stopped":
                print(f"  Manual override ACTIVE — skipping start for bot {bot_id} ({label})")
                return
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

        # If already in an active breakout, check for reversion/recovery/exhaustion
        if _bo_state.get("active") in ("UP", "DOWN"):
            _active_dir   = _bo_state["active"]
            _fire_price   = _bo_state.get("fire_price", state.price)
            _price_change = state.price - _fire_price
            print(f"BREAKOUT ACTIVE ({_active_dir}) — fire=${_fire_price:,.0f}  "
                  f"current=${state.price:,.0f}  Δ=${_price_change:+,.0f}")

            # UP reversion: price fell >2×ATR below fire price — breakout failed
            if _active_dir == "UP" and state.price < _fire_price - 2 * state.atr:
                print(f"BREAKOUT UP REVERTED — price ${state.price:,.0f} is "
                      f"${_fire_price - state.price:,.0f} below fire price (>2×ATR) — clearing state")
                notify_critical(f"Breakout UP reverted — price ${state.price:,.0f} fell "
                                f"${_fire_price - state.price:,.0f} below fire ${_fire_price:,.0f}")
                clear_breakout_state()
                _bo_state["active"] = None
                # Do NOT return — fall through to normal regime logic below

            # DOWN recovery: price recovered >1.5×ATR above fire price
            elif _active_dir == "DOWN" and state.price > _fire_price + 1.5 * state.atr:
                print(f"BREAKOUT DOWN RECOVERED — price ${state.price:,.0f} recovered "
                      f"${state.price - _fire_price:,.0f} above fire price — clearing state")
                notify(f"Breakout DOWN recovered — price ${state.price:,.0f} back above fire ${_fire_price:,.0f}")
                clear_breakout_state()
                _bo_state["active"] = None
                # Do NOT return — fall through to normal regime logic below

            else:
                # Breakout still active — track centre drift, check exhaustion, manage bots
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

                    # If BUY_ONLY or SELL_ONLY was active before the breakout, deploy
                    # the appropriate intensive tiers at the new price rather than normal
                    # tiers. Without this, the exhaustion redeploy silently overwrites the
                    # intensive grid with normal tiers and the BUY_ONLY re-entry never fires
                    # (because _prev_inventory_mode was "BUY_ONLY" throughout the breakout
                    # and the entry condition _prev != current is therefore False).
                    if state.inventory_mode == "BUY_ONLY":
                        _exhaust_tiers = _make_intensive_buy_tiers(state.price, state.tiers)
                        _exhaust_note  = " [intensive buy grid — BUY_ONLY still active]"
                    elif state.inventory_mode == "SELL_ONLY":
                        _exhaust_tiers = _make_intensive_sell_tiers(state.price, state.tiers)
                        _exhaust_note  = " [intensive sell grid — SELL_ONLY still active]"
                    else:
                        _exhaust_tiers = state.tiers
                        _exhaust_note  = ""

                    notify(f"Grid redeployed at ${state.price:,.0f} "
                           f"(breakout {_active_dir} exhaustion, moved ${_price_change:+,.0f})"
                           f"{_exhaust_note}")

                    if DRY_RUN:
                        update_grid_center(state.price, grid_width=state.grid_width)
                        print(f"[SIMULATION] Would redeploy grid centered at ${state.price:,.0f}"
                              f"{_exhaust_note}")
                        for i, bot_id in enumerate(GRID_BOTS[:3]):
                            tier = _exhaust_tiers[i] if i < len(_exhaust_tiers) else _exhaust_tiers[-1]
                            print(f"  [SIM] Bot {bot_id} ({tier['name']}): "
                                  f"${tier['grid_low']:,.0f}–${tier['grid_high']:,.0f}")
                    elif _can_act():
                        _record_action()
                        redeploy_all_bots(GRID_BOTS, _exhaust_tiers)
                        update_grid_center(state.price, grid_width=state.grid_width,
                                           deployed_tiers=_exhaust_tiers)
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

                # Allow target timeout / reversal / completion to be detected even
                # while a breakout is active. check_targets() saves state to disk;
                # next cycle will pick up the cleared target.
                try:
                    check_targets(state.price, state.atr)
                except Exception as _ct_err:
                    print(f"  Warning: target side-effect check failed: {_ct_err}")
                return  # breakout still active — do not fall through to normal regime logic

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
                # Any DCA capital deployed (single bot, scout, or retest) means
                # stop ALL grid bots to free maximum capital for safety orders.
                _any_dca_live = any(_pt_state.get(k) for k in (
                    "dca_bot_id", "dca_scout_bot_id", "dca_retest_bot_id"))
                if _any_dca_live:
                    print(f"  [Target] DCA capital deployed — ALL bots OFF (capital reserved for DCA)")
                    for i, bot in enumerate(GRID_BOTS[:3]):
                        tier_name = ["inner", "mid", "outer"][i]
                        _act(bot, False, f"{tier_name} (DCA active: {_pt_label})")
                else:
                    # DCA not yet launched (hold period or DCA not configured).
                    if state.inventory_mode == "BUY_ONLY":
                        # BUY_ONLY + UP target (no DCA): the target and inventory goal are
                        # aligned — both want BTC to appreciate.  No reason to stop buys.
                        # The intensive buy grid is already deployed from BUY_ONLY entry;
                        # just keep all bots running — no redeploy needed.
                        print(f"  [Target] BUY_ONLY — all bots ON (target monitoring in background)")
                        for i, bot in enumerate(GRID_BOTS[:3]):
                            tier_name = ["inner", "mid", "outer"][i]
                            _act(bot, True, f"{tier_name} (BUY_ONLY target: {_pt_label})")
                    else:
                        # Normal / SELL_ONLY: keep outer running as a safety net; inner+mid off.
                        print(f"  [Target] inner+mid off, outer running (DCA pending or not configured)")
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

                # DCA bot launch — single or dual entry.
                #
                # Single entry: one bot launched immediately with full capital.
                #
                # Dual entry (dca_dual_entry=True):
                #   Scout  — fires immediately at dca_scout_pct% of base order capital.
                #             Tracked via dca_scout_bot_id.
                #   Retest — fires when price pulls back within dca_retest_tolerance_pct%
                #             of the fire price, after dca_scout_buffer_cycles cycles.
                #             Uses remaining (100-scout)% capital. Tracked via dca_retest_bot_id.
                #             dca_bot_id is only set once the retest bot is live — this is
                #             the "DCA active" marker used by the capital guard and SL logic.
                #
                # Rate limit fix: _record_action() moved inside the success path so
                # a failed launch does not consume a rate-limit slot.
                _tp_steps   = _pt_state.get("dca_tp_steps") or []
                _has_tp     = bool(_tp_steps) or bool(_pt_tp)
                _dual       = bool(_pt_state.get("dca_dual_entry"))
                _scout_id   = _pt_state.get("dca_scout_bot_id")
                _retest_id  = _pt_state.get("dca_retest_bot_id")
                _main_id    = _pt_state.get("dca_bot_id")

                if _pt_state.get("dca_enabled") and _has_tp and _hold_secs == 0:
                    bo_usd    = float(_pt_state.get("dca_base_order_usd", 500))
                    so_usd    = round(bo_usd * 0.5, 2)
                    so_count  = int(_pt_state.get("dca_safety_count", 5))
                    so_step   = float(_pt_state.get("dca_safety_step_pct", 1.5))
                    so_mult   = float(_pt_state.get("dca_safety_volume_mult", 1.2))
                    _trailing = bool(_pt_state.get("dca_trailing_enabled"))
                    _trail_dev= float(_pt_state.get("dca_trailing_deviation_pct") or 1.0)

                    # TP config: prefer explicit steps; fall back to % derived from price_target
                    if _tp_steps:
                        tp_desc = " | ".join(f"{s['profit_pct']}%→{s['close_pct']}%" for s in _tp_steps)
                        tp_pct  = 2.0   # fallback not used when steps provided
                    else:
                        tp_pct  = round((_pt_tp - state.price) / state.price * 100, 2)
                        tp_desc = f"{tp_pct:.1f}%"

                    def _launch_dca(label, capital_usd, steps, trailing, trail_dev):
                        """Create, enable, and return bot id. Raises on failure."""
                        so = round(capital_usd * 0.5, 2)
                        bd = create_dca_bot(
                            label=label,
                            base_order_usd=capital_usd,
                            safety_order_usd=so,
                            take_profit_pct=tp_pct if not steps else 2.0,
                            take_profit_steps=steps if steps else None,
                            safety_order_count=so_count,
                            safety_order_step_pct=so_step,
                            safety_order_volume_mult=so_mult,
                            trailing_enabled=trailing,
                            trailing_deviation_pct=trail_dev,
                        )
                        bid = str(bd.get("id", ""))
                        if not bid:
                            raise ValueError(f"3Commas returned no bot id: {bd}")
                        enable_dca_bot(bid)
                        return bid

                    if not _dual:
                        # ── Single entry ──────────────────────────────────────
                        if not _main_id:
                            _last_attempt = float(_pt_state.get("dca_last_attempt_ts") or 0)
                            _attempt_age  = time.time() - _last_attempt
                            _RETRY_COOLDOWN = 300
                            if _attempt_age < _RETRY_COOLDOWN:
                                print(f"  DCA launch: retry cooldown — "
                                      f"{int(_RETRY_COOLDOWN - _attempt_age)}s remaining")
                            elif DRY_RUN:
                                print(f"  [SIM] Would launch DCA bot '{_pt_label}' "
                                      f"base=${bo_usd} TP={tp_desc} trailing={_trailing}")
                            elif _can_act():
                                _fail_count = int(_pt_state.get("dca_fail_count") or 0)
                                update_target(_pt_state["id"], {"dca_last_attempt_ts": time.time()})
                                try:
                                    bid = _launch_dca(_pt_label, bo_usd, _tp_steps, _trailing, _trail_dev)
                                    _record_action()   # only after success
                                    update_target(_pt_state["id"], {
                                        "dca_bot_id": bid,
                                        "dca_last_attempt_ts": None,
                                        "dca_fail_count": 0,
                                    })
                                    notify(f"DCA bot launched '{_pt_label}' id={bid} base=${bo_usd:.0f}")
                                    print(f"  DCA bot launched: id={bid} base=${bo_usd} TP={tp_desc}")
                                except Exception as _dca_err:
                                    new_fails = _fail_count + 1
                                    update_target(_pt_state["id"], {"dca_fail_count": new_fails})
                                    print(f"  ERROR: DCA bot launch failed (attempt {new_fails}): {_dca_err}")
                                    if new_fails == 1:
                                        notify_critical(f"DCA launch FAILED '{_pt_label}': {_dca_err}")
                            else:
                                print(f"  Rate limit — DCA launch deferred to next cycle")

                    else:
                        # ── Dual entry ────────────────────────────────────────
                        _scout_pct = float(_pt_state.get("dca_scout_pct") or 30) / 100.0
                        _buf_cycles= int(_pt_state.get("dca_scout_buffer_cycles") or 5)
                        _retest_tol= float(_pt_state.get("dca_retest_tolerance_pct") or 0.5) / 100.0
                        _cycles_active = int(_pt_state.get("dca_scout_cycles_active") or 0)

                        if not _scout_id:
                            # Phase 1 — launch scout bot immediately.
                            # Back off 5 min after a failed attempt to avoid spamming
                            # 3Commas and Telegram on every cycle.
                            _last_attempt = float(_pt_state.get("dca_last_attempt_ts") or 0)
                            _attempt_age  = time.time() - _last_attempt
                            _RETRY_COOLDOWN = 300  # 5 minutes between retries
                            if _attempt_age < _RETRY_COOLDOWN:
                                print(f"  DCA scout: retry cooldown — "
                                      f"{int(_RETRY_COOLDOWN - _attempt_age)}s remaining")
                            else:
                                scout_capital = round(bo_usd * _scout_pct, 2)
                                if DRY_RUN:
                                    print(f"  [SIM] Would launch SCOUT DCA '{_pt_label}' "
                                          f"capital=${scout_capital} ({_scout_pct:.0%} of ${bo_usd})")
                                elif _can_act():
                                    _fail_count = int(_pt_state.get("dca_fail_count") or 0)
                                    update_target(_pt_state["id"], {
                                        "dca_last_attempt_ts": time.time(),
                                    })
                                    try:
                                        scout_label = f"{_pt_label} [scout]"
                                        bid = _launch_dca(scout_label, scout_capital, _tp_steps, _trailing, _trail_dev)
                                        _record_action()
                                        update_target(_pt_state["id"], {
                                            "dca_scout_bot_id": bid,
                                            "dca_scout_cycles_active": 0,
                                            "dca_last_attempt_ts": None,
                                            "dca_fail_count": 0,
                                        })
                                        notify(f"DCA scout launched '{_pt_label}' id={bid} "
                                               f"capital=${scout_capital:.0f} ({_scout_pct:.0%})")
                                        print(f"  DCA scout launched: id={bid} capital=${scout_capital}")
                                    except Exception as _e:
                                        new_fails = _fail_count + 1
                                        update_target(_pt_state["id"], {"dca_fail_count": new_fails})
                                        print(f"  ERROR: DCA scout launch failed (attempt {new_fails}): {_e}")
                                        # Only notify on first failure — subsequent retries are silent
                                        if new_fails == 1:
                                            notify_critical(f"DCA scout launch FAILED '{_pt_label}': {_e}")
                                else:
                                    print(f"  Rate limit — DCA scout launch deferred")

                        elif not _retest_id:
                            # Phase 2 — wait for retest then launch main bot
                            new_cycles = _cycles_active + 1
                            update_target(_pt_state["id"], {"dca_scout_cycles_active": new_cycles})

                            _fire_px  = float(_pt_state.get("fired_price") or state.price)
                            _retest_lo = _fire_px * (1.0 - _retest_tol)
                            _retest_hi = _fire_px * (1.0 + _retest_tol)
                            _in_retest = _retest_lo <= state.price <= _retest_hi
                            _buf_met   = new_cycles >= _buf_cycles

                            print(f"  DCA retest watch: fire=${_fire_px:,.0f} "
                                  f"zone={_retest_lo:,.0f}–{_retest_hi:,.0f} "
                                  f"now=${state.price:,.0f} "
                                  f"cycles={new_cycles}/{_buf_cycles} "
                                  f"in_zone={_in_retest}")

                            if _buf_met and _in_retest:
                                retest_capital = round(bo_usd * (1.0 - _scout_pct), 2)
                                if DRY_RUN:
                                    print(f"  [SIM] Retest confirmed — would launch main DCA "
                                          f"capital=${retest_capital} ({1-_scout_pct:.0%} of ${bo_usd})")
                                elif _can_act():
                                    try:
                                        retest_label = f"{_pt_label} [retest]"
                                        bid = _launch_dca(retest_label, retest_capital, _tp_steps, _trailing, _trail_dev)
                                        _record_action()
                                        update_target(_pt_state["id"], {
                                            "dca_retest_bot_id": bid,
                                            "dca_bot_id": bid,   # marks DCA as fully live
                                        })
                                        notify(f"DCA retest confirmed '{_pt_label}' — "
                                               f"main bot launched id={bid} capital=${retest_capital:.0f}")
                                        print(f"  DCA retest bot launched: id={bid} capital=${retest_capital}")
                                    except Exception as _e:
                                        notify_critical(f"DCA retest launch FAILED '{_pt_label}': {_e}")
                                        print(f"  ERROR: DCA retest launch failed: {_e}")
                                else:
                                    print(f"  Rate limit — DCA retest launch deferred")
                        else:
                            print(f"  Dual DCA active — scout={_scout_id} retest={_retest_id}")

                # ── DCA stop loss ──────────────────────────────────────────
                # Covers both single-entry (dca_bot_id) and dual-entry (scout
                # and/or retest bots). Any live bot gets panic-sold and all IDs
                # are cleared so capital returns to the grid.
                _dca_sl_pct   = float(_pt_state.get("dca_stop_loss_pct") or 0)
                _sl_bots_live = [b for b in [
                    _pt_state.get("dca_bot_id"),
                    _pt_state.get("dca_scout_bot_id"),
                    _pt_state.get("dca_retest_bot_id"),
                ] if b]
                if _sl_bots_live and _dca_sl_pct > 0:
                    _sl_entry = float(_pt_state.get("fired_price") or state.price)
                    _sl_level = _sl_entry * (1.0 - _dca_sl_pct / 100.0)
                    if state.price < _sl_level:
                        print(f"  DCA stop loss triggered — price ${state.price:,.0f} < "
                              f"${_sl_level:,.0f} ({_dca_sl_pct}% below entry "
                              f"${_sl_entry:,.0f}) — panic-selling {len(_sl_bots_live)} bot(s)")
                        if DRY_RUN:
                            print(f"  [SIM] Would panic_sell: {_sl_bots_live}")
                        else:
                            try:
                                for _sl_bid in _sl_bots_live:
                                    panic_sell_dca_bot(_sl_bid)
                                update_target(_pt_state["id"], {
                                    "dca_bot_id": None,
                                    "dca_scout_bot_id": None,
                                    "dca_retest_bot_id": None,
                                    "dca_scout_cycles_active": 0,
                                })
                                notify_critical(
                                    f"DCA STOP LOSS '{_pt_label}' — "
                                    f"${state.price:,.0f} hit {_dca_sl_pct:.1f}% SL "
                                    f"(entry ${_sl_entry:,.0f}). "
                                    f"All positions closed, capital released to grid."
                                )
                                print(f"  DCA stop loss: {len(_sl_bots_live)} bot(s) panic-sold")
                            except Exception as _sl_err:
                                print(f"  Warning: DCA stop loss failed: {_sl_err}")
                    else:
                        print(f"  DCA SL watch: ${state.price:,.0f} | "
                              f"SL at ${_sl_level:,.0f} ({_dca_sl_pct}% below "
                              f"${_sl_entry:,.0f}) | bots live: {len(_sl_bots_live)}")

            else:  # DOWN target (support_failure or breakout DOWN)
                print(f"  [Target] all bots off (capital protection)")
                for bot in GRID_BOTS:
                    _act(bot, False, f"target DOWN: {_pt_label}")

                # ── SmartTrade sell launch ─────────────────────────────────
                # On support_failure DOWN, launch a SmartTrade spot sell:
                # sell X% of BTC at market with TP steps below entry and SL above.
                # Same 6-min sweep guard as DCA bot — if price snaps back before
                # the hold expires, the target clears before the trade fires.
                ST_LAUNCH_HOLD_SECS = 360
                _fired_at_st  = _pt_state.get("fired_at") or 0
                _hold_secs_st = max(0, ST_LAUNCH_HOLD_SECS - (time.time() - _fired_at_st))
                if _hold_secs_st > 0:
                    print(f"  SmartTrade hold — sweep guard ({_hold_secs_st:.0f}s remaining)")

                _st_enabled  = _pt_state.get("smart_trade_enabled") and not _pt_state.get("smart_trade_id")
                _st_tp_steps = _pt_state.get("smart_trade_tp_steps") or []
                _st_sl_pct   = float(_pt_state.get("smart_trade_sl_pct", 1.5))
                _st_sell_pct = float(_pt_state.get("smart_trade_sell_pct", 25.0))

                if _st_enabled and _hold_secs_st == 0 and _st_tp_steps:
                    snap = portfolio_snapshot()
                    _btc_available = snap["btc_qty"] if snap else None
                    if _btc_available and _btc_available > 0:
                        sell_qty = round(_btc_available * _st_sell_pct / 100.0, 8)
                        tp_desc  = " | ".join(
                            f"{s['profit_pct']}%→{s['close_pct']}%" for s in _st_tp_steps
                        )
                        if DRY_RUN:
                            print(f"  [SIM] Would create SmartTrade SELL '{_pt_label}' | "
                                  f"qty={sell_qty:.6f} BTC ({_st_sell_pct:.0f}% of {_btc_available:.6f}) "
                                  f"TP={tp_desc} SL={_st_sl_pct}%")
                        elif _can_act():
                            _record_action()
                            try:
                                from threecommas_dca import create_smart_trade
                                st_data = create_smart_trade(
                                    pair="USDC_BTC",
                                    sell_btc_qty=sell_qty,
                                    tp_steps=_st_tp_steps,
                                    sl_pct=_st_sl_pct,
                                    label=_pt_label,
                                )
                                st_id = str(st_data.get("id", ""))
                                if st_id:
                                    update_target(_pt_state["id"], {"smart_trade_id": st_id})
                                    notify(f"SmartTrade SELL '{_pt_label}' — "
                                           f"{sell_qty:.4f} BTC, SL={_st_sl_pct}%")
                                    print(f"  SmartTrade launched: id={st_id} "
                                          f"qty={sell_qty:.6f} BTC SL={_st_sl_pct}%")
                            except Exception as _st_err:
                                print(f"  Warning: SmartTrade launch failed: {_st_err}")
                        else:
                            print(f"  Rate limit — SmartTrade launch deferred to next cycle")
                    else:
                        print(f"  SmartTrade skipped — no BTC qty in inventory cache")

                # ── SmartTrade status poll ─────────────────────────────────
                # Once a SmartTrade is live, poll its status every cycle so the
                # engine knows when 3Commas closes it (TP hit, SL hit, or manual
                # cancel). On any terminal status: clear the target and fall through
                # to the recovery/drift logic so the grid restarts immediately.
                # This is the PRIMARY completion path — the 2h timeout in
                # price_targets.py is the safety-net fallback.
                _st_id_live = _pt_state.get("smart_trade_id")
                if _st_id_live:
                    try:
                        from threecommas_dca import get_smart_trade_status
                        _st_resp    = get_smart_trade_status(_st_id_live)
                        _st_type    = (_st_resp.get("status") or {}).get("type", "unknown")
                        _TERMINAL   = {"finished", "cancelled", "failed",
                                       "panic_sold", "cancelled_error"}
                        if _st_type in _TERMINAL:
                            print(f"  SmartTrade {_st_id_live} is {_st_type} — "
                                  f"clearing target '{_pt_label}', restarting grid")
                            update_target(_pt_state["id"], {
                                "fired":          False,
                                "consec_above":   0,
                                "smart_trade_id": None,
                                "cleared_at":     time.time(),
                            })
                            notify(f"SmartTrade '{_pt_label}' {_st_type} — "
                                   f"target cleared, grid restarting at ${state.price:,.0f}")
                            _pt_state = None   # cleared — skip the return below
                        else:
                            print(f"  SmartTrade {_st_id_live} status={_st_type} — "
                                  f"holding bots stopped")
                    except Exception as _poll_err:
                        print(f"  Warning: SmartTrade status poll failed: {_poll_err}")

            # Only skip the rest of the engine cycle if the target is still active.
            # If the SmartTrade just completed above, _pt_state was set to None and
            # we fall through so the recovery/drift blocks can restart the grid.
            if _pt_state is not None:
                return   # skip fresh breakout detection AND drift while target is live

        # Fresh breakout detection
        _direction = breakout_detected(df, regime=state.regime, gap_ratio=state.gap_ratio)
        if _direction:
            print(f"BREAKOUT DETECTED — direction: {_direction}")
            notify_critical(f"Breakout {_direction} detected at ${state.price:,.0f} — grid bots adjusting")

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
            if state.inventory_mode in ("BUY_ONLY", "SELL_ONLY") or _prev_weekend_mode:
                # Biased/weekend mode — grid is intentionally deployed at a specific
                # geometry. A drift redeployment would overwrite it with normal
                # symmetric tiers. Suppress and fall through.
                _drift_suppress_reason = (
                    f"{state.inventory_mode} intensive mode" if state.inventory_mode != "NORMAL"
                    else "weekend tight grid"
                )
                print(f"  Drift suppressed — {_drift_suppress_reason} active, preserving biased grid")
            else:
                # ── Flood-fill guard ──────────────────────────────────────────
                # Prevent rapid recentres when price oscillates around the drift
                # threshold on 2-min cycles. Min 20 min between recentres.
                _can_redeploy, _redeploy_wait = redeploy_allowed()
                if not _can_redeploy:
                    print(f"  Flood guard: drift detected but suppressing redeploy — "
                          f"{_redeploy_wait/60:.1f}min remaining "
                          f"(min {1200//60}min between recentres)")
                    # Fall through to normal tiered bot decisions on current ranges
                else:
                    notify(f"Grid drift — recentring to ${state.price:,.0f} (was ${state.center:,.0f})")
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
                        update_grid_center(state.price, grid_width=state.grid_width)
                        print("[SIMULATION] Would redeploy grid bots with tiered ranges:")
                        for i, bot_id in enumerate(GRID_BOTS[:3]):
                            tier = state.tiers[i] if i < len(state.tiers) else state.tiers[-1]
                            print(f"  [SIM] Bot {bot_id} ({tier['name']}): "
                                  f"${tier['grid_low']:,.0f}–${tier['grid_high']:,.0f}, "
                                  f"{tier['levels']} levels, ${tier['step']:,.0f} step")
                    elif _can_act():
                        _record_action()
                        redeploy_all_bots(GRID_BOTS, state.tiers)
                        update_grid_center(state.price, grid_width=state.grid_width,
                                           deployed_tiers=state.tiers)
                    else:
                        print(f"Rate limit reached ({MAX_ACTIONS_PER_HOUR}/hr) — skipping drift redeploy")
                        print(f"  Bots remain on current ranges — center NOT advanced")

                    return

        # ===============================
        # REGIME TRANSITION REDEPLOY
        # ===============================
        # When coming out of a "stopped" regime (TREND_DOWN / COMPRESSION) into
        # an active one, bots were off for hours and their stored grid ranges are
        # stale. Redeploy at the current price rather than calling start_bot(),
        # which would restart bots at their old, potentially distant ranges.
        _STOPPED_REGIMES   = {"TREND_DOWN", "COMPRESSION"}
        # BUY_ONLY included here: when ratio recovers to NORMAL the intensive
        # buy grid must be replaced with a fresh normal-parameter deployment.
        _STOPPED_INV_MODES = {"SELL_ONLY", "BUY_ONLY"}
        _regime_recovery  = _prev_regime in _STOPPED_REGIMES and state.regime not in _STOPPED_REGIMES
        _invmode_recovery = (_prev_inventory_mode in _STOPPED_INV_MODES
                             and state.inventory_mode not in _STOPPED_INV_MODES)
        if _regime_recovery or _invmode_recovery:
            _reason = (f"Regime {_prev_regime} → {state.regime}" if _regime_recovery
                       else f"Inventory mode {_prev_inventory_mode} → {state.inventory_mode}")
            # If we're recovering mid-weekend into RANGE/NORMAL, restore the tight
            # grid rather than the full-width normal tiers.
            _post_recovery_weekend = (
                _is_weekend_grid_hours()
                and state.regime == "RANGE"
                and state.inventory_mode == "NORMAL"
                and not _bo_state.get("active")
            )
            _recovery_tiers = (_make_weekend_tiers(state.price, state.tiers)
                               if _post_recovery_weekend else state.tiers)
            _mode_note = " [weekend tight grid restored]" if _post_recovery_weekend else ""

            # ── Trendline auto-activate ───────────────────────────────────
            # If TREND_DOWN cleared via stabilisation (not trendline recovery),
            # check whether the episode low matched an inactive drawn trendline.
            # If so, swap it in as the active trendline before the redeploy so
            # the new grid is immediately calibrated to the correct lower support.
            _tl_note = ""
            if _regime_recovery and _prev_regime == "TREND_DOWN":
                _rs_now = get_regime_state()
                _td_last_low = _rs_now.get("td_last_low")
                if _td_last_low:
                    _matched_tl = _try_auto_activate_trendline(
                        _td_last_low, state.price, state.atr)
                    if _matched_tl:
                        _tl_note = (f" — trendline '{_matched_tl.get('label','?')}' "
                                    f"auto-activated at ${_td_last_low:,.0f}")
                        notify(f"Trendline auto-activated: '{_matched_tl.get('label','?')}' "
                               f"matched TREND_DOWN low ${_td_last_low:,.0f} "
                               f"— engine now tracking lower support")

            print(f"{_reason} — redeploying at ${state.price:,.0f}{_mode_note}{_tl_note}")
            notify(f"{_reason} — grid redeployed at ${state.price:,.0f}{_mode_note}{_tl_note}")
            if DRY_RUN:
                print(f"[SIMULATION] Would redeploy grid at ${state.price:,.0f}{_mode_note}")
            elif _can_act():
                _record_action()
                redeploy_all_bots(GRID_BOTS, _recovery_tiers)
                update_grid_center(state.price, grid_width=state.grid_width,
                                   deployed_tiers=_recovery_tiers)
                if _post_recovery_weekend:
                    _prev_weekend_mode = True
            else:
                print(f"Rate limit reached — skipping recovery redeploy")
            return

        # ===============================
        # WEEKEND TIGHT GRID MODE
        # ===============================
        # Default mode when nothing else is happening: Fri close → Mon EU open.
        # Compresses grid to 65% width centred on current price so tighter step
        # spacing generates more fills during low-volatility weekend oscillation.
        #
        # Only activates when: RANGE regime + NORMAL inventory + no active breakout.
        # If any of those change mid-weekend, regime/inventory logic takes over and
        # weekend mode stays pending in the background until they clear.
        # Exits on time (Monday 07:00 UTC) — never on regime/inventory interruption.
        #
        # Redeploy guard: if price is within half a step of any current grid level
        # the redeploy is deferred cycle by cycle (no timeout) until clearance.
        # Drift is suppressed while weekend mode is active (see drift block above).
        _weekend_hours    = _is_weekend_grid_hours()
        _weekend_eligible = (
            _weekend_hours
            and state.regime       == "RANGE"
            and state.inventory_mode == "NORMAL"
            and not _bo_state.get("active")
        )
        _entering_weekend = _weekend_eligible and not _prev_weekend_mode
        _exiting_weekend  = _prev_weekend_mode and not _weekend_hours   # time-based exit only

        if _exiting_weekend:
            # Monday 07:00 UTC — return to full-width normal grid
            print(f"  Weekend mode ENDING — Monday EU open, redeploying normal grid at ${state.price:,.0f}")
            notify(f"Weekend mode ended — Monday EU open, normal grid redeployed at ${state.price:,.0f}")
            if DRY_RUN:
                print(f"  [SIM] Would redeploy normal tiers at ${state.price:,.0f}")
            elif _can_act():
                _record_action()
                redeploy_all_bots(GRID_BOTS, state.tiers)
                update_grid_center(state.price, grid_width=state.grid_width,
                                   deployed_tiers=state.tiers)
            else:
                print(f"  Rate limit reached — normal redeploy deferred to next cycle")
            _prev_weekend_mode = False

        elif _entering_weekend:
            # First eligible cycle in weekend window — check the near-level guard.
            # Use deployed_tiers (actual live bot levels) not state.tiers.
            # state.tiers is recalculated each cycle centred on current price,
            # so it always has a level exactly at current price → guard would
            # permanently defer. deployed_tiers are fixed to the last redeploy.
            _deployed_tiers = _saved_grid.get("deployed_tiers") or []
            _near, _near_lvl, _threshold = _price_near_level(state.price, _deployed_tiers)
            if _near:
                _dist = abs(state.price - _near_lvl)
                print(f"  Weekend mode DEFERRED — price ${state.price:,.0f} is "
                      f"${_dist:,.0f} from level ${_near_lvl:,.0f} "
                      f"(threshold ${_threshold:,.0f}) — waiting for clearance")
            else:
                _wt = _make_weekend_tiers(state.price, state.tiers)
                _inner_step = round(_wt[0]["step"]) if _wt else "?"
                print(f"  Weekend mode ACTIVATING — tight grid at ${state.price:,.0f} "
                      f"(inner step ≈ ${_inner_step:,}, was ${round(state.tiers[0]['step']):,})")
                if DRY_RUN:
                    print(f"  [SIM] Would redeploy weekend tight tiers "
                          f"{_wt[0]['grid_low']:,.0f}–{_wt[0]['grid_high']:,.0f}")
                elif _can_act():
                    _record_action()
                    redeploy_all_bots(GRID_BOTS, _wt)
                    update_grid_center(state.price, grid_width=state.grid_width,
                                       deployed_tiers=_wt)
                    _prev_weekend_mode = True
                    notify(f"Weekend tight grid deployed at ${state.price:,.0f} — "
                           f"inner step ${_inner_step:,} (normal ${round(state.tiers[0]['step']):,})")
                else:
                    print(f"  Rate limit reached — weekend tight redeploy deferred to next cycle")

        elif _prev_weekend_mode and _weekend_hours:
            # Already in weekend mode — log status each cycle
            # Sunday 23:00 UTC check: if Asia open and price has drifted > 40%
            # of tight grid width from centre, silently recentre the tight grid.
            import datetime as _dt
            _now = _dt.datetime.utcnow()
            _sunday_asia = (_now.weekday() == 6 and _now.hour >= 23)
            if _sunday_asia and state.tiers:
                _tight_width   = state.tiers[0]["grid_high"] - state.tiers[0]["grid_low"]
                _tight_centre  = (state.tiers[0]["grid_high"] + state.tiers[0]["grid_low"]) / 2
                _tight_drift   = abs(state.price - _tight_centre)
                if _tight_drift > _tight_width * 0.40:
                    print(f"  Weekend mode: Sunday Asia open recentre — "
                          f"drift ${_tight_drift:,.0f} > 40% of tight grid (${_tight_width * 0.40:,.0f})")
                    notify(f"Weekend grid recentred for Asia open at ${state.price:,.0f}")
                    _wt2 = _make_weekend_tiers(state.price, state.tiers)
                    if not DRY_RUN and _can_act():
                        _record_action()
                        redeploy_all_bots(GRID_BOTS, _wt2)
                        update_grid_center(state.price, grid_width=state.grid_width,
                                           deployed_tiers=_wt2)
                else:
                    print(f"  Weekend mode ACTIVE (Sunday/Asia) — tight grid, drift ${_tight_drift:,.0f} within threshold")
            else:
                print(f"  Weekend mode ACTIVE — tight grid running, drift suppressed")

        # ===============================
        # INVENTORY PROTECTION
        # ===============================
        if state.inventory_mode == "SELL_ONLY":
            if _prev_inventory_mode != "SELL_ONLY":
                # First cycle at critically high BTC ratio — redeploy in intensive
                # sell mode: all tier ranges shifted above current price (initial
                # orders are sells only) and compressed to 60% width for a denser
                # sell cluster. As price rises into the range, sells fill and BTC
                # converts to USDC. If price falls, orders sit unexecuted — no
                # forced selling at a loss. Drift suppressed while active.
                # Recovery to NORMAL triggers a fresh symmetric redeploy.
                notify_critical(
                    f"SELL ONLY — BTC ratio {state.btc_ratio:.0%} too high, "
                    f"entering intensive sell mode (grid shifted above price)"
                )
                _intensive_tiers = _make_intensive_sell_tiers(state.price, state.tiers)
                if DRY_RUN:
                    print(f"  [SIM] Would redeploy intensive sell: "
                          f"inner {_intensive_tiers[0]['grid_low']:,.0f}–"
                          f"{_intensive_tiers[0]['grid_high']:,.0f}")
                elif _can_act():
                    _record_action()
                    redeploy_all_bots(GRID_BOTS, _intensive_tiers)
                    update_grid_center(state.price, grid_width=state.grid_width,
                                       deployed_tiers=_intensive_tiers)
                else:
                    print(f"  Rate limit reached — intensive sell redeploy deferred to next cycle")
            print(f"SELL ONLY: ratio {state.btc_ratio:.0%} — intensive sell mode active, liquidating above price")
            # Fall through to tiered bot decisions — bots remain ON to sell.

        if state.inventory_mode == "BUY_ONLY":
            if _prev_inventory_mode != "BUY_ONLY":
                # First cycle at critically low BTC ratio — redeploy in intensive
                # buy mode: all tier ranges shifted below current price (initial
                # orders are buys only) and compressed to 60% width for a denser
                # buy cluster. Drift redeployment is suppressed while this mode
                # is active. Recovery to normal triggers a fresh redeploy above.
                notify_critical(
                    f"BUY ONLY — BTC ratio {state.btc_ratio:.0%} critically low, "
                    f"entering intensive buy mode (grid shifted below price)"
                )
                _intensive_tiers = _make_intensive_buy_tiers(state.price, state.tiers)
                if DRY_RUN:
                    print(f"  [SIM] Would redeploy intensive buy: "
                          f"inner {_intensive_tiers[0]['grid_low']:,.0f}–"
                          f"{_intensive_tiers[0]['grid_high']:,.0f}")
                elif _can_act():
                    _record_action()
                    redeploy_all_bots(GRID_BOTS, _intensive_tiers)
                    update_grid_center(state.price, grid_width=state.grid_width,
                                       deployed_tiers=_intensive_tiers)
                else:
                    print(f"  Rate limit reached — intensive buy redeploy deferred to next cycle")
            print(f"BUY ONLY: ratio {state.btc_ratio:.0%} — intensive buy mode active, accumulating below price")
            # Fall through to tiered bot decisions — bots remain ON to accumulate.

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
            if _prev_regime != "COMPRESSION":
                notify(f"COMPRESSION — inner+mid off, outer running at ${state.price:,.0f}")
            print("COMPRESSION — inner+mid off, outer running (wide range catches low-vol oscillations)")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 2, tier_name)   # only outer (index 2) runs

        elif state.trending_down:
            # Strong downside move — inner and mid OFF, outer ON as safety net
            if not _prev_trending_down:
                notify(f"Trending DOWN (gap={state.gap_ratio:.2f}×ATR) — inner+mid off at ${state.price:,.0f}")
            print(f"TRENDING DOWN (gap={state.gap_ratio:.2f}×ATR) — inner+mid off, outer holding")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 2, tier_name)  # only outer (index 2) runs

        elif state.regime == "TREND_DOWN":
            # Confirmed TREND_DOWN (hysteresis-filtered) — same as trending_down
            if _prev_regime != "TREND_DOWN":
                notify_critical(f"TREND_DOWN confirmed — inner+mid off, outer holding at ${state.price:,.0f}")
            print(f"TREND_DOWN — inner+mid off, outer holding")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 2, tier_name)

        elif state.trending_up and state.regime not in ("RANGE", "TREND_UP"):
            # Price running hard above trendline AND regime confirms directional move.
            # Excludes RANGE (price above support, not a real trend) and TREND_UP
            # (already confirmed uptrend — all bots run for pullback fills).
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
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, True, tier_name)

    finally:
        # ===============================
        # DASHBOARD STATUS EXPORT
        # Always runs — even on early return or exception
        # ===============================
        if state.price is not None:
            _regime_state = get_regime_state()
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
                # Flash move state
                "flash_move_active":   get_flash_move_state().get("active"),
                "flash_move_cooldown": get_flash_move_state().get("cooldown_remaining", 0),
                # Price target state
                "price_target_active":  bool(_pt_state),
                "price_target_label":   _pt_state.get("label")   if _pt_state else None,
                "price_target_trigger": _pt_state.get("trigger_price") if _pt_state else None,
                "price_target_tp":      _pt_state.get("price_target")  if _pt_state else None,
                "price_target_dca_id":  _pt_state.get("dca_bot_id")    if _pt_state else None,
                # Weekend mode
                "weekend_mode": _prev_weekend_mode,
                # TREND_DOWN stabilisation progress (for dashboard + future retest logic)
                "td_low":             _regime_state.get("td_low"),
                "td_stable_cycles":   _regime_state.get("td_no_new_low_count", 0),
                "td_stable_needed":   8,
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

            # Track regime and flags across cycles for transition detection
            if state.regime:
                _prev_regime = state.regime
            _prev_trending_down    = bool(state.trending_down)
            _prev_inventory_mode   = state.inventory_mode
            # _prev_weekend_mode is updated directly in the weekend mode block above

if __name__ == "__main__":
    schedule.every(2).minutes.do(run)

    # Run once immediately on startup
    run()

    print("Engine running...")

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nEngine stopped safely.")