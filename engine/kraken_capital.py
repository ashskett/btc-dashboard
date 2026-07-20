"""Kraken read-only client for P&L capital accounting.

Auth: HMAC-SHA512 (API-Key + API-Sign). Read-only key (Query Funds / Ledgers /
Trades). Credentials from env: KRAKEN_API_KEY, KRAKEN_API_SECRET.
"""
import os
import time
import json
import base64
import hashlib
import hmac
import urllib.parse

import requests

API_URL = "https://api.kraken.com"
KEY = os.environ.get("KRAKEN_API_KEY", "")
SECRET = os.environ.get("KRAKEN_API_SECRET", "")


def _sign(path: str, data: dict) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    sig = hmac.new(base64.b64decode(SECRET), message, hashlib.sha512)
    return base64.b64encode(sig.digest()).decode()


def _private(method: str, data: dict | None = None) -> dict:
    path = f"/0/private/{method}"
    data = dict(data or {})
    data["nonce"] = int(time.time() * 1000)
    headers = {"API-Key": KEY, "API-Sign": _sign(path, data)}
    r = requests.post(API_URL + path, headers=headers, data=data, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError("Kraken error: %s" % j["error"])
    return j["result"]


def get_balance() -> dict:
    return _private("Balance")


def get_ledgers(ltype: str = "all") -> list:
    """All ledger entries (paginated), optionally filtered by type
    (deposit/withdrawal/trade/transfer/...)."""
    out = []
    ofs = 0
    while True:
        res = _private("Ledgers", {"type": ltype, "ofs": ofs})
        ledger = res.get("ledger", {})
        if not ledger:
            break
        for lid, e in ledger.items():
            e["_id"] = lid
            out.append(e)
        ofs += len(ledger)
        if ofs >= int(res.get("count", 0)):
            break
        time.sleep(1)  # respect rate limit
    return out


if __name__ == "__main__":
    print("=== Kraken Balance ===")
    bal = get_balance()
    for asset, amt in sorted(bal.items()):
        if float(amt) != 0:
            print("  %-8s %s" % (asset, amt))

    print("\n=== Deposits / Withdrawals (ledger) ===")
    deps = get_ledgers("deposit")
    wds = get_ledgers("withdrawal")
    entries = sorted(deps + wds, key=lambda e: float(e.get("time", 0)))
    import datetime
    for e in entries:
        dt = datetime.datetime.utcfromtimestamp(float(e["time"])).strftime("%Y-%m-%d")
        print("  %s | %-11s | %-8s | amount=%14s | fee=%s" % (
            dt, e.get("type"), e.get("asset"), e.get("amount"), e.get("fee")))
    print("\ncounts: deposits=%d withdrawals=%d" % (len(deps), len(wds)))
