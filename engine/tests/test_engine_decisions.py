"""
Tests for the engine's tiered bot decision table.

This is the highest-value test file — it covers the exact logic that caused
today's incident (bots restarted at wrong center after TREND_DOWN ended).

Approach: mock all external I/O (market data, 3Commas API, file writes) and
call engine.run() directly, then assert which of start_bot / stop_bot /
redeploy_all_bots was called.

The 240-second cycle guard is bypassed by resetting _last_run_ts = 0 before
each call.
"""
import sys
import time
import json
import pytest
from unittest.mock import patch, MagicMock, call
import pandas as pd


# ── Minimal fake DataFrame factory ────────────────────────────────────────

def _make_df(price=70000.0, atr=600.0, bb_width=0.02, n=50):
    closes = [price] * n
    return pd.DataFrame(
        {
            "open":     closes,
            "high":     [c + atr * 0.3 for c in closes],
            "low":      [c - atr * 0.3 for c in closes],
            "close":    closes,
            "volume":   [1000.0] * n,
            "atr":      [atr] * n,
            "bb_width": [bb_width] * n,
        },
        index=pd.date_range("2026-01-01", periods=n, freq="1h"),
    )


# ── Context manager that patches everything engine.run() touches ──────────

def _engine_patches(
    price=70000.0,
    atr=600.0,
    regime="RANGE",
    trending_up=False,
    trending_down=False,
    gap_ratio=1.0,
    btc_ratio=0.60,
    grid_center=70000.0,
    prev_regime=None,
    breakout_active=None,
    inventory_mode="NORMAL",
):
    """Return a dict of patches to apply via nested `with patch(...)` calls."""
    import engine

    df = _make_df(price=price, atr=atr)

    # Fake grid/tier data
    fake_tiers = [
        {"name": "inner", "grid_low": price - 700, "grid_high": price + 700,
         "levels": 6, "step": 233, "center": price, "fee_ok": True,
         "min_step": 100, "tilt": 0, "trend_tilt": 0.0,
         "grid_levels": [price - 700 + i * 233 for i in range(7)]},
        {"name": "mid", "grid_low": price - 1400, "grid_high": price + 1400,
         "levels": 6, "step": 467, "center": price, "fee_ok": True,
         "min_step": 100, "tilt": 0, "trend_tilt": 0.0,
         "grid_levels": [price - 1400 + i * 467 for i in range(7)]},
        {"name": "outer", "grid_low": price - 2800, "grid_high": price + 2800,
         "levels": 6, "step": 933, "center": price, "fee_ok": True,
         "min_step": 100, "tilt": 0, "trend_tilt": 0.0,
         "grid_levels": [price - 2800 + i * 933 for i in range(7)]},
    ]
    fake_grid = {
        "center": price, "grid_low": price - 1400, "grid_high": price + 1400,
        "grid_width": 2800.0, "levels": 6, "step": 467, "tilt": 0,
        "support": price - 2800, "resistance": price + 2800, "tiers": fake_tiers,
        "compression": False,
    }
    fake_grid_state = {"grid_center": grid_center, "grid_width_at_deploy": 2800.0}
    fake_ts = {"gap_ratio": gap_ratio, "trending_up": trending_up, "trending_down": trending_down}
    fake_bo_state = {"active": breakout_active, "fire_price": None, "cycles_active": 0}

    return {
        "get_btc_data": MagicMock(return_value=df),
        "get_btc_data_short": MagicMock(return_value=df),
        "add_indicators": MagicMock(return_value=df),
        "get_active_trendline": MagicMock(return_value=price),
        "get_grid_state": MagicMock(return_value=fake_grid_state),
        "get_grid_center": MagicMock(return_value=grid_center),
        "update_grid_center": MagicMock(),
        "detect_regime": MagicMock(return_value=regime),
        "trend_strength": MagicMock(return_value=fake_ts),
        "compression_exit_fast": MagicMock(return_value=False),
        "calculate_grid_parameters": MagicMock(return_value=fake_grid),
        "calculate_inventory": MagicMock(return_value=(btc_ratio, 0.0)),
        "get_session": MagicMock(return_value="US"),
        "start_bot": MagicMock(),
        "stop_bot": MagicMock(),
        "redeploy_all_bots": MagicMock(),
        "drift_detected": MagicMock(return_value=False),
        "breakout_detected": MagicMock(return_value=None),
        "breakout_exhausting": MagicMock(return_value=False),
        "proximity_alert": MagicMock(return_value=None),
        "get_breakout_state": MagicMock(return_value=fake_bo_state),
        "increment_active_cycles": MagicMock(),
        "breakout_inner_ready": MagicMock(return_value=False),
        "check_targets": MagicMock(return_value=None),
        "write_status": MagicMock(),
        "write_log_entry": MagicMock(),
        "show_dashboard": MagicMock(),
        "portfolio_snapshot": MagicMock(return_value=None),
    }


