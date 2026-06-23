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
    """Capital flows INTO/OUT OF the tracked BTC+USDC portfolio, netted per day.

    portfolio_log tracks only BTC + USDC. So a "capital flow" for it is any change
    to BTC+USDC value that ISN'T a BTC<->USDC grid trade:
      - external sends of BTC/USDC (cold storage, Kraken transfers, theft)
      - conversions of OTHER assets into/out of USDC or BTC (GBP cash, or alt
        holdings like ETH/TAO/HYPE sold to USDC) — these jump the BTC+USDC value
        even though total wealth is unchanged.

    Trick: sum the GBP-value of EVERY BTC and USDC transaction leg. A BTC<->USDC
    grid trade has equal-and-opposite legs (+USDC, -BTC) that CANCEL. What's left
    is exactly the sends + the conversions from/to non-tracked assets. We net per
    day to keep the ledger small (thousands of grid fills collapse to ~0/day)."""
    accs = cc.list_accounts()
    # ONLY the tracked-space accounts. Other-asset legs (GBP/ETH/...) sit outside
    # BTC+USDC and must not be summed — their effect shows via the USDC/BTC leg.
    rel = [a for a in accs if cc._ccode(a) in {"BTC", "USDC"}]
    INCLUDE = {"send", "advanced_trade_fill", "fiat_deposit", "fiat_withdrawal", "trade", "buy", "sell"}
    # date -> {"net": GBP, "ts": timestamp of the largest single tx that day}
    # Timestamping at the largest tx (not noon) aligns the subtraction with the
    # actual portfolio_usd jump, so the deposit-adjusted chart doesn't spike.
    daily = {}
    for a in rel:
        try:
            txs = cc.list_transactions(a.get("id"))
        except Exception:
            continue
        for t in txs:
            if t.get("type") not in INCLUDE:
                continue
            nat = float((t.get("native_amount") or {}).get("amount") or 0)  # GBP, signed
            if nat == 0:
                continue
            created = t.get("created_at", "") or ""
            date = created[:10] or datetime.date.today().isoformat()
            try:
                tts = datetime.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except Exception:
                tts = None
            rec = daily.setdefault(date, {"net": 0.0, "ts": None, "maxabs": 0.0})
            rec["net"] += nat
            if tts is not None and abs(nat) > rec["maxabs"]:
                rec["maxabs"] = abs(nat)
                rec["ts"] = tts

    out = []
    for date, rec in sorted(daily.items()):
        gbp = rec["net"]
        if abs(gbp) < 50:   # ignore sub-£50 daily residue (rounding / dust)
            continue
        usd = gbp * (_fiat_to_usd("GBP", date) or 1.33)
        ts = rec["ts"]
        if ts is None:
            try:
                ts = datetime.datetime.fromisoformat(date + "T12:00:00+00:00").timestamp()
            except Exception:
                ts = datetime.datetime.now().timestamp()
        out.append({
            "ts": round(ts, 0),
            "amount_usd": round(usd, 2),
            "label": "net capital flow into BTC+USDC (£%+.0f)" % gbp,
            "txid": "netflow-%s" % date,   # deterministic id -> dedups on re-sync
            "type": "netflow",
            "source": "coinbase",
        })
    out.sort(key=lambda e: e["ts"])
    return out


ALERT_SEEN_FILE = os.path.join(HERE, "capital_alerts_seen.json")
ALERT_THRESHOLD_USD = 1500   # Telegram-alert real external moves at/above this


def check_new_capital_alerts():
    """Detect NEW external deposits/withdrawals/crypto-sends and Telegram-alert the
    large ones, so a real capital move never goes unnoticed. Alt<->USDC converts
    are NOT alerted (they're trading, not capital). First run just seeds the
    seen-set (no alert spam on history)."""
    try:
        import notify
    except Exception:
        notify = None
    try:
        seen = set(json.load(open(ALERT_SEEN_FILE)))
    except Exception:
        seen = set()
    first_run = not seen
    accs = cc.list_accounts()
    rel = [a for a in accs if cc._ccode(a) in {"GBP", "EUR", "USD", "USDC", "BTC"}]
    new_alerts = []
    for a in rel:
        try:
            txs = cc.list_transactions(a.get("id"))
        except Exception:
            continue
        for t in txs:
            if t.get("type") not in ("fiat_deposit", "fiat_withdrawal", "send"):
                continue
            txid = t.get("id")
            if not txid or txid in seen:
                continue
            seen.add(txid)
            amt = t.get("amount", {}) or {}
            asset = amt.get("currency")
            qty = float(amt.get("amount") or 0)
            date = (t.get("created_at", "") or "")[:10]
            usd = _usd_value(asset, qty, date)
            usd = usd if qty >= 0 else -usd
            if abs(usd) >= ALERT_THRESHOLD_USD:
                new_alerts.append((date, usd, t.get("type"), asset, qty))
    try:
        json.dump(sorted(seen), open(ALERT_SEEN_FILE, "w"))
    except Exception:
        pass
    if first_run:
        return {"alerted": 0, "seeded": len(seen), "first_run": True}
    for date, usd, typ, asset, qty in new_alerts:
        sign = "+" if usd >= 0 else "-"
        msg = (f"💰 Capital move detected: {sign}${abs(usd):,.0f}  "
               f"({typ.replace('_', ' ')} {abs(qty):.4f} {asset}, {date}). "
               f"Check the P&L page is still accurate.")
        if notify:
            notify.notify_critical(msg)
        print("ALERT:", msg)
    return {"alerted": len(new_alerts), "seen": len(seen)}


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
    alerts = {}
    try:
        alerts = check_new_capital_alerts()
    except Exception as e:  # never let alerting break the sync
        print("alert check failed:", e)
    return {"manual": len(manual), "synced": len(synced),
            "total": len(merged), "alerts": alerts}


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
