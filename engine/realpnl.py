"""REAL cost-basis P&L for the grid bots — the honest number, red or green.

WHY THIS EXISTS
3Commas reports "profit" per grid LINE: sell_rung - buy_rung within the bot's
*current* grid frame. Because the engine recentres the grid up and down, BTC
acquired on a higher previous grid can be sold on a lower new grid and STILL be
booked as a positive "take profit" by 3Commas. So 3Commas' headline profit
overstates true performance. Ash wants the ACTUAL profit relating to price
movement — even when negative.

WHAT THIS DOES
On every grid SELL, computes the true realised P&L vs the bot's real average
entry cost:

    realised = sell_qty * (sell_price - avg_cost)

WHERE avg_cost COMES FROM (and why we DON'T self-track inventory)
A grid bot's BASE position (the BTC it holds to back its sell rungs) is acquired
and shed by 3Commas in ways that do NOT reliably show up as discrete Filled
orders in the market_orders streams. A self-maintained buy/sell ledger therefore
drifts away from the real position (observed: captured fills netted -0.26 BTC
while the bot actually held +0.31 BTC) — and a sell against an empty ledger
produced a meaningless $0.

3Commas, however, always knows the bot's real position and its mark-to-market:
    unrealized_profit_loss = btc_held * (spot - avg_cost)
  =>  avg_cost = spot - unrealized_profit_loss / btc_held
Selling at market does NOT change the average cost of the remaining position, so
sampling avg_cost once per cycle (when we detect the sell) gives the correct cost
basis for the units just sold. This is robust, needs no buy-capture, and is never
$0. We cache the last good avg_cost per bot to cover the moment a bot sells to
flat.

OBSERVABILITY ONLY. Never trades. Sole side effects: writing real_pnl_state.json
and returning new-sell events for a Telegram push.
"""
import os
import json
import time

import threecommas as tc

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "real_pnl_state.json")

BOT_NAMES = {"2743885": "Narrow", "2743889": "Mid", "2743888": "Wider"}
_MAX_PROCESSED = 4000   # cap the dedup set so the state file can't grow forever


# ---------------------------------------------------------------- 3Commas reads

def _bot_detail(bot_id):
    r = tc._signed_request("GET", f"/ver1/grid_bots/{bot_id}")
    return r.json() if r.status_code == 200 else {}


def _avg_cost(bot_id):
    """(avg_cost, btc_held, spot) backed out of 3Commas' own position figures.
    avg_cost is None when the bot holds ~no BTC (basis undefined)."""
    d = _bot_detail(bot_id)
    btc = float(d.get("investment_base_currency") or 0)
    spot = float(d.get("current_price") or 0)
    unreal = float(d.get("unrealized_profit_loss") or 0)
    avg = (spot - unreal / btc) if (btc > 1e-6 and spot) else None
    return avg, btc, spot


def _fills(bot_id):
    """Chronological filled BUY/SELL orders (both streams), for SELL detection.
    Buys are returned too but only used to mark them processed (they don't realise
    P&L — the cost basis comes from 3Commas, not from summing buys)."""
    try:
        r = tc._signed_request(
            "GET", f"/ver1/grid_bots/{bot_id}/market_orders?limit=200")
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for stream in ("grid_lines_orders", "balancing_orders"):
        for o in data.get(stream, []):
            if o.get("status_string") != "Filled":
                continue
            price = float(o.get("average_price") or o.get("rate") or 0)
            qty = float(o.get("quantity") or 0)
            side = (o.get("order_type") or "").upper()
            if not price or not qty or side not in ("BUY", "SELL"):
                continue
            ts = o.get("updated_at") or o.get("update_at") or o.get("created_at") or ""
            oid = o.get("order_id")
            if oid is None:
                oid = "%s-%s-%s-%.8f-%.2f" % (stream[:3], ts, side, qty, price)
            # grid_lines_orders = real grid-rung trade; balancing_orders =
            # base-position acquisition/liquidation (e.g. capital-protection
            # sell-off when a target parks the bots) — NOT grid profit.
            source = "grid" if stream == "grid_lines_orders" else "base"
            out.append({"order_id": oid, "ts": ts, "side": side,
                        "price": price, "qty": qty, "source": source})
    out.sort(key=lambda x: str(x["ts"]))
    return out


# ---------------------------------------------------------------- state

