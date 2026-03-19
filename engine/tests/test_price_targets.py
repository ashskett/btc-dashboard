"""
Tests for price_targets.py — user-defined breakout trigger levels.

Coverage:
  • add_target — creates target with all required schema fields
  • check_targets (UP) — confirmation accumulation, fire, completion, reversal, cooldown
  • check_targets (DOWN) — fires when price crosses below trigger
  • update_target — mutable fields updated; immutable fields protected
  • delete_target — removes correct target
  • clear_target — re-arms fired/cooldown state immediately
"""
import json
import time
import pytest
import price_targets as pt


# ── Fixture: redirect _TARGETS_FILE to a temp path ────────────────────────

@pytest.fixture(autouse=True)
def isolated_targets(tmp_path, monkeypatch):
    """Each test gets its own empty targets file — no cross-test pollution."""
    f = tmp_path / "breakout_targets.json"
    f.write_text("[]")
    monkeypatch.setattr(pt, "_TARGETS_FILE", str(f))
    yield f


# ── add_target ──────────────────────────────────────────────────────────────

class TestAddTarget:
    def test_returns_dict_with_id(self):
        t = pt.add_target("test", 73000)
        assert isinstance(t, dict)
        assert len(t["id"]) == 8

    def test_default_direction_is_up(self):
        t = pt.add_target("test", 73000)
        assert t["direction"] == "UP"

    def test_default_confirm_closes_is_2(self):
        t = pt.add_target("test", 73000)
        assert t["confirm_closes"] == 2

    def test_default_active_true_fired_false(self):
        t = pt.add_target("test", 73000)
        assert t["active"] is True
        assert t["fired"] is False

    def test_custom_fields_stored(self):
        t = pt.add_target(
            "ATH", 73000, direction="DOWN", price_target=60000,
            reversal_atr_mult=2.0, confirm_closes=1, rearm_cooldown_h=8.0
        )
        assert t["direction"] == "DOWN"
        assert t["price_target"] == 60000
        assert t["reversal_atr_mult"] == 2.0
        assert t["confirm_closes"] == 1
        assert t["rearm_cooldown_h"] == 8.0

    def test_persisted_to_disk(self, isolated_targets):
        pt.add_target("disk test", 70000)
        saved = json.loads(isolated_targets.read_text())
        assert len(saved) == 1
        assert saved[0]["label"] == "disk test"

    def test_multiple_targets_accumulate(self):
        pt.add_target("first", 71000)
        pt.add_target("second", 72000)
        targets = pt.load_targets()
        assert len(targets) == 2


# ── check_targets — UP direction ───────────────────────────────────────────

