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
import orderbook  # Phase 0: read-only order-book liquidity collector (no decisions)
import amplitude  # Phase 0: realized swing amplitude vs fee floor (observability)
import fills_capture  # persist BUY/SELL fills each cycle before 3Commas wipes them
import realpnl  # real cost-basis P&L (honest realised number per sell, red or green)
import slide_guard  # Phase 0: sustained-grind ("falling knife") detector (observability)
import ride_mode  # manually-armed trend-up accumulation mode
from indicators import add_indicators
from regime import (detect_regime, trend_strength, compression_exit_fast, get_regime_state,
                    TRENDING_UP_EXIT, TRENDING_DOWN_EXIT)
from threecommas import stop_bot, start_bot, redeploy_all_bots
from price_targets import check_targets, update_target, get_support_failure_status
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


def _utcnow() -> datetime.datetime:
    """Current UTC time as a timezone-aware datetime.

    Single source of 'now' for all wall-clock logic (weekend window, rate
    limiter). Tests patch this to run deterministically regardless of the real
    day. Timezone-aware to avoid the utcnow() deprecation warning.
    """
    return datetime.datetime.now(datetime.timezone.utc)


def _can_act() -> bool:
    """Return True if we are under the MAX_ACTIONS_PER_HOUR limit."""
    now = _utcnow()
    cutoff = now - datetime.timedelta(hours=1)
    # Drop timestamps older than 1 hour
    while _action_timestamps and _action_timestamps[0] < cutoff:
        _action_timestamps.popleft()
    return len(_action_timestamps) < MAX_ACTIONS_PER_HOUR


def _record_action():
    """Record a bot action timestamp."""
    _action_timestamps.append(_utcnow())

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


def _apply_intensive_fee_guard(tier: dict, width: float) -> int:
    """Return a level count whose step clears this tier's fee floor."""
    levels = max(int(tier.get("levels", 5)), 2)
    min_step = float(tier.get("min_step") or 0)
    if min_step <= 0:
        return levels

    max_levels = int(width / min_step) + 1
    return max(min(levels, max_levels), 2)


def _make_intensive_buy_tiers(price: float, tiers: list, atr: float = None,
                              btc_ratio: float = None) -> list:
    """
    Build buy-biased tier parameters for BUY_ONLY mode.

    Shifts each tier's range entirely below current price so the bot's initial
    orders are buys only (no sell orders sit above price at deployment time).
    Range is compressed to 60% of normal width to create a denser buy cluster —
    45% when BTC is CRITICALLY low (< BUY_CRITICAL_RATIO), so the rungs sit even
    closer to price and shallow dips refill inventory sooner (Ash 2026-07-06:
    "the limit orders just need to be closer to price to have more chance of
    filling, until we get to non critical levels of btc" — big market entries
    belong to key-level breaks, never the grid).

        grid_high = price × 0.9995  (fractional buffer — avoids placing orders
                                     right on the live price spread)
        grid_low  = grid_high − (original_width × mult)

    Width floor: ≥1.2×ATR. Guards against the degenerate case where the
    drift-zone cap collapsed the source tier (observed live 2026-07-06: inner
    deployed $23 wide — 38% of buy capital parked in a $23 slot).
    Levels and step are recalculated proportionally.
    """
    import copy as _copy
    result = []
    _mult = 0.45 if (btc_ratio is not None and btc_ratio < BUY_CRITICAL_RATIO) else 0.60
    # Holdings-preserving anchor (added 2026-07-07): a strictly all-below ladder
    # needs ZERO base, so every chase re-anchor made 3Commas market-DUMP the BTC
    # the rungs had just accumulated (observed: 0.147 BTC balancing-sold at -$16
    # then re-bought higher — accumulate/dump churn paying ~0.4% a round trip).
    # Fix: place the range top so the fraction ABOVE price ≈ the account's
    # current BTC ratio — the grid keeps holdings as base (no balancing sell; no
    # buy either, since need ≈ have) and gives them profit-taking sell rungs
    # above. Same holdings-matching math as threecommas._size_tiers_to_holdings
    # (live-validated since Jul 1). Ratio ≤3% (dust) keeps the pure all-below
    # ladder — nothing worth preserving.
    _hold_f = min(btc_ratio, 0.5) if (btc_ratio is not None and btc_ratio > 0.03) else 0.0
    for tier in tiers:
        t = _copy.deepcopy(tier)
        orig_width = float(t.get("grid_high", price + 1000)) - float(t.get("grid_low", price - 1000))
        new_width  = round(orig_width * _mult, 2)
        if atr and atr > 0:
            new_width = max(new_width, round(1.2 * atr, 2))
        new_high   = round(price + _hold_f * new_width, 2) if _hold_f > 0 \
                     else round(price * 0.9995, 2)
        new_low    = round(new_high - new_width, 2)
        n          = _apply_intensive_fee_guard(t, new_width)
        new_step   = round(new_width / (n - 1), 2)
        t["grid_high"]   = new_high
        t["grid_low"]    = new_low
        t["levels"]      = n
        t["step"]        = new_step
        if t.get("min_step"):
            t["fee_ok"] = bool(new_step >= float(t["min_step"]))
        t["grid_levels"] = [round(new_low + i * new_step, 2) for i in range(n)]
        result.append(t)
    return result


def _make_intensive_sell_tiers(price: float, tiers: list, sell_to_ratio: float | None = None) -> list:
    """Reposition each tier so the bot SHEDS BTC down toward target on deploy —
    without ever buying BTC.

    Root-cause fix (2026-06-17). A 3Commas grid bot's base BTC position is set,
    on enable, to roughly the fraction of its range that sits *above* price —
    those are the sell orders, and the bot must hold BTC to back them. The old
    implementation placed the entire range *above* price (base ≈ 100% BTC), so
    on deploy 3Commas MARKET-BOUGHT BTC to fund the sell wall. Live evidence:
    entering SELL_ONLY spiked holdings 0.53→1.08 BTC (spent ~$36k of USDC) and
    latched SELL_ONLY on via the inflated ratio, churning BTC up/down and
    eventually dumping 0.476 BTC onto support. Selling by first buying is exactly
    backwards.

    Instead we keep the range straddling price with only `sell_to_ratio` of its
    width above price (sell side) and the rest below (buy side). The bot's base
    settles near `sell_to_ratio`, so 3Commas SELLS the excess down to it through
    the bot's own limit orders — controlled, no market buy. Defaults to the
    inventory target_btc. Width is left uncompressed so steps stay fee-OK (the
    old 0.60 compression could fall below the fee floor).
    """
    from inventory import get_inventory_settings
    if sell_to_ratio is None:
        sell_to_ratio = get_inventory_settings().get("target_btc", 0.45)
    sell_to_ratio = min(max(float(sell_to_ratio), 0.05), 0.95)  # clamp to sane band

    import copy as _copy
    result = []
    for tier in tiers:
        t = _copy.deepcopy(tier)
        width = float(t.get("grid_high", price + 1000)) - float(t.get("grid_low", price - 1000))
        new_high = round(price + width * sell_to_ratio, 2)          # sell side above price
        new_low  = round(price - width * (1.0 - sell_to_ratio), 2)  # buy side below price
        n        = _apply_intensive_fee_guard(t, width)
        new_step = round(width / (n - 1), 2)
        t["grid_high"]   = new_high
        t["grid_low"]    = new_low
        t["levels"]      = n
        t["step"]        = new_step
        if t.get("min_step"):
            t["fee_ok"] = bool(new_step >= float(t["min_step"]))
        t["grid_levels"] = [round(new_low + i * new_step, 2) for i in range(n)]
        result.append(t)
    return result


