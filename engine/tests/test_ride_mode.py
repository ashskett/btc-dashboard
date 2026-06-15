"""Tests for ride_mode.py — armed trend-up accumulation state machine."""
import ride_mode as rm


def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "STATE_FILE", str(tmp_path / "ride_mode.json"))


def test_starts_disarmed(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    assert rm.is_armed() is False
    assert rm.get_state()["armed"] is False


def test_arm_sets_trailing_high_to_price(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    rm.arm(65000, disarm_pct=5.0)
    s = rm.get_state()
    assert s["armed"] is True
    assert s["armed_price"] == 65000 and s["trailing_high"] == 65000
    assert s["disarm_pct"] == 5.0


def test_trailing_high_ratchets_up_only(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    rm.arm(65000)
    rm.update_trailing_high(67000)
    assert rm.get_state()["trailing_high"] == 67000
    rm.update_trailing_high(66000)              # lower → no change
    assert rm.get_state()["trailing_high"] == 67000


def test_auto_disarm_threshold(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    rm.arm(65000, disarm_pct=5.0)
    rm.update_trailing_high(70000)              # high = 70000 → disarm at 66500
    assert rm.disarm_level() == 66500.0
    assert rm.should_auto_disarm(67000) is False
    assert rm.should_auto_disarm(66500) is True   # exactly at level
    assert rm.should_auto_disarm(66000) is True


def test_disarm_records_reason_and_clears_armed(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    rm.arm(65000)
    rm.disarm("auto: 5% below high")
    s = rm.get_state()
    assert s["armed"] is False and s["disarmed_reason"] == "auto: 5% below high"


def test_should_auto_disarm_false_when_disarmed(tmp_path, monkeypatch):
    _iso(tmp_path, monkeypatch)
    assert rm.should_auto_disarm(1.0) is False   # not armed