def _run_cycle(patches_dict, prev_regime=None, bot_ids=None):
    """Apply patches, set prev_regime, reset cooldown, and call engine.run()."""
    import engine
    if bot_ids is None:
        bot_ids = ["bot_inner", "bot_mid", "bot_outer"]

    with patch.multiple("engine", **patches_dict):
        engine.GRID_BOTS = bot_ids
        engine.DRY_RUN = False
        engine._last_run_ts = 0
        engine._prev_regime = prev_regime
        # Reset rate limiter
        engine._action_timestamps.clear()
        engine.run()
        return patches_dict


# ── Decision table tests ───────────────────────────────────────────────────

class TestBotDecisionTable:
    """
    Verify that the correct bots are started/stopped for each regime/state.

    Bot mapping (by index in GRID_BOTS):
      0 = inner   1 = mid   2 = outer
    """

    def test_range_starts_all_bots(self):
        p = _engine_patches(regime="RANGE", prev_regime=None)
        _run_cycle(p, prev_regime="RANGE")   # no transition — normal RANGE cycle
        assert p["start_bot"].call_count == 3, f"Expected 3 start_bot calls, got {p['start_bot'].call_count}"
        assert p["stop_bot"].call_count == 0

    def test_trend_down_stops_inner_and_mid_keeps_outer(self):
        p = _engine_patches(regime="TREND_DOWN")
        _run_cycle(p, prev_regime="TREND_DOWN")
        stop_ids  = [c.args[0] for c in p["stop_bot"].call_args_list]
        start_ids = [c.args[0] for c in p["start_bot"].call_args_list]
        assert "bot_inner" in stop_ids
        assert "bot_mid"   in stop_ids
        assert "bot_outer" in start_ids
        assert "bot_outer" not in stop_ids

    def test_trending_down_stops_inner_and_mid(self):
        p = _engine_patches(regime="RANGE", trending_down=True)
        _run_cycle(p, prev_regime="RANGE")
        stop_ids = [c.args[0] for c in p["stop_bot"].call_args_list]
        assert "bot_inner" in stop_ids
        assert "bot_mid"   in stop_ids

    def test_compression_stops_inner_and_mid(self):
        p = _engine_patches(regime="COMPRESSION")
        _run_cycle(p, prev_regime="COMPRESSION")
        stop_ids = [c.args[0] for c in p["stop_bot"].call_args_list]
        assert "bot_inner" in stop_ids
        assert "bot_mid"   in stop_ids

    def test_trend_up_starts_all_bots(self):
        p = _engine_patches(regime="TREND_UP")
        _run_cycle(p, prev_regime="TREND_UP")
        assert p["start_bot"].call_count == 3

    def test_trending_up_in_non_range_keeps_all_bots_on(self):
        """trending_up AND regime != RANGE → all bots ON (inner fills outweigh sell risk)."""
        p = _engine_patches(regime="TREND_UP", trending_up=True)
        _run_cycle(p, prev_regime="TREND_UP")
        assert p["start_bot"].call_count == 3
        assert p["stop_bot"].call_count == 0

    def test_trending_up_in_range_keeps_all_bots_on(self):
        """trending_up in RANGE is normal ranging above support — all bots on."""
        p = _engine_patches(regime="RANGE", trending_up=True)
        _run_cycle(p, prev_regime="RANGE")
        assert p["start_bot"].call_count == 3


