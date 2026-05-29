"""Tests for backtest.py — the read-only log analysis tool.

These validate the pure logic (Schmitt replay, recentre detection, fill→cycle
assignment, timestamp parsing) on small synthetic series. No I/O or network.
"""
import backtest


# ── replay_trending_down ────────────────────────────────────────────────

def _cycles_from_gaps(gaps):
    return [{"gap_ratio": g, "price": 70000.0, "trending_down": False} for g in gaps]


class TestReplayTrendingDown:
    def test_enters_only_below_entry(self):
        # gap dips to -1.8 (between exit -1.0 and entry -2.0) — must NOT enter.
        cycles = _cycles_from_gaps([0.0, -1.8, -1.9, 0.0])
        states = backtest.replay_trending_down(cycles, entry=-2.0, exit_=-1.0)
        assert states == [False, False, False, False]

    def test_schmitt_hysteresis_band(self):
        # Enter at -2.5, stay down through -1.5 (above entry but below exit),
        # only clear once gap recovers above exit (-1.0).
        gaps = [0.0, -2.5, -1.5, -1.2, -0.9, 0.0]
        states = backtest.replay_trending_down(_cycles_from_gaps(gaps), -2.0, -1.0)
        assert states == [False, True, True, True, False, False]

    def test_no_oscillation_on_threshold(self):
        # Sitting exactly on entry then jittering should not flip every cycle
        # once latched (mirrors the becf77c oscillation fix).
        gaps = [-2.1, -1.9, -2.1, -1.9, -1.1, -0.5]
        states = backtest.replay_trending_down(_cycles_from_gaps(gaps), -2.0, -1.0)
        # Latches True at first cycle, stays True until it clears above -1.0.
        assert states[0] is True
        assert all(states[i] for i in range(4))   # stays down through the jitter
        assert states[5] is False

    def test_none_gap_preserves_state(self):
        gaps = [-2.5, None, None, -0.5]
        states = backtest.replay_trending_down(_cycles_from_gaps(gaps), -2.0, -1.0)
        assert states == [True, True, True, False]


# ── detect_recenters ──────────────────────────────────────────────────────

class TestDetectRecenters:
    def test_detects_center_moves(self):
        cycles = [{"center": c} for c in [70000, 70000, 71000, 71000, 69000]]
        events = backtest.detect_recenters(cycles, eps=1.0)
        assert events == [2, 4]

    def test_ignores_float_jitter(self):
        cycles = [{"center": c} for c in [70000.0, 70000.4, 70000.2]]
        assert backtest.detect_recenters(cycles, eps=1.0) == []

    def test_handles_missing_center(self):
        cycles = [{"center": 70000}, {"center": None}, {"center": 72000}]
        # None is skipped; 72000 compared against last seen 70000 -> event at idx 2
        assert backtest.detect_recenters(cycles, eps=1.0) == [2]


# ── assign_fills_to_cycles ────────────────────────────────────────────────

class TestAssignFills:
    def test_buckets_to_active_cycle(self):
        cycles = [{"ts": 100}, {"ts": 200}, {"ts": 300}]
        fills = [{"t": 150}, {"t": 250}, {"t": 305}]
        out = backtest.assign_fills_to_cycles(cycles, fills)
        idxs = [idx for _, idx in out]
        assert idxs == [0, 1, 2]

    def test_drops_fills_before_first_cycle(self):
        cycles = [{"ts": 100}, {"ts": 200}]
        fills = [{"t": 50}, {"t": 150}]
        out = backtest.assign_fills_to_cycles(cycles, fills)
        assert [idx for _, idx in out] == [0]


# ── _parse_iso ─────────────────────────────────────────────────────────────

class TestParseIso:
    def test_parses_fractional_z(self):
        assert backtest._parse_iso("2026-03-23T19:52:08.806Z") is not None

    def test_ordering_is_consistent(self):
        a = backtest._parse_iso("2026-03-23T19:52:08.806Z")
        b = backtest._parse_iso("2026-03-23T19:52:09.806Z")
        assert b > a

    def test_bad_value_returns_none(self):
        assert backtest._parse_iso("not-a-date") is None
        assert backtest._parse_iso(None) is None
