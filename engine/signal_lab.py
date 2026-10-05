"""signal_lab.py — hypothesis-testing harness over the data lake (2026-09-23).

The research half of the quant-engine pivot. Every result is reported GROSS and
NET of realistic costs, split by year, with sample counts — the promotion rule
is: no idea reaches even paper trading unless its NET edge holds across
subperiods. Rejections are valid output (see the order-book grid verdict).

Usage (droplet, venv python):
    python signal_lab.py list           # registered hypotheses
    python signal_lab.py run all        # run everything, save + print
    python signal_lab.py run dip_buy    # run one

Results append to datalake/lab_results.jsonl (the candidate library's paper
trail) and the latest run is served at GET /lab on the dashboard.

Method notes:
- Panel = hourly BTC klines (2017→) joined with lake features, forward simple
  returns at several horizons computed from closes.
- bucket_test: decile conditional means + rank IC + per-year IC sign — a weak
  signal must at least keep its sign across years.
- event_test: de-clustered events (a new event needs `horizon` hours since the
  last) vs the unconditional baseline of the SAME period.
- Costs: 0.07%/side measured Coinbase fee + 1bp slippage → 16bps round trip.
"""
import os
import sys
import json
import time
import datetime

import pandas as pd
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
LAKE = os.path.join(HERE, "datalake")
RESULTS = os.path.join(LAKE, "lab_results.jsonl")

FEE_SIDE = 0.0007
SLIP = 0.0001
COST_RT = 2 * (FEE_SIDE + SLIP)          # 0.16% round trip
HORIZONS = [4, 8, 24, 72]                # hours


# ── data loading ────────────────────────────────────────────────────────────

def _load_jsonl(name):
    path = os.path.join(LAKE, name + ".jsonl")
    rows = []
    with open(path) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return pd.DataFrame(rows)


def load_klines(sym="BTCUSDT"):
    df = _load_jsonl("klines_1h")
    df = df[df["sym"] == sym].drop_duplicates("ts").sort_values("ts")
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df.set_index("dt")


def load_feature(domain, col, sym=None, src=None):
    """A lake feature as an hourly series (last obs per hour, ffilled ≤48h)."""
    df = _load_jsonl(domain)
    if sym is not None and "sym" in df.columns:
        df = df[df["sym"] == sym]
    if src is not None and "src" in df.columns:
        df = df[df["src"] == src]
    df = df.dropna(subset=[col]).sort_values("ts")
    s = pd.Series(df[col].values,
                  index=pd.to_datetime(df["ts"], unit="s", utc=True))
    return s.resample("1h").last().ffill(limit=48)


def build_panel(sym="BTCUSDT", features=None):
    """Hourly close panel + forward returns + joined features.
    features: {name: series} from load_feature()."""
    k = load_klines(sym)
    p = pd.DataFrame({"c": k["c"], "h": k["h"], "l": k["l"],
                      "vol": k["vol"], "taker_buy_vol": k["taker_buy_vol"]})
    p["ret_24h"] = p["c"].pct_change(24)
    for hz in HORIZONS:
        p["fwd_%dh" % hz] = p["c"].shift(-hz) / p["c"] - 1
    for name, s in (features or {}).items():
        p[name] = s.reindex(p.index)
    return p


# ── evaluation primitives ───────────────────────────────────────────────────

def bucket_test(panel, feature, horizons=HORIZONS, q=10):
    """Conditional forward returns by feature decile + rank IC + yearly IC."""
    df = panel.dropna(subset=[feature])
    out = {"feature": feature, "n": int(len(df)), "horizons": {}}
    if len(df) < 500:
        out["verdict"] = "INSUFFICIENT DATA"
        return out
    df = df.copy()
    df["bucket"] = pd.qcut(df[feature], q, labels=False, duplicates="drop")
    def _spearman(a, b):
        # rank-then-pearson == spearman, without the scipy dependency
        return a.rank().corr(b.rank())

    for hz in horizons:
        col = "fwd_%dh" % hz
        d = df.dropna(subset=[col])
        ic = _spearman(d[feature], d[col])
        yearly = d.groupby(d.index.year).apply(
            lambda g: _spearman(g[feature], g[col]),
            include_groups=False).dropna()
        buckets = d.groupby("bucket")[col].agg(["mean", "median", "count"])
        out["horizons"][hz] = {
            "ic": round(float(ic), 4),
            "ic_by_year": {int(y): round(float(v), 3)
                           for y, v in yearly.items()},
            "ic_sign_consistency": round(float(
                (np.sign(yearly) == np.sign(ic)).mean()), 2) if len(yearly) else None,
            "bottom_decile_mean_bps": round(float(buckets["mean"].iloc[0]) * 1e4, 1),
            "top_decile_mean_bps": round(float(buckets["mean"].iloc[-1]) * 1e4, 1),
        }
    return out


