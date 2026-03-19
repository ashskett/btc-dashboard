"""
Tests for regime.py — detect_regime(), trend_strength(), hysteresis.

Key scenarios covered:
  • trend_strength gap-ratio thresholds (trending_up / trending_down flags)
  • TREND_DOWN two-cycle hysteresis (today's bug root: one dip below trendline
    should NOT trigger TREND_DOWN; two consecutive dips should)
  • TREND_DOWN resets immediately when price returns above trendline
  • COMPRESSION guard: should not fire when price is far above trendline
  • Default RANGE returned when no other condition matches
"""
import json
import pytest
import regime as r


# ── Helpers ────────────────────────────────────────────────────────────────

def _reset_regime_state(tmp_path, monkeypatch):
    """Point regime_state.json at a temp file and reset counter to 0."""
    state_file = str(tmp_path / "regime_state.json")
    monkeypatch.setattr(r, "_REGIME_STATE_FILE", state_file)
    json.dump({"below_tl_count": 0}, open(state_file, "w"))
    return state_file


# ── trend_strength tests ───────────────────────────────────────────────────

class TestTrendStrength:
    def test_trending_up_fires_above_5_5x_atr(self):
        ts = r.trend_strength(price=70000 + 600 * 6.0, trendline=70000, atr=600)
        assert ts["trending_up"] is True
        assert ts["trending_down"] is False

    def test_trending_up_does_not_fire_at_5x_atr(self):
        # 5.0× is below the 5.5× threshold
        ts = r.trend_strength(price=70000 + 600 * 5.0, trendline=70000, atr=600)
        assert ts["trending_up"] is False

    def test_trending_down_fires_below_negative_2x_atr(self):
        ts = r.trend_strength(price=70000 - 600 * 2.5, trendline=70000, atr=600)
        assert ts["trending_down"] is True
        assert ts["trending_up"] is False

    def test_trending_down_does_not_fire_at_negative_1_5x_atr(self):
        # -1.5× is above the -2.0× threshold (was the old setting that fired too often)
        ts = r.trend_strength(price=70000 - 600 * 1.5, trendline=70000, atr=600)
        assert ts["trending_down"] is False

    def test_gap_ratio_calculated_correctly(self):
        ts = r.trend_strength(price=70600, trendline=70000, atr=600)
        assert ts["gap_ratio"] == pytest.approx(1.0, abs=0.01)

    def test_zero_atr_returns_zero_gap_ratio(self):
        ts = r.trend_strength(price=70000, trendline=69000, atr=0)
        assert ts["gap_ratio"] == 0.0
        assert ts["trending_up"] is False
        assert ts["trending_down"] is False

    def test_neutral_zone_neither_flag(self):
        # price == trendline → gap_ratio = 0
        ts = r.trend_strength(price=70000, trendline=70000, atr=600)
        assert ts["trending_up"] is False
        assert ts["trending_down"] is False


# ── TREND_DOWN hysteresis ──────────────────────────────────────────────────

