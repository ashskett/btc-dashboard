"""Tests for fills_capture.py — every-cycle BUY/SELL persistence."""
import json
import fills_capture as fc


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
    def json(self):
        return self._p


_ORDERS = {"balancing_orders": [
    {"order_id": "o1", "status_string": "Filled", "average_price": "60000",
     "order_type": "buy", "quantity": "0.1", "created_at": "2026-06-10T00:00:00Z"},
    {"order_id": "o2", "status_string": "Filled", "rate": "61000",
     "order_type": "sell", "quantity": "0.1", "created_at": "2026-06-10T01:00:00Z"},
    {"order_id": "o3", "status_string": "Active", "average_price": "62000",
     "order_type": "buy"},  # not filled → skipped
]}


def _patch(monkeypatch, tmp_path, payload=_ORDERS, status=200):
    f = tmp_path / "fills_log.jsonl"
    f.write_text("")
    monkeypatch.setattr(fc, "FILLS_LOG", str(f))
    monkeypatch.setattr(fc.tc, "_signed_request",
                        lambda m, p: _Resp(status, payload))
    return f


def test_appends_filled_buy_and_sell(monkeypatch, tmp_path):
    f = _patch(monkeypatch, tmp_path)
    n = fc.capture(["123"])
    assert n == 2  # o1 BUY + o2 SELL; o3 (Active) skipped
    rows = [json.loads(l) for l in open(str(f)) if l.strip()]
    assert {r["side"] for r in rows} == {"BUY", "SELL"}
    assert any(r["side"] == "BUY" and r["price"] == 60000 for r in rows)


def test_dedup_on_second_call(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    assert fc.capture(["123"]) == 2
    assert fc.capture(["123"]) == 0  # same order_ids → nothing new


def test_non_200_is_safe(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, status=500)
    assert fc.capture(["123"]) == 0


def test_empty_bot_ids(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    assert fc.capture([]) == 0
