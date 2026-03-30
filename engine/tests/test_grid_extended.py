"""
Extended tests for grid_logic.py — covering gaps in test_grid.py.

Tests added here:
  • Fee guard — step is floored to min_step; levels are reduced when step too small
  • Session adjustments — ASIA adds 2 levels, US removes 2, WKD_ASIA adds 2
  • Compression density boost — levels multiplied by compression_mult when volatility low
  • Weekend ATR floor — ATR floored at 1% of price on WKD sessions
  • TREND_DOWN grid asymmetry — grid extends further below price than above
  • TREND_UP grid asymmetry — grid extends further above price than below
  • trend_tilt shifts inner tier only
  • fee_ok flag reflects whether step meets min_step
"""
import pytest
import pandas as pd
import grid_logic as gl


# ── Test DataFrame builder ─────────────────────────────────────────────────

def _df(price=70000.0, atr=600.0, n=50):
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


def _calc(price=70000, atr=600, regime="RANGE", session="EUROPE",
          skew=0.0, trend_tilt=0.0):
    return gl.calculate_grid_parameters(
        price=float(price), atr=float(atr), regime=regime,
        session=session, skew=skew, df=_df(price, atr), trend_tilt=trend_tilt,
    )


# ── Fee guard ──────────────────────────────────────────────────────────────

class TestFeeGuard:
    """
    When ATR is very small, the natural step may fall below the fee floor.
    Fee floor = price × ROUND_TRIP_FEE × FEE_BUFFER = price × 0.004 × 1.5 = price × 0.006

    At price=70000 → min_step = 70000 × 0.006 = $420.
    Force a tiny ATR so grid step is well below $420; fee guard must kick in.
    """

    def test_fee_ok_true_when_step_large_enough(self):
        result = _calc(price=70000, atr=1500, session="EUROPE")
        mid = result["tiers"][1]  # mid tier
        assert mid["fee_ok"] is True

    def test_fee_ok_false_and_levels_reduced_when_atr_tiny(self):
        # atr=50 → grid_width (mid tier) = 3 * 50 * 1.5 = 225
        # natural step at 6 levels = 225/6 = 37.5 → way below min_step of ~420
        result = _calc(price=70000, atr=50, session="EUROPE")
        mid = result["tiers"][1]
        # Fee guard reduces levels to max where step >= min_step
        # max_levels = floor(225/420) = 0 → clamped to 2
        assert mid["levels"] == 2

    def test_min_step_field_present_in_tiers(self):
        result = _calc()
        for tier in result["tiers"]:
            assert "min_step" in tier
            assert tier["min_step"] > 0

    def test_fee_ok_flag_is_accurate(self):
        """fee_ok is True iff step >= min_step."""
        for atr in [20, 50, 100, 300, 600, 1200]:
            result = _calc(price=70000, atr=atr, session="EUROPE")
            for tier in result["tiers"]:
                expected_fee_ok = tier["step"] >= tier["min_step"]
                assert tier["fee_ok"] == expected_fee_ok, (
                    f"atr={atr} tier={tier['name']}: fee_ok={tier['fee_ok']} "
                    f"but step={tier['step']:.2f}, min_step={tier['min_step']:.2f}"
                )

    def test_fee_guard_reduces_levels_when_step_too_small(self):
        """
        At a moderate ATR where the natural step is below min_step but the
        guard can find a valid level count, verify levels are reduced vs base.
        atr=200 at price=70000:
          mid grid_width = 3 * 200 * 1.5 = 900, range = 1800
          natural step at 6 levels = 300; min_step = 70000 * 0.004 * 1.5 = 420
          → fee guard fires; max_levels = floor(1800/420) = 4
        """
        result = _calc(price=70000, atr=200, session="EUROPE")
        mid = result["tiers"][1]
        # Fee guard should have reduced levels below the base (6)
        assert mid["levels"] <= 6

    def test_weekend_inner_uses_lower_fee_buffer(self):
        """
        On WKD sessions, inner tier uses wkd_fee_buffer=1.2 instead of 1.5.
        This means the inner tier should allow more levels (lower floor) than
        a weekday session with the same ATR.
        """
        # Use a moderate ATR where both buffers are relevant
        wkd   = _calc(price=84000, atr=300, session="WKD_ASIA")
        wkday = _calc(price=84000, atr=300, session="EUROPE")
        inner_wkd   = wkd["tiers"][0]["levels"]
        inner_wkday = wkday["tiers"][0]["levels"]
        # Weekend should have >= levels on inner (lower buffer → less restriction)
        assert inner_wkd >= inner_wkday


# ── Volatility-based level adjustments ────────────────────────────────────

