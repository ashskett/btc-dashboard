"""Tests for amplitude.py — realized swing vs fee-floor (observability)."""
import amplitude as amp


def _push(prices):
    amp.reset()
    for p in prices:
        amp.update(p)


def test_fee_floor_is_point_three_pct():
    # ROUND_TRIP_FEE (0.20% maker basis) × FEE_BUFFER (1.5) = 0.30%
    assert round(amp.FEE_FLOOR_PCT, 2) == 0.3


def test_warming_until_min_samples():
    _push([70000, 70100, 70050])  # < _MIN_SAMPLES
    s = amp.snapshot(70050)
    assert s["band"] == "warming"
    assert s["swing_pct"] is None and s["ratio"] is None


def test_rich_when_swing_clears_floor():
    _push([70000, 70100, 70560, 70200, 70000, 70300])  # ~0.8% range
    s = amp.snapshot(70300)
    assert s["swing_pct"] > 0.6
    assert s["ratio"] >= amp.RICH_RATIO
    assert s["band"] == "rich"


def test_thin_when_swing_below_floor():
    _push([70000, 70050, 70030, 70010, 70040, 70020])  # ~0.07% range
    s = amp.snapshot(70020)
    assert s["ratio"] < 1.0
    assert s["band"] == "thin"


def test_ratio_tracks_fee_floor():
    _push([70000, 70210, 70000, 70100, 70050, 70000])  # 0.3% range
    s = amp.snapshot(70000)
    # 0.3% swing / 0.30% floor ≈ 1.0
    assert 0.9 <= s["ratio"] <= 1.1
    assert s["band"] == "ok"
