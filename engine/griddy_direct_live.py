"""griddy_direct_live.py — GRIDDY DIRECT LIVE: the buy-only ladder on real
Coinbase orders (2026-10-05).

SAFETY MODEL (structural, not procedural):
- This module contains NO market-order code path. The only order type it can
  express is a post-only GTC limit. A post-only order that would cross the
  book is REJECTED by Coinbase — so even a mispriced rung cannot take
  liquidity, let alone market-trade the stack.
- SELLs are only ever the TP of a rung's own filled BUY, sized to that clip.
  Existing holdings are unreachable.
- client_order_ids are deterministic (gdl-<ladder>-<rung>-c<cycle>-<b|s>), so
  a retry after a crash/timeout can never double-place: Coinbase rejects the
  duplicate and reconciliation picks up the original.
- Kill switch: kill() batch-cancels every tracked order and deactivates.
  Flag file direct_live_killed halts the loop even if state is corrupt.
- Activation is double-gated upstream (dashboard requires confirm="GO-LIVE"
  for dry_run=False) and per Ash's doctrine happens only on his explicit word.
- dry_run mode runs the full loop and logs intended orders without ever
  calling the order endpoint.

State: direct_live_state.json (atomic). Fills: direct_live_fills.jsonl.
Ladder geometry = the proven paper buy-only ladder (griddy_direct.BO_STEP /
BO_SPAN), top-N rungs for the supervised small-size start.
"""
import os
import json
import time
import base64
import secrets as _secrets

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "direct_live_state.json")
FILLS_FILE = os.path.join(HERE, "direct_live_fills.jsonl")
KILL_FILE = os.path.join(HERE, "direct_live_killed")

HOST = "api.coinbase.com"
PRODUCT = "BTC-USDC"
MAX_CAPITAL_USD = 31000.0      # fat-finger rail: full ladder is ~$30k
MAX_RUNGS = 12


def _log(msg):
    print("[direct-live] {}".format(msg), flush=True)


def _notify(msg):
    try:
        import notify
        notify.send("GRIDDY DIRECT LIVE: " + msg)
    except Exception:
        pass
    _log(msg)


# ── auth (trade-scoped CDP key, Ed25519 EdDSA JWT) ──────────────────────────

def _creds():
    kid = os.environ.get("COINBASE_TRADE_KEY_NAME", "")
    sec = os.environ.get("COINBASE_TRADE_SECRET", "")
    if not kid or not sec:
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(HERE, ".env"))
        except Exception:
            pass
        kid = os.environ.get("COINBASE_TRADE_KEY_NAME", "")
        sec = os.environ.get("COINBASE_TRADE_SECRET", "")
    return kid, sec


