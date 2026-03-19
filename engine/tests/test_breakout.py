"""
Tests for breakout.py — momentum and volatility-spike detection.

Key scenarios:
  • Momentum fires after N consecutive closes + sufficient move
  • Momentum does NOT fire on insufficient move (even with N-close streak)
  • Momentum does NOT fire on insufficient streak (even with large move)
  • Volatility spike fires when ATR > baseline × mult
  • TREND_UP regime guard suppresses UP signals
  • Post-exhaustion cooldown suppresses re-fire after DOWN clears
"""
import json
import time
import pytest
import breakout as bo


# ── Helpers ────────────────────────────────────────────────────────────────

def _reset_state(tmp_path, monkeypatch):
    state_file = str(tmp_path / "breakout_state.json")
    monkeypatch.setattr(bo, "_STATE_FILE", state_file)
    _clear(state_file)
    return state_file


def _clear(state_file):
    json.dump(
        {"consec_up": 0, "consec_down": 0, "active": None,
         "fire_price": None, "cycles_active": 0},
        open(state_file, "w"),
    )


def _make_momentum_up_df(atr=600.0, n=50, streak=4, move_mult=4.0, custom_closes=None):
    """
    Build a df where the last `streak` closes are consecutive UP moves.

    The engine measures move_N_up as df.close.iloc[-1] - df.close.iloc[-STREAK],
    i.e. from the FIRST bar of the streak to the LAST.  To get that move to equal
    atr × move_mult, we need `streak` steps from the pre-streak flat level:
    - iloc[-streak-1] = flat base (last flat bar, excluded from measurement)
    - iloc[-streak]   = base + step  ← measurement starts here
    - ...
    - iloc[-1]        = base + streak * step  ← measurement ends here
    move_N_up = streak * step = atr * move_mult  ✓
    """
    import pandas as pd
    base = 70000.0
    if custom_closes is not None:
        closes = custom_closes
    else:
        # move_N_up = df.close.iloc[-1] - df.close.iloc[-streak]
        # With closes = [base + i*step for i in range(streak)]:
        #   iloc[-streak] = base, iloc[-1] = base + (streak-1)*step
        #   move = (streak-1)*step  →  step = atr*move_mult/(streak-1)
        step = atr * move_mult / (streak - 1)
        closes = [base] * (n - streak) + [base + i * step for i in range(streak)]
    return pd.DataFrame(
        {
            "open":     closes,
            "high":     [c + atr * 0.3 for c in closes],
            "low":      [c - atr * 0.3 for c in closes],
            "close":    closes,
            "volume":   [1000.0] * len(closes),
            "atr":      [atr] * len(closes),
            "bb_width": [0.02] * len(closes),
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="1h"),
    )


def _make_momentum_down_df(atr=600.0, n=50, streak=4, move_mult=4.0):
    import pandas as pd
    base = 70000.0
    step = atr * move_mult / (streak - 1)
    closes = [base] * (n - streak) + [base - i * step for i in range(streak)]
    return pd.DataFrame(
        {
            "open":     closes,
            "high":     [c + atr * 0.3 for c in closes],
            "low":      [c - atr * 0.3 for c in closes],
            "close":    closes,
            "volume":   [1000.0] * len(closes),
            "atr":      [atr] * len(closes),
            "bb_width": [0.02] * len(closes),
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="1h"),
    )


def _make_flat_df(price=70000.0, atr=600.0, n=50):
    import pandas as pd
    closes = [price] * n
    return pd.DataFrame(
        {
            "open":     closes,
            "high":     [c + atr * 0.3 for c in closes],
            "low":      [c - atr * 0.3 for c in closes],
            "close":    closes,
            "volume":   [1000.0] * n,
            "atr":      [atr] * n,
            "bb_width": [0.02] * n,
        },
        index=pd.date_range("2026-01-01", periods=n, freq="1h"),
    )


# ── Momentum layer ─────────────────────────────────────────────────────────

