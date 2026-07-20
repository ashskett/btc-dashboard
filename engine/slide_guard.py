"""slide_guard.py — Sustained-grind ("falling knife") detector. OBSERVABILITY ONLY.

WHY THIS EXISTS
The flash_move detector fires on a single violent candle/cycle (>1.5xATR). It
deliberately ignores a slow staircase: on 2026-06-25 BTC ground down ~$2,580
over ~25 min in SIX consecutive 5m red candles, the biggest only $721 (1.08xATR)
— under the $1,005 flash threshold — so flash never fired and the grid bought
several rungs into the knife before TREND_DOWN (which needs 4 closes to confirm)
shut inner/mid off. This module targets that SHAPE: N consecutive same-direction
5m closes whose cumulative move exceeds a multiple of ATR.

PHASE 0 — OBSERVABILITY ONLY. It does NOT stop bots and is NOT wired into any
trading decision. It only logs when it WOULD have fired, so we can judge over a
couple of weeks whether it catches knives without false-tripping normal grid
oscillation. If it proves out, a later phase can let it pre-empt TREND_DOWN.

Side effects: writes slide_guard_state.json (dedupe) + slide_guard_log.jsonl
(history), and optionally one Telegram heads-up per new would-fire (clearly
marked NO ACTION). Self-fetches its own 5m candles so engine wiring is one call.
"""
import os
import json
import time

import market_data as md

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "slide_guard_state.json")
LOG_FILE = os.path.join(HERE, "slide_guard_log.jsonl")

# ── Tuning (observability — easy to retune from the log later) ──────────────
CONSEC_CANDLES = 4     # consecutive same-direction 5m closes to qualify as a run
CUM_ATR_MULT   = 1.5   # cumulative move across the run must exceed this x 1H ATR
LOOKBACK       = 10    # how many recent 5m candles to scan
NOTIFY         = True   # send ONE Telegram heads-up per new would-fire (no action)


def _load():
    try:
        if os.path.exists(STATE_FILE):
            return json.load(open(STATE_FILE))
    except Exception:
        pass
    return {"active": None, "fire_price": None, "fired_at": None, "run_start": None}


def _save(s):
    try:
        json.dump(s, open(STATE_FILE, "w"), indent=2)
    except Exception as e:
        print(f"Warning: slide_guard state save failed: {e}")


def _run_length(df, direction):
    """Count consecutive 5m candles (ending at the latest) closing in `direction`,
    and the cumulative price move across that run."""
    n = 0
    start_open = None
    for i in range(1, min(LOOKBACK, len(df)) + 1):
        o = df["open"].iloc[-i]
        c = df["close"].iloc[-i]
        is_dir = (c < o) if direction == "DOWN" else (c > o)
        if not is_dir:
            break
        n += 1
        start_open = o   # opens of the oldest candle in the run so far
    cum = (df["close"].iloc[-1] - start_open) if start_open is not None else 0.0
    return n, cum


def observe(price=None, atr=None, df_5m=None):
    """Call once per engine cycle. Returns a dict describing the slide state.
    OBSERVABILITY ONLY — never stops bots. Wrap the caller in try/except anyway."""
    result = {"status": "clear", "direction": None, "consec": 0,
              "cum_move": 0.0, "cum_atr": 0.0, "would_fire": False}
    try:
        if df_5m is None:
            df_5m = md.get_btc_data_short(timeframe="5m", limit=max(LOOKBACK + 2, 12))
        if df_5m is None or len(df_5m) < CONSEC_CANDLES or not atr:
            return result
    except Exception:
        return result

    s = _load()
    # Determine the latest candle's direction, then measure that run.
    last_dir = "DOWN" if df_5m["close"].iloc[-1] < df_5m["open"].iloc[-1] else "UP"
    consec, cum = _run_length(df_5m, last_dir)
    cum_atr = abs(cum) / atr if atr else 0.0
    qualifies = consec >= CONSEC_CANDLES and cum_atr >= CUM_ATR_MULT

    result.update({"direction": last_dir, "consec": consec,
                   "cum_move": round(cum, 1), "cum_atr": round(cum_atr, 2),
                   "would_fire": qualifies})

    if qualifies:
        # New would-fire only when we weren't already flagged in this direction.
        is_new = s.get("active") != last_dir
        result["status"] = "new" if is_new else "active"
        if is_new:
            s.update({"active": last_dir, "fire_price": float(df_5m["close"].iloc[-1]),
                      "fired_at": time.time(),
                      "run_start": float(df_5m["close"].iloc[-1] - cum)})
            _save(s)
            rec = {"ts": int(time.time()), "direction": last_dir,
                   "consec": int(consec), "cum_move": round(float(cum), 1),
                   "cum_atr": round(float(cum_atr), 2),
                   "price": round(float(df_5m["close"].iloc[-1]), 1),
                   "atr": round(float(atr), 1), "action": "OBSERVE_ONLY"}
            try:
                with open(LOG_FILE, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            except Exception as e:
                print(f"Warning: slide_guard log write failed: {e}")
            print("SLIDE_GUARD would-fire {} — {} consec 5m closes, "
                  "${:,.0f} ({:.2f}xATR) — OBSERVE ONLY, no action".format(
                      last_dir, consec, abs(cum), cum_atr))
            if NOTIFY:
                try:
                    import notify
                    notify.notify(
                        "SLIDE_GUARD (observe only, NO action): sustained %s — "
                        "%d consecutive 5m closes, $%s move (%.2fxATR) ending @ $%s. "
                        "Logged for monitoring; flash_move did not fire (too gradual)."
                        % (last_dir, consec, "{:,.0f}".format(abs(cum)), cum_atr,
                           "{:,.0f}".format(float(df_5m['close'].iloc[-1]))))
                except Exception:
                    pass
    else:
        # Run broken or below threshold — clear the active flag.
        if s.get("active"):
            s.update({"active": None, "fire_price": None, "fired_at": None,
                      "run_start": None})
            _save(s)
    return result


def get_state():
    return _load()


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(observe(atr=670.0), indent=2))