class TestTrendDownHysteresis:
    """
    The two-cycle hysteresis was the direct cause of today's incident:
    TREND_DOWN should require 2 consecutive cycles below trendline - ATR×0.15.
    A single dip must not trigger it.
    """

    def _make_below_df(self, make_df, atr=600.0, trendline=70000.0):
        """DataFrame where close is well below trendline."""
        price = trendline - atr * 0.5   # 0.5× ATR below — comfortably below min_gap
        return make_df(price=price, atr=atr)

    def _make_above_df(self, make_df, atr=600.0, trendline=70000.0):
        """DataFrame where close is above trendline."""
        price = trendline + atr * 0.5
        return make_df(price=price, atr=atr)

    def test_single_dip_returns_range_not_trend_down(
        self, make_df, tmp_path, monkeypatch
    ):
        _reset_regime_state(tmp_path, monkeypatch)
        df = self._make_below_df(make_df)
        trendline = 70000.0
        result = r.detect_regime(df, trendline)
        # First cycle below trendline: count=1, need 2 → should NOT be TREND_DOWN
        assert result != "TREND_DOWN", (
            "Single cycle below trendline must not trigger TREND_DOWN"
        )

    def test_two_consecutive_dips_return_trend_down(
        self, make_df, tmp_path, monkeypatch
    ):
        _reset_regime_state(tmp_path, monkeypatch)
        df = self._make_below_df(make_df)
        trendline = 70000.0
        r.detect_regime(df, trendline)           # cycle 1: count → 1
        result = r.detect_regime(df, trendline)  # cycle 2: count → 2 → TREND_DOWN
        assert result == "TREND_DOWN"

    def test_trend_down_resets_when_price_returns_above(
        self, make_df, tmp_path, monkeypatch
    ):
        _reset_regime_state(tmp_path, monkeypatch)
        trendline = 70000.0
        df_below = self._make_below_df(make_df)
        df_above = self._make_above_df(make_df)
        r.detect_regime(df_below, trendline)   # count → 1
        r.detect_regime(df_below, trendline)   # count → 2, TREND_DOWN
        r.detect_regime(df_above, trendline)   # count reset → 0
        # Now one more below — count is 1, should NOT be TREND_DOWN yet
        result = r.detect_regime(df_below, trendline)
        assert result != "TREND_DOWN", (
            "Counter must reset when price returns above trendline"
        )

    def test_min_gap_prevents_trivial_dips(self, make_df, tmp_path, monkeypatch):
        """A dip of only ATR×0.05 (below min_gap of ATR×0.15) should not count."""
        _reset_regime_state(tmp_path, monkeypatch)
        atr = 600.0
        trendline = 70000.0
        # Price just barely below trendline — less than min_gap (atr×0.15 = 90)
        price = trendline - atr * 0.05
        df = make_df(price=price, atr=atr)
        r.detect_regime(df, trendline)
        result = r.detect_regime(df, trendline)
        # Even after 2 cycles, the dip is too shallow → not TREND_DOWN
        assert result != "TREND_DOWN"


# ── COMPRESSION guard ──────────────────────────────────────────────────────

class TestCompressionGuard:
    def test_no_compression_when_far_above_trendline(self, tmp_path, monkeypatch):
        """
        COMPRESSION should not fire when price is > 1.5× ATR above trendline.
        A tight BB/ATR above support is a bullish consolidation, not a dead market.
        """
        _reset_regime_state(tmp_path, monkeypatch)
        import pandas as pd
        atr = 600.0
        trendline = 67000.0
        price = 70000.0   # gap_ratio = (70000-67000)/600 = 5.0 → above_trendline guard

        n = 50
        # Deliberately narrow BB width (below 10th percentile) and low ATR
        # to trigger the compression condition — but the trendline guard must block it
        narrow_bb = 0.001
        low_atr = atr * 0.8   # below mean

        closes = [price] * n
        df = pd.DataFrame(
            {
                "open":     closes,
                "high":     [c + 10 for c in closes],
                "low":      [c - 10 for c in closes],
                "close":    closes,
                "volume":   [1000.0] * n,
                "atr":      [low_atr] * n,
                "bb_width": [narrow_bb] * n,
            },
            index=pd.date_range("2026-01-01", periods=n, freq="1h"),
        )
        result = r.detect_regime(df, trendline)
        assert result != "COMPRESSION", (
            "COMPRESSION must be blocked when price is far above the trendline"
        )


# ── Default RANGE ──────────────────────────────────────────────────────────

class TestDefaultRange:
    def test_normal_ranging_market_returns_range(self, make_df, tmp_path, monkeypatch):
        _reset_regime_state(tmp_path, monkeypatch)
        trendline = 69500.0
        df = make_df(price=70000.0, atr=600.0, bb_width=0.02)
        result = r.detect_regime(df, trendline)
        assert result == "RANGE"
