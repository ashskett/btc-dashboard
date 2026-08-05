"""audit.py — autonomous trading-behaviour auditor.

WHY: too many failure modes have only been caught because Ash happened to look —
sold ~0.32 BTC at the day's low on a redeploy (~$450), slam-bought $24k base on a
weekend exit, bled inventory to 2.5% while a stale ladder never refilled, engine
silently dead 24h. Each cost real money before it was spotted. This auditor scans
the log files every ~60s (from the dashboard watchdog, INDEPENDENT of the engine
cycle) for those classes of problem and Telegram-alerts the moment one appears,
with the estimated $ impact — so a bad behaviour is caught in minutes.

ALERT-FIRST: it flags + logs; it does NOT auto-trade. Auto-remediation on live
money is added per-rule only with explicit sign-off. Findings → Telegram (deduped,
per-key cooldown) + audit_log.jsonl + the /audit endpoint.

Each check is independent and wrapped — one failing never stops the others, and
adding a new check is a single function. Reads only local state/log files (no API
calls) so it's cheap and can't add latency or rate-limit pressure.
"""
import os
import json
import time
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE   = os.path.join(HERE, "audit_state.json")
LOG_FILE     = os.path.join(HERE, "audit_log.jsonl")
STATUS_FILE  = os.path.join(HERE, "engine_status.json")
GRID_FILE    = os.path.join(HERE, "grid_state.json")
PORT_FILE    = os.path.join(HERE, "portfolio_log.jsonl")
FILLS_FILE   = os.path.join(HERE, "fills_log.jsonl")
RPNL_FILE    = os.path.join(HERE, "real_pnl_state.json")

# ── Tunables ────────────────────────────────────────────────────────────────
NEAR_EXTREME_ATR   = 0.5     # a fill is "near" the window low/high within this ×ATR
BURST_MIN_BTC      = 0.10    # min BTC in a 20-min burst to count as a big trade
BOUNCE_ATR         = 0.3     # price must have moved this ×ATR back to confirm bad timing
FORCED_TRADE_BTC   = 0.12    # |Δbtc| within a redeploy window ⇒ forced base trade (regression)
RATIO_EXTREME_MINS = 30      # ratio beyond a band this long ⇒ flag
CASCADE_N          = 3       # this many redeploys...
CASCADE_MINS       = 15      # ...within this window ⇒ flag
LOSS_STREAK_USD    = 60.0    # realised P&L dropping more than this in the lookback ⇒ flag
MIN_IMPACT_USD     = 150.0   # ignore near-low/near-high events with $ impact below this (noise)
ALERT_COOLDOWN_S   = 3600    # per-finding re-alert cooldown


