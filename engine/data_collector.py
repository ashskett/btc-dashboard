"""data_collector.py — the DATA LAKE feeder (Ash, 2026-09-23).

"data data data" — collect every remotely relevant free feed so the Signal Lab
can hunt correlations later. Report-only infrastructure: no keys, no trading
surface, no engine coupling. Append-only JSONL per domain in datalake/.

Run from cron every 5 minutes (venv python). Cadence gating by wall clock:
  FAST  (every run, 5m):  perp funding/basis, open interest, cross-venue spot
  MED   (:00/:15/:30/:45): positioning ratios, taker flow, global mcap/dominance,
                           stablecoin caps, DVOL, mempool
  SLOW  (top of hour):     macro (DXY/SPX/gold), Fear&Greed, 1h klines, hashrate

Every row: {"ts": <epoch>, ...}. Sources fail independently — one dead API
never blocks the rest. History backfills live in *_hist.jsonl (see
data_backfill.py, run once).
"""
import os
import json
import time
import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LAKE = os.path.join(HERE, "datalake")
UA = {"User-Agent": "Mozilla/5.0 (grid-engine datalake)"}
TIMEOUT = 12

SYMS = ["BTCUSDT", "ETHUSDT"]


def _get(url, params=None):
    r = requests.get(url, params=params, timeout=TIMEOUT, headers=UA)
    r.raise_for_status()
    return r.json()


