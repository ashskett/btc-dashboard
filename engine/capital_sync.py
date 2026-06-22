"""Authoritative capital-events ledger for the P&L page.

Pulls real deposits / withdrawals / external crypto transfers from the Coinbase
account (read-only API) and writes them to capital_events.json with USD values.
This REPLACES the old heuristic auto-detector in pnl.html, which mistook trades /
USDC<->BTC converts for deposits.

Classification (from the Coinbase transaction `type`):
  CAPITAL FLOW (logged):  fiat_deposit, fiat_withdrawal, send (crypto in/out)
  IGNORED (not capital):  advanced_trade_fill, trade, buy, sell  (incl USDC/BTC
                          converts), interest, subscription, and `tx` (internal
                          +/- pairs that net to zero)

USD value: USDC/USDT/USD ~= amount; GBP/EUR via the Coinbase BTC-USD/BTC-fiat
ratio at the transaction date; other crypto via its ASSET-USD spot at that date.

Manual capital_events (those without a `txid`) are preserved; synced ones are
keyed by Coinbase transaction id so re-runs update rather than duplicate.
"""
import os
import json
import datetime

import requests
import coinbase_capital as cc

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(HERE, "capital_events.json")

CAPITAL_TYPES = {"fiat_deposit", "fiat_withdrawal", "send"}
STABLE = {"USDC", "USDT", "USD", "DAI", "PYUSD"}

_spot_cache = {}


def _spot(pair, date):
    """Coinbase spot price for `pair` (e.g. BTC-USD) on YYYY-MM-DD, cached."""
    key = (pair, date)
    if key in _spot_cache:
        return _spot_cache[key]
    try:
        r = requests.get(f"https://api.coinbase.com/v2/prices/{pair}/spot",
                         params={"date": date}, timeout=15)
        v = float(r.json()["data"]["amount"]) if r.ok else 0.0
    except Exception:
        v = 0.0
    _spot_cache[key] = v
    return v


def _fiat_to_usd(code, date):
    """USD per 1 unit of fiat `code` on date, derived from BTC cross-rates
    (Coinbase reliably has BTC-USD and BTC-GBP/EUR)."""
    if code == "USD":
        return 1.0
    btc_usd = _spot("BTC-USD", date)
    btc_fiat = _spot(f"BTC-{code}", date)
    return (btc_usd / btc_fiat) if (btc_usd and btc_fiat) else 0.0


def _usd_value(asset, amount, date):
    a = (asset or "").upper()
    if a in STABLE:
        return abs(amount)
    if a in ("GBP", "EUR"):
        return abs(amount) * _fiat_to_usd(a, date)
    spot = _spot(f"{a}-USD", date)
    return abs(amount) * spot if spot else 0.0


def build_events():
    """Return the list of synced capital events from Coinbase."""
    accs = cc.list_accounts()
    want = {"GBP", "EUR", "USD", "USDC", "BTC"}
    rel = [a for a in accs if cc._ccode(a) in want]
    out = []
    seen = set()
    for a in rel:
        try:
            txs = cc.list_transactions(a.get("id"))
        except Exception:
            continue
        for t in txs:
            if t.get("type") not in CAPITAL_TYPES:
                continue
            txid = t.get("id")
            if not txid or txid in seen:
                continue
            seen.add(txid)
            amt = t.get("amount", {}) or {}
            asset = amt.get("currency")
            qty = float(amt.get("amount") or 0)
            created = t.get("created_at", "")
            date = created[:10] or datetime.date.today().isoformat()
            try:
                ts = datetime.datetime.fromisoformat(
                    created.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = datetime.datetime.now().timestamp()
            usd = _usd_value(asset, qty, date)
            usd = usd if qty >= 0 else -usd  # withdrawals / sends-out are negative
            label = "%s %s %s" % (
                t.get("type").replace("_", " "),
                ("%+.4f" % qty).rstrip("0").rstrip("."), asset)
            out.append({
                "ts": round(ts, 0),
                "amount_usd": round(usd, 2),
                "label": label,
                "txid": txid,
                "type": t.get("type"),
                "asset": asset,
                "source": "coinbase",
            })
    out.sort(key=lambda e: e["ts"])
    return out


def sync():
    """Refresh capital_events.json: keep manual events, replace synced ones."""
    existing = []
    if os.path.exists(EVENTS_FILE):
        try:
            existing = json.load(open(EVENTS_FILE))
        except Exception:
            existing = []
    manual = [e for e in existing if not e.get("txid")]
    synced = build_events()
    merged = manual + synced
    merged.sort(key=lambda e: e.get("ts", 0))
    with open(EVENTS_FILE, "w") as f:
        json.dump(merged, f, indent=2)
    return {"manual": len(manual), "synced": len(synced), "total": len(merged)}


if __name__ == "__main__":
    res = sync()
    print("capital_sync:", res)
    # quick summary
    evs = json.load(open(EVENTS_FILE))
    net = sum(e["amount_usd"] for e in evs)
    print("net capital (USD): ${:+,.0f} across {} events".format(net, len(evs)))
    for e in evs[-8:]:
        d = datetime.datetime.utcfromtimestamp(e["ts"]).strftime("%Y-%m-%d")
        print("  %s  $%+9.0f  %s" % (d, e["amount_usd"], e["label"]))