def _make_ride_tiers(price: float, tiers: list, btc_ratio: float = None,
                     atr: float = None) -> list:
    """RIDE mode = accumulate on DIPS ONLY (Ash 2026-07-21). Trend-up accumulation.

    The OLD version straddled price ~75% below / ~25% above — the above-price band
    forced 3Commas to MARKET-BUY base on enable, so arming ride at a pump top
    slam-bought ~$18k of BTC at the high (2026-07-20). Fix: place the fraction
    ABOVE price = CURRENT holdings (btc_ratio), so a redeploy triggers NO market
    trade — not a buy, not a sell. At entry ride is armed when under-weight
    (ratio ≈ 0), so the whole grid sits BELOW price → every rung is a buy limit on
    a pullback, accumulating dips, needing zero base. As it accumulates and trails
    up, the small above-price band that appears just holds what it bought (never
    force-sold). Full width for deep-pullback room; width floor 1.2×ATR.
    """
    import copy as _copy
    _f = max(0.0, min(0.9, btc_ratio)) if (btc_ratio is not None) else 0.0
    result = []
    for tier in tiers:
        t = _copy.deepcopy(tier)
        orig_width = float(t.get("grid_high", price + 1000)) - float(t.get("grid_low", price - 1000))
        new_width  = round(orig_width, 2)                     # full width — pullback room
        if atr and atr > 0:
            new_width = max(new_width, round(1.2 * atr, 2))
        new_high   = round(price + _f * new_width, 2)         # base above = current holdings → no market trade
        new_low    = round(new_high - new_width, 2)           # rest below price → dip buys
        n          = _apply_intensive_fee_guard(t, new_width)
        new_step   = round(new_width / max(n - 1, 1), 2)
        t["grid_high"]   = new_high
        t["grid_low"]    = new_low
        t["levels"]      = n
        t["step"]        = new_step
        if t.get("min_step"):
            t["fee_ok"] = bool(new_step >= float(t["min_step"]))
        t["grid_levels"] = [round(new_low + i * new_step, 2) for i in range(n)]
        result.append(t)
    return result


def _anchor_tiers_to_walls(tiers, liquidity, atr, max_nudge_atr=0.5, persist_min=3):
    """Order-book Phase 2: in RANGE, nudge a tier's boundary onto a nearby DURABLE
    wall so the bottom/top rung sits where price is likeliest to reverse. Measured
    over 13d: durable walls hold ~61% (support) / 63% (resistance) in RANGE.

    Bounded + safe: only nudges if the wall is within max_nudge_atr×ATR of the
    boundary AND the resulting step still clears the tier's fee floor (min_step) —
    so it never widens spacing past break-even or distorts the grid. Bid wall →
    raise grid_low just above it; ask wall → lower grid_high just below it.
    Returns (tiers, n_anchored).
    """
    if not liquidity or not atr:
        return tiers, 0
    bw = liquidity.get("nearest_bid_wall")
    aw = liquidity.get("nearest_ask_wall")
    buf = atr * 0.05          # sit the rung just inside the wall
    cap = atr * max_nudge_atr
    out, n_anchored = [], 0
    for tier in tiers:
        t = dict(tier)
        lo, hi = float(t["grid_low"]), float(t["grid_high"])
        n = max(int(t.get("levels", 2) or 2), 2)
        minstep = float(t.get("min_step") or 0)
        changed = False
        if bw and int(bw.get("persistence", 0)) >= persist_min:
            target = float(bw["price"]) + buf
            if abs(target - lo) <= cap and target < hi and (hi - target) / (n - 1) >= minstep:
                lo, changed = target, True
        if aw and int(aw.get("persistence", 0)) >= persist_min:
            target = float(aw["price"]) - buf
            if abs(target - hi) <= cap and target > lo and (target - lo) / (n - 1) >= minstep:
                hi, changed = target, True
        if changed:
            n_anchored += 1
            t["grid_low"], t["grid_high"] = round(lo, 2), round(hi, 2)
            t["step"] = round((hi - lo) / (n - 1), 2)
            t["grid_levels"] = [round(lo + i * t["step"], 2) for i in range(n)]
        out.append(t)
    return out, n_anchored


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
    now = _utcnow()
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
_prev_ride_active: bool = False   # was RIDE mode active last cycle (entry detection)
_prev_weekend_mode: bool = False
# Sell-into-support protection (added 2026-06-17). SELL_ONLY mass-market-sells
# BTC; two guards stop it dumping at the worst moment:
#  (1) a fresh SELL_ONLY trigger must persist SELL_ONLY_CONFIRM_CYCLES cycles
#      before acting — btc_ratio is noisy/inflated (3Commas counts bot-locked
#      BTC), so a single spike must not liquidate; and
#  (2) never mass-sell while price sits within SUPPORT_GUARD_ATR×ATR *above* the
#      active support trendline (most likely to bounce). Holds (NORMAL) until
#      price recovers or breaks *below* support, where the guard releases and
#      capital protection resumes.
_sell_only_confirm: int = 0
SELL_ONLY_CONFIRM_CYCLES: int = 3
SUPPORT_GUARD_ATR: float = 1.0
# "Sell on the bounce, not the low" guard (added 2026-06-23). In a sharp dip the
# grid buys BTC, the ratio tops out, and SELL_ONLY would dump it right at the low.
# Instead, once confirmed, hold until price has bounced ≥ SELL_BOUNCE_ATR×ATR off
# the recent low — so we rebalance into a bounce, not the bottom. Capped at
# SELL_ONLY_MAX_WAIT cycles so a persistent overweight near the low still sells.
SELL_BOUNCE_ATR: float = 0.4
SELL_ONLY_MAX_WAIT: int = 12   # ~24 min; fire regardless after this many cycles
SELL_LOW_LOOKBACK: int = 4     # candles used for the recent-low reference
# BUY_ONLY chase re-anchor (added 2026-07-06). The old behaviour deployed the buy
# ladder ONCE on mode entry and suppressed drift — so in a rising market the
# rungs got left behind and NOTHING refilled inventory (observed: ratio bled to
# 2.5% while price rallied away from a stale ladder, inner tier $23 wide).
# Now: while BUY_ONLY is active, if price runs > BUY_CHASE_ATR×ATR above the
# deployed ladder top, re-anchor the ladder under the new price (limit orders
# only — market entries belong to key-level breaks, never the grid). Rate-limited
# to one chase per BUY_CHASE_MIN_SECS so it never churns orders cycle-to-cycle.
BUY_CHASE_ATR: float = 0.5
BUY_CHASE_MIN_SECS: int = 1800          # ≥30 min between chase redeploys
BUY_CRITICAL_RATIO: float = 0.15        # below this, ladder compresses tighter (0.45×)
_buy_chase = {"ts": 0.0}                # last chase redeploy (mutable, no global stmt)
# Falling-knife BUY brake (added 2026-07-09). slide_guard sees a sustained
# down-grind several candles before it confirms as trending_down/TREND_DOWN. In
# NORMAL mode that gap let the grid keep buying the waterfall — loading up at an
# avg cost above price, then churning rungs at a real loss (observed 07-07: same
# Mid rung cycled twice at −$18.45 each; stack accumulated at ~$63k avg while
# price slid to $62k, −$1k unrealised). Brake = when slide_guard flags DOWN and
# we're in NORMAL inventory (not underweight/BUY_ONLY where we WANT the bottom),
# pause inner+mid (stop buying the knife); outer holds; Lower Floor SmartTrade is
# the hard backstop. Releases when the grind clears (slide_guard status→clear).
_knife_brake = {"on": False}
KNIFE_BRAKE_ATR: float = 1.5            # slide_guard cum-move threshold to brake (matches its default)
# Drift stabilisation: counts consecutive cycles where drift threshold is exceeded.
# Recentre only fires after DRIFT_CONFIRM_CYCLES cycles — filters single-candle spikes.
_drift_confirm_cycles: int = 0
DRIFT_CONFIRM_CYCLES: int = 3
# Regime-aware recentre gate (added 2026-05-29, validated via backtest.py over
# the post-stabilisation window May 22–29). That window showed recentres fired
# during a trend almost never pay off: 100% of trending_down recentres and 83%
# of trending_up recentres earned <2 fills in the following hour — the grid was
# chasing a directional move it couldn't fill, cancelling resting orders for no
# churn. Suppressing all 21 trending_down recentres in that window would have
# forgone 0 fills. So: during trending_down only recentre on an extreme
# (>2x deploy width) safety-valve move; during trending_up require a wider
# threshold AND more confirmation cycles. RANGE recentring is unchanged.
# Tightened 2026-06-16: a fortnight of data showed trending recentres are STILL
# mostly duds even with the first gate (trending_up 86% dud / 14 rec, trending_down
# 80% dud / 15 rec) — the autonomous engine keeps chasing directional moves it can't
# fill. Now that RIDE MODE covers *deliberate* trend-riding (operator-armed, trails
# up), the autonomous gate should be far more conservative during trends: only
# recentre on a genuinely large, sustained move. Widened the trending thresholds.
TREND_DOWN_RECENTRE_EXTREME_MULT: float = 2.5  # trending_down: recentre only if drift exceeds this × deploy width
TREND_UP_DRIFT_MULT: float = 1.60              # trending_up: much wider drift threshold (vs 0.85 in RANGE)
TREND_UP_CONFIRM_CYCLES: int = 8               # trending_up: more consecutive confirmations (vs 3 in RANGE)
# Track the last start/stop action sent to each bot so we don't spam
# redundant enable/disable API calls every cycle.  3Commas re-places
# all grid orders on every enable call, so calling start_bot() on an
# already-running bot cancels existing orders and restarts them — fills
# can only happen within a single 2-min cycle window.
_bot_last_action: dict[int, str] = {}   # bot_id → "started" | "stopped"
_bot_action_cycle: int = 0              # counter for periodic forced re-check