def event_test(panel, mask, label, horizons=HORIZONS):
    """Forward returns after de-clustered events vs same-period baseline.
    net_bps subtracts the full 16bps round trip from the event mean."""
    out = {"event": label, "horizons": {}}
    first_true = mask[mask].index.min() if mask.any() else None
    if first_true is None:
        out["verdict"] = "NO EVENTS"
        return out
    base = panel[panel.index >= first_true]
    for hz in horizons:
        col = "fwd_%dh" % hz
        ev_idx = []
        last = None
        for t in mask[mask].index:
            if last is None or (t - last) >= pd.Timedelta(hours=hz):
                ev_idx.append(t)
                last = t
        ev = panel.loc[ev_idx, col].dropna()
        bl = base[col].dropna()
        if len(ev) < 10:
            out["horizons"][hz] = {"n": int(len(ev)), "verdict": "TOO FEW EVENTS"}
            continue
        mean = float(ev.mean())
        tstat = mean / (float(ev.std()) / np.sqrt(len(ev))) if ev.std() else 0.0
        yearly = ev.groupby(ev.index.year).mean()
        out["horizons"][hz] = {
            "n": int(len(ev)),
            "mean_bps": round(mean * 1e4, 1),
            "median_bps": round(float(ev.median()) * 1e4, 1),
            "win_rate": round(float((ev > 0).mean()), 3),
            "baseline_bps": round(float(bl.mean()) * 1e4, 1),
            "edge_vs_baseline_bps": round((mean - float(bl.mean())) * 1e4, 1),
            "net_bps": round((mean - COST_RT) * 1e4, 1),
            "tstat": round(tstat, 2),
            "by_year_bps": {int(y): round(float(v) * 1e4, 0)
                            for y, v in yearly.items()},
        }
    return out


def save_result(name, payload):
    payload = dict(payload)
    payload.update({"hypothesis": name, "ts": round(time.time(), 1),
                    "cost_rt_bps": COST_RT * 1e4})
    os.makedirs(LAKE, exist_ok=True)
    with open(RESULTS, "a") as f:
        f.write(json.dumps(payload) + "\n")
    return payload


# ── hypotheses ──────────────────────────────────────────────────────────────

def hyp_funding_extreme():
    """Very negative funding = crowded shorts paying longs. Capitulation-bounce
    folklore says buy it. Ask the data."""
    fh = _load_jsonl("funding_hist")
    fh = fh[fh["sym"] == "BTCUSDT"].sort_values("ts")
    s = pd.Series(fh["funding_rate"].values,
                  index=pd.to_datetime(fh["ts"], unit="s", utc=True))
    s = s.resample("1h").last().ffill(limit=9)
    p = build_panel(features={"funding": s})
    res = {"buckets": bucket_test(p, "funding")}
    pctl = p["funding"].rolling(24 * 30).rank(pct=True)
    res["ev_neg_funding"] = event_test(
        p, (pctl < 0.02) & (p["funding"] < 0), "funding in bottom 2% of 30d & negative")
    res["ev_neg_funding_dip"] = event_test(
        p, (pctl < 0.05) & (p["ret_24h"] < -0.03),
        "funding bottom 5% of 30d AND price -3%+ in 24h")
    res["ev_high_funding"] = event_test(
        p, pctl > 0.98, "funding in top 2% of 30d (overheated longs)")
    return res