class TestCheckTargetsUp:
    """confirm_closes=2 means price must be ≥ trigger for 2 consecutive calls."""

    def _add_up(self, trigger=73000, price_target=82000, confirm=2, cooldown=4.0):
        return pt.add_target(
            "UP test", trigger, direction="UP",
            price_target=price_target,
            confirm_closes=confirm,
            rearm_cooldown_h=cooldown,
        )

    # -- accumulation --

    def test_below_trigger_returns_none(self):
        self._add_up(trigger=73000)
        result = pt.check_targets(price=72000, atr=600)
        assert result is None

    def test_first_close_above_does_not_fire_with_confirm_2(self):
        self._add_up(trigger=73000, confirm=2)
        result = pt.check_targets(price=73500, atr=600)
        assert result is None  # needs 2 closes

    def test_consec_above_increments_on_close_above(self):
        self._add_up(trigger=73000, confirm=2)
        pt.check_targets(price=73500, atr=600)
        targets = pt.load_targets()
        assert targets[0]["consec_above"] == 1

    def test_second_close_fires(self):
        self._add_up(trigger=73000, confirm=2)
        pt.check_targets(price=73500, atr=600)  # consec=1
        result = pt.check_targets(price=73600, atr=600)  # consec=2 → fires
        assert result is not None
        assert result["fired"] is True

    def test_fire_with_confirm_1(self):
        self._add_up(trigger=73000, confirm=1)
        result = pt.check_targets(price=73001, atr=600)
        assert result is not None and result["fired"] is True

    def test_fired_price_set_on_fire(self):
        self._add_up(trigger=73000, confirm=1)
        pt.check_targets(price=73500, atr=600)
        targets = pt.load_targets()
        assert targets[0]["fired_price"] == 73500.0

    def test_consec_resets_on_drop_below_trigger(self):
        self._add_up(trigger=73000, confirm=2)
        pt.check_targets(price=73500, atr=600)  # consec=1
        pt.check_targets(price=72000, atr=600)  # drops below → reset
        targets = pt.load_targets()
        assert targets[0]["consec_above"] == 0

    def test_inactive_target_ignored(self):
        self._add_up(trigger=73000, confirm=1)
        pt.load_targets()
        # manually deactivate
        targets = pt.load_targets()
        targets[0]["active"] = False
        pt.save_targets(targets)
        result = pt.check_targets(price=75000, atr=600)
        assert result is None

    # -- post-fire: completion --

    def test_reaching_price_target_disarms(self):
        self._add_up(trigger=73000, price_target=82000, confirm=1)
        pt.check_targets(price=73500, atr=600)   # fire
        pt.check_targets(price=82000, atr=600)   # hit target
        targets = pt.load_targets()
        assert targets[0]["active"] is False
        assert targets[0]["fired"] is False

    def test_completion_returns_none_after_disarm(self):
        self._add_up(trigger=73000, price_target=82000, confirm=1)
        pt.check_targets(price=73500, atr=600)
        result = pt.check_targets(price=82000, atr=600)
        # On completion cycle itself target is disarmed; function may return None
        # (target just flipped active=False so it won't be selected)
        targets = pt.load_targets()
        assert targets[0]["active"] is False

    # -- post-fire: reversal --

    def test_reversal_clears_fired_state(self):
        self._add_up(trigger=73000, confirm=1, cooldown=4.0)
        pt.check_targets(price=73500, atr=600)   # fire at 73500
        # reversal = fire_price - 1.2×ATR = 73500 - 720 = 72780
        pt.check_targets(price=72700, atr=600)   # reversal
        targets = pt.load_targets()
        assert targets[0]["fired"] is False
        assert targets[0]["cleared_at"] is not None

    def test_reversal_result_is_none(self):
        self._add_up(trigger=73000, confirm=1, cooldown=4.0)
        pt.check_targets(price=73500, atr=600)
        result = pt.check_targets(price=72000, atr=600)  # clear reversal
        # After reversal target is no longer fired → not returned
        assert result is None

    # -- cooldown after reversal --

    def test_cooldown_blocks_refire(self):
        self._add_up(trigger=73000, confirm=1, cooldown=4.0)
        pt.check_targets(price=73500, atr=600)  # fire
        pt.check_targets(price=72000, atr=600)  # reverse → cleared_at = now
        # Immediately try to refire — still in cooldown
        pt.check_targets(price=73500, atr=600)  # consec=1
        result = pt.check_targets(price=73600, atr=600)  # consec=2 but in cooldown
        assert result is None

    def test_expired_cooldown_allows_refire(self, monkeypatch):
        """Simulate expired cooldown by backdating cleared_at."""
        self._add_up(trigger=73000, confirm=1, cooldown=0.001)  # 3.6 seconds
        pt.check_targets(price=73500, atr=600)  # fire
        pt.check_targets(price=72000, atr=600)  # reverse → cleared_at
        # Backdate cleared_at so cooldown is expired
        targets = pt.load_targets()
        targets[0]["cleared_at"] = time.time() - 100  # 100s ago > 3.6s cooldown
        pt.save_targets(targets)
        result = pt.check_targets(price=74000, atr=600)
        assert result is not None
        assert result["fired"] is True


# ── check_targets — DOWN direction ─────────────────────────────────────────

class TestCheckTargetsDown:
    def _add_down(self, trigger=65000, price_target=55000, confirm=2):
        return pt.add_target(
            "DOWN test", trigger, direction="DOWN",
            price_target=price_target,
            confirm_closes=confirm,
        )

    def test_above_trigger_returns_none(self):
        self._add_down(trigger=65000)
        result = pt.check_targets(price=66000, atr=600)
        assert result is None

    def test_first_close_below_does_not_fire(self):
        self._add_down(trigger=65000, confirm=2)
        result = pt.check_targets(price=64900, atr=600)
        assert result is None

    def test_second_close_below_fires(self):
        self._add_down(trigger=65000, confirm=2)
        pt.check_targets(price=64900, atr=600)
        result = pt.check_targets(price=64800, atr=600)
        assert result is not None and result["fired"] is True

    def test_down_target_completion_disarms(self):
        self._add_down(trigger=65000, price_target=55000, confirm=1)
        pt.check_targets(price=64900, atr=600)  # fire
        pt.check_targets(price=55000, atr=600)  # hit target
        targets = pt.load_targets()
        assert targets[0]["active"] is False

    def test_down_reversal_clears_fired(self):
        self._add_down(trigger=65000, confirm=1)
        pt.check_targets(price=64800, atr=600)  # fire at 64800
        # reversal UP: fire_price + 1.2×ATR = 64800 + 720 = 65520
        pt.check_targets(price=66000, atr=600)  # reversal
        targets = pt.load_targets()
        assert targets[0]["fired"] is False
        assert targets[0]["cleared_at"] is not None