# ── Regime-transition redeploy test ───────────────────────────────────────

class TestRegimeTransitionRedeploy:
    """
    THE KEY TEST — covers the exact bug that caused today's incident.

    When regime transitions from TREND_DOWN (or COMPRESSION) → RANGE,
    the engine must call redeploy_all_bots() rather than start_bot().
    This ensures bots are recentred at the current price, not left at the
    stale ranges they had when they were stopped hours ago.
    """

    def test_trend_down_to_range_calls_redeploy_not_start(self):
        p = _engine_patches(regime="RANGE")
        _run_cycle(p, prev_regime="TREND_DOWN")
        assert p["redeploy_all_bots"].called, (
            "redeploy_all_bots() must be called on TREND_DOWN → RANGE transition"
        )
        assert p["start_bot"].call_count == 0, (
            "start_bot() must NOT be called on regime transition "
            "(it would restart bots at their stale, pre-TREND_DOWN ranges)"
        )

    def test_compression_to_range_calls_redeploy(self):
        p = _engine_patches(regime="RANGE")
        _run_cycle(p, prev_regime="COMPRESSION")
        assert p["redeploy_all_bots"].called, (
            "redeploy_all_bots() must be called on COMPRESSION → RANGE transition"
        )

    def test_range_to_range_does_not_call_redeploy(self):
        """Steady RANGE: redeploy must NOT be called every cycle."""
        p = _engine_patches(regime="RANGE")
        _run_cycle(p, prev_regime="RANGE")
        assert not p["redeploy_all_bots"].called, (
            "redeploy_all_bots() must NOT be called on a normal RANGE→RANGE cycle"
        )

    def test_trend_down_to_trend_up_calls_redeploy(self):
        p = _engine_patches(regime="TREND_UP")
        _run_cycle(p, prev_regime="TREND_DOWN")
        assert p["redeploy_all_bots"].called

    def test_redeploy_updates_grid_center(self):
        """After regime-transition redeploy, grid center must be updated."""
        p = _engine_patches(regime="RANGE", price=69800.0)
        _run_cycle(p, prev_regime="TREND_DOWN")
        assert p["update_grid_center"].called, (
            "update_grid_center() must be called after regime-transition redeploy"
        )

    def test_none_prev_regime_does_not_trigger_redeploy(self):
        """Fresh engine start (_prev_regime=None) must not trigger spurious redeploy."""
        p = _engine_patches(regime="RANGE")
        _run_cycle(p, prev_regime=None)
        assert not p["redeploy_all_bots"].called


class TestIntensiveTierFeeGuard:
    """BUY_ONLY/SELL_ONLY tier transforms must preserve the fee floor."""

    def _narrow_tiers(self):
        return [
            {
                "name": "inner",
                "grid_low": 69000,
                "grid_high": 70400,
                "levels": 8,
                "step": 200,
                "min_step": 420,
                "fee_ok": False,
                "grid_levels": [],
            },
            {
                "name": "mid",
                "grid_low": 68000,
                "grid_high": 71000,
                "levels": 6,
                "step": 600,
                "min_step": 420,
                "fee_ok": True,
                "grid_levels": [],
            },
        ]

    def test_intensive_sell_reduces_levels_until_fee_ok(self):
        import engine

        tiers = engine._make_intensive_sell_tiers(70000, self._narrow_tiers())

        inner = tiers[0]
        assert inner["levels"] < 8
        assert inner["step"] >= inner["min_step"]
        assert inner["fee_ok"] is True
        assert len(inner["grid_levels"]) == inner["levels"]
        assert inner["grid_low"] > 70000

    def test_intensive_buy_reduces_levels_until_fee_ok(self):
        import engine

        tiers = engine._make_intensive_buy_tiers(70000, self._narrow_tiers())

        inner = tiers[0]
        assert inner["levels"] < 8
        assert inner["step"] >= inner["min_step"]
        assert inner["fee_ok"] is True
        assert len(inner["grid_levels"]) == inner["levels"]
        assert inner["grid_high"] < 70000
