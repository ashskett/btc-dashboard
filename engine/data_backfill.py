"""data_backfill.py — one-time historical seed for the data lake (2026-09-23).

Pulls everything Binance retains so the Signal Lab has history on day one:
  funding_hist.jsonl     — full perp funding-rate history (8h prints, ~2019→)
  klines_1h.jsonl        — full spot 1h OHLCV history (BTC 2017→, ETH 2017→)
  oi_hist.jsonl          — open interest, 1h, last ~30d (Binance retention cap)
  positioning_hist.jsonl — long/short + taker ratios, 1h, last ~30d (same cap)

Idempotent-ish: skips a target file that already has rows. Run once via venv:
  /root/grid-engine/venv/bin/python /root/grid-engine/data_backfill.py
"""
import os
import json
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LAKE = os.path.join(HERE, "datalake")
UA = {"User-Agent": "Mozilla/5.0 (grid-engine datalake backfill)"}
SYMS = ["BTCUSDT", "ETHUSDT"]


def _get(url, params):
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=20, headers=UA)
            if r.status_code == 429:
                time.sleep(30)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5)


def _target(name):
    path = os.path.join(LAKE, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print("skip {} (already seeded)".format(name), flush=True)
        return None
    return path


def backfill_funding():
    path = _target("funding_hist.jsonl")
    if not path:
        return
    with open(path, "w") as f:
        for sym in SYMS:
            # startTime=0 is treated as ABSENT (returns the latest page) —
            # must start from a real epoch. Binance USDT-M perps launched
            # Sep 2019.
            start, n = 1567900800000, 0
            while True:
                d = _get("https://fapi.binance.com/fapi/v1/fundingRate",
                         {"symbol": sym, "startTime": start, "limit": 1000})
                if not d:
                    break
                for x in d:
                    f.write(json.dumps({"ts": x["fundingTime"] // 1000,
                                        "sym": sym,
                                        "funding_rate": float(x["fundingRate"])}) + "\n")
                n += len(d)
                nxt = d[-1]["fundingTime"] + 1
                if nxt <= start:
                    break   # not advancing — bail rather than loop forever
                start = nxt
                # NOTE: don't break on len(d) < limit — Binance pages this
                # endpoint at 500 regardless of the requested limit. Paginate
                # until an empty response.
                time.sleep(0.3)
            print("funding_hist {}: {} rows".format(sym, n), flush=True)


def backfill_klines():
    path = _target("klines_1h.jsonl")
    if not path:
        return
    with open(path, "w") as f:
        for sym in SYMS:
            start, n = 0, 0
            while True:
                d = _get("https://api.binance.com/api/v3/klines",
                         {"symbol": sym, "interval": "1h",
                          "startTime": start, "limit": 1000})
                if not d:
                    break
                now_ms = int(time.time() * 1000)
                for k in d:
                    if k[6] > now_ms:      # skip the still-open candle
                        continue
                    f.write(json.dumps({"sym": sym, "ts": k[0] // 1000,
                                        "o": float(k[1]), "h": float(k[2]),
                                        "l": float(k[3]), "c": float(k[4]),
                                        "vol": float(k[5]), "trades": k[8],
                                        "taker_buy_vol": float(k[9])}) + "\n")
                n += len(d)
                nxt = d[-1][0] + 1
                if nxt <= start:
                    break
                start = nxt
                if len(d) < 1000 and d[-1][6] > now_ms:
                    break   # reached the live edge
                time.sleep(0.25)
            print("klines_1h {}: {} rows".format(sym, n), flush=True)


def backfill_oi():
    path = _target("oi_hist.jsonl")
    if not path:
        return
    with open(path, "w") as f:
        for sym in SYMS:
            d = _get("https://fapi.binance.com/futures/data/openInterestHist",
                     {"symbol": sym, "period": "1h", "limit": 500})
            for x in d or []:
                f.write(json.dumps({"ts": x["timestamp"] // 1000, "sym": sym,
                                    "oi_contracts": float(x["sumOpenInterest"]),
                                    "oi_usd": float(x["sumOpenInterestValue"])}) + "\n")
            print("oi_hist {}: {} rows".format(sym, len(d or [])), flush=True)


def backfill_positioning():
    path = _target("positioning_hist.jsonl")
    if not path:
        return
    eps = {"ls_global": "globalLongShortAccountRatio",
           "ls_top_acct": "topLongShortAccountRatio",
           "ls_top_pos": "topLongShortPositionRatio"}
    with open(path, "w") as f:
        for sym in SYMS:
            series = {}
            for key, ep in eps.items():
                d = _get("https://fapi.binance.com/futures/data/" + ep,
                         {"symbol": sym, "period": "1h", "limit": 500})
                for x in d or []:
                    series.setdefault(x["timestamp"] // 1000, {})[key] = \
                        float(x["longShortRatio"])
                time.sleep(0.3)
            d = _get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                     {"symbol": sym, "period": "1h", "limit": 500})
            for x in d or []:
                series.setdefault(x["timestamp"] // 1000, {})["taker_buy_sell"] = \
                    float(x["buySellRatio"])
            for ts in sorted(series):
                row = {"ts": ts, "sym": sym}
                row.update(series[ts])
                f.write(json.dumps(row) + "\n")
            print("positioning_hist {}: {} rows".format(sym, len(series)), flush=True)


if __name__ == "__main__":
    os.makedirs(LAKE, exist_ok=True)
    for job in (backfill_funding, backfill_oi, backfill_positioning,
                backfill_klines):
        try:
            job()
        except Exception as e:  # noqa: BLE001
            print("{} FAILED: {}".format(job.__name__, str(e)[:200]), flush=True)
    print("backfill complete", flush=True)
