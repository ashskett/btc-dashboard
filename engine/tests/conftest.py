"""
Shared fixtures and import setup for engine tests.

All heavy/server-only dependencies are mocked at the sys.modules level before
any engine code is imported, so tests can run in a plain CI environment without
3Commas credentials, ccxt, Flask, or a live market feed.
"""
import sys
import os
from unittest.mock import MagicMock

# ── Mock server-only / hard-to-install modules ─────────────────────────────
# Must happen before any engine module is imported (even transitively).
_MOCK_MODULES = [
    "ta",
    "ccxt",
    "schedule",
    "dotenv",
    "requests",
    "flask",
    "flask_cors",
    "cryptography",
    "cryptography.hazmat",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.asymmetric.padding",
    "cryptography.hazmat.backends",   # threecommas.py: from cryptography.hazmat.backends import default_backend
    # server-only helper imported by engine.py
    "dashboard",
]
for _mod in _MOCK_MODULES:
    sys.modules.setdefault(_mod, MagicMock())

# Add engine directory to path so tests can import engine modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Fixtures ───────────────────────────────────────────────────────────────
import pytest
import pandas as pd


def _build_df(closes, atr=600.0, bb_width=0.02):
    """Build a minimal OHLCV DataFrame with pre-computed indicators."""
    n = len(closes)
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


@pytest.fixture
def make_df():
    """Factory: make_df(n=50, price=70000, atr=600, bb_width=0.02, trend=None)."""
    def _factory(n=50, price=70000.0, atr=600.0, bb_width=0.02, trend=None):
        if trend == "up":
            closes = [price + i * atr * 0.15 for i in range(n)]
        elif trend == "down":
            closes = [price - i * atr * 0.15 for i in range(n)]
        else:
            closes = [price] * n
        return _build_df(closes, atr=atr, bb_width=bb_width)
    return _factory


@pytest.fixture
def flat_df(make_df):
    return make_df()


@pytest.fixture
def custom_closes():
    """Helper to build a df from an explicit close list."""
    def _factory(closes, atr=600.0, bb_width=0.02):
        return _build_df(closes, atr=atr, bb_width=bb_width)
    return _factory