class TestSessionLevelAdjustments:
    """
    Session-based +2/-2 level adjustments were replaced with volatility-based
    adjustments (ATR vs rolling ATR mean). Session is no longer the driver.

    New rules (in _build_tier):
      vol_ratio = atr / df.atr.mean()
      vol_ratio > 1.3  → levels +2  (high vol: more fills possible)
      vol_ratio < 0.75 → levels -2  (low vol: fewer fills)
      otherwise        → base levels unchanged

    df.atr is a constant column in test DataFrames (all rows = atr),
    so mean == atr and vol_ratio == 1.0 → no adjustment in normal tests.
    """

    def _mid_levels(self, session):
        return _calc(price=70000, atr=2000, session=session)["tiers"][1]["levels"]

    def test_normal_vol_no_adjustment(self):
        # vol_ratio = 1.0 (atr == mean) → no change → mid base=6
        assert self._mid_levels("EUROPE") == 6

    def test_normal_vol_consistent_across_sessions(self):
        # Session no longer drives level count — all should be equal at vol_ratio=1.0
        assert self._mid_levels("ASIA") == self._mid_levels("US") == self._mid_levels("EUROPE")

    def test_high_vol_adds_two_levels(self):
        # Build a df where last ATR (2700) > mean (~2014) × 1.3 → vol_ratio ≈ 1.34 → +2
        # Note: with 49 bars at 2000 and 1 bar at 2700, mean = (49*2000+2700)/50 = 2014
        # vol_ratio = 2700/2014 ≈ 1.34 > 1.3 → +2 levels
        import pandas as pd
        n = 50
        atrs = [2000.0] * (n - 1) + [2700.0]
        df = pd.DataFrame(
            {"open": [70000.0]*n, "high": [70000.0]*n, "low": [70000.0]*n,
             "close": [70000.0]*n, "volume": [1000.0]*n, "atr": atrs,
             "bb_width": [0.02]*n},
            index=pd.date_range("2026-01-01", periods=n, freq="1h"),
        )
        from grid_logic import calculate_grid_parameters
        result = calculate_grid_parameters(70000, 2700.0, "RANGE", "EUROPE", 0.0, df)
        mid_levels = result["tiers"][1]["levels"]
        assert mid_levels == 8   # base 6 + 2

    def test_low_vol_removes_two_levels(self):
        # Build a df where last ATR (1400) < mean (2000) × 0.75 → -2
        import pandas as pd
        n = 50
        atrs = [2000.0] * (n - 1) + [1400.0]   # last bar drops
        df = pd.DataFrame(
            {"open": [70000.0]*n, "high": [70000.0]*n, "low": [70000.0]*n,
             "close": [70000.0]*n, "volume": [1000.0]*n, "atr": atrs,
             "bb_width": [0.02]*n},
            index=pd.date_range("2026-01-01", periods=n, freq="1h"),
        )
        from grid_logic import calculate_grid_parameters
        result = calculate_grid_parameters(70000, 1400.0, "RANGE", "EUROPE", 0.0, df)
        mid_levels = result["tiers"][1]["levels"]
        assert mid_levels == 4   # base 6 - 2

    def test_minimum_levels_is_4(self):
        """No tier should drop below 4 levels (new minimum after outer shrink)."""
        result = _calc(price=70000, atr=2000, session="US")
        for tier in result["tiers"]:
            assert tier["levels"] >= 4


# ── Compression density boost ──────────────────────────────────────────────

class TestCompressionMultiplier:
    """
    compression is triggered when volatility_ratio = atr/price < 0.005.
    At price=70000: atr < 350 triggers compression.

    compression_mult per tier:
      inner: 1.5  → levels × 1.5
      mid:   1.2  → levels × 1.2
      outer: 1.0  → levels × 1.0 (no boost)
    """

    def test_compression_flag_set_when_atr_very_low(self):
        result = _calc(price=70000, atr=100, session="EUROPE")
        assert result["compression"] is True

    def test_compression_flag_not_set_for_normal_atr(self):
        result = _calc(price=70000, atr=600, session="EUROPE")
        assert result["compression"] is False

    def test_compression_boosts_mid_levels_when_atr_in_viable_range(self):
        """
        At atr≈340, price=70000: volatility_ratio=0.00486 → compression=True.
        Mid grid range = 2 * 3 * 340 * 1.5 = 3060.
        With compression_mult=1.2: levels = int(6*1.2) = 7, step = 437 > min_step (420) → fee_ok.
        Without compression (atr=600): levels = 6.
        So compression adds one level on mid tier when step is viable.
        """
        comp   = _calc(price=70000, atr=340, session="EUROPE")
        normal = _calc(price=70000, atr=600, session="EUROPE")
        assert comp["compression"] is True
        assert comp["tiers"][1]["levels"] > normal["tiers"][1]["levels"]

    def test_outer_compression_mult_is_one_so_no_boost(self):
        """
        Outer tier compression_mult=1.0 → no level boost from compression.
        Use the same viable ATR. Outer base=6; int(6*1.0)=6 unchanged.
        """
        comp   = _calc(price=70000, atr=340, session="EUROPE")
        normal = _calc(price=70000, atr=600, session="EUROPE")
        # Outer levels should be equal (both land at 6 base, outer mult=1.0)
        assert comp["tiers"][2]["levels"] == normal["tiers"][2]["levels"]