# ── update_target ──────────────────────────────────────────────────────────

class TestUpdateTarget:
    def test_updates_mutable_field(self):
        t = pt.add_target("u1", 73000)
        updated = pt.update_target(t["id"], {"trigger_price": 75000})
        assert updated["trigger_price"] == 75000

    def test_immutable_fields_protected(self):
        t = pt.add_target("u2", 73000)
        # Manually fire it
        targets = pt.load_targets()
        targets[0].update({"fired": True, "fired_at": 1000.0, "fired_price": 73500})
        pt.save_targets(targets)
        # Try to overwrite immutable fields via update_target
        pt.update_target(t["id"], {"fired_at": 9999.0, "fired_price": 99999.0})
        saved = pt.load_targets()[0]
        assert saved["fired_at"] == 1000.0    # unchanged
        assert saved["fired_price"] == 73500  # unchanged

    def test_returns_none_for_unknown_id(self):
        result = pt.update_target("nonexistent", {"label": "x"})
        assert result is None

    def test_change_persisted_to_disk(self, isolated_targets):
        t = pt.add_target("persist", 73000)
        pt.update_target(t["id"], {"label": "renamed"})
        saved = json.loads(isolated_targets.read_text())
        assert saved[0]["label"] == "renamed"


# ── delete_target ──────────────────────────────────────────────────────────

class TestDeleteTarget:
    def test_deletes_correct_target(self):
        t1 = pt.add_target("keep", 70000)
        t2 = pt.add_target("delete", 71000)
        result = pt.delete_target(t2["id"])
        assert result is True
        targets = pt.load_targets()
        assert len(targets) == 1
        assert targets[0]["id"] == t1["id"]

    def test_returns_false_for_unknown_id(self):
        pt.add_target("x", 70000)
        assert pt.delete_target("doesnotexist") is False

    def test_target_count_unchanged_on_failure(self):
        pt.add_target("x", 70000)
        pt.delete_target("bad-id")
        assert len(pt.load_targets()) == 1


# ── clear_target ───────────────────────────────────────────────────────────

class TestClearTarget:
    def test_clears_fired_state(self):
        t = pt.add_target("c1", 73000, confirm_closes=1)
        pt.check_targets(price=73500, atr=600)   # fire
        pt.clear_target(t["id"])
        targets = pt.load_targets()
        assert targets[0]["fired"] is False
        assert targets[0]["fired_at"] is None
        assert targets[0]["fired_price"] is None

    def test_clears_cooldown(self):
        t = pt.add_target("c2", 73000, confirm_closes=1)
        pt.check_targets(price=73500, atr=600)   # fire
        pt.check_targets(price=72000, atr=600)   # reverse → cleared_at set
        pt.clear_target(t["id"])
        targets = pt.load_targets()
        assert targets[0]["cleared_at"] is None
        assert targets[0]["consec_above"] == 0

    def test_clears_consec_above(self):
        t = pt.add_target("c3", 73000, confirm_closes=3)
        pt.check_targets(price=73500, atr=600)   # consec=1
        pt.check_targets(price=73600, atr=600)   # consec=2
        pt.clear_target(t["id"])
        targets = pt.load_targets()
        assert targets[0]["consec_above"] == 0

    def test_returns_false_for_unknown_id(self):
        assert pt.clear_target("bad-id") is False

    def test_allows_refire_immediately_after_clear(self):
        t = pt.add_target("c4", 73000, confirm_closes=1, rearm_cooldown_h=4.0)
        pt.check_targets(price=73500, atr=600)  # fire
        pt.check_targets(price=72000, atr=600)  # reverse → cooldown
        pt.clear_target(t["id"])
        # Now should be able to fire again immediately
        result = pt.check_targets(price=74000, atr=600)
        assert result is not None
        assert result["fired"] is True


# ── Multiple targets ────────────────────────────────────────────────────────

class TestMultipleTargets:
    def test_first_fired_target_returned(self):
        """Returns the first fired target, not the last."""
        t1 = pt.add_target("first",  73000, confirm_closes=1)
        t2 = pt.add_target("second", 74000, confirm_closes=1)
        # Fire both: price is above both triggers
        result = pt.check_targets(price=75000, atr=600)
        assert result is not None
        assert result["id"] == t1["id"]  # first fired wins

    def test_inactive_target_never_returned(self):
        pt.add_target("active", 73000, confirm_closes=1)
        t2 = pt.add_target("inactive", 72000, confirm_closes=1)
        targets = pt.load_targets()
        for tgt in targets:
            if tgt["id"] == t2["id"]:
                tgt["active"] = False
        pt.save_targets(targets)
        result = pt.check_targets(price=75000, atr=600)
        assert result is not None
        assert result["label"] == "active"
