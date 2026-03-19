"""
Tests for inventory._calculate_skew() — pure math, no API calls.

Constants in inventory.py (as of Mar 2026):
    TARGET_BTC = 0.40
    LOWER_BAND = 0.30
    UPPER_BAND = 0.47
    TAPER_ZONE = 0.03
    MAX_SKEW   = 0.25  (from config.py)

Behaviour:
    Inside [LOWER_BAND, UPPER_BAND]: skew = 0.0 (neutral zone)
    Below LOWER_BAND: skew ramps negative over TAPER_ZONE, then holds full value
    Above UPPER_BAND: skew ramps positive over TAPER_ZONE, then holds full value
    Clamped to [-MAX_SKEW, +MAX_SKEW] = [-0.25, +0.25]

Taper formula:
    distance  = |ratio - band_edge|
    taper     = min(distance / TAPER_ZONE, 1.0)     # 0→1 over TAPER_ZONE
    full_skew = ratio - TARGET_BTC
    skew      = clamp(full_skew * taper, -MAX_SKEW, MAX_SKEW)
"""
import pytest
import inventory


# Pull live constants so tests stay correct if the values are tweaked.
_TARGET    = inventory.TARGET_BTC   # 0.40
_LOWER     = inventory.LOWER_BAND   # 0.30
_UPPER     = inventory.UPPER_BAND   # 0.47
_TAPER     = inventory.TAPER_ZONE   # 0.03
_MAX_SKEW  = 0.25                   # from config.MAX_SKEW


# Helper to call the private function directly
def _skew(ratio):
    return inventory._calculate_skew(ratio)


# ── Neutral band ───────────────────────────────────────────────────────────

class TestNeutralBand:
    """Anywhere inside [LOWER_BAND, UPPER_BAND] skew must be exactly 0."""

    def test_at_lower_band_edge(self):
        assert _skew(_LOWER) == 0.0

    def test_at_upper_band_edge(self):
        assert _skew(_UPPER) == 0.0

    def test_at_target(self):
        assert _skew(_TARGET) == 0.0

    def test_midpoint_in_band(self):
        mid = (_LOWER + _UPPER) / 2
        assert _skew(mid) == 0.0

    def test_just_above_lower(self):
        assert _skew(_LOWER + 0.001) == 0.0

    def test_just_below_upper(self):
        assert _skew(_UPPER - 0.001) == 0.0


# ── Below lower band — negative skew ramp ─────────────────────────────────

class TestBelowLowerBand:
    """Skew goes negative as ratio falls below LOWER_BAND."""

    def test_just_outside_lower_band_starts_tapering(self):
        # 1% below LOWER_BAND → taper = 1/3, full_skew = (LOWER-0.01) - TARGET (negative)
        ratio = _LOWER - 0.01
        taper     = min(0.01 / _TAPER, 1.0)
        full_skew = ratio - _TARGET
        expected  = max(-_MAX_SKEW, min(_MAX_SKEW, full_skew * taper))
        assert abs(_skew(ratio) - expected) < 1e-9

    def test_halfway_through_taper(self):
        ratio = _LOWER - _TAPER / 2  # halfway → taper=0.5
        taper     = 0.5
        full_skew = ratio - _TARGET
        expected  = max(-_MAX_SKEW, min(_MAX_SKEW, full_skew * taper))
        assert abs(_skew(ratio) - expected) < 1e-9

    def test_full_taper_at_band_minus_taper_zone(self):
        ratio = _LOWER - _TAPER   # exactly at full taper (taper=1.0)
        taper     = 1.0
        full_skew = ratio - _TARGET
        expected  = max(-_MAX_SKEW, min(_MAX_SKEW, full_skew * taper))
        assert abs(_skew(ratio) - expected) < 1e-9

    def test_skew_is_negative_below_lower_band(self):
        for ratio in [_LOWER - 0.01, _LOWER - 0.05, _LOWER - 0.15]:
            assert _skew(ratio) < 0, f"Expected negative skew at ratio={ratio}"

    def test_skew_magnitude_increases_as_ratio_falls(self):
        """The further below the band, the more negative the skew."""
        s1 = _skew(_LOWER - 0.01)
        s2 = _skew(_LOWER - 0.05)
        s3 = _skew(_LOWER - 0.10)
        assert s1 > s2 > s3   # all negative; s3 most negative

    def test_clamped_at_max_skew_very_low_ratio(self):
        """Far below target — full_skew * 1.0 would exceed MAX_SKEW → clamped."""
        assert _skew(0.10) == -_MAX_SKEW

    def test_at_zero_ratio_clamped(self):
        assert _skew(0.0) == -_MAX_SKEW