def _mark_all_bots_started():
    """After a redeploy_all_bots call, mark all bots as started so _act()
    doesn't redundantly call enable on the next cycle."""
    for bot_id in GRID_BOTS:
        _bot_last_action[bot_id] = "started"


def _compute_tier_states(state, trendline, trendline_active):
    """Per-tier enabled flag plus the EXACT condition that re-enables each
    disabled tier. Mirrors the TIERED BOT DECISIONS table in run().

    Added (2026-05-29) to answer the daily-review request: "define the exact
    market condition that should re-enable Narrow/Mid, so disabled lanes do not
    rely on manual memory." Narrow=inner, Mid=mid, Wider=outer. The outer tier
    is a permanent safety net in this decision path, so it has no re-enable
    condition. Pure observability — does not influence any bot action.
    """
    atr = state.atr or 0.0
    gap = getattr(state, "gap_ratio", 0.0)
    tl  = trendline if trendline_active else None

    # Default posture: all tiers ON (RANGE / TREND_UP / mild compression).
    inner_on = mid_on = True
    inner_reason = mid_reason = "Active — normal grid trading"
    inner_reenable = mid_reenable = None
    inner_price = mid_price = None

    if state.regime == "COMPRESSION":
        inner_on = mid_on = False
        inner_reason = mid_reason = "Off — COMPRESSION (range too tight to profit after fees)"
        inner_reenable = mid_reenable = "regime exits COMPRESSION (volatility expands)"
    elif state.trending_down or state.regime == "TREND_DOWN":
        inner_on = mid_on = False
        resume = round(tl - atr, 0) if tl else None
        cond = f"gap_ratio recovers above {TRENDING_DOWN_EXIT:.1f}×ATR" + (
            f" (price > ${resume:,.0f})" if resume else "")
        inner_reason = mid_reason = f"Off — downside move (gap={gap:.2f}×ATR)"
        inner_reenable = mid_reenable = cond
        inner_price = mid_price = resume
    elif state.trending_up and state.regime not in ("RANGE", "TREND_UP"):
        # Inner off (hard run above trendline in an unconfirmed regime); mid stays on.
        inner_on = False
        resume = round(tl + (TRENDING_UP_EXIT * atr), 0) if tl else None
        inner_reason = f"Off — hard run above trendline (gap={gap:.2f}×ATR)"
        inner_reenable = f"gap_ratio falls below {TRENDING_UP_EXIT:.1f}×ATR" + (
            f" (price < ${resume:,.0f})" if resume else "")
        inner_price = resume

    return [
        {"tier": "inner", "bot": "Narrow", "enabled": inner_on,
         "reason": inner_reason, "reenable_when": inner_reenable,
         "reenable_price": inner_price},
        {"tier": "mid", "bot": "Mid", "enabled": mid_on,
         "reason": mid_reason, "reenable_when": mid_reenable,
         "reenable_price": mid_price},
        {"tier": "outer", "bot": "Wider", "enabled": True,
         "reason": "Active — permanent safety net", "reenable_when": None,
         "reenable_price": None},
    ]


def _recentre_gate_params(trending_down: bool, trending_up: bool):
    """Regime-aware recentre gate parameters.

    Returns (drift_mult, required_confirm, tag) for the drift check:
      - trending_down → 2.0× deploy width, normal confirm — a safety valve only,
        because backtest showed 100% of trending_down recentres earned <2 fills.
      - trending_up   → 1.10× width and 6 confirmation cycles — chase only
        sustained, larger drifts (83% of trending_up recentres were duds).
      - RANGE/other    → 0.85× width, 3 confirmation cycles (unchanged).

    Pure function (no globals beyond module constants) so it is unit-testable.
    """
    trending_up = trending_up and not trending_down
    if trending_down:
        return (TREND_DOWN_RECENTRE_EXTREME_MULT, DRIFT_CONFIRM_CYCLES,
                f"  [{TREND_DOWN_RECENTRE_EXTREME_MULT:.2f}x trending_down safety-valve]")
    if trending_up:
        return (TREND_UP_DRIFT_MULT, TREND_UP_CONFIRM_CYCLES,
                f"  [{TREND_UP_DRIFT_MULT:.2f}x / {TREND_UP_CONFIRM_CYCLES}cyc trending_up]")
    return (0.85, DRIFT_CONFIRM_CYCLES, "")


