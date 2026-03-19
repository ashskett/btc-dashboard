"""
Tests for liquidity.py — support/resistance detection and grid adjustment.

find_liquidity_levels(df, lookback=50):
    Returns (support, resistance) = (min of lows, max of highs) over last `lookback` rows.

generate_liquidity_grid(price, grid_low, grid_high, levels, support, resistance):
    Returns a list of `levels + 1` prices, evenly spaced, with small biases:
      - Levels within 25% of grid width from resistance: shifted up   ×1.0002
      - Levels within 25% of grid width from support:    shifted down ×0.9998
"""
import numpy as np
import pandas as pd
import pytest
import liquidity as liq


# ── Fixtures ───────────────────────────────────────────────────────────────

def _make_df(n=60, price=70000.0, atr=600.0):
    """Build a minimal OHLCV df with uniform highs/lows."""
    closes = [price] * n
    return pd.DataFrame(
        {
            "open":   closes,
            "high":   [c + atr * 0.3 for c in closes],
            "low":    [c - atr * 0.3 for c in closes],
            "close":  closes,
            "volume": [1000.0] * n,
        },
        index=pd.date_range("2026-01-01", periods=n, freq="1h"),
    )


# ── find_liquidity_levels ──────────────────────────────────────────────────

class TestFindLiquidityLevels:
    def test_returns_tuple_of_two(self):
        df = _make_df()
        result = liq.find_liquidity_levels(df)
        assert isinstance(result, tuple) and len(result) == 2

    def test_support_is_minimum_low(self):
        df = _make_df(n=60, price=70000, atr=600)
        support, _ = liq.find_liquidity_levels(df)
        expected = (70000 - 600 * 0.3)
        assert abs(support - expected) < 0.01

    def test_resistance_is_maximum_high(self):
        df = _make_df(n=60, price=70000, atr=600)
        _, resistance = liq.find_liquidity_levels(df)
        expected = (70000 + 600 * 0.3)
        assert abs(resistance - expected) < 0.01

    def test_support_less_than_resistance(self):
        df = _make_df()
        support, resistance = liq.find_liquidity_levels(df)
        assert support < resistance

    def test_lookback_limits_rows_used(self):
        """Only the last `lookback` rows should matter."""
        n = 100
        closes = [70000.0] * n
        # Build df where the first 50 rows have extreme highs/lows
        df = pd.DataFrame(
            {
                "open":   closes,
                "high":   [80000.0] * 50 + [70180.0] * 50,
                "low":    [60000.0] * 50 + [69820.0] * 50,
                "close":  closes,
                "volume": [1000.0] * n,
            },
            index=pd.date_range("2026-01-01", periods=n, freq="1h"),
        )
        # lookback=50 → should only see the last 50 rows (no extremes)
        support, resistance = liq.find_liquidity_levels(df, lookback=50)
        assert resistance < 72000   # extreme 80k not in lookback
        assert support > 68000     # extreme 60k not in lookback

    def test_default_lookback_is_50(self):
        """find_liquidity_levels with default lookback uses 50 rows."""
        n = 100
        closes = [70000.0] * n
        df = pd.DataFrame(
            {
                "open":   closes,
                "high":   [80000.0] * 50 + [70180.0] * 50,
                "low":    [60000.0] * 50 + [69820.0] * 50,
                "close":  closes,
                "volume": [1000.0] * n,
            },
            index=pd.date_range("2026-01-01", periods=n, freq="1h"),
        )
        # Default is 50 — same as explicit lookback=50 above
        support, resistance = liq.find_liquidity_levels(df)
        assert resistance < 72000
        assert support > 68000

    def test_fewer_rows_than_lookback(self):
        """Works when df has fewer rows than lookback."""
        df = _make_df(n=10)
        support, resistance = liq.find_liquidity_levels(df, lookback=50)
        assert support < resistance


# ── generate_liquidity_grid ────────────────────────────────────────────────

class TestGenerateLiquidityGrid:
    def _grid(self, price=70000, grid_low=68000, grid_high=72000, levels=10,
              support=67000, resistance=73000):
        return liq.generate_liquidity_grid(
            price, grid_low, grid_high, levels, support, resistance
        )

    def test_returns_list(self):
        result = self._grid()
        assert isinstance(result, list)

    def test_length_is_levels_plus_one(self):
        """linspace(low, high, levels+1) → levels+1 values."""
        result = self._grid(levels=10)
        assert len(result) == 11

    def test_levels_5(self):
        result = self._grid(levels=5)
        assert len(result) == 6

    def test_all_values_are_floats(self):
        for v in self._grid():
            assert isinstance(v, float)

    def test_values_in_grid_range_with_bias(self):
        """All values should stay close to [grid_low, grid_high] (bias is ±0.02%)."""
        gl, gh = 68000, 72000
        result = self._grid(grid_low=gl, grid_high=gh)
        for v in result:
            # ±0.02% bias on a $72k level = ±$14.4 — allow a generous margin
            assert gl * 0.999 <= v <= gh * 1.001

    def test_no_bias_applied_far_from_liquidity(self):
        """Levels far from support and resistance have no bias (level unchanged)."""
        # Support/resistance far outside grid — no level will be within 25% zone
        grid_low, grid_high = 69000, 71000
        support, resistance = 50000, 90000   # way outside

        # linspace gives evenly spaced levels; none within 25% of grid width from S/R
        raw = list(np.linspace(grid_low, grid_high, 11))
        result = liq.generate_liquidity_grid(
            70000, grid_low, grid_high, 10, support, resistance
        )
        for raw_v, adj_v in zip(raw, result):
            assert raw_v == adj_v  # no bias applied

    def test_resistance_bias_shifts_level_up(self):
        """
        A level near resistance (within 25% of grid width) gets multiplied by 1.0002.
        Grid: 69000–71000 (width=2000). Resistance zone = within 2000*0.25=500 of resistance.
        Put resistance at 71200 → level at 71000 is 200 from resistance (within 500).
        """
        grid_low, grid_high = 69000, 71000
        resistance = 71200    # 200 above grid_high; level at 71000 is within zone
        support = 60000       # far below

        raw = list(np.linspace(grid_low, grid_high, 11))
        result = liq.generate_liquidity_grid(
            70000, grid_low, grid_high, 10, support, resistance
        )

        # Find a level that was close to resistance
        any_biased_up = any(r > raw_v for r, raw_v in zip(result, raw))
        assert any_biased_up, "Expected at least one level to be shifted upward near resistance"

    def test_support_bias_shifts_level_down(self):
        """A level near support gets multiplied by 0.9998 (shifted down)."""
        grid_low, grid_high = 69000, 71000
        support = 68800    # 200 below grid_low; level at 69000 is within zone
        resistance = 90000

        raw = list(np.linspace(grid_low, grid_high, 11))
        result = liq.generate_liquidity_grid(
            70000, grid_low, grid_high, 10, support, resistance
        )

        any_biased_down = any(r < raw_v for r, raw_v in zip(result, raw))
        assert any_biased_down, "Expected at least one level to be shifted downward near support"

    def test_levels_2(self):
        """Edge case: minimum levels."""
        result = self._grid(levels=2)
        assert len(result) == 3
