"""
Tests for session.py — market session detection and weekend helper.

get_session() returns one of:
  Weekday (Mon–Fri UTC):
    "ASIA"    00:00–06:59
    "EUROPE"  07:00–12:59
    "US"      13:00–20:59
    "ASIA"    21:00–23:59  (late-night re-enters ASIA)

  Weekend (Sat–Sun UTC):
    "WKD_ASIA"  00:00–07:59
    "WKD_EU"    08:00–15:59
    "WKD_US"    16:00–23:59

is_weekend() returns True only on Saturday or Sunday.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch
import session as sess


def _dt(weekday, hour, minute=0):
    """
    Build a timezone-aware UTC datetime with the given weekday (0=Mon … 6=Sun)
    and hour.  The year/month/day are chosen so weekday() matches.
    2026-03-16 is a Monday (weekday 0).
    """
    # Monday 2026-03-16 + weekday offset
    from datetime import timedelta
    monday = datetime(2026, 3, 16, 0, 0, 0, tzinfo=timezone.utc)
    return monday + timedelta(days=weekday, hours=hour, minutes=minute)


def _patch_now(weekday, hour, minute=0):
    """Return a context manager that patches datetime.now() inside session.py."""
    dt = _dt(weekday, hour, minute)
    return patch("session.datetime", wraps=datetime) , dt


# We need to patch the datetime class *inside* the session module.
# The simplest approach: replace datetime.now with a lambda.

class _FakeDatetime(datetime):
    """Subclass that overrides now() with a fixed value."""
    _now_val = None

    @classmethod
    def now(cls, tz=None):
        return cls._now_val


def _set_now(weekday, hour, minute=0):
    """Patch session.datetime to return a fixed UTC datetime."""
    _FakeDatetime._now_val = _dt(weekday, hour, minute)
    return patch("session.datetime", _FakeDatetime)


# ── Weekday sessions ───────────────────────────────────────────────────────

class TestWeekdaySession:
    """Monday = weekday 0."""

    def test_midnight_is_asia(self):
        with _set_now(0, 0):
            assert sess.get_session() == "ASIA"

    def test_hour_6_is_asia(self):
        with _set_now(0, 6):
            assert sess.get_session() == "ASIA"

    def test_hour_7_is_europe(self):
        with _set_now(0, 7):
            assert sess.get_session() == "EUROPE"

    def test_hour_12_is_europe(self):
        with _set_now(0, 12):
            assert sess.get_session() == "EUROPE"

    def test_hour_13_is_us(self):
        with _set_now(0, 13):
            assert sess.get_session() == "US"

    def test_hour_20_is_us(self):
        with _set_now(0, 20):
            assert sess.get_session() == "US"

    def test_hour_21_is_asia_late(self):
        """21:00–23:59 wraps back to ASIA."""
        with _set_now(0, 21):
            assert sess.get_session() == "ASIA"

    def test_hour_23_is_asia(self):
        with _set_now(0, 23):
            assert sess.get_session() == "ASIA"

    def test_friday_is_weekday(self):
        """Friday = weekday 4 — should use weekday session names."""
        with _set_now(4, 10):
            assert sess.get_session() == "EUROPE"


# ── Weekend sessions ───────────────────────────────────────────────────────

class TestWeekendSession:
    """Saturday = weekday 5, Sunday = weekday 6."""

    def test_saturday_midnight_is_wkd_asia(self):
        with _set_now(5, 0):
            assert sess.get_session() == "WKD_ASIA"

    def test_saturday_hour_7_is_wkd_asia(self):
        with _set_now(5, 7):
            assert sess.get_session() == "WKD_ASIA"

    def test_saturday_hour_8_is_wkd_eu(self):
        with _set_now(5, 8):
            assert sess.get_session() == "WKD_EU"

    def test_saturday_hour_15_is_wkd_eu(self):
        with _set_now(5, 15):
            assert sess.get_session() == "WKD_EU"

    def test_saturday_hour_16_is_wkd_us(self):
        with _set_now(5, 16):
            assert sess.get_session() == "WKD_US"

    def test_saturday_hour_23_is_wkd_us(self):
        with _set_now(5, 23):
            assert sess.get_session() == "WKD_US"

    def test_sunday_uses_weekend_sessions(self):
        with _set_now(6, 12):
            assert sess.get_session() == "WKD_EU"


# ── is_weekend ─────────────────────────────────────────────────────────────

class TestIsWeekend:
    def test_monday_is_not_weekend(self):
        with _set_now(0, 12):
            assert sess.is_weekend() is False

    def test_friday_is_not_weekend(self):
        with _set_now(4, 12):
            assert sess.is_weekend() is False

    def test_saturday_is_weekend(self):
        with _set_now(5, 12):
            assert sess.is_weekend() is True

    def test_sunday_is_weekend(self):
        with _set_now(6, 12):
            assert sess.is_weekend() is True


# ── Session boundary precision ─────────────────────────────────────────────

class TestSessionBoundaries:
    """Check exact boundaries — off-by-one hour would be a bug."""

    @pytest.mark.parametrize("hour,expected", [
        (6,  "ASIA"),
        (7,  "EUROPE"),
        (12, "EUROPE"),
        (13, "US"),
        (20, "US"),
        (21, "ASIA"),
    ])
    def test_weekday_hour_boundaries(self, hour, expected):
        with _set_now(1, hour):  # Tuesday
            assert sess.get_session() == expected

    @pytest.mark.parametrize("hour,expected", [
        (7,  "WKD_ASIA"),
        (8,  "WKD_EU"),
        (15, "WKD_EU"),
        (16, "WKD_US"),
    ])
    def test_weekend_hour_boundaries(self, hour, expected):
        with _set_now(5, hour):  # Saturday
            assert sess.get_session() == expected