# ── Above upper band — positive skew ramp ─────────────────────────────────

class TestAboveUpperBand:
    """Skew goes positive as ratio rises above UPPER_BAND."""

    def test_just_outside_upper_band_starts_tapering(self):
        ratio = _UPPER + 0.01
        taper     = min(0.01 / _TAPER, 1.0)
        full_skew = ratio - _TARGET
        expected  = max(-_MAX_SKEW, min(_MAX_SKEW, full_skew * taper))
        assert abs(_skew(ratio) - expected) < 1e-9

    def test_halfway_through_upper_taper(self):
        ratio = _UPPER + _TAPER / 2
        taper     = 0.5
        full_skew = ratio - _TARGET
        expected  = max(-_MAX_SKEW, min(_MAX_SKEW, full_skew * taper))
        assert abs(_skew(ratio) - expected) < 1e-9

    def test_full_taper_at_upper_plus_taper_zone(self):
        ratio = _UPPER + _TAPER  # taper = 1.0
        full_skew = ratio - _TARGET
        expected  = max(-_MAX_SKEW, min(_MAX_SKEW, full_skew))
        assert abs(_skew(ratio) - expected) < 1e-9

    def test_skew_is_positive_above_upper_band(self):
        for ratio in [_UPPER + 0.01, _UPPER + 0.05, _UPPER + 0.15]:
            assert _skew(ratio) > 0, f"Expected positive skew at ratio={ratio}"

    def test_skew_magnitude_increases_as_ratio_rises(self):
        s1 = _skew(_UPPER + 0.01)
        s2 = _skew(_UPPER + 0.05)
        s3 = _skew(_UPPER + 0.10)
        assert s1 < s2 < s3   # all positive; s3 largest

    def test_clamped_at_max_skew_very_high_ratio(self):
        assert _skew(0.90) == _MAX_SKEW

    def test_at_unity_ratio_clamped(self):
        assert _skew(1.0) == _MAX_SKEW


# ── Symmetry and boundary ─────────────────────────────────────────────────

class TestSkewProperties:
    def test_skew_never_exceeds_max_skew(self):
        """Exhaustive check: skew always within [-MAX_SKEW, +MAX_SKEW]."""
        for i in range(101):
            ratio = i / 100.0
            s = _skew(ratio)
            assert -_MAX_SKEW <= s <= _MAX_SKEW, (
                f"Skew {s:.4f} out of bounds at ratio={ratio:.2f}"
            )

    def test_continuous_at_lower_band_boundary(self):
        """No cliff-edge jump at the lower band edge."""
        s_just_inside  = _skew(_LOWER + 0.0001)   # 0.0 (neutral)
        s_just_outside = _skew(_LOWER - 0.0001)   # tiny negative
        assert s_just_inside == 0.0
        assert s_just_outside < 0.0
        # The jump must be tiny — not a cliff-edge
        assert abs(s_just_outside - s_just_inside) < 0.01

    def test_continuous_at_upper_band_boundary(self):
        s_just_inside  = _skew(_UPPER - 0.0001)   # 0.0
        s_just_outside = _skew(_UPPER + 0.0001)   # tiny positive
        assert s_just_inside == 0.0
        assert s_just_outside > 0.0
        assert abs(s_just_outside - s_just_inside) < 0.01