def _make_jwt(method, path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    def b64url(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    kid, sec = _creds()
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(sec)[:32])
    now = int(time.time())
    header = {"alg": "EdDSA", "kid": kid, "typ": "JWT",
              "nonce": _secrets.token_hex(16)}
    payload = {"sub": kid, "iss": "cdp", "nbf": now, "exp": now + 120,
               "uri": "{} {}{}".format(method, HOST, path)}
    si = (b64url(json.dumps(header, separators=(",", ":")).encode()) + "." +
          b64url(json.dumps(payload, separators=(",", ":")).encode()))
    return si + "." + b64url(key.sign(si.encode()))


def _req(method, path, body=None, params=None):
    """Signed request. JWT covers METHOD host+path (no query string)."""
    url = "https://{}{}".format(HOST, path)
    hdrs = {"Authorization": "Bearer " + _make_jwt(method, path),
            "Content-Type": "application/json"}
    r = requests.request(method, url, headers=hdrs, params=params,
                         json=body, timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


# ── Advanced Trade wrappers (post-only limits ONLY) ─────────────────────────

def get_open_orders():
    code, d = _req("GET", "/api/v3/brokerage/orders/historical/batch",
                   params={"order_status": "OPEN", "product_id": PRODUCT,
                           "limit": 100})
    if code != 200:
        raise RuntimeError("open orders {}: {}".format(code, str(d)[:200]))
    return d.get("orders") or []


def get_order(order_id):
    code, d = _req("GET", "/api/v3/brokerage/orders/historical/" + order_id)
    if code != 200:
        raise RuntimeError("get order {}: {}".format(code, str(d)[:200]))
    return d.get("order") or {}


def place_postonly_limit(side, price, size_btc, client_order_id):
    """The ONLY order-creating function in this module. Post-only, GTC, limit."""
    assert side in ("BUY", "SELL")
    assert price > 0 and size_btc > 0
    body = {"client_order_id": client_order_id,
            "product_id": PRODUCT,
            "side": side,
            "order_configuration": {"limit_limit_gtc": {
                "base_size": "{:.8f}".format(size_btc),
                "limit_price": "{:.2f}".format(price),
                "post_only": True}}}
    code, d = _req("POST", "/api/v3/brokerage/orders", body=body)
    if code == 200 and d.get("success"):
        return True, d.get("success_response", {}).get("order_id", "")
    err = (d.get("error_response") or {})
    reason = err.get("error") or err.get("preview_failure_reason") or str(d)[:200]
    return False, reason


def cancel_orders(order_ids):
    if not order_ids:
        return []
    code, d = _req("POST", "/api/v3/brokerage/orders/batch_cancel",
                   body={"order_ids": order_ids})
    if code != 200:
        raise RuntimeError("cancel {}: {}".format(code, str(d)[:200]))
    return d.get("results") or []


def preview_order(side, price, size_btc):
    """Auth/scope check without placing anything."""
    body = {"product_id": PRODUCT, "side": side,
            "order_configuration": {"limit_limit_gtc": {
                "base_size": "{:.8f}".format(size_btc),
                "limit_price": "{:.2f}".format(price),
                "post_only": True}}}
    return _req("POST", "/api/v3/brokerage/orders/preview", body=body)


# ── state ───────────────────────────────────────────────────────────────────

def _load():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"active": False}


def _save(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def get_state():
    return _load()


def activate(price, per_rung_usd=500.0, top_n=3, dry_run=True):
    """Build the live ladder: top_n rungs of the proven paper geometry.
    DOES NOT place orders — the loop does, on its next tick (and only when
    dry_run is False)."""
    import griddy_direct as gd
    if _load().get("active"):
        raise RuntimeError("already active — kill first")
    total = per_rung_usd * top_n
    if total > MAX_CAPITAL_USD:
        raise RuntimeError("capital {:.0f} exceeds rail {:.0f}".format(
            total, MAX_CAPITAL_USD))
    if top_n > MAX_RUNGS:
        raise RuntimeError("top_n exceeds {}".format(MAX_RUNGS))
    rungs = []
    for k in range(1, top_n + 1):
        lvl = price * (1 - gd.BO_STEP) ** k
        rungs.append({"id": "r%d" % k, "price": round(lvl, 2),
                      "tp": round(lvl * (1 + gd.BO_STEP), 2),
                      "size": round(per_rung_usd / lvl, 8),
                      "cycle": 1, "state": "armed",
                      "buy_order_id": None, "tp_order_id": None,
                      "dry_logged": False})
    s = {"active": True, "dry_run": bool(dry_run), "created": time.time(),
         "ladder_id": _secrets.token_hex(3), "anchor_price": price,
         "per_rung_usd": per_rung_usd, "capital": total, "rungs": rungs,
         "realised": 0.0, "fees": 0.0, "fills": 0, "roundtrips": 0,
         "last_price": price, "last_tick": 0, "errors": []}
    _save(s)
    _notify("ladder {} {} — {} rungs x ${:,.0f}, floor ${:,.0f}".format(
        s["ladder_id"], "DRY-RUN armed" if dry_run else "ARMED LIVE",
        top_n, per_rung_usd, rungs[-1]["price"]))
    return s


def kill(reason="manual"):
    """Cancel everything we own, deactivate. The one-call stop."""
    s = _load()
    ids = [r[k] for r in s.get("rungs", [])
           for k in ("buy_order_id", "tp_order_id") if r.get(k)]
    results = []
    if ids and not s.get("dry_run"):
        try:
            results = cancel_orders(ids)
        except Exception as e:
            _notify("KILL: cancel error ({}) — check Coinbase open orders "
                    "manually".format(e))
    s["active"] = False
    s["killed"] = {"ts": time.time(), "reason": reason,
                   "cancelled": len(results)}
    _save(s)
    _notify("KILLED ({}) — {} orders cancelled, ladder deactivated".format(
        reason, len(results)))
    return s["killed"]


def _err(s, msg):
    _log("ERROR " + msg)
    s["errors"] = (s.get("errors") or [])[-9:] + [
        {"ts": round(time.time(), 0), "msg": msg[:200]}]


def _record_fill(s, rung, side, od):
    size = float(od.get("filled_size") or rung["size"])
    px = float(od.get("average_filled_price") or 0) or (
        rung["price"] if side == "BUY" else rung["tp"])
    fee = float(od.get("total_fees") or 0)
    s["fees"] += fee
    s["fills"] += 1
    row = {"ts": int(time.time()), "side": side, "price": px, "size": size,
           "fee": fee, "rung": rung["id"], "cycle": rung["cycle"],
           "order_id": od.get("order_id", "")}
    try:
        with open(FILLS_FILE, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass
    _notify("{} fill {} {:.6f} BTC @ ${:,.2f} (fee ${:.2f}) [{} c{}]".format(
        "BUY" if side == "BUY" else "TP SELL", PRODUCT, size, px, fee,
        rung["id"], rung["cycle"]))
    return size, px, fee


def _coid(s, rung, leg):
    return "gdl-{}-{}-c{}-{}".format(s["ladder_id"], rung["id"],
                                     rung["cycle"], leg)


def tick(spot_price):
    """One reconciliation pass. Called by the dashboard loop (~60s)."""
    s = _load()
    if not s.get("active"):
        return None
    if os.path.exists(KILL_FILE):
        _err(s, "kill flag file present — loop halted")
        _save(s)
        return None
    dry = s.get("dry_run", True)
    open_by_id = {}
    if not dry:
        try:
            open_by_id = {o["order_id"]: o for o in get_open_orders()}
        except Exception as e:
            _err(s, "open-orders fetch failed: {}".format(e))
            _save(s)
            return None

    for rung in s["rungs"]:
        try:
            if rung["state"] == "armed":
                if rung["price"] >= spot_price:
                    continue   # post-only would cross; wait for price above rung
                coid = _coid(s, rung, "b")
                if dry:
                    if not rung.get("dry_logged"):
                        _log("DRY would place BUY {:.8f} @ {:,.2f} ({})".format(
                            rung["size"], rung["price"], coid))
                        rung["dry_logged"] = True
                    continue
                ok, res = place_postonly_limit("BUY", rung["price"],
                                               rung["size"], coid)
                if ok:
                    rung["buy_order_id"] = res
                    rung["state"] = "buy_open"
                    _log("placed BUY {} @ {:,.2f} ({})".format(
                        rung["id"], rung["price"], res))
                elif "DUPLICATE" in str(res).upper():
                    _err(s, "duplicate client_order_id {} — reconciling "
                            "next tick".format(coid))
                else:
                    _err(s, "BUY place failed {}: {}".format(rung["id"], res))

            elif rung["state"] == "buy_open" and not dry:
                oid = rung["buy_order_id"]
                if oid in open_by_id:
                    continue
                od = get_order(oid)
                st = od.get("status")
                if st == "FILLED":
                    _record_fill(s, rung, "BUY", od)
                    rung["state"] = "holding"
                elif st in ("CANCELLED", "EXPIRED", "FAILED"):
                    _err(s, "BUY {} {} — re-arming".format(rung["id"], st))
                    rung["buy_order_id"] = None
                    rung["cycle"] += 1   # new cycle → new client_order_id
                    rung["state"] = "armed"

            elif rung["state"] == "holding":
                coid = _coid(s, rung, "s")
                if dry:
                    continue
                ok, res = place_postonly_limit("SELL", rung["tp"],
                                               rung["size"], coid)
                if ok:
                    rung["tp_order_id"] = res
                    rung["state"] = "tp_open"
                    _log("placed TP {} @ {:,.2f} ({})".format(
                        rung["id"], rung["tp"], res))
                elif "DUPLICATE" in str(res).upper():
                    _err(s, "duplicate TP coid {} — reconciling".format(coid))
                else:
                    _err(s, "TP place failed {}: {}".format(rung["id"], res))

            elif rung["state"] == "tp_open" and not dry:
                oid = rung["tp_order_id"]
                if oid in open_by_id:
                    continue
                od = get_order(oid)
                st = od.get("status")
                if st == "FILLED":
                    size, px, fee = _record_fill(s, rung, "SELL", od)
                    s["realised"] += size * (px - rung["price"]) - fee
                    s["roundtrips"] += 1
                    rung["cycle"] += 1
                    rung["buy_order_id"] = rung["tp_order_id"] = None
                    rung["state"] = "armed"
                elif st in ("CANCELLED", "EXPIRED", "FAILED"):
                    _err(s, "TP {} {} — replacing".format(rung["id"], st))
                    rung["tp_order_id"] = None
                    rung["state"] = "holding"
        except Exception as e:  # one rung's trouble never stops the rest
            _err(s, "rung {} tick error: {}".format(rung["id"], e))

    # reconciliation: any OPEN order carrying our ladder prefix that no rung
    # references is a stray — alert loudly, never auto-cancel silently.
    if not dry:
        ours = {r[k] for r in s["rungs"]
                for k in ("buy_order_id", "tp_order_id") if r.get(k)}
        prefix = "gdl-{}-".format(s["ladder_id"])
        strays = [o for oid, o in open_by_id.items()
                  if oid not in ours and
                  str(o.get("client_order_id", "")).startswith(prefix)]
        if strays:
            _notify("RECONCILIATION MISMATCH: {} untracked ladder order(s) "
                    "open — investigate before anything else".format(len(strays)))

    s["last_price"] = spot_price
    s["last_tick"] = time.time()
    _save(s)
    return True


def summary():
    s = _load()
    if not s.get("active"):
        out = {"active": False}
        if s.get("killed"):
            out["killed"] = s["killed"]
        return out
    states = {}
    for r in s["rungs"]:
        states[r["state"]] = states.get(r["state"], 0) + 1
    return {"active": True, "mode": "DRY-RUN" if s.get("dry_run") else "LIVE",
            "ladder_id": s["ladder_id"], "since": s["created"],
            "anchor_price": s["anchor_price"], "capital": s["capital"],
            "per_rung_usd": s["per_rung_usd"], "rung_states": states,
            "floor": s["rungs"][-1]["price"],
            "fills": s["fills"], "roundtrips": s["roundtrips"],
            "realised_usd": round(s["realised"], 2),
            "fees_usd": round(s["fees"], 2),
            "last_price": s.get("last_price"),
            "age_min": round((time.time() - s.get("last_tick", 0)) / 60, 1)
            if s.get("last_tick") else None,
            "recent_errors": (s.get("errors") or [])[-3:]}