def hyp_dip_buy():
    """Does buying a hard 24h dip pay? Direct validation of the buy-only
    ladder's premise, over 9 years."""
    p = build_panel()
    res = {}
    for thresh in (-0.03, -0.05, -0.08):
        res["dip_%d" % int(-thresh * 100)] = event_test(
            p, p["ret_24h"] < thresh,
            "24h return below {:.0%}".format(thresh))
    return res


def hyp_momentum():
    """Does a strong 24h move continue (trend) or revert? The core regime
    question for any grid."""
    p = build_panel()
    p["mom24"] = p["ret_24h"]
    res = {"buckets": bucket_test(p, "mom24")}
    res["ev_up_3pct"] = event_test(p, p["mom24"] > 0.03, "24h return above +3%")
    return res


def hyp_taker_flow():
    """Taker buy share of hourly volume (aggression imbalance) vs forward
    returns. 9y of history straight from klines."""
    p = build_panel()
    p["taker_share"] = (p["taker_buy_vol"] / p["vol"]).where(p["vol"] > 0)
    p["taker_share_24h"] = p["taker_share"].rolling(24).mean()
    return {"buckets_1h": bucket_test(p, "taker_share"),
            "buckets_24h": bucket_test(p, "taker_share_24h")}


def hyp_seasonality():
    """Day-of-week / hour-of-day effects, 9y. Directly tests weekend-mode
    folklore."""
    p = build_panel()
    r1 = p["c"].pct_change().shift(-1)   # next-hour return
    by_dow = (r1.groupby(p.index.dayofweek).mean() * 24 * 1e4).round(1)
    by_hour = (r1.groupby(p.index.hour).mean() * 1e4).round(2)
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    recent = p[p.index >= p.index.max() - pd.Timedelta(days=730)]
    r1r = recent["c"].pct_change().shift(-1)
    by_dow_recent = (r1r.groupby(recent.index.dayofweek).mean() * 24 * 1e4).round(1)
    return {"daily_bps_by_dow_9y": {dows[int(k)]: float(v) for k, v in by_dow.items()},
            "daily_bps_by_dow_2y": {dows[int(k)]: float(v) for k, v in by_dow_recent.items()},
            "hourly_bps_by_utc_hour_9y": {int(k): float(v) for k, v in by_hour.items()}}


