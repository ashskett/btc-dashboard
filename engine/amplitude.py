"""Realized swing amplitude vs the fee floor — observability gate (Phase 0).

The research (fills_research.py) showed the grid harvests well only when the
realized short-term swing clears the round-trip fee floor (~0.6%): that happened
often at the volatile range lows and rarely during the compressed highs. This
module turns that into a LIVE, measured signal so we can calibrate lean-in /
lean-out thresholds before any trading change.

Each cycle the engine pushes the current price; we keep a ~30-minute ring (12
cycles at ~2.5 min) and report the high–low swing over it as a % of price, the
fee floor as a %, and their ratio. ratio ≥ 1 means swings clear the floor (grid
can profit); ratio < 1 means a sub-floor grind (grid churns for fees).

OBSERVABILITY ONLY — nothing here changes trading. It feeds status.grid_amplitude
and the engine log so thresholds can be set from real data. The provisional
rich/ok/thin bands are placeholders to be calibrated, NOT decision points yet.

Method matches the research deliberately (close-to-close over the ring) so live
numbers are comparable to the 85-day study. Note: the ring is in-memory, so for
~30 min after an engine restart the signal reads 'warming' until it refills.
"""
from collections import deque

from grid_logic import ROUND_TRIP_FEE, FEE_BUFFER

WINDOW = 12   # cycles in the swing window (~30 min at ~2.5 min/cycle)
_MIN_SAMPLES = 6

# Fee floor as a % — the minimum round-trip move the grid needs to profit.
# Kept in sync with grid_logic's fee guard (min_step = price × ROUND_TRIP_FEE ×
# FEE_BUFFER), so it tracks any change there automatically.
FEE_FLOOR_PCT = ROUND_TRIP_FEE * FEE_BUFFER * 100.0

# Provisional bands (TO BE CALIBRATED from live data — not yet used for any
# decision). ratio = realized swing % / fee floor %.
RICH_RATIO = 1.3   # comfortably above the floor → candidate "lean in"
THIN_RATIO = 1.0   # below the floor → candidate "lean out"

_prices: deque = deque(maxlen=WINDOW)


def reset():
    """Clear the ring (tests)."""
    _prices.clear()


def update(price):
    """Push the current cycle price onto the ring."""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return
    if p > 0:
        _prices.append(p)


def snapshot(price):
    """Return the current amplitude/fee-floor reading. Observability only."""
    n = len(_prices)
    base = {
        "fee_floor_pct": round(FEE_FLOOR_PCT, 3),
        "window": WINDOW,
        "samples": n,
    }
    if n < _MIN_SAMPLES or not price:
        base.update({"swing_pct": None, "ratio": None, "band": "warming"})
        return base
    hi, lo = max(_prices), min(_prices)
    swing = (hi - lo) / float(price) * 100.0
    ratio = swing / FEE_FLOOR_PCT if FEE_FLOOR_PCT else None
    if ratio is None:
        band = "unknown"
    elif ratio >= RICH_RATIO:
        band = "rich"     # swings clear the floor comfortably — grid in its element
    elif ratio >= THIN_RATIO:
        band = "ok"
    else:
        band = "thin"     # sub-floor grind — grid churns for fees
    base.update({
        "swing_pct": round(swing, 3),
        "ratio": round(ratio, 2) if ratio is not None else None,
        "band": band,
    })
    return base