def _append(domain, row):
    row["ts"] = row.get("ts") or round(time.time(), 1)
    path = os.path.join(LAKE, domain + ".jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def _log(msg):
    print("{} {}".format(datetime.datetime.now(datetime.timezone.utc)
                         .strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# ── FAST (every 5 min) ──────────────────────────────────────────────────────

def collect_funding_basis():
    """Binance perp mark/index premium + predicted funding; Bybit funding/OI.
    Spot-perp basis is the classic leverage-demand gauge."""
    for sym in SYMS:
        d = _get("https://fapi.binance.com/fapi/v1/premiumIndex",
                 {"symbol": sym})
        _append("funding", {
            "src": "binance", "sym": sym,
            "mark": float(d["markPrice"]), "index": float(d["indexPrice"]),
            "basis_bps": round((float(d["markPrice"]) / float(d["indexPrice"]) - 1) * 1e4, 3),
            "funding_rate": float(d["lastFundingRate"]),
            "next_funding": int(d["nextFundingTime"]) // 1000})
    d = _get("https://api.bybit.com/v5/market/tickers",
             {"category": "linear", "symbol": "BTCUSDT"})
    t = d["result"]["list"][0]
    _append("funding", {
        "src": "bybit", "sym": "BTCUSDT",
        "mark": float(t["markPrice"]), "index": float(t["indexPrice"]),
        "basis_bps": round((float(t["markPrice"]) / float(t["indexPrice"]) - 1) * 1e4, 3),
        "funding_rate": float(t["fundingRate"]),
        "oi_contracts": float(t["openInterest"]),
        "oi_usd": float(t["openInterestValue"])})


def collect_open_interest():
    for sym in SYMS:
        d = _get("https://fapi.binance.com/fapi/v1/openInterest",
                 {"symbol": sym})
        _append("oi", {"src": "binance", "sym": sym,
                       "oi_contracts": float(d["openInterest"])})


def collect_spot_cross():
    """Cross-venue spot BTC — venue spreads widen under stress/flow imbalance."""
    row = {}
    try:
        row["binance"] = float(_get("https://api.binance.com/api/v3/ticker/price",
                                    {"symbol": "BTCUSDT"})["price"])
    except Exception:
        pass
    try:
        k = _get("https://api.kraken.com/0/public/Ticker", {"pair": "XBTUSD"})
        row["kraken"] = float(k["result"]["XXBTZUSD"]["c"][0])
    except Exception:
        pass
    try:
        row["coinbase"] = float(_get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot")["data"]["amount"])
    except Exception:
        pass
    if len(row) >= 2:
        vals = sorted(row.values())
        row["spread_bps"] = round((vals[-1] / vals[0] - 1) * 1e4, 3)
    if row:
        _append("spot_cross", row)


# ── MED (every 15 min) ──────────────────────────────────────────────────────

def collect_positioning():
    """Binance futures positioning: global long/short accounts, top-trader
    accounts AND positions, taker buy/sell flow. 5m period, latest point."""
    eps = {"ls_global": "globalLongShortAccountRatio",
           "ls_top_acct": "topLongShortAccountRatio",
           "ls_top_pos": "topLongShortPositionRatio"}
    for sym in SYMS:
        row = {"sym": sym}
        for key, ep in eps.items():
            try:
                d = _get("https://fapi.binance.com/futures/data/" + ep,
                         {"symbol": sym, "period": "5m", "limit": 1})
                if d:
                    row[key] = float(d[-1]["longShortRatio"])
            except Exception:
                pass
        try:
            d = _get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                     {"symbol": sym, "period": "5m", "limit": 1})
            if d:
                row["taker_buy_sell"] = float(d[-1]["buySellRatio"])
                row["taker_buy_vol"] = float(d[-1]["buyVol"])
                row["taker_sell_vol"] = float(d[-1]["sellVol"])
        except Exception:
            pass
        if len(row) > 1:
            _append("positioning", row)


def collect_global():
    """CoinGecko globals: total mcap, BTC dominance, stablecoin caps (USDT+USDC
    supply growth ~ dry powder entering/leaving the system)."""
    g = _get("https://api.coingecko.com/api/v3/global")["data"]
    row = {"total_mcap_usd": g["total_market_cap"].get("usd"),
           "btc_dominance": round(g["market_cap_percentage"].get("btc", 0), 3),
           "eth_dominance": round(g["market_cap_percentage"].get("eth", 0), 3),
           "mcap_change_24h_pct": round(
               g.get("market_cap_change_percentage_24h_usd") or 0, 3)}
    try:
        s = _get("https://api.coingecko.com/api/v3/simple/price",
                 {"ids": "tether,usd-coin", "vs_currencies": "usd",
                  "include_market_cap": "true"})
        row["usdt_mcap"] = s.get("tether", {}).get("usd_market_cap")
        row["usdc_mcap"] = s.get("usd-coin", {}).get("usd_market_cap")
    except Exception:
        pass
    _append("global", row)


def collect_dvol():
    """Deribit BTC implied-vol index (options market's fear gauge)."""
    now_ms = int(time.time() * 1000)
    d = _get("https://www.deribit.com/api/v2/public/get_volatility_index_data",
             {"currency": "BTC", "resolution": "3600",
              "start_timestamp": now_ms - 2 * 3600 * 1000,
              "end_timestamp": now_ms})
    data = d.get("result", {}).get("data") or []
    if data:
        last = data[-1]   # [ts, open, high, low, close]
        _append("dvol", {"dvol": last[4]})


def collect_mempool():
    fees = _get("https://mempool.space/api/v1/fees/recommended")
    row = {"fee_fastest": fees.get("fastestFee"),
           "fee_hour": fees.get("hourFee")}
    try:
        mp = _get("https://mempool.space/api/mempool")
        row["mempool_count"] = mp.get("count")
        row["mempool_vsize"] = mp.get("vsize")
    except Exception:
        pass
    _append("chain", row)


# ── SLOW (hourly) ───────────────────────────────────────────────────────────

def collect_macro():
    """Risk-asset context via Yahoo: dollar index, S&P 500, gold. BTC's beta
    to macro risk comes and goes — that itself is a trackable regime signal."""
    for sym, name in (("DX-Y.NYB", "dxy"), ("^GSPC", "spx"), ("GC=F", "gold")):
        try:
            d = _get("https://query1.finance.yahoo.com/v8/finance/chart/" + sym,
                     {"interval": "1d", "range": "1d"})
            meta = d["chart"]["result"][0]["meta"]
            _append("macro", {"sym": name,
                              "price": meta.get("regularMarketPrice"),
                              "prev_close": meta.get("chartPreviousClose")})
        except Exception as e:
            _log("macro {} failed: {}".format(name, e))


def collect_sentiment():
    d = _get("https://api.alternative.me/fng/?limit=1")["data"][0]
    _append("sentiment", {"fear_greed": int(d["value"]),
                          "label": d["value_classification"]})


def collect_klines():
    """Last CLOSED Binance 1h kline (spot) — long-horizon OHLCV backbone that
    joins cleanly onto the backfilled history."""
    for sym in SYMS:
        d = _get("https://api.binance.com/api/v3/klines",
                 {"symbol": sym, "interval": "1h", "limit": 2})
        k = d[0]   # d[1] is the still-open candle
        _append("klines_1h", {"sym": sym, "ts": k[0] // 1000,
                              "o": float(k[1]), "h": float(k[2]),
                              "l": float(k[3]), "c": float(k[4]),
                              "vol": float(k[5]), "trades": k[8],
                              "taker_buy_vol": float(k[9])})


def collect_hashrate():
    try:
        d = _get("https://mempool.space/api/v1/mining/hashrate/3d")
        _append("chain", {"hashrate_3d": d.get("currentHashrate"),
                          "difficulty": d.get("currentDifficulty")})
    except Exception as e:
        _log("hashrate failed: {}".format(e))


# ── driver ──────────────────────────────────────────────────────────────────

FAST = [collect_funding_basis, collect_open_interest, collect_spot_cross]
MED = [collect_positioning, collect_global, collect_dvol, collect_mempool]
SLOW = [collect_macro, collect_sentiment, collect_klines, collect_hashrate]


def run():
    os.makedirs(LAKE, exist_ok=True)
    minute = datetime.datetime.now(datetime.timezone.utc).minute
    jobs = list(FAST)
    if minute % 15 < 5:
        jobs += MED
    if minute < 5:
        jobs += SLOW
    ok = fail = 0
    for job in jobs:
        try:
            job()
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            _log("{} FAILED: {}".format(job.__name__, str(e)[:120]))
    _log("collected: {} ok, {} failed ({} jobs)".format(ok, fail, len(jobs)))


if __name__ == "__main__":
    run()