def _tail(path, max_bytes=400_000):
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            if sz > max_bytes:
                f.seek(-max_bytes, os.SEEK_END); f.readline()
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _load_jsonl_tail(path, since_ts=None, ts_key="ts"):
    out = []
    for line in _tail(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        out.append(e)
    return out


def _load(path, default=None):
    try:
        return json.load(open(path))
    except Exception:
        return default if default is not None else {}


def _iso_to_ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _recent_ports(mins):
    cut = time.time() - mins * 60
    # scale the tail read to the window: ~1 snapshot/2min × ~250B, 1.5x headroom
    need = int((mins / 2) * 250 * 1.5) + 100_000
    out = []
    for line in _tail(PORT_FILE, max_bytes=min(need, 12_000_000)).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("ts") and e["ts"] >= cut:
            out.append(e)
    return out


def _recent_fills(mins):
    cut = time.time() - mins * 60
    out = []
    for e in _load_jsonl_tail(FILLS_FILE):
        t = _iso_to_ts(e.get("time"))
        if t and t >= cut:
            e["_ts"] = t
            out.append(e)
    out.sort(key=lambda x: x["_ts"])
    return out


# ── Checks: each returns a list of findings {key, sev, msg} ──────────────────

def _biggest_step(ports, span=3):
    """Largest holdings DROP and JUMP over a ~span-snapshot (≈6 min) window, with
    the price at the point the move completed. Detecting via btc_qty deltas (not
    by blending fills) isolates a real BURST — a base liquidation/acquisition —
    from ordinary small grid rung fills, and is API-free."""
    q  = [float(p.get("btc_qty") or 0) for p in ports]
    px = [float(p.get("btc_price") or 0) for p in ports]
    drop = jump = (0.0, 0.0)   # (btc, price_at_completion)
    for i in range(len(ports)):
        j = min(i + span, len(ports) - 1)
        d = q[i] - q[j]
        if d > drop[0]:
            drop = (d, px[j])
        if -d > jump[0]:
            jump = (-d, px[j])
    return drop, jump


def _check_sell_near_low(st, ports, atr, price):
    if atr <= 0 or len(ports) < 3:
        return []
    lo = min(float(p.get("btc_price") or 0) for p in ports if p.get("btc_price"))
    (drop_btc, sell_px), _ = _biggest_step(ports)
    if drop_btc < BURST_MIN_BTC or not sell_px:
        return []
    if sell_px <= lo + NEAR_EXTREME_ATR * atr and price >= sell_px + BOUNCE_ATR * atr:
        cost = drop_btc * (price - sell_px)
        if cost < MIN_IMPACT_USD:
            return []
        return [{"key": "sell_near_low", "sev": "high",
                 "msg": ("Sold ~{:.3f} BTC near the low ${:,.0f} (@ ~${:,.0f}) — price now "
                         "${:,.0f}. Est. opportunity cost ~${:,.0f}. Base shed should wait "
                         "for a bounce.").format(drop_btc, lo, sell_px, price, cost)}]
    return []


def _check_buy_near_high(st, ports, atr, price, mode=None):
    if atr <= 0 or len(ports) < 3:
        return []
    # In BUY_ONLY we are INTENTIONALLY accumulating (under-weight) — buying near a
    # local high is the plan, not a fault. Only flag buys-near-high in NORMAL.
    if mode == "BUY_ONLY":
        return []
    hi = max(float(p.get("btc_price") or 0) for p in ports if p.get("btc_price"))
    _, (jump_btc, buy_px) = _biggest_step(ports)
    if jump_btc < BURST_MIN_BTC or not buy_px:
        return []
    if buy_px >= hi - NEAR_EXTREME_ATR * atr and price <= buy_px - BOUNCE_ATR * atr:
        cost = jump_btc * (buy_px - price)
        if cost < MIN_IMPACT_USD:
            return []
        return [{"key": "buy_near_high", "sev": "high",
                 "msg": ("Bought ~{:.3f} BTC near the high ${:,.0f} (@ ~${:,.0f}) — price now "
                         "${:,.0f}. Underwater ~${:,.0f}.").format(
                             jump_btc, hi, buy_px, price, cost)}]
    return []


def _check_forced_redeploy_trade(st, ports, intensive_recent=False):
    """Regression guard: a NORMAL redeploy should NEVER market-trade a big base
    chunk (the 2026-07-05/07-17 fixes + guard A). If btc_qty jumps > FORCED_TRADE_BTC
    within ~5min of a redeploy AND we were NOT in an intensive mode (BUY_ONLY /
    SELL_ONLY legitimately acquire/shed base), a fix regressed — alert loudly."""
    grid = _load(GRID_FILE)
    rts = grid.get("last_redeploy_ts")
    if not rts or rts == st.get("last_redeploy_seen"):
        return []
    st["last_redeploy_seen"] = rts
    if intensive_recent:
        return []   # BUY_ONLY/SELL_ONLY repositioning base is expected, not a regression
    near = [p for p in ports if abs((p.get("ts") or 0) - rts) <= 300]
    if len(near) < 2:
        return []
    q = [float(p.get("btc_qty") or 0) for p in near]
    dq = max(q) - min(q)
    if dq > FORCED_TRADE_BTC:
        px = float((_load(STATUS_FILE) or {}).get("price") or 0)
        return [{"key": "forced_redeploy_trade", "sev": "critical",
                 "msg": ("REGRESSION: a redeploy market-traded {:.3f} BTC (~${:,.0f}) — "
                         "redeploys must deploy at holdings, never force a base buy/sell. "
                         "Check _size_tiers_to_holdings.").format(dq, dq * px)}]
    return []


def _check_redeploy_cascade(st):
    grid = _load(GRID_FILE)
    rts = grid.get("last_redeploy_ts")
    hist = st.get("redeploy_hist", [])
    if rts and rts not in hist:
        hist.append(rts)
    now = time.time()
    hist = [t for t in hist if now - t <= CASCADE_MINS * 60]
    st["redeploy_hist"] = hist
    if len(hist) >= CASCADE_N:
        return [{"key": "redeploy_cascade", "sev": "high",
                 "msg": "%d redeploys in %d min — cascade churning orders. "
                        "Check drift/mode-flip suppression." % (len(hist), CASCADE_MINS)}]
    return []


def _check_ratio_extreme(st, status):
    ratio = status.get("btc_ratio")
    if ratio is None:
        return []
    inv = _load(os.path.join(HERE, "inventory_settings.json"),
                {"min_btc": 0.2, "max_btc": 0.7})
    hi, lo = inv.get("max_btc", 0.7), inv.get("min_btc", 0.2)
    # UNDER-weight alerts removed (Ash 2026-08-03): in the static grid, low BTC
    # near the range top is DESIGN — the ladder rebuilds via dip fills only.
    # Only OVER-weight persists as a finding (heavy exposure = when the DOWN
    # key level actually matters).
    extreme = ratio >= hi
    now = time.time()
    if not extreme:
        st["ratio_extreme_since"] = None
        return []
    since = st.get("ratio_extreme_since") or now
    st["ratio_extreme_since"] = since
    held_m = (now - since) / 60
    if held_m >= RATIO_EXTREME_MINS:
        side = "over-weight" if ratio >= hi else "under-weight"
        return [{"key": "ratio_extreme", "sev": "medium",
                 "msg": "BTC ratio %s at %.0f%% for %.0f min (band %.0f–%.0f%%). "
                        "%s." % (side, ratio * 100, held_m, lo * 100, hi * 100,
                                 "Little dry powder / heavy exposure" if ratio >= hi
                                 else "Nearly out of BTC — limit ladder should be refilling")}]
    return []


def _check_realised_drop(st):
    rp = _load(RPNL_FILE)
    tot = sum(b.get("realised_cum", 0.0) for b in (rp.get("bots") or {}).values())
    hist = st.get("realised_hist", [])
    now = time.time()
    hist.append([now, tot])
    hist = [h for h in hist if now - h[0] <= 3600]
    st["realised_hist"] = hist
    if len(hist) >= 2:
        drop = hist[0][1] - tot
        if drop > LOSS_STREAK_USD:
            return [{"key": "realised_drop", "sev": "medium",
                     "msg": ("Real realised P&L fell ${:,.0f} in the last hour "
                             "(grid booking net losses). Now ${:,.0f}.").format(drop, tot)}]
    return []


DIVERGENCE_USD = 1500.0   # alert when Σrealised outruns true account drift by this
CHOP_LOSS_USD    = 250.0  # losing this much over 7d in a SIDEWAYS market ⇒ alert
CHOP_BAND_PCT    = 0.03   # |7d price change| below this = "chop" (grid's home turf)
ALPHA_30D_USD    = 1500.0 # engine effect (vs month-start-mix HODL) worse than this ⇒ alert


def _check_chop_loss(st, status):
    """ASH'S JULY WOUND, GUARDED (2026-08-05): 'losing money with bitcoin trading
    perfectly for a grid engine'. In sideways markets the grid MUST make money —
    that is its entire edge. If price is flat over ~7d but the flow-adjusted
    account is DOWN meaningfully, the machine is malfunctioning economically even
    if no single trade looks wrong. Alert loudly and early."""
    ports = _recent_ports(60 * 24 * 8)   # ~8 days
    if len(ports) < 100:
        return []
    now_p = ports[-1]
    then_p = None
    for p in ports:
        if (now_p["ts"] - p["ts"]) <= 7.5 * 86400:
            then_p = p
            break
    if not then_p or (now_p["ts"] - then_p["ts"]) < 6 * 86400:
        return []
    px0, px1 = float(then_p.get("btc_price") or 0), float(now_p.get("btc_price") or 0)
    if not px0 or abs(px1 / px0 - 1) > CHOP_BAND_PCT:
        return []   # trending market — grid-vs-price divergence is expected there
    flows = 0.0
    try:
        for ev in _load(os.path.join(HERE, "capital_events.json"), []):
            if then_p["ts"] <= float(ev.get("ts") or 0) <= now_p["ts"]:
                flows += float(ev.get("amount_usd") or 0)
    except Exception:
        pass
    change = float(now_p["portfolio_usd"]) - float(then_p["portfolio_usd"]) - flows
    if change < -CHOP_LOSS_USD:
        return [{"key": "chop_loss", "sev": "high",
                 "msg": ("LOSING IN CHOP: price flat over 7d (${:,.0f}→${:,.0f}, "
                         "{:+.1f}%) but account is {:,.0f} flow-adjusted. A grid "
                         "must profit in sideways markets — investigate NOW "
                         "(churn? fees? repositioning?).").format(
                             px0, px1, 100 * (px1 / px0 - 1), change)}]
    return []


def _check_engine_alpha_30d(st, status):
    """Engine-vs-HODL guard: compares the account against 'held the 30d-ago mix,
    did nothing'. In a strong rally a grid legitimately lags HODL (it sells on
    the way up), so the threshold is generous — this catches sustained
    destruction like July (engine −$4.1k vs HODL), not normal grid behaviour."""
    ports = _recent_ports(60 * 24 * 32)
    if len(ports) < 500:
        return []
    now_p = ports[-1]
    then_p = ports[0]
    if (now_p["ts"] - then_p["ts"]) < 25 * 86400:
        return []
    px0, px1 = float(then_p.get("btc_price") or 0), float(now_p.get("btc_price") or 0)
    r0 = float(then_p.get("btc_ratio") or 0)
    if not px0:
        return []
    flows = 0.0
    try:
        for ev in _load(os.path.join(HERE, "capital_events.json"), []):
            if then_p["ts"] <= float(ev.get("ts") or 0) <= now_p["ts"]:
                flows += float(ev.get("amount_usd") or 0)
    except Exception:
        pass
    market = float(then_p["portfolio_usd"]) * r0 * (px1 / px0 - 1)
    actual = float(now_p["portfolio_usd"]) - float(then_p["portfolio_usd"]) - flows
    engine = actual - market
    if engine < -ALPHA_30D_USD:
        return [{"key": "engine_alpha_30d", "sev": "high",
                 "msg": ("ENGINE DESTROYING VALUE: over ~30d the machine's activity "
                         "cost ${:,.0f} versus simply holding the month-ago mix "
                         "(market {:+,.0f}, actual {:+,.0f}). This is the July "
                         "failure pattern — review /alpha and consider freezing.")
                 .format(-engine, market, actual)}]
    return []


def _check_pnl_divergence(st, status):
    """THE JULY LESSON (2026-07-29): cumulative per-sell realised printed +$5,790
    while the flow-adjusted account was DOWN ~$1,200 at equal price — cost-basis
    resets across mode churn make Σrealised ≠ account P&L, and the gap went
    unnoticed for a month. This check keeps one sample/day of (realised_cum,
    portfolio, price, cum flows) and compares: over any stored window with an
    equal-price endpoint (±2%), gap = Δrealised − (Δportfolio − Δflows). Alerts
    when the ledger's claim outruns reality by > DIVERGENCE_USD."""
    rp = _load(RPNL_FILE)
    realised = sum(b.get("realised_cum", 0.0) for b in (rp.get("bots") or {}).values())
    price = float(status.get("price") or 0)
    ports = _recent_ports(30)
    if not ports or not price:
        return []
    port_now = float(ports[-1].get("portfolio_usd") or 0)
    flows_cum = 0.0
    try:
        flows_cum = sum(float(e.get("amount_usd") or 0)
                        for e in _load(os.path.join(HERE, "capital_events.json"), []))
    except Exception:
        pass
    now = time.time()
    hist = st.get("pnl_div_hist", [])
    if not hist or now - hist[-1][0] >= 86400:   # one sample per day
        hist.append([now, round(realised, 2), round(port_now, 2),
                     round(price, 2), round(flows_cum, 2)])
        st["pnl_div_hist"] = hist[-60:]          # keep ~2 months
    findings = []
    # oldest sample ≥5 days old whose price is within 2% of now = fair benchmark
    for s in hist:
        s_ts, s_real, s_port, s_px, s_flows = s
        if now - s_ts < 5 * 86400 or not s_px:
            continue
        if abs(s_px - price) / price > 0.02:
            continue
        claimed = realised - s_real
        actual = (port_now - s_port) - (flows_cum - s_flows)
        gap = claimed - actual
        if gap > DIVERGENCE_USD:
            days = (now - s_ts) / 86400
            findings.append({"key": "pnl_divergence", "sev": "high",
                             "msg": ("P&L DIVERGENCE: ledger claims {:+,.0f} realised over "
                                     "{:.0f}d but the flow-adjusted account moved {:+,.0f} "
                                     "at equal price — ${:,.0f} of reported profit is not "
                                     "in the account. Check churn (mode flips/redeploys).")
                             .format(claimed, days, actual, gap)})
            break
    return findings


CHECKS_STATUS = [_check_sell_near_low, _check_buy_near_high]  # need (st, ports, atr, price)


def run(notify_fn=None):
    """Run all checks; alert new findings; append to audit_log. Returns findings."""
    st = _load(STATE_FILE, {})
    status = _load(STATUS_FILE)
    grid_ok = True
    atr = float(status.get("atr") or 0)
    price = float(status.get("price") or 0)
    ports = _recent_ports(90)

    # Track inventory mode over the last few ticks so the forced-trade check can
    # tell a legit intensive (BUY_ONLY/SELL_ONLY) base move from a NORMAL regression.
    mode = status.get("inventory_mode")
    mh = (st.get("mode_hist", []) + [mode])[-6:]
    st["mode_hist"] = mh
    intensive_recent = any(m in ("BUY_ONLY", "SELL_ONLY") for m in mh)

    findings = []
    def _safe(fn, *a):
        try:
            findings.extend(fn(*a) or [])
        except Exception as e:  # a broken check must never stop the rest
            findings.append({"key": "audit_error_%s" % fn.__name__, "sev": "low",
                             "msg": "audit check %s failed: %s" % (fn.__name__, e)})

    _safe(_check_sell_near_low, st, ports, atr, price)
    _safe(_check_buy_near_high, st, ports, atr, price, mode)
    _safe(_check_forced_redeploy_trade, st, ports, intensive_recent)
    _safe(_check_redeploy_cascade, st)
    _safe(_check_ratio_extreme, st, status)
    _safe(_check_realised_drop, st)
    _safe(_check_pnl_divergence, st, status)
    _safe(_check_chop_loss, st, status)
    _safe(_check_engine_alpha_30d, st, status)

    seen = st.get("seen", {})
    now = time.time()
    fresh = []
    for f in findings:
        last = seen.get(f["key"], 0)
        if now - last >= ALERT_COOLDOWN_S:
            seen[f["key"]] = now
            fresh.append(f)
    st["seen"] = seen
    st["last_run"] = now
    try:
        json.dump(st, open(STATE_FILE, "w"), indent=2)
    except Exception:
        pass

    if fresh:
        try:
            with open(LOG_FILE, "a") as f:
                for x in fresh:
                    f.write(json.dumps({"ts": int(now), **x}) + "\n")
        except Exception:
            pass
        if notify_fn:
            icon = {"critical": "🚨", "high": "⚠", "medium": "•", "low": "·"}
            body = "\n".join("%s %s" % (icon.get(x["sev"], "•"), x["msg"]) for x in fresh)
            try:
                notify_fn("Griddy AUDIT — %d issue(s):\n%s" % (len(fresh), body))
            except Exception:
                pass
    return findings


if __name__ == "__main__":
    fs = run()
    print("audit findings:", len(fs))
    for x in fs:
        print("  [%s] %s" % (x["sev"], x["msg"]))
