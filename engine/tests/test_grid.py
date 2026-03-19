"""
Tests for grid_logic.py — drift_detected(), calculate_grid_parameters().

Key scenarios:
  • drift fires at ≥85% of grid_width from center
  • drift does NOT fire below the threshold
  • tilt correctly shifts the effective center
  • calculate_grid_parameters returns three tiers (inner/mid/outer) in the right order
"""
import pytest
import grid_logic as gl


# ── drift_detected ─────────────────────────────────────────────────────────

class TestDriftDetected:
    """
    Threshold is 85% of grid_width from the (tilt-adjusted) center.
    drift = abs(price - (center + tilt)) > grid_width * 0.85
    """

    def test_no_drift_when_price_is_at_center(self):
        assert gl.drift_detected(price=70000, center=70000, grid_width=3000) is False

    def test_no_drift_just_below_threshold(self):
        # threshold = 3000 * 0.85 = 2550; distance = 2549 → no drift
        assert gl.drift_detected(price=72549, center=70000, grid_width=3000) is False

    def test_drift_fires_just_above_threshold(self):
        # threshold = 3000 * 0.85 = 2550; distance = 2551 → strictly above → fires
        assert gl.drift_detected(price=72551, center=70000, grid_width=3000) is True

    def test_drift_fires_above_threshold(self):
        assert gl.drift_detected(price=73000, center=70000, grid_width=3000) is True

    def test_drift_fires_downward(self):
        # price below center
        assert gl.drift_detected(price=67000, center=70000, grid_width=3000) is True

    def test_no_drift_downward_below_threshold(self):
        assert gl.drift_detected(price=67500, center=70000, grid_width=3000) is False

    def test_tilt_shifts_effective_center_upward(self):
        """
        With tilt=500, effective center = 70000 + 500 = 70500.
        Price at 72550 is only 2050 from 70500 → below threshold of 2550 → no drift.
        Without tilt, distance = 2550 → would fire.
        """
        assert gl.drift_detected(price=72550, center=70000, grid_width=3000, tilt=500) is False

    def test_tilt_shifts_effective_center_downward(self):
        """
        With tilt=-500, effective center = 69500.
        Price at 72000 is 2500 from 69500 → below 2550 → no drift.
        """
        assert gl.drift_detected(price=72000, center=70000, grid_width=3000, tilt=-500) is False

    def test_today_incident_scenario(self):
        """
        Reproduce today's scenario: center=69131, price=69648,
        deploy_grid_width=2449. Threshold = 2449×0.85 = 2082.
        Distance = 517 → drift must NOT fire.
        """
        assert gl.drift_detected(
            price=69648, center=69131, grid_width=2449
        ) is False

    def test_drift_would_fire_at_correct_distance(self):
        """Same scenario but price has moved 2100 away — drift should fire."""
        assert gl.drift_detected(
            price=69131 + 2100, center=69131, grid_width=2449
        ) is True


# ── calculate_grid_parameters ──────────────────────────────────────────────

class TestCalculateGridParameters:
    """
    Smoke-test the tier structure returned by calculate_grid_parameters.
    We don't test exact prices (those depend on ATR math) but verify the
    structure and ordering invariants that the engine relies on.
    """

    def _call(self, price=70000, atr=600, regime="RANGE", session="US"):
        import pandas as pd
        n = 50
        closes = [price] * n
        df = pd.DataFrame(
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
        return gl.calculate_grid_parameters(
            price=price, atr=atr, regime=regime, session=session,
            skew=0.0, df=df
        )

    def test_returns_three_tiers(self):
        result = self._call()
        assert len(result["tiers"]) == 3

    def test_tier_names_in_order(self):
        tiers = self._call()["tiers"]
        assert [t["name"] for t in tiers] == ["inner", "mid", "outer"]

    def test_inner_narrower_than_mid(self):
        tiers = self._call()["tiers"]
        inner_width = tiers[0]["grid_high"] - tiers[0]["grid_low"]
        mid_width   = tiers[1]["grid_high"] - tiers[1]["grid_low"]
        assert inner_width < mid_width

    def test_mid_narrower_than_outer(self):
        tiers = self._call()["tiers"]
        mid_width   = tiers[1]["grid_high"] - tiers[1]["grid_low"]
        outer_width = tiers[2]["grid_high"] - tiers[2]["grid_low"]
        assert mid_width < outer_width

    def test_tiers_contain_current_price(self):
        price = 70000
        tiers = self._call(price=price)["tiers"]
        for tier in tiers:
            assert tier["grid_low"] < price < tier["grid_high"], (
                f"Tier {tier['name']} does not contain price {price}: "
                f"{tier['grid_low']} – {tier['grid_high']}"
            )

    def test_each_tier_has_required_keys(self):
        tiers = self._call()["tiers"]
        required = {"name", "grid_low", "grid_high", "levels", "step", "center"}
        for tier in tiers:
            assert required.issubset(tier.keys()), (
                f"Tier {tier['name']} missing keys: {required - tier.keys()}"
            )

    def test_step_is_positive(self):
        tiers = self._call()["tiers"]
        for tier in tiers:
            assert tier["step"] > 0

    def test_levels_is_positive_integer(self):
        tiers = self._call()["tiers"]
        for tier in tiers:
            assert isinstance(tier["levels"], int) and tier["levels"] > 0
