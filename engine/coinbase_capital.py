"""Coinbase CDP (Ed25519) — read-only client to pull deposit/withdrawal history
for the P&L capital-events ledger.

Auth: CDP API key. Secret is base64 of 64 bytes (seed||pub); we sign a JWT
(EdDSA) per request. Read-only ("View") scope only.

Credentials from env: COINBASE_CDP_KEY_ID, COINBASE_CDP_SECRET.
"""
import os
import json
import time
import base64
import secrets as _secrets

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HOST = "api.coinbase.com"
KEY_ID = os.environ.get("COINBASE_CDP_KEY_ID", "")
SECRET_B64 = os.environ.get("COINBASE_CDP_SECRET", "")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _signing_key() -> Ed25519PrivateKey:
    raw = base64.b64decode(SECRET_B64)
    seed = raw[:32]  # Coinbase Ed25519 secret = seed(32) || pubkey(32)
    return Ed25519PrivateKey.from_private_bytes(seed)


def _make_jwt(method: str, path: str) -> str:
    """Build a short-lived CDP JWT for `METHOD host+path` (no query string)."""
    key = _signing_key()
    now = int(time.time())
    header = {"alg": "EdDSA", "kid": KEY_ID, "typ": "JWT",
              "nonce": _secrets.token_hex(16)}
    payload = {"sub": KEY_ID, "iss": "cdp", "nbf": now, "exp": now + 120,
               "uri": f"{method} {HOST}{path}"}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64url(json.dumps(payload, separators=(",", ":")).encode()))
    sig = key.sign(signing_input.encode())
    return signing_input + "." + _b64url(sig)