def hyp_fear_greed():
    """Fear & Greed extremes vs forward returns, full index history (2018→).
    Backfills fng_hist.jsonl on first run."""
    import requests
    path = os.path.join(LAKE, "fng_hist.jsonl")
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        d = requests.get("https://api.alternative.me/fng/?limit=0", timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"}).json()["data"]
        with open(path, "w") as f:
            for x in sorted(d, key=lambda r: int(r["timestamp"])):
                f.write(json.dumps({"ts": int(x["timestamp"]),
                                    "fear_greed": int(x["value"])}) + "\n")
    fng = _load_jsonl("fng_hist")
    s = pd.Series(fng["fear_greed"].values,
                  index=pd.to_datetime(fng["ts"], unit="s", utc=True))
    s = s.resample("1h").last().ffill(limit=25)
    p = build_panel(features={"fng": s})
    res = {"buckets": bucket_test(p, "fng", horizons=[24, 72])}
    res["ev_extreme_fear"] = event_test(p, p["fng"] <= 15,
                                        "Fear&Greed <= 15", horizons=[24, 72])
    res["ev_extreme_greed"] = event_test(p, p["fng"] >= 85,
                                         "Fear&Greed >= 85", horizons=[24, 72])
    return res


def hyp_funding_capitulation_wf():
    """Walk-forward + parameter sensitivity for the first-batch winner
    (funding percentile low AND 24h dip). Promotion test: pick params on PAST
    data only, each year 2021-2026, measure that year out-of-sample NET."""
    fh = _load_jsonl("funding_hist")
    fh = fh[fh["sym"] == "BTCUSDT"].sort_values("ts")
    s = pd.Series(fh["funding_rate"].values,
                  index=pd.to_datetime(fh["ts"], unit="s", utc=True))
    s = s.resample("1h").last().ffill(limit=9)
    p = build_panel(features={"funding": s})
    p["fpctl"] = p["funding"].rolling(24 * 30).rank(pct=True)
    p = p.dropna(subset=["fpctl"])

    def _ev(mask, sub, hz):
        """De-clustered event mean fwd return on subset `sub`."""
        col = "fwd_%dh" % hz
        idx, last = [], None
        for t in mask[mask].index:
            if last is None or (t - last) >= pd.Timedelta(hours=hz):
                idx.append(t)
                last = t
        ev = sub.loc[[t for t in idx if t in sub.index], col].dropna()
        return len(ev), (float(ev.mean()) if len(ev) else None)

    grid = [(pc, dp) for pc in (0.02, 0.05, 0.10)
            for dp in (-0.02, -0.03, -0.05)]
    res = {"sensitivity": {}, "walk_forward": {}}

    # full-sample sensitivity (context only — NOT the promotion criterion)
    for hz in (24, 72):
        tbl = {}
        for pc, dp in grid:
            mask = (p["fpctl"] < pc) & (p["ret_24h"] < dp)
            n, m = _ev(mask, p, hz)
            tbl["pctl<{:.0%} dip<{:.0%}".format(pc, dp)] = {
                "n": n, "net_bps": round((m - COST_RT) * 1e4, 1) if m is not None else None}
        res["sensitivity"][hz] = tbl

    # walk-forward: choose params on data strictly BEFORE the test year
    for hz in (24, 72):
        oos = {}
        for year in range(2021, 2027):
            cut = pd.Timestamp(year=year, month=1, day=1, tz="UTC")
            train = p[p.index < cut]
            test = p[(p.index >= cut) & (p.index < cut + pd.DateOffset(years=1))]
            best, best_net = None, None
            for pc, dp in grid:
                mask = (train["fpctl"] < pc) & (train["ret_24h"] < dp)
                n, m = _ev(mask, train, hz)
                if m is None or n < 25:
                    continue
                net = m - COST_RT
                if best_net is None or net > best_net:
                    best, best_net = (pc, dp), net
            if best is None:
                continue
            pc, dp = best
            mask = (test["fpctl"] < pc) & (test["ret_24h"] < dp)
            n, m = _ev(mask, test, hz)
            oos[year] = {"params": "pctl<{:.0%} dip<{:.0%}".format(pc, dp),
                         "train_net_bps": round(best_net * 1e4, 1),
                         "oos_n": n,
                         "oos_net_bps": round((m - COST_RT) * 1e4, 1) if m is not None else None}
        vals = [(v["oos_n"], v["oos_net_bps"]) for v in oos.values()
                if v["oos_net_bps"] is not None and v["oos_n"] > 0]
        tot_n = sum(n for n, _ in vals)
        res["walk_forward"][hz] = {
            "by_year": oos,
            "oos_total_events": tot_n,
            "oos_weighted_net_bps": round(
                sum(n * b for n, b in vals) / tot_n, 1) if tot_n else None,
            "oos_years_positive": "{}/{}".format(
                sum(1 for _, b in vals if b > 0), len(vals))}
    return res


HYPOTHESES = {
    "funding_capitulation_wf": hyp_funding_capitulation_wf,
    "funding_extreme": hyp_funding_extreme,
    "dip_buy": hyp_dip_buy,
    "momentum": hyp_momentum,
    "taker_flow": hyp_taker_flow,
    "seasonality": hyp_seasonality,
    "fear_greed": hyp_fear_greed,
}


def main(argv):
    if len(argv) < 2 or argv[1] == "list":
        for name, fn in HYPOTHESES.items():
            print("{:18s} {}".format(name, (fn.__doc__ or "").strip().split("\n")[0]))
        return
    if argv[1] == "run":
        targets = (list(HYPOTHESES) if len(argv) < 3 or argv[2] == "all"
                   else [argv[2]])
        for name in targets:
            t0 = time.time()
            try:
                res = HYPOTHESES[name]()
                res = save_result(name, res)
                print("=" * 70)
                print("{}  ({:.1f}s)".format(name, time.time() - t0))
                print(json.dumps(res, indent=1, default=str))
            except Exception as e:  # noqa: BLE001
                print("{} FAILED: {}".format(name, e))


if __name__ == "__main__":
    main(sys.argv)