def _apply_sell_guards(mode, prev_mode, price, atr, trendline, btc_ratio,
                       confirm_count, recent_low=None):
    """Sell-into-support protection. Given a would-be inventory `mode`, decide
    whether SELL_ONLY (which mass-market-sells BTC) is actually allowed to fire.

    Returns (mode, confirm_count, note). Pure — no globals or IO, so it is unit
    tested directly. Guards (only SELL_ONLY is affected; BUY_ONLY/NORMAL pass
    through):
      1. Support guard — if price sits within SUPPORT_GUARD_ATR×ATR *above* the
         active support trendline, hold (NORMAL). Releases once price breaks below.
      2. Fresh-entry confirmation — a new SELL_ONLY trigger must persist
         SELL_ONLY_CONFIRM_CYCLES cycles (btc_ratio is noisy/inflated).
      3. Bounce guard — even once confirmed, don't sell while price is still on the
         recent low; wait until it has bounced ≥ SELL_BOUNCE_ATR×ATR off it, so we
         rebalance into strength, not at the bottom of a dip. Bypassed after
         SELL_ONLY_MAX_WAIT cycles so a stuck overweight still gets sold.
    """
    note = ""
    if mode != "SELL_ONLY":
        return mode, 0, note
    # Guard 1: support proximity (applies whether entering or already selling)
    if trendline and atr and 0 < (price - trendline) < SUPPORT_GUARD_ATR * atr:
        return "NORMAL", 0, (
            f"SELL suppressed — price ${price:,.0f} within {SUPPORT_GUARD_ATR:.1f}×ATR "
            f"(${SUPPORT_GUARD_ATR * atr:,.0f}) above support ${trendline:,.0f}; "
            f"holding (NORMAL) until it recovers or breaks below.")

    # Has price bounced off the recent low yet?
    bounced = (recent_low is None or atr <= 0
               or price >= recent_low + SELL_BOUNCE_ATR * atr)

    # Guards 2 + 3: fresh-entry confirmation AND a bounce — unless the overweight
    # has persisted too long, in which case fire regardless to cap risk.
    if prev_mode != "SELL_ONLY":
        confirm_count += 1
        enough = confirm_count >= SELL_ONLY_CONFIRM_CYCLES
        timed_out = confirm_count >= SELL_ONLY_MAX_WAIT
        if (enough and bounced) or timed_out:
            why = (f"max-wait {confirm_count} cycles" if timed_out
                   else f"confirmed {confirm_count} cycles + bounced off low")
            return "SELL_ONLY", confirm_count, f"SELL proceeding ({why})."
        if not enough:
            return "NORMAL", confirm_count, (
                f"SELL pending confirm {confirm_count}/{SELL_ONLY_CONFIRM_CYCLES} "
                f"(ratio {btc_ratio:.0%}) — not acting on a single spike.")
        return "NORMAL", confirm_count, (
            f"SELL holding — price ${price:,.0f} on the recent low ${recent_low:,.0f}; "
            f"waiting for a bounce (≥{SELL_BOUNCE_ATR:.1f}×ATR) so we don't sell the "
            f"bottom ({confirm_count}/{SELL_ONLY_MAX_WAIT}).")

    # Already in SELL_ONLY (staying) — keep selling unless we're sitting on the low.
    if not bounced:
        return "NORMAL", confirm_count, (
            f"SELL paused — price ${price:,.0f} on the recent low ${recent_low:,.0f}; "
            f"waiting for a bounce.")
    return "SELL_ONLY", confirm_count, note