class TestMomentumDetection:
    def test_fires_on_sufficient_streak_and_move(self, tmp_path, monkeypatch):
        """
        The momentum counter accumulates across engine cycles (calls to
        breakout_detected).  Pre-seed the state with streak-1 cycles already
        counted, then make the final call that tips it over the threshold.
        The DataFrame must show the total move over MOMENTUM_STREAK bars ≥ min_move.
        """
        state_file = _reset_state(tmp_path, monkeypatch)
        monkeypatch.setattr(bo, "SWEEP_GUARD_ENABLED", False)
        atr = 600.0
        # Pre-seed: MOMENTUM_STREAK - 1 consecutive up-closes already accumulated
        json.dump(
            {"consec_up": bo.MOMENTUM_STREAK - 1, "consec_down": 0,
             "active": None, "fire_price": None, "cycles_active": 0},
            open(state_file, "w"),
        )
        # DataFrame: last bar is UP, total move over last N bars > MOMENTUM_ATR_MULT × ATR
        df = _make_momentum_up_df(
            atr=atr, streak=bo.MOMENTUM_STREAK,
            move_mult=bo.MOMENTUM_ATR_MULT + 0.5
        )
        result = bo.breakout_detected(df)
        assert result == "UP"

    def test_does_not_fire_on_insufficient_move(self, tmp_path, monkeypatch):
        """Streak is at threshold but total move is only 1.0×ATR — below mult."""
        state_file = _reset_state(tmp_path, monkeypatch)
        monkeypatch.setattr(bo, "SWEEP_GUARD_ENABLED", False)
        atr = 600.0
        json.dump(
            {"consec_up": bo.MOMENTUM_STREAK - 1, "consec_down": 0,
             "active": None, "fire_price": None, "cycles_active": 0},
            open(state_file, "w"),
        )
        df = _make_momentum_up_df(atr=atr, streak=bo.MOMENTUM_STREAK, move_mult=1.0)
        result = bo.breakout_detected(df)
        assert result is None

    def test_does_not_fire_on_insufficient_streak(self, tmp_path, monkeypatch):
        """State has streak-2 accumulated; one UP close brings it to streak-1 — not enough."""
        state_file = _reset_state(tmp_path, monkeypatch)
        monkeypatch.setattr(bo, "SWEEP_GUARD_ENABLED", False)
        atr = 600.0
        json.dump(
            {"consec_up": bo.MOMENTUM_STREAK - 2, "consec_down": 0,
             "active": None, "fire_price": None, "cycles_active": 0},
            open(state_file, "w"),
        )
        df = _make_momentum_up_df(
            atr=atr, streak=bo.MOMENTUM_STREAK,
            move_mult=bo.MOMENTUM_ATR_MULT + 1.0
        )
        result = bo.breakout_detected(df)
        assert result is None

    def test_momentum_down_fires(self, tmp_path, monkeypatch):
        state_file = _reset_state(tmp_path, monkeypatch)
        monkeypatch.setattr(bo, "SWEEP_GUARD_ENABLED", False)
        atr = 600.0
        json.dump(
            {"consec_up": 0, "consec_down": bo.MOMENTUM_STREAK - 1,
             "active": None, "fire_price": None, "cycles_active": 0},
            open(state_file, "w"),
        )
        df = _make_momentum_down_df(
            atr=atr, streak=bo.MOMENTUM_STREAK,
            move_mult=bo.MOMENTUM_ATR_MULT + 0.5
        )
        result = bo.breakout_detected(df)
        assert result == "DOWN"

    def test_does_not_refire_while_active(self, tmp_path, monkeypatch):
        """Once a breakout is active, detection should not return another signal."""
        state_file = _reset_state(tmp_path, monkeypatch)
        monkeypatch.setattr(bo, "SWEEP_GUARD_ENABLED", False)
        # Manually set active state
        json.dump(
            {"consec_up": 5, "consec_down": 0, "active": "UP",
             "fire_price": 72000.0, "cycles_active": 1, "fired_at": time.time()},
            open(state_file, "w"),
        )
        df = _make_momentum_up_df(atr=600.0, streak=bo.MOMENTUM_STREAK, move_mult=bo.MOMENTUM_ATR_MULT + 1.0)
        result = bo.breakout_detected(df)
        assert result is None


# ── Regime guard ───────────────────────────────────────────────────────────

class TestRegimeGuard:
    def test_trend_up_suppresses_momentum_up(self, tmp_path, monkeypatch):
        _reset_state(tmp_path, monkeypatch)
        monkeypatch.setattr(bo, "SWEEP_GUARD_ENABLED", False)
        df = _make_momentum_up_df(atr=600.0, streak=bo.MOMENTUM_STREAK, move_mult=bo.MOMENTUM_ATR_MULT + 0.5)
        result = bo.breakout_detected(df, regime="TREND_UP")
        assert result is None, "UP signal must be suppressed in TREND_UP regime"


# ── Post-exhaustion cooldown ───────────────────────────────────────────────

class TestPostExhaustionCooldown:
    def test_down_cooldown_suppresses_momentum_down_refire(self, tmp_path, monkeypatch):
        """
        After a DOWN breakout clears, momentum DOWN should be suppressed for
        POST_CLEAR_COOLDOWN_DOWN seconds.  This prevents the re-fire loop seen
        in the Mar 18 incident (bots stopped 3h in a clean RANGE market).
        """
        state_file = _reset_state(tmp_path, monkeypatch)
        monkeypatch.setattr(bo, "SWEEP_GUARD_ENABLED", False)
        # Simulate a freshly cleared DOWN breakout (cleared_at = now)
        json.dump(
            {"consec_up": 0, "consec_down": 0, "active": None,
             "fire_price": None, "cycles_active": 0,
             "cleared_at": time.time(), "cleared_direction": "DOWN"},
            open(state_file, "w"),
        )
        df = _make_momentum_down_df(atr=600.0, streak=bo.MOMENTUM_STREAK, move_mult=bo.MOMENTUM_ATR_MULT + 0.5)
        result = bo.breakout_detected(df)
        assert result is None, "DOWN re-fire must be suppressed within cooldown window"