def _load():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"bots": {}, "processed_ids": [], "seed_ts": None, "last_update_ts": None}


def _save(state):
    pids = state.get("processed_ids", [])
    if len(pids) > _MAX_PROCESSED:
        state["processed_ids"] = pids[-_MAX_PROCESSED:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def seed(bot_ids):
    """Reset each bot's realised counter to 0, cache its current avg cost, and
    mark all existing fills processed so tracking starts clean from NOW."""
    state = _load()
    state["bots"] = {}
    processed = set(state.get("processed_ids", []))
    for bid in [str(b) for b in bot_ids][:3]:
        avg, btc, spot = _avg_cost(bid)
        state["bots"][bid] = {
            "name": BOT_NAMES.get(bid, bid),
            "avg_cost": round(avg, 2) if avg else None,
            "realised_cum": 0.0,
        }
        for fl in _fills(bid):
            processed.add(fl["order_id"])
    state["processed_ids"] = sorted(x for x in processed if x is not None)
    state["seed_ts"] = round(time.time())
    state["last_update_ts"] = round(time.time())
    _save(state)
    return state


def update(bot_ids):
    """Apply new SELL fills, valuing each against 3Commas' live average cost.
    Returns new-SELL events (each with REAL realised P&L). Auto-seeds first run."""
    state = _load()
    if not state.get("bots"):
        seed(bot_ids)
        return []
    processed = set(state.get("processed_ids", []))
    new_sells = []
    for bid in [str(b) for b in bot_ids][:3]:
        bot = state["bots"].setdefault(bid, {
            "name": BOT_NAMES.get(bid, bid), "avg_cost": None, "realised_cum": 0.0})
        # Refresh the cost basis from 3Commas (cache last good for sell-to-flat).
        avg, _btc, _spot = _avg_cost(bid)
        if avg is not None:
            bot["avg_cost"] = round(avg, 2)
        basis = bot.get("avg_cost")
        for fl in _fills(bid):
            oid = fl["order_id"]
            if oid in processed:
                continue
            processed.add(oid)
            if fl["side"] != "SELL":
                continue          # buys set future basis (already in 3C avg) — no realise
            if basis is None:
                continue          # no basis yet — skip rather than emit a bogus $0
            realised = fl["qty"] * (fl["price"] - basis)
            bot["realised_cum"] = round(bot["realised_cum"] + realised, 2)
            new_sells.append({
                "bot": bot["name"], "ts": fl["ts"], "qty": fl["qty"],
                "price": round(fl["price"], 2), "avg_cost": round(basis, 2),
                "realised": round(realised, 2), "realised_cum": bot["realised_cum"],
                "source": fl.get("source", "grid"),
            })
    state["processed_ids"] = sorted(x for x in processed if x is not None)
    state["last_update_ts"] = round(time.time())
    _save(state)
    return new_sells


def summary(bot_ids):
    """Live real-P&L snapshot per bot + totals (realised + current unrealised)."""
    state = _load()
    bots, tot_real, tot_unreal, tot_btc = [], 0.0, 0.0, 0.0
    for bid in [str(b) for b in bot_ids][:3]:
        bot = state.get("bots", {}).get(bid)
        if not bot:
            continue
        avg, btc, spot = _avg_cost(bid)
        if avg is None:
            avg = bot.get("avg_cost") or 0.0
        unreal = btc * (spot - avg) if (btc > 1e-6 and avg) else 0.0
        bots.append({
            "bot": bot["name"], "realised": round(bot.get("realised_cum", 0.0), 2),
            "inventory_btc": round(btc, 6), "avg_cost": round(avg, 2),
            "spot": round(spot, 2), "unrealised": round(unreal, 2),
            "total_real": round(bot.get("realised_cum", 0.0) + unreal, 2),
        })
        tot_real += bot.get("realised_cum", 0.0)
        tot_unreal += unreal
        tot_btc += btc
    return {
        "bots": bots,
        "total_realised": round(tot_real, 2),
        "total_unrealised": round(tot_unreal, 2),
        "total_real_pnl": round(tot_real + tot_unreal, 2),
        "total_btc_held": round(tot_btc, 6),
        "since": state.get("seed_ts"),
    }


def true_pnl(days=30, price_tol=0.015):
    """TRUE mark-to-market P&L over ~`days`: portfolio now vs then AT THE SAME BTC
    PRICE, adjusted for deposits/withdrawals. This is the number that cannot lie.

    WHY (2026-07-29): cumulative per-sell realised printed +$5,790 for a month in
    which the flow-adjusted account was DOWN ~$1,200 at equal price — cost-basis
    resets across mode-churn make Σrealised ≠ account P&L. Per-sell alerts stay
    (they're honest per trade); THIS is the headline.

    Benchmark: median portfolio_usd of snapshots aged [days-6, days+8] whose
    btc_price is within price_tol of now. If price has moved too much for an
    equal-price match, says so honestly instead of faking a number."""
    import statistics
    here = os.path.dirname(os.path.abspath(__file__))
    # bounded tail read (~10MB ≈ 45+ days of 2-min snapshots)
    try:
        path = os.path.join(here, "portfolio_log.jsonl")
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 10_000_000:
                f.seek(-10_000_000, os.SEEK_END); f.readline()
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception as e:
        return {"ok": False, "error": "portfolio_log unreadable: %s" % e}
    snaps = []
    for l in lines:
        try:
            e = json.loads(l)
            if e.get("ts") and e.get("btc_price") and e.get("portfolio_usd"):
                snaps.append(e)
        except Exception:
            continue
    if not snaps:
        return {"ok": False, "error": "no snapshots"}
    now = snaps[-1]
    px_now, port_now = float(now["btc_price"]), float(now["portfolio_usd"])
    t_now = float(now["ts"])
    lo, hi = t_now - (days + 8) * 86400, t_now - (days - 6) * 86400
    matches = [float(s["portfolio_usd"]) for s in snaps
               if lo <= float(s["ts"]) <= hi
               and abs(float(s["btc_price"]) - px_now) / px_now <= price_tol]
    # net capital flows since the start of the benchmark window
    flows = 0.0
    try:
        for ev in json.load(open(os.path.join(here, "capital_events.json"))):
            if lo <= float(ev.get("ts") or 0) <= t_now:
                flows += float(ev.get("amount_usd") or 0)
    except Exception:
        pass
    out = {"ok": True, "price_now": round(px_now, 0), "portfolio_now": round(port_now, 0),
           "net_flows_usd": round(flows, 0), "window_days": days,
           "n_benchmark_snaps": len(matches)}
    if len(matches) >= 10:
        then = statistics.median(matches)
        out["portfolio_then_same_price"] = round(then, 0)
        out["true_pnl_usd"] = round(port_now - then - flows, 0)
    else:
        out["true_pnl_usd"] = None
        out["note"] = ("no equal-price benchmark ~%dd ago (price then differed >%.1f%%) — "
                       "true P&L not computable at matched price" % (days, price_tol * 100))
    return out


def format_sell_alert(ev):
    """Telegram text with the REAL P&L number FIRST, so it shows in the phone
    notification preview. Honest: red when it's red."""
    amt = ev["realised"]
    head = ("+$" if amt >= 0 else "-$") + "{:,.2f}".format(abs(amt))
    verdict = "profit" if amt >= 0 else "LOSS"
    cum = ev["realised_cum"]
    cum_str = ("+$" if cum >= 0 else "-$") + "{:,.2f}".format(abs(cum))
    # Distinguish a real grid-rung sell from a forced base-position liquidation
    # (3Commas selling base BTC for capital protection — not grid trading).
    if ev.get("source") == "base":
        kind, note = "base liquidation", "  ⚠ base sell-off, not grid profit"
    else:
        kind, note = "grid SELL", ""
    return (
        "{head} {verdict} · Griddy {bot} {kind}{note}\n"
        "{qty:.4f} BTC @ ${price:,.0f} (avg cost ${avg:,.0f})\n"
        "{bot} real realised since tracking: {cum}"
    ).format(head=head, verdict=verdict, bot=ev["bot"], kind=kind, note=note,
             qty=ev["qty"], price=ev["price"], avg=ev["avg_cost"], cum=cum_str)


if __name__ == "__main__":
    import sys
    bots = ["2743885", "2743889", "2743888"]
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "seed":
        s = seed(bots)
        print("seeded:", json.dumps(s["bots"], indent=2))
    elif cmd == "update":
        evs = update(bots)
        print("new sells:", len(evs))
        for e in evs:
            print(format_sell_alert(e), "\n")
    else:
        print(json.dumps(summary(bots), indent=2))
