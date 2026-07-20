"""Every-cycle BUY/SELL fill capture (observability only).

Why this exists: 3Commas wipes a grid bot's `market_orders` (the only source
with the BUY leg) on every bot restart, and the engine restarts bots often
(recentres, regime flips, redeploys). The `profits` endpoint persists but
records the SELL/profit leg only — so on the chart, buys vanished while sells
piled up. The dashboard only snapshotted market_orders when /bots/fills was
polled, which is far less frequent than bots restart, so most buys were lost.

Fix: the engine calls capture() every cycle, persisting each filled BUY/SELL
into fills_log.jsonl within ~2.5 min of the fill — before any restart can wipe
it. De-duped by order_id; same record format the dashboard already reads.

Read-only w.r.t. trading. Sole side effect: appending de-duped fill lines to
fills_log.jsonl. It cannot recover buys 3Commas already deleted — it fixes the
chart going forward.
"""
import json
import os

import threecommas as tc

HERE = os.path.dirname(os.path.abspath(__file__))
FILLS_LOG = os.path.join(HERE, "fills_log.jsonl")


def _existing_ids():
    ids = set()
    if os.path.exists(FILLS_LOG):
        with open(FILLS_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(json.loads(line).get("order_id"))
                except Exception:
                    pass
    return ids


def capture(bot_ids):
    """Fetch each grid bot's filled market_orders and append any NEW BUY/SELL
    fills to fills_log.jsonl (deduped by order_id). Returns count appended.

    Mirrors the dashboard's market_orders parsing so the on-disk format is
    identical. Each bot fetch is independent — one failing never blocks others.
    """
    if not bot_ids:
        return 0
    seen = _existing_ids()
    new = []
    for idx, bid in enumerate(list(bot_ids)[:3]):
        bid = str(bid)
        try:
            r = tc._signed_request(
                "GET", f"/ver1/grid_bots/{bid}/market_orders?limit=200")
            if r.status_code != 200:
                continue
            data = r.json()
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # BOTH streams move real BTC: grid_lines_orders = rung fills,
        # balancing_orders = base-position buys/sells on enable/disable.
        # (Bug until 2026-07-06: only balancing_orders was read, so every grid
        # rung fill was missing from fills_log and the chart's fill arrows.)
        for stream in ("grid_lines_orders", "balancing_orders"):
            for item in data.get(stream) or []:
                if item.get("status_string") != "Filled":
                    continue
                price = float(item.get("average_price") or item.get("rate") or 0)
                if not price:
                    continue
                ts = (item.get("created_at") or item.get("updated_at")
                      or item.get("update_at"))
                oid = item.get("order_id")
                if oid is None:   # balancing orders can lack an id — synthesise
                    oid = "%s-%s-%s-%.8f" % (stream[:3], ts,
                                             (item.get("order_type") or "").upper(),
                                             float(item.get("quantity") or 0))
                if oid in seen:
                    continue
                seen.add(oid)
                new.append({
                    "order_id":  oid,
                    "bot_id":    bid,
                    "bot_index": idx,
                    "time":      ts,
                    "price":     price,
                    "side":      (item.get("order_type") or "").upper(),
                    "qty":       float(item.get("quantity") or 0),
                })
    if new:
        try:
            with open(FILLS_LOG, "a") as f:
                for fill in new:
                    f.write(json.dumps(fill) + "\n")
        except Exception as e:  # noqa: BLE001
            print(f"Warning: fills_capture write failed: {e}")
            return 0
    return len(new)
