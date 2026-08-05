"""Trend-up RIDE MODE — manually-armed accumulation state machine.

When the operator has conviction a trend will continue (e.g. confirmed macro
uptrend) they ARM ride mode. While armed, the engine shifts from "mean-revert +
sell-to-target" to ACCUMULATE: buy-heavy grids that buy every pullback, trail UP
with price (never down), keep only light sells to bank spikes, and suppress the
inventory system's forced selling so accumulated BTC is held.

It DISARMS automatically if price falls `disarm_pct` below the trailing high (the
trend has likely broken) — or manually any time. On disarm the engine reverts to
normal, which then manages the now-larger BTC position.

This module is the pure state machine only. The engine reads get_state() each
cycle and, while armed, alters deployment. State persists in ride_mode.json so an
armed ride survives a restart. Nothing here trades.
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "ride_mode.json")

# Auto-disarm if price falls this % below the trailing high. Default 3% (tight —
# exits quickly on a reversal); override per-arm via arm(price, disarm_pct=...)
# or POST /ride/arm {"disarm_pct": N}.
DEFAULT_DISARM_PCT = 3.0


def _load() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"armed": False}


def _save(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: could not write ride_mode.json: {e}")


def get_state() -> dict:
    s = _load()
    s.setdefault("armed", False)
    return s


def is_armed() -> bool:
    return bool(_load().get("armed"))


def arm(price: float, disarm_pct: float = DEFAULT_DISARM_PCT) -> dict:
    """Arm ride mode at the current price."""
    s = {
        "armed": True,
        "deployed": False,
        "armed_at": time.time(),
        "armed_price": float(price),
        "trailing_high": float(price),
        "disarm_pct": float(disarm_pct),
        "disarmed_reason": None,
        "disarmed_at": None,
    }
    _save(s)
    print(f"[Ride] ARMED at ${price:,.0f} (auto-disarm {disarm_pct:.1f}% below high)")
    return s


def mark_deployed() -> dict:
    """Persist that the ride grid has been deployed for this arm session — so an
    engine RESTART does not re-run the entry redeploy (2026-08-05: every deploy
    restart re-triggered '[Ride] entering' + a full redeploy because the flag
    lived in process memory)."""
    s = _load()
    s["deployed"] = True
    _save(s)
    return s


def disarm(reason: str = "manual") -> dict:
    s = _load()
    if not s.get("armed"):
        s["armed"] = False
        return s
    s["armed"] = False
    s["disarmed_reason"] = reason
    s["disarmed_at"] = time.time()
    _save(s)
    print(f"[Ride] DISARMED ({reason})")
    return s


def update_trailing_high(price: float) -> dict:
    """Ratchet the trailing high up while armed (never down)."""
    s = _load()
    if s.get("armed") and float(price) > float(s.get("trailing_high") or 0):
        s["trailing_high"] = float(price)
        _save(s)
    return s


def disarm_level(state: dict | None = None) -> float | None:
    """Price at/below which ride mode auto-disarms, or None if not armed."""
    s = state or _load()
    if not s.get("armed"):
        return None
    th = float(s.get("trailing_high") or s.get("armed_price") or 0)
    pct = float(s.get("disarm_pct", DEFAULT_DISARM_PCT))
    return th * (1.0 - pct / 100.0) if th else None


def should_auto_disarm(price: float, state: dict | None = None) -> bool:
    """True if armed AND price has fallen disarm_pct below the trailing high."""
    s = state or _load()
    if not s.get("armed"):
        return False
    lvl = disarm_level(s)
    return lvl is not None and float(price) <= lvl