def _get(path: str, params: dict | None = None) -> dict:
    """Authenticated GET. `path` must be the bare path (the JWT signs it without
    query string)."""
    jwt = _make_jwt("GET", path)
    r = requests.get(f"https://{HOST}{path}",
                     headers={"Authorization": f"Bearer {jwt}",
                              "Content-Type": "application/json"},
                     params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def _ccode(a: dict) -> str:
    c = a.get("currency", "")
    return (c.get("code") if isinstance(c, dict) else c) or ""


def list_accounts() -> list:
    """All accounts, following pagination."""
    out = []
    path = "/v2/accounts"
    params = {"limit": 100}
    while True:
        data = _get(path, params)
        out.extend(data.get("data", []))
        nxt = (data.get("pagination") or {}).get("next_starting_after")
        if not nxt:
            break
        params = {"limit": 100, "starting_after": nxt}
    return out


def get_primary_balance() -> dict | None:
    """Accurate BTC + USDC quantities in the Primary (Default) portfolio — the one
    the grid trades. Two small fast calls (portfolios + one breakdown), unlike the
    3Commas pie_chart which is slow AND over-reports bot-locked BTC during
    SELL_ONLY. Returns {btc_qty, usdc_qty} or None on failure."""
    pf = _get("/api/v3/brokerage/portfolios")
    default = next((p for p in pf.get("portfolios", [])
                    if p.get("type") == "DEFAULT" and not p.get("deleted")), None)
    if not default:
        return None
    b = _get(f"/api/v3/brokerage/portfolios/{default['uuid']}")
    positions = (b.get("breakdown", {}) or {}).get("spot_positions", []) or []
    btc_qty = usdc_qty = 0.0
    for p in positions:
        asset = (p.get("asset") or "").upper()
        qty = float(p.get("total_balance_crypto") or 0)
        if asset == "BTC":
            btc_qty = qty
        elif asset in ("USDC", "USDT", "USD"):
            usdc_qty += qty
    return {"btc_qty": btc_qty, "usdc_qty": usdc_qty}


def list_transactions(account_id: str) -> list:
    out = []
    path = f"/v2/accounts/{account_id}/transactions"
    params = {"limit": 100}
    while True:
        data = _get(path, params)
        out.extend(data.get("data", []))
        nxt = (data.get("pagination") or {}).get("next_starting_after")
        if not nxt:
            break
        params = {"limit": 100, "starting_after": nxt}
    return out


# Transaction types that are NOT external capital flows (exclude from the ledger).
NOISE_TYPES = {
    "advanced_trade_fill", "trade", "buy", "sell", "interest", "fiat_interest",
    "subscription", "staking_reward", "inflation_reward", "reward",
}

if __name__ == "__main__":
    from collections import Counter
    accs = list_accounts()
    want = {"GBP", "EUR", "USD", "USDC", "BTC"}
    rel = [a for a in accs if _ccode(a) in want]
    print("total accounts: %d   relevant(GBP/EUR/USD/USDC/BTC): %d" % (len(accs), len(rel)))

    all_type_counts = Counter()
    candidates = []  # non-noise = capital-flow candidates
    for a in rel:
        try:
            txs = list_transactions(a.get("id"))
        except Exception as e:
            print("  WARN %s %s: %s" % (_ccode(a), a.get("id")[:8], e)); continue
        for t in txs:
            all_type_counts[t.get("type")] += 1
            if t.get("type") not in NOISE_TYPES:
                candidates.append((_ccode(a), t))

    print("\n=== ALL transaction types across relevant accounts ===")
    for ty, n in all_type_counts.most_common():
        print("  %-22s %d" % (ty, n))

    print("\n=== CAPITAL-FLOW CANDIDATES (non-trade), chronological ===")
    candidates.sort(key=lambda x: x[1].get("created_at", ""))
    for ccode, t in candidates:
        amt = t.get("amount", {}); nat = t.get("native_amount", {})
        print("  %s | %-16s | acct=%-4s | amt=%14s %-5s | native=%10s %s | %s" % (
            t.get("created_at", "")[:10], t.get("type"), ccode,
            amt.get("amount"), amt.get("currency"),
            nat.get("amount"), nat.get("currency"),
            (t.get("description") or "")[:28]))
    print("\ncandidate count: %d" % len(candidates))

    # ── Net capital in GBP (native currency) ──
    # Real external flows = fiat_deposit + fiat_withdrawal + send (crypto in/out).
    # 'tx' are internal +/- pairs that net to zero → excluded.
    CAPITAL_TYPES = {"fiat_deposit", "fiat_withdrawal", "send"}
    by_type = Counter()
    net_gbp = 0.0
    fiat_net = 0.0
    for ccode, t in candidates:
        ty = t.get("type")
        if ty not in CAPITAL_TYPES:
            continue
        g = float((t.get("native_amount") or {}).get("amount") or 0)  # GBP
        by_type[ty] += g
        net_gbp += g
        if ty in ("fiat_deposit", "fiat_withdrawal"):
            fiat_net += g
    print("\n=== NET CAPITAL (GBP, native) ===")
    for ty, v in by_type.items():
        print("  %-16s £%+.2f" % (ty, v))
    print("  ---------------------------------")
    print("  NET capital injected (fiat+crypto): £%+.2f" % net_gbp)
    print("  fiat-only net (most certain):       £%+.2f" % fiat_net)

    # Current portfolio value (USD from portfolio_log) → GBP via Coinbase spot.
    try:
        pf = None
        for line in open("portfolio_log.jsonl"):
            line = line.strip()
            if line:
                try: pf = json.loads(line)
                except Exception: pass
        port_usd = pf.get("portfolio_usd") if pf else None
        # GBP-USD spot (USD per GBP)
        sp = _get("/v2/prices/GBP-USD/spot") if False else None
    except Exception as e:
        port_usd = None
        print("  (portfolio read err: %s)" % e)
    # GBP/USD via exchange-rates (public, no auth needed but reuse client)
    try:
        rr = requests.get("https://api.coinbase.com/v2/exchange-rates?currency=GBP", timeout=20).json()
        gbpusd = float(rr["data"]["rates"]["USD"])  # USD per 1 GBP
    except Exception as e:
        gbpusd = None
        print("  (fx err: %s)" % e)
    if port_usd and gbpusd:
        port_gbp = port_usd / gbpusd
        print("\n=== IMPLIED P&L ===")
        print("  current portfolio: $%.0f  = £%.0f  (GBP/USD %.4f)" % (port_usd, port_gbp, gbpusd))
        print("  net capital in:    £%.0f" % net_gbp)
        print("  ==> P&L (incl crypto flows): £%+.0f" % (port_gbp - net_gbp))
        print("  ==> P&L (fiat-only basis):   £%+.0f" % (port_gbp - fiat_net))
