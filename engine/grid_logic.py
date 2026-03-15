import json
import os

from liquidity import find_liquidity_levels, generate_liquidity_grid

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grid_state.json")


def calculate_grid_width(atr):
    return atr * 3


# ── Fee constants ─────────────────────────────────────────────────────────────
# Coinbase Advanced Trade fees from transaction history:
#   Maker (limit orders resting): 0.10%
#   Taker (crossing the spread):  0.20%
# Grid bot orders are limit orders but fills are taker events.
# Use conservative round-trip (buy taker + sell taker) = 0.40%
TAKER_FEE       = 0.0020   # per leg
ROUND_TRIP_FEE  = TAKER_FEE * 2   # 0.40% total — conservative
FEE_BUFFER      = 1.5      # safety multiplier: step must be 1.5× the break-even minimum
# So effective minimum step = price × 0.40% × 1.5 = price × 0.60%
# At $70,000 that's $420 minimum step

# ── Tier definitions ─────────────────────────────────────────────────────────
# Each tier is a multiplier on grid_width (ATR×3).
# Inner bot catches chop, mid catches normal swings, outer catches extensions.
TIERS = [
    {"name": "inner", "range_mult": 0.75, "base_levels": 10},
    {"name": "mid",   "range_mult": 1.5,  "base_levels": 6},   # was 8 — reduced to keep step ~30% above fee floor
    {"name": "outer", "range_mult": 3.0,  "base_levels": 6},
]


def _build_tier(price, atr, regime, session, skew, df, support, resistance,
                range_mult, base_levels, compression):
    """Build grid parameters for a single tier."""
    # Weekend ATR floor: quiet Sat/Sun can push ATR so low that the fee guard
    # collapses inner/mid to 2 levels with $500+ steps — no fills possible.
    # Floor at 1% of price (≈$840 at $84k) so there is always enough range
    # for at least 4–5 meaningful levels across the tiers.
    if session.startswith("WKD_"):
        atr = max(atr, price * 0.010)

    grid_width = calculate_grid_width(atr) * range_mult

    # Inventory tilt — proportional to this tier's range
    tilt_strength = 0.5
    tilt = skew * tilt_strength * grid_width

    # Regime bias — asymmetric range around price
    if regime == "TREND_DOWN":
        grid_low  = price - (grid_width * 1.5) - tilt
        grid_high = price + (grid_width * 0.5) - tilt
    elif regime == "TREND_UP":
        grid_low  = price - (grid_width * 0.5) - tilt
        grid_high = price + (grid_width * 1.5) - tilt
    else:
        grid_low  = price - grid_width - tilt
        grid_high = price + grid_width - tilt

    # Level count — compression and session adjustments
    levels = base_levels
    if compression:
        levels = int(levels * 1.5)   # denser grids when vol is squeezed
    if session in ("ASIA", "WKD_ASIA"):
        levels += 2
    elif session == "US":
        levels -= 2
    # WKD_US: no penalty — weekend US is already thin; don't reduce density further
    levels = max(levels, 6)

    step = (grid_high - grid_low) / levels

    # ── Fee guard ─────────────────────────────────────────────────────────────
    # Each completed grid cycle = buy + sell. Both are taker fills.
    # Step must exceed round-trip fees × safety buffer to guarantee profit.
    min_step = price * ROUND_TRIP_FEE * FEE_BUFFER
    if step < min_step:
        # Reduce level count until step is profitable, keeping range fixed
        max_levels = int((grid_high - grid_low) / min_step)
        max_levels = max(max_levels, 2)   # always keep at least 2 levels
        if max_levels < levels:
            levels = max_levels
            step   = (grid_high - grid_low) / levels
    # ─────────────────────────────────────────────────────────────────────────

    grid_levels = generate_liquidity_grid(
        price, grid_low, grid_high, levels, support, resistance
    )

    return {
        "center":     price,
        "grid_low":   round(grid_low,  2),
        "grid_high":  round(grid_high, 2),
        "levels":     levels,
        "step":       round(step, 2),
        "grid_width": round(grid_width, 2),
        "tilt":       round(tilt, 2),
        "grid_levels": grid_levels,
        "min_step":   round(price * ROUND_TRIP_FEE * FEE_BUFFER, 2),
        "fee_ok":     bool(step >= price * ROUND_TRIP_FEE * FEE_BUFFER),
    }


def calculate_grid_parameters(price, atr, regime, session, skew, df):
    """Returns a single (mid-tier) grid for backwards compatibility,
    plus a 'tiers' key with all three bot grids."""

    volatility_ratio = atr / price
    compression = bool(volatility_ratio < 0.005)

    support, resistance = find_liquidity_levels(df)

    tiers = []
    for t in TIERS:
        tier_grid = _build_tier(
            price, atr, regime, session, skew, df,
            support, resistance,
            range_mult=t["range_mult"],
            base_levels=t["base_levels"],
            compression=compression,
        )
        tier_grid["name"] = t["name"]
        tiers.append(tier_grid)

    # Mid tier (index 1) is the "main" grid used for dashboard/drift detection
    mid = tiers[1]

    return {
        # Single-grid fields (used by engine state, dashboard, drift detection)
        "center":     mid["center"],
        "grid_low":   mid["grid_low"],
        "grid_high":  mid["grid_high"],
        "levels":     mid["levels"],
        "step":       mid["step"],
        "grid_width": mid["grid_width"],
        "tilt":       mid["tilt"],
        "compression": compression,
        "grid_levels": mid["grid_levels"],
        "support":    support,
        "resistance": resistance,
        # Per-bot tier grids
        "tiers":      tiers,
    }


def get_grid_center():

    if not os.path.exists(STATE_FILE):

        center = 68000

        with open(STATE_FILE, "w") as f:
            json.dump({"grid_center": center}, f)

        return center

    with open(STATE_FILE) as f:
        data = json.load(f)

    return data["grid_center"]


def update_grid_center(price):

    with open(STATE_FILE, "w") as f:
        json.dump({"grid_center": price}, f)


def drift_detected(price, center, grid_width, tilt=0):
    """
    Check if price has drifted beyond the threshold from the tilt-adjusted
    grid center. Without accounting for tilt, drift triggers asymmetrically
    when inventory is skewed.
    """
    adjusted_center = center + tilt
    drift = abs(price - adjusted_center)
    threshold = grid_width * 0.75
    return drift > threshold