def run():
    global _last_run_ts, _prev_regime, _prev_trending_down, _prev_inventory_mode, _prev_weekend_mode, _bot_action_cycle, _drift_confirm_cycles, _prev_ride_active, _sell_only_confirm
    now = time.time()
    if now - _last_run_ts < 100:
        print(f"Skipping — last cycle was {int(now - _last_run_ts)}s ago (min 240s between runs)")
        return
    _last_run_ts = now

    # Every 10 cycles (~20 min) clear _bot_last_action so the engine
    # re-sends start/stop commands. Catches external state changes (bot
    # auto-disabled by 3Commas, manual stop, API failure on prior cycle).
    _bot_action_cycle += 1
    if _bot_action_cycle >= 10:
        _bot_last_action.clear()
        _bot_action_cycle = 0

    print("Checking market...")

    state = EngineState()
    _bo_state  = {}      # populated in breakout section; needed in finally block
    _liquidity = None    # order-book snapshot (Phase 0); needed in finally block
    _amplitude = None    # swing-amplitude vs fee floor (Phase 0); needed in finally
    _pt_state  = None    # active price target (if any); needed in finally block
    _prox      = None    # proximity alert direction; needed in finally block
    TRENDLINE  = None    # declared early so finally block can always reference it
    _trendline_active = False
    _dca_launch_error = None   # set when a DCA launch attempt fails this cycle
                                # — picked up by log_data and /notifications
    _decision_summary = "Cycle started"
    _bot_actions = []

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
        # SWING AMPLITUDE vs FEE FLOOR (Phase 0 — observability only)
        # Push price onto the ~30-min ring and read the swing/fee-floor ratio.
        # No API call, no trading decision — feeds status + log so we can
        # calibrate lean-in/lean-out thresholds from live data.
        # ===============================
        try:
            amplitude.update(state.price)
            _amplitude = amplitude.snapshot(float(state.price))
        except Exception as _ae:
            print(f"Warning: amplitude snapshot failed: {_ae}")
            _amplitude = None

        # ===============================
        # ORDER-BOOK LIQUIDITY (Phase 0 — observability only, no decisions)
        # Read-only Coinbase L2 snapshot. Wrapped so a book outage or slow fetch
        # can NEVER interrupt the trading cycle — on any failure we simply carry
        # no liquidity data this cycle.
        # ===============================
        try:
            _liquidity = orderbook.snapshot(float(state.price), float(state.atr))
        except Exception as _obe:
            print(f"Warning: order-book snapshot failed: {_obe} — continuing without liquidity")
            _liquidity = None

        # ===============================
        # FILL CAPTURE (persist BUY/SELL fills before 3Commas wipes market_orders
        # on the next bot restart — fixes the chart's missing buy arrows). Pure
        # data capture, wrapped so it can never interrupt the cycle.
        # ===============================
        try:
            _nf = fills_capture.capture(GRID_BOTS)
            if _nf:
                print(f"  Captured {_nf} new fill(s) to fills_log.jsonl")
        except Exception as _fe:
            print(f"Warning: fill capture failed: {_fe}")

        # ===============================
        # REAL (cost-basis) P&L — the honest number, red or green. On every new
        # grid SELL, push the TRUE realised P&L vs running avg cost (not the
        # 3Commas grid-step "profit", which overstates across recentres). Pure
        # observability; wrapped so it can never interrupt the cycle.
        # ===============================
        try:
            _sells = realpnl.update(GRID_BOTS)
            if _sells:
                _msg = "\n\n".join(realpnl.format_sell_alert(e) for e in _sells)
                print("  Real P&L: %d new sell(s)\n%s" % (len(_sells), _msg))
                notify_critical(_msg)
        except Exception as _re:
            print(f"Warning: real-pnl update failed: {_re}")

        # ===============================
        # SLIDE GUARD (observability) — flags a sustained staircase grind that the
        # single-candle flash detector misses. Logs would-fire only; NEVER stops
        # bots. Wrapped so it can never interrupt the cycle.
        # ===============================
        _slide = {}   # ensure defined even if observe throws — read by the knife brake
        try:
            _slide = slide_guard.observe(price=state.price, atr=state.atr)
            if _slide.get("status") == "new":
                print("  Slide guard would-fire %s — %d consec, %.2fxATR"
                      % (_slide["direction"], _slide["consec"], _slide["cum_atr"]))
        except Exception as _se:
            print(f"Warning: slide_guard observe failed: {_se}")

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
            # Hysteresis: once in SELL_ONLY/BUY_ONLY, ratio must recover past
            # a wider exit threshold before returning to NORMAL. Prevents
            # oscillation when ratio hovers near the entry threshold — each
            # flip triggers a full redeploy (stop→intensive grid→start), so
            # rapid toggling produces tiny fills and wasted API calls.
            _sell_exit = _max_btc - 0.05   # e.g. 0.70 → exit at 0.65
            _buy_exit  = _min_btc + 0.05   # e.g. 0.35 → exit at 0.40
            if _prev_inventory_mode == "SELL_ONLY":
                # Stay in SELL_ONLY until ratio drops below exit threshold
                if state.btc_ratio > _sell_exit:
                    state.inventory_mode = "SELL_ONLY"
                else:
                    state.inventory_mode = "NORMAL"
            elif _prev_inventory_mode == "BUY_ONLY":
                # Stay in BUY_ONLY until ratio rises above exit threshold
                if state.btc_ratio < _buy_exit:
                    state.inventory_mode = "BUY_ONLY"
                else:
                    state.inventory_mode = "NORMAL"
            elif state.btc_ratio > _max_btc:
                state.inventory_mode = "SELL_ONLY"
            elif state.btc_ratio < _min_btc:
                state.inventory_mode = "BUY_ONLY"
            else:
                state.inventory_mode = "NORMAL"

            # ── Sell-into-support protection ────────────────────────────────────
            # Guards on SELL_ONLY (which mass-market-sells BTC) — see the pure
            # helper _apply_sell_guards and the module note above.
            try:
                _recent_low = (float(df["low"].tail(SELL_LOW_LOOKBACK).min())
                               if df is not None and "low" in df.columns and len(df) else None)
            except Exception:
                _recent_low = None
            state.inventory_mode, _sell_only_confirm, _sell_guard_note = _apply_sell_guards(
                state.inventory_mode, _prev_inventory_mode, state.price, state.atr,
                TRENDLINE, state.btc_ratio, _sell_only_confirm, recent_low=_recent_low)
            if _sell_guard_note:
                print(f"  {_sell_guard_note}")
            state.sell_guard = _sell_guard_note or None
            # ─────────────────────────────────────────────────────────────────────

            _exit_note = ""
            if _prev_inventory_mode == "SELL_ONLY" and state.inventory_mode == "SELL_ONLY":
                _exit_note = f", exit < {_sell_exit:.0%}"
            elif _prev_inventory_mode == "BUY_ONLY" and state.inventory_mode == "BUY_ONLY":
                _exit_note = f", exit > {_buy_exit:.0%}"
            print(f"Inventory hard stops: min_btc={_min_btc:.0%}, max_btc={_max_btc:.0%} "
                  f"(current ratio {state.btc_ratio:.0%} → {state.inventory_mode}{_exit_note})")

        # ===============================
        # GRID PARAMETERS
        # ===============================
        # Asymmetric inner-tier tilt when price is grinding up inside RANGE.
        # gap_ratio > 3.0 (trending_up) in RANGE = price well above trendline
        # but no confirmed regime break. Without tilt the inner grid is symmetric
        # and goes idle when price exits the upper boundary.
        # 0.12 shifts the inner grid up by 12% of its width (~$340 at current ATR).
        _trend_tilt = 0.12 if (state.regime == "RANGE" and state.trending_up) else 0.0

        # Per-tier capital budgets (portfolio × pct) so the grid calc can enforce
        # the min-$/fill floor by capping level density. Cheap cached read (no API
        # call). If the portfolio value is unknown the floor is skipped and levels
        # fall back to the fee-guard result — self-corrects on the next good cycle.
        _tier_budgets = None
        try:
            from threecommas import load_tier_budgets
            _snap = portfolio_snapshot()
            _pf = _snap.get("portfolio_usd", 0) if _snap else 0
            if _pf and _pf > 0:
                _tier_budgets = {b["name"]: _pf * b.get("pct", 0) / 100.0
                                 for b in load_tier_budgets()}
        except Exception as _e:
            print(f"  Warning: tier-budget calc for $/fill floor failed: {_e}")

        grid = calculate_grid_parameters(
            state.price,
            state.atr,
            state.regime,
            state.session,
            state.skew,
            df,
            trend_tilt=_trend_tilt,
            budgets=_tier_budgets,
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

        # ── Order-book Phase 2: anchor tier boundaries to durable walls ─────────
        # RANGE + NORMAL only (where walls hold ≥60% as boundaries, and the grid is
        # symmetric). Bounded & fee-safe — see _anchor_tiers_to_walls. Intensive /
        # ride modes rebuild tiers themselves, so they're excluded.
        state.wall_anchored = 0
        if state.regime == "RANGE" and state.inventory_mode == "NORMAL" and _liquidity:
            try:
                state.tiers, state.wall_anchored = _anchor_tiers_to_walls(
                    state.tiers, _liquidity, state.atr)
                if state.wall_anchored:
                    print(f"  Order-book anchor: nudged {state.wall_anchored} tier "
                          f"boundary(ies) onto durable walls (Phase 2)")
            except Exception as _wae:
                print(f"  Warning: wall-anchor failed: {_wae}")

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
                _bot_actions.append({
                    "bot": str(bot_id),
                    "action": "stop",
                    "reason": f"{label} (manual override locked stopped)",
                })
                return
            desired = "started" if should_run else "stopped"
            action = "start" if should_run else "stop"
            _bot_actions.append({
                "bot": str(bot_id),
                "action": action,
                "reason": f"{label} ({_decision_summary})",
            })
            if _bot_last_action.get(bot_id) == desired:
                # Bot is already in the desired state — skip the API call.
                # Calling enable on a running grid bot cancels and re-places
                # all orders, killing any in-progress grid cycles.
                return
            if DRY_RUN:
                print(f"[SIMULATION] Would {action} bot {bot_id} ({label})")
                _bot_last_action[bot_id] = desired
            else:
                if should_run:
                    r = start_bot(bot_id)
                    if r and r.status_code in (200, 201, 204):
                        _bot_last_action[bot_id] = desired
                    # On failure: do NOT cache — next cycle will retry
                else:
                    r = stop_bot(bot_id)
                    if r and r.status_code in (200, 201, 204):
                        _bot_last_action[bot_id] = desired

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
                        _exhaust_tiers = _make_intensive_buy_tiers(state.price, state.tiers, atr=state.atr)
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
                        _mark_all_bots_started()
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
                    # BUY_ONLY override: when BTC is critically low the inventory
                    # system is in BUY_ONLY trying to rebuild it. Pausing inner+mid
                    # for a BREAKOUT_UP would freeze the very accumulation we need
                    # (and forced a manual breakout-clear to buy). In BUY_ONLY the
                    # tiers are buy-only grids, so keeping them ON means "keep
                    # buying the dips", not "buy the spike". Override the pause.
                    if state.inventory_mode == "BUY_ONLY":
                        print(f"BREAKOUT_UP active ({_cycles} cycles) — BUY_ONLY: all "
                              f"tiers stay ON to keep accumulating BTC (pause overridden)")
                        for i, bot in enumerate(GRID_BOTS[:3]):
                            tier_name = ["inner", "mid", "outer"][i]
                            _act(bot, True, f"{tier_name} (breakout UP — BUY_ONLY accumulate)")
                    elif breakout_inner_ready(df):
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
        # RIDE MODE (manually-armed trend-up accumulation)
        # ===============================
        # When armed, overrides inventory + tiered decisions: deploy a buy-heavy
        # grid (75% buys below price / 25% light sells above), keep ALL tiers on,
        # and trail UP with price so it buys every pullback and never sells the
        # position down. Auto-disarms if price falls disarm_pct below the trailing
        # high (trend break), then falls through to normal logic. Placed AFTER
        # flash-move/breakout so those safety paths still take precedence.
        state.ride_active = False
        try:
            _ride = ride_mode.get_state()
        except Exception:
            _ride = {"armed": False}
        if _ride.get("armed"):
            ride_mode.update_trailing_high(state.price)
            _ride = ride_mode.get_state()
            if ride_mode.should_auto_disarm(state.price):
                _rm = (f"auto: ${state.price:,.0f} fell "
                       f"{_ride.get('disarm_pct', 5):.0f}% below high "
                       f"${_ride.get('trailing_high', 0):,.0f}")
                ride_mode.disarm(_rm)
                notify_critical(f"RIDE auto-disarmed at ${state.price:,.0f} — "
                                f"trend break; reverting to normal grid")
                _prev_ride_active = False
                # fall through to normal logic (no return) so the engine re-manages
                # the now-larger BTC position this same cycle
            else:
                _ride_tiers = _make_ride_tiers(state.price, state.tiers,
                                               btc_ratio=state.btc_ratio, atr=state.atr)
                _entering   = not _prev_ride_active
                _gw         = state.grid_width or 1
                _trail      = (state.center is None) or \
                              ((state.price - (state.center or state.price)) > _gw * 0.5)
                if _entering or _trail:
                    _why = "entering" if _entering else "trailing up"
                    print(f"[Ride] {_why} — buy-heavy accumulation grid at ${state.price:,.0f}")
                    notify(f"RIDE mode {_why} — buy-heavy accumulation at ${state.price:,.0f}")
                    if DRY_RUN:
                        print(f"  [SIM] Would deploy ride tiers (buy-heavy + light sells)")
                    elif _can_act():
                        _record_action()
                        redeploy_all_bots(GRID_BOTS, _ride_tiers)
                        _mark_all_bots_started()
                        update_grid_center(state.price, grid_width=state.grid_width,
                                           deployed_tiers=_ride_tiers)
                    else:
                        print(f"  Rate limit reached — ride deploy deferred to next cycle")
                for i, bot in enumerate(GRID_BOTS[:3]):
                    _act(bot, True, f"{['inner','mid','outer'][i]} (RIDE accumulate)")
                state.inventory_mode = "RIDE"
                state.ride_active = True
                _disarm_lvl = ride_mode.disarm_level(_ride) or 0
                _decision_summary = (f"RIDE: buy-heavy accumulation, trailing up "
                                     f"(disarm < ${_disarm_lvl:,.0f})")
                _prev_ride_active = True
                return   # skip normal inventory/tiered logic — status export runs in finally
        else:
            _prev_ride_active = False   # not armed — reset entry detection

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
                                    _err_str = str(_dca_err)[:300]
                                    _dca_launch_error = f"single[{new_fails}]: {_err_str}"
                                    print(f"  ERROR: DCA bot launch failed (attempt {new_fails}): {_dca_err}")
                                    # Notify on first failure and then every 10 attempts
                                    # so silent breakage surfaces on Telegram instead of
                                    # only stdout (seen: 44 failed attempts with no alert).
                                    if new_fails == 1 or new_fails % 10 == 0:
                                        notify_critical(
                                            f"DCA launch FAILED x{new_fails} '{_pt_label}': {_dca_err}"
                                        )
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
                                        _err_str = str(_e)[:300]
                                        _dca_launch_error = f"scout[{new_fails}]: {_err_str}"
                                        print(f"  ERROR: DCA scout launch failed (attempt {new_fails}): {_e}")
                                        # Notify on first failure and then every 10 attempts
                                        # so silent breakage surfaces on Telegram (previously
                                        # only the very first failure alerted).
                                        if new_fails == 1 or new_fails % 10 == 0:
                                            notify_critical(
                                                f"DCA scout launch FAILED x{new_fails} '{_pt_label}': {_e}"
                                            )
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
                                        _err_str = str(_e)[:300]
                                        _dca_launch_error = f"retest: {_err_str}"
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
                # Stop inner + mid for capital protection, but KEEP the outer
                # (Wider) bot running — its wide range keeps catching oscillation
                # like it does in TREND_DOWN, instead of parking the whole grid.
                print(f"  [Target] inner+mid off, outer running (capital protection)")
                _act(GRID_BOTS[0], False, f"target DOWN: {_pt_label}")
                _act(GRID_BOTS[1], False, f"target DOWN: {_pt_label}")
                if len(GRID_BOTS) > 2:
                    _act(GRID_BOTS[2], True, f"target DOWN: {_pt_label} (outer safety net kept on)")

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
                # Failsafe fires (breakdown ran away without a retest) carry less
                # confirmation, so size them down (default ×0.5, per-target override
                # via failsafe_size_mult). Set by price_targets._advance_support_failure.
                if _pt_state.get("sf_fire_reduced"):
                    _fs_mult = float(_pt_state.get("failsafe_size_mult", 0.5))
                    _st_sell_pct *= _fs_mult
                    print(f"  SmartTrade FAILSAFE fire (no retest) — reduced to "
                          f"{_st_sell_pct:.1f}% (×{_fs_mult})")

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
                # Downside breakout
                if state.inventory_mode == "BUY_ONLY":
                    # BUY_ONLY + BREAKOUT_DOWN: a sharp drop is an accumulation
                    # opportunity when BTC ratio is critically low.  Redeploy the
                    # intensive buy grid centred on the new lower price so orders
                    # sit below the breakout level ready to fill on any bounce or
                    # continued grind down.  TREND_DOWN regime will suppress
                    # inner+mid independently if the drop becomes a sustained move.
                    print("BREAKOUT DOWN + BUY_ONLY — redeploying intensive buy grid at new level (dip accumulation)")
                    notify(f"Breakout DOWN — BUY_ONLY mode: intensive buy grid redeployed at ${state.price:,.0f}")
                    if DRY_RUN:
                        print("[SIMULATION] Would redeploy intensive buy grid for BUY_ONLY dip accumulation")
                    elif _can_act():
                        _record_action()
                        _dip_tiers = _make_intensive_buy_tiers(state.price, state.tiers, atr=state.atr)
                        redeploy_all_bots(GRID_BOTS, _dip_tiers)
                        _mark_all_bots_started()
                        update_grid_center(state.price, grid_width=state.grid_width,
                                           deployed_tiers=_dip_tiers)
                    else:
                        # Rate limited — keep bots running on current grid rather than stopping
                        print("  Rate limit reached — keeping bots running with current grid (BUY_ONLY)")
                        for bot in GRID_BOTS:
                            _act(bot, True, "BUY_ONLY breakout down — rate limited, stay on")
                else:
                    # Normal / SELL_ONLY: stop everything — capital protection
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
        #
        # ── Regime-aware recentre gate ────────────────────────────────────
        # Threshold multiplier + confirmation cycles depend on the trend state
        # (see _recentre_gate_params): RANGE = 0.85× / 3 cycles; trending_up =
        # 1.10× / 6 cycles; trending_down = 2.0× safety-valve only. Recentring
        # during a trend almost never earns fills (backtest May 22–29:
        # trending_down 100% / trending_up 83% of recentres earned <2 fills in
        # the next hour), so we chase far less aggressively when a trend is on.
        #
        # Stabilisation: drift must be confirmed for _required_confirm
        # consecutive cycles before a recentre fires — filters single-candle
        # spikes that reverse before the next cycle.
        _trending_down_now = bool(getattr(state, "trending_down", False))
        _drift_mult, _required_confirm, _drift_tag = _recentre_gate_params(
            _trending_down_now, bool(getattr(state, "trending_up", False))
        )
        _drift_gw          = state.deploy_grid_width or state.grid_width
        _drift_threshold   = _drift_gw * _drift_mult
        print(f"  Drift check: deploy_gw=${_drift_gw:,.0f}  current_gw=${state.grid_width:,.0f}"
              f"  dist=${abs(state.price - (state.center + (state.tilt or 0))):,.0f}"
              f"  threshold=${_drift_threshold:,.0f}{_drift_tag}")
        if drift_detected(state.price, state.center, _drift_gw,
                          tilt=state.tilt or 0, threshold_mult=_drift_mult):
            _drift_confirm_cycles += 1
            state.drift_triggered = True
            if _drift_confirm_cycles < _required_confirm:
                # ── Stabilisation wait ────────────────────────────────────────
                # Don't recentre on the first cycle beyond the threshold —
                # require _required_confirm consecutive hits to confirm the
                # move is sustained, not a spike reverting next candle.
                print(f"  Drift stabilising: {_drift_confirm_cycles}/{_required_confirm} cycles "
                      f"beyond threshold — waiting for confirmation")
                # Fall through to normal tiered bot decisions on current ranges
            elif (state.inventory_mode in ("BUY_ONLY", "SELL_ONLY") or _prev_weekend_mode
                  or state.btc_ratio >= _max_btc or state.btc_ratio <= _min_btc):
                # Suppress the drift recentre when the grid is intentionally biased
                # (BUY_ONLY/SELL_ONLY/weekend) OR when we're already over/under-weight
                # (ratio at/beyond the inventory bands). The latter is what started
                # the 2026-07-17 cascade: a drift recentre fired at 86% ratio while
                # mode was still NORMAL, trading base at the day's low. When we're
                # beyond a band, a mode flip is imminent — let the bounce-guarded
                # SELL_ONLY/BUY_ONLY logic own the reposition, not a blind recentre.
                if state.inventory_mode != "NORMAL":
                    _drift_suppress_reason = f"{state.inventory_mode} intensive mode"
                elif _prev_weekend_mode:
                    _drift_suppress_reason = "weekend tight grid"
                elif state.btc_ratio >= _max_btc:
                    _drift_suppress_reason = f"over-weight ({state.btc_ratio:.0%}≥{_max_btc:.0%}) — SELL_ONLY owns reposition"
                else:
                    _drift_suppress_reason = f"under-weight ({state.btc_ratio:.0%}≤{_min_btc:.0%}) — BUY_ONLY owns reposition"
                print(f"  Drift suppressed — {_drift_suppress_reason}")
                _drift_confirm_cycles = 0
            else:
                # ── Flood-fill guard ──────────────────────────────────────────
                # Minimum gap between recentres: 45 min during trending_down
                # (outer-only, sustained move), 20 min otherwise.
                _min_redeploy_secs = 2700 if _trending_down_now else 1200
                _can_redeploy, _redeploy_wait = redeploy_allowed(
                    min_interval_secs=_min_redeploy_secs
                )
                if not _can_redeploy:
                    print(f"  Flood guard: drift detected but suppressing redeploy — "
                          f"{_redeploy_wait/60:.1f}min remaining "
                          f"(min {_min_redeploy_secs//60}min between recentres)")
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
                        if redeploy_all_bots(GRID_BOTS, state.tiers, size_base=True):
                            _mark_all_bots_started()
                            update_grid_center(state.price, grid_width=state.grid_width,
                                               deployed_tiers=state.tiers)
                    else:
                        print(f"Rate limit reached ({MAX_ACTIONS_PER_HOUR}/hr) — skipping drift redeploy")
                        print(f"  Bots remain on current ranges — center NOT advanced")

                    _drift_confirm_cycles = 0
                    return
        else:
            # Price back inside threshold — reset stabilisation counter.
            if _drift_confirm_cycles > 0:
                print(f"  Drift cleared (was {_drift_confirm_cycles} cycle(s)) — counter reset")
            _drift_confirm_cycles = 0

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
                if redeploy_all_bots(GRID_BOTS, _recovery_tiers, size_base=True):
                    _mark_all_bots_started()
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
                if redeploy_all_bots(GRID_BOTS, state.tiers, size_base=True):
                    _mark_all_bots_started()
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
                    _mark_all_bots_started()
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
            _now = _utcnow()
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
                        _mark_all_bots_started()
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
                    _mark_all_bots_started()
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
                _intensive_tiers = _make_intensive_buy_tiers(state.price, state.tiers,
                                                             atr=state.atr, btc_ratio=state.btc_ratio)
                if DRY_RUN:
                    print(f"  [SIM] Would redeploy intensive buy: "
                          f"inner {_intensive_tiers[0]['grid_low']:,.0f}–"
                          f"{_intensive_tiers[0]['grid_high']:,.0f}")
                elif _can_act():
                    _record_action()
                    redeploy_all_bots(GRID_BOTS, _intensive_tiers)
                    _mark_all_bots_started()
                    update_grid_center(state.price, grid_width=state.grid_width,
                                       deployed_tiers=_intensive_tiers)
                    _buy_chase["ts"] = time.time()
                else:
                    print(f"  Rate limit reached — intensive buy redeploy deferred to next cycle")
            else:
                # Already in BUY_ONLY — CHASE: if price has run away above the
                # deployed ladder, re-anchor it under the new price so shallow
                # dips keep refilling inventory. Limit orders only, never a
                # market buy (key-level breaks own the big entries).
                try:
                    _dep = (get_grid_state() or {}).get("deployed_tiers") or []
                    _dep_top = max((float(t.get("grid_high") or 0) for t in _dep), default=0.0)
                except Exception:
                    _dep_top = 0.0
                _gap = state.price - _dep_top if _dep_top else 0.0
                _since = time.time() - _buy_chase["ts"]
                if (_dep_top and state.atr and _gap > BUY_CHASE_ATR * state.atr
                        and _since >= BUY_CHASE_MIN_SECS):
                    print(f"  BUY_ONLY chase — price ${state.price:,.0f} is "
                          f"${_gap:,.0f} ({_gap/state.atr:.1f}×ATR) above ladder top "
                          f"${_dep_top:,.0f} — re-anchoring buy ladder")
                    _chase_tiers = _make_intensive_buy_tiers(state.price, state.tiers,
                                                             atr=state.atr, btc_ratio=state.btc_ratio)
                    if DRY_RUN:
                        print(f"  [SIM] Would chase-redeploy intensive buy under ${state.price:,.0f}")
                    elif _can_act():
                        _record_action()
                        redeploy_all_bots(GRID_BOTS, _chase_tiers)
                        _mark_all_bots_started()
                        update_grid_center(state.price, grid_width=state.grid_width,
                                           deployed_tiers=_chase_tiers)
                        _buy_chase["ts"] = time.time()
                        notify(f"BUY_ONLY chase — buy ladder re-anchored under "
                               f"${state.price:,.0f} (was topping at ${_dep_top:,.0f}); "
                               f"ratio {state.btc_ratio:.0%}, accumulating via limit orders")
                    else:
                        print(f"  Rate limit reached — chase redeploy deferred")
            print(f"BUY ONLY: ratio {state.btc_ratio:.0%} — intensive buy mode active, accumulating below price")
            # Fall through to tiered bot decisions — bots remain ON to accumulate.

        # ===============================
        # TIERED BOT DECISIONS
        # ===============================
        # Philosophy: bots stay running unless there is strong confirmed evidence
        # they are fighting the market. The outer bot (Bot 3) acts as a permanent
        # safety net through compression because its wide range can still catch
        # low-volatility oscillations.
        #
        # Tier mapping: GRID_BOTS[0]=inner  GRID_BOTS[1]=mid  GRID_BOTS[2]=outer
        #
        # State                   │ inner │  mid  │ outer │ Rationale
        # ────────────────────────┼───────┼───────┼───────┼──────────────────────────────────────
        # RANGE                   │  ON   │  ON   │  ON   │ Normal — all bots trade
        # TREND_UP                │  ON   │  ON   │  ON   │ Confirmed uptrend — all bots run for pullback fills
        # trending_up + TREND_UP  │  ON   │  ON   │  ON   │ Same — inner kept on, chop fills outweigh stop risk
        # trending_up (RANGE/etc) │  OFF  │  ON   │  ON   │ Hard run above TL in non-confirmed regime — inner off
        # TREND_DOWN              │  OFF  │  OFF  │  ON   │ Outer catches the bounce
        # trending_down           │  OFF  │  OFF  │  ON   │ Same — strong dump, wait with outer
        # COMPRESSION             │  OFF  │  OFF  │  ON   │ Outer wide enough for low-vol oscillations
        # Note: trending_up in RANGE regime = price above support, NOT a trend — all bots ON

        if state.regime == "COMPRESSION":
            _decision_summary = "COMPRESSION: inner+mid off; outer on to catch low-volatility oscillations"
            if _prev_regime != "COMPRESSION":
                notify(f"COMPRESSION — inner+mid off, outer running at ${state.price:,.0f}")
            print("COMPRESSION — inner+mid off, outer running (wide range catches low-vol oscillations)")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 2, tier_name)   # only outer (index 2) runs

        elif state.trending_down:
            # Strong downside move — inner and mid OFF, outer ON as safety net
            # Resume condition: gap_ratio > -1.0 (TRENDING_DOWN_EXIT in regime.py)
            # i.e. price must recover to trendline − 1×ATR before inner+mid restart.
            _resume_price = round(TRENDLINE - state.atr, 0)
            _decision_summary = f"Trending DOWN: gap={state.gap_ratio:.2f}x ATR; inner+mid off; outer on; resumes >${_resume_price:,.0f}"
            if not _prev_trending_down:
                notify(f"Trending DOWN (gap={state.gap_ratio:.2f}×ATR) — inner+mid off at ${state.price:,.0f}. Resumes above ${_resume_price:,.0f}")
            print(f"TRENDING DOWN (gap={state.gap_ratio:.2f}×ATR) — inner+mid off, outer holding | resume > ${_resume_price:,.0f}")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 2, tier_name)  # only outer (index 2) runs

        elif state.regime == "TREND_DOWN":
            # Confirmed TREND_DOWN (hysteresis-filtered) — same as trending_down
            _decision_summary = "TREND_DOWN: confirmed downside regime; inner+mid off; outer on"
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
            _decision_summary = f"Trending UP: gap={state.gap_ratio:.2f}x ATR in {state.regime}; inner off; mid+outer on"
            print(f"TRENDING UP (gap={state.gap_ratio:.2f}×ATR, regime={state.regime}) — inner off, mid+outer running")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 1, tier_name)  # mid (index 1) and outer (index 2) run

        elif (state.inventory_mode == "NORMAL"
              and _slide.get("direction") == "DOWN" and _slide.get("would_fire")
              and float(_slide.get("cum_atr") or 0) >= KNIFE_BRAKE_ATR):
            # FALLING-KNIFE BUY BRAKE — a sustained down-grind slide_guard caught
            # before trending_down confirms. Pause inner+mid so the grid stops
            # buying the waterfall (and stops churning underwater rungs); outer
            # holds; Lower Floor SmartTrade is the backstop. NORMAL mode only —
            # in BUY_ONLY we WANT the bottom, and SELL_ONLY already sheds.
            _consec = _slide.get("consec"); _catr = _slide.get("cum_atr")
            _decision_summary = (f"KNIFE BRAKE: down-grind {_consec}c/{_catr}×ATR "
                                 f"— inner+mid paused (stop buying the knife)")
            if not _knife_brake["on"]:
                notify(f"Knife brake — sustained down-grind ({_consec} candles, "
                       f"{_catr}×ATR) at ${state.price:,.0f}. Inner+mid paused to stop "
                       f"buying the drop; outer holds; Lower Floor SmartTrade is the backstop.")
            _knife_brake["on"] = True
            print(f"KNIFE BRAKE — inner+mid off (slide DOWN {_consec}c/{_catr}×ATR)")
            for i, bot in enumerate(GRID_BOTS):
                tier_name = ["inner", "mid", "outer"][i] if i < 3 else f"bot{i}"
                _act(bot, i >= 2, tier_name)   # only outer runs

        else:
            # RANGE or TREND_UP — all bots run
            if _prev_trending_down:
                notify(f"Trending DOWN cleared — inner+mid back online at ${state.price:,.0f}")
            if _knife_brake["on"]:
                notify(f"Knife brake released — down-grind stabilised at ${state.price:,.0f}; "
                       f"inner+mid back online.")
            _knife_brake["on"] = False
            if state.regime == "TREND_UP":
                _decision_summary = "TREND_UP: all bots on for pullback fills"
                print("TREND_UP — all bots running")
            elif state.compression:
                _decision_summary = "Mild compression: compression not confirmed; all bots on"
                print("Mild compression — all bots running (compression not confirmed)")
            else:
                _decision_summary = "RANGE: all bots on for normal grid trading"
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
                "sell_guard":     getattr(state, "sell_guard", None),
                "compression":    bool(state.compression),
                "trending_up":    bool(getattr(state, "trending_up",   False)),
                "trending_down":  bool(getattr(state, "trending_down",  False)),
                "gap_ratio":      round(getattr(state, "gap_ratio", 0.0), 3),
                "dry_run":        DRY_RUN,
                "tiers":          state.tiers,
                "decision_summary": _decision_summary,
                "bot_actions":     _bot_actions,
                "tier_states":     _compute_tier_states(state, TRENDLINE, _trendline_active),
                # Order-book liquidity (Phase 0 — observability only)
                "liquidity":       _liquidity,
                # Order-book Phase 2: tier boundaries nudged onto durable walls this cycle
                "wall_anchored":   int(getattr(state, "wall_anchored", 0)),
                # Swing amplitude vs fee floor (Phase 0 — observability only)
                "grid_amplitude":  _amplitude,
                # Ride mode (manually-armed trend-up accumulation)
                "ride_mode":       ride_mode.get_state(),
                "ride_active":     bool(getattr(state, "ride_active", False)),
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
                "dca_launch_error":     _dca_launch_error,
                # Support-failure targets — surface sf_phase so a stuck/BROKEN
                # target is visible on the dashboard instead of silently dead.
                "support_targets":      get_support_failure_status(),
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