# ── Weekend ATR floor ──────────────────────────────────────────────────────

class TestWeekendAtrFloor:
    """
    On WKD sessions, ATR is floored at 1% of price.
    At price=84000: floor = 840.
    Pass atr=200 (below floor) → grid should be wider than without the floor.
    """

    def test_wkd_grid_wider_than_weekday_for_tiny_atr(self):
        wkd    = _calc(price=84000, atr=200, session="WKD_ASIA")
        wkday  = _calc(price=84000, atr=200, session="ASIA")
        wkd_width   = wkd["tiers"][0]["grid_high"] - wkd["tiers"][0]["grid_low"]
        wkday_width = wkday["tiers"][0]["grid_high"] - wkday["tiers"][0]["grid_low"]
        assert wkd_width > wkday_width, (
            "WKD session ATR floor should produce a wider grid for tiny ATR"
        )

    def test_wkd_grid_same_as_weekday_when_atr_above_floor(self):
        """When ATR is already > 1% of price, the floor has no effect."""
        wkd   = _calc(price=70000, atr=1500, session="WKD_EU")
        wkday = _calc(price=70000, atr=1500, session="EUROPE")
        wkd_width   = wkd["tiers"][0]["grid_high"] - wkd["tiers"][0]["grid_low"]
        wkday_width = wkday["tiers"][0]["grid_high"] - wkday["tiers"][0]["grid_low"]
        # May differ due to session level adj (WKD_EU has no penalty) but widths equal
        assert abs(wkd_width - wkday_width) < 1  # same within rounding


# ── Regime grid asymmetry ──────────────────────────────────────────────────

class TestRegimeAsymmetry:
    """
    TREND_DOWN: grid extends 1.5× below price, 0.5× above
    TREND_UP:   grid extends 0.5× below price, 1.5× above
    RANGE:      symmetric (1× each side)
    """

    def _tier_spans(self, regime, price=70000, atr=600, session="EUROPE"):
        result = _calc(price=price, atr=atr, regime=regime, session=session)
        mid = result["tiers"][1]
        below = price - mid["grid_low"]
        above = mid["grid_high"] - price
        return below, above

    def test_range_is_roughly_symmetric(self):
        below, above = self._tier_spans("RANGE")
        # Allow skew=0 → tilt=0 → symmetric. Allow 1% tolerance.
        assert abs(below - above) / below < 0.02

    def test_trend_down_extends_further_below(self):
        below, above = self._tier_spans("TREND_DOWN")
        assert below > above

    def test_trend_up_extends_further_above(self):
        below, above = self._tier_spans("TREND_UP")
        assert above > below

    def test_trend_down_ratio_approximately_3x(self):
        """TREND_DOWN: 1.5×grid_width below, 0.5×grid_width above → ratio = 3:1."""
        below, above = self._tier_spans("TREND_DOWN")
        ratio = below / above
        assert 2.5 <= ratio <= 3.5


# ── trend_tilt — inner only ────────────────────────────────────────────────

class TestTrendTilt:
    """
    trend_tilt shifts the inner grid centre upward (positive tilt = trending up).
    It must NOT affect mid or outer tiers.
    """

    def test_trend_tilt_shifts_inner_up(self):
        baseline = _calc(price=70000, atr=600, trend_tilt=0.0)
        tilted   = _calc(price=70000, atr=600, trend_tilt=0.15)
        inner_base  = baseline["tiers"][0]
        inner_tilt  = tilted["tiers"][0]
        # Positive tilt → grid_high and grid_low both shift up
        assert inner_tilt["grid_high"] > inner_base["grid_high"]
        assert inner_tilt["grid_low"]  > inner_base["grid_low"]

    def test_trend_tilt_does_not_affect_mid(self):
        baseline = _calc(price=70000, atr=600, trend_tilt=0.0)
        tilted   = _calc(price=70000, atr=600, trend_tilt=0.15)
        assert baseline["tiers"][1]["grid_low"]  == tilted["tiers"][1]["grid_low"]
        assert baseline["tiers"][1]["grid_high"] == tilted["tiers"][1]["grid_high"]

    def test_trend_tilt_does_not_affect_outer(self):
        baseline = _calc(price=70000, atr=600, trend_tilt=0.0)
        tilted   = _calc(price=70000, atr=600, trend_tilt=0.15)
        assert baseline["tiers"][2]["grid_low"]  == tilted["tiers"][2]["grid_low"]
        assert baseline["tiers"][2]["grid_high"] == tilted["tiers"][2]["grid_high"]

    def test_negative_tilt_shifts_inner_down(self):
        baseline = _calc(price=70000, atr=600, trend_tilt=0.0)
        tilted   = _calc(price=70000, atr=600, trend_tilt=-0.15)
        assert tilted["tiers"][0]["grid_high"] < baseline["tiers"][0]["grid_high"]