_consec_cycle_errors = 0


def safe_run():
    """Run one cycle, but NEVER let an exception escape and kill the engine loop.

    History: on 2026-06-27 a transient Coinbase API error ("INTERNAL / Something
    went wrong") raised out of get_btc_data() — which sits above the cycle's first
    try/except — propagated out of the scheduled job, and killed the engine
    process for ~24h (the grid kept trading on 3Commas, but Griddy managed
    nothing). A single bad API response must only cost ONE skipped cycle."""
    global _consec_cycle_errors
    try:
        run()
        _consec_cycle_errors = 0
    except Exception as _cycle_err:  # noqa: BLE001
        _consec_cycle_errors += 1
        import traceback
        print(f"CYCLE ERROR #{_consec_cycle_errors} (engine stays alive, "
              f"retrying next cycle): {_cycle_err}")
        traceback.print_exc()
        # One transient blip is normal and self-heals next cycle; only alert if
        # the engine genuinely can't complete cycles for several in a row.
        if _consec_cycle_errors == 3:
            try:
                notify_critical(
                    f"Griddy engine: 3 consecutive cycle failures "
                    f"({type(_cycle_err).__name__}: {_cycle_err}). Still retrying "
                    f"every 2 min — check if it persists.")
            except Exception:
                pass


if __name__ == "__main__":
    schedule.every(2).minutes.do(safe_run)

    # Run once immediately on startup (wrapped so a transient boot error can't
    # stop the loop from starting).
    safe_run()

    print("Engine running...")

    try:
        while True:
            try:
                schedule.run_pending()
            except Exception as _loop_err:  # belt-and-braces: never exit the loop
                import traceback
                print(f"LOOP ERROR (engine stays alive): {_loop_err}")
                traceback.print_exc()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nEngine stopped safely.")
