import json, os, time

_REGIME_STATE_FILE = os.path.join(os.path.dirname(__file__), "regime_state.json")

def _load_regime_state():
    try:
        return json.load(open(_REGIME_STATE_FILE))
    except Exception:
        return {"below_tl_count": 0}

def _save_regime_state(state):
    try:
        json.dump(state, open(_REGIME_STATE_FILE, "w"))
    except Exception:
        pass

def get_regime_state() -> dict:
    """Public accessor — returns current persisted regime state dict."""
    return _load_regime_state()

def detect_regime(df, trendline):

    price = df.close.iloc[-1]
    bb_width = df.bb_width.iloc[-1]
    atr = df.atr.iloc[-1]

    # ── TREND_DOWN hysteresis (Schmitt trigger) ────────────────────────────────
    #
    # ENTRY: price < trendline − ATR×0.75 for 2 consecutive cycles.
    #        0.75×ATR is ~$300 at current vol — filters normal pullbacks.
    #        (Was ATR×0.15 = ~$61 — fired on trivial dips and then got stuck
    #         because the exit threshold was the same as entry.)
    #
    # EXIT:  price > trendline − ATR×0.15 (within $61 of trendline = recovered).
    #        Separate exit threshold prevents chop: once TREND_DOWN fires, price
    #        must recover to near the trendline before it clears — not just
    #        bounce to the entry threshold.
    #
    # This mirrors the trending_up Schmitt trigger (entry 5.5, exit 4.5).
    #
    TREND_DOWN_ENTRY_GAP  = 1.0   # ATR multiplier to enter TREND_DOWN
    TREND_DOWN_EXIT_GAP   = 0.25  # ATR multiplier: trendline-recovery exit
    TD_STABLE_CYCLES      = 8     # consecutive cycles without new low to auto-clear
    TD_BOUNCE_ATR         = 0.5   # minimum bounce from low (× ATR) to auto-clear

    rs = _load_regime_state()
    td_active = rs.get("trend_down_active", False)

    if td_active:
        # ── Track lowest price during this episode ────────────────────────
        td_low = rs.get("td_low")
        if td_low is None or price < td_low:
            rs["td_low"] = price
            rs["td_no_new_low_count"] = 0
        else:
            rs["td_no_new_low_count"] = rs.get("td_no_new_low_count", 0) + 1

        # ── Exit path 1: price recovered close to drawn trendline ─────────
        if price >= trendline - atr * TREND_DOWN_EXIT_GAP:
            rs.update({"trend_down_active": False, "below_tl_count": 0,
                       "td_low": None, "td_no_new_low_count": 0,
                       "td_last_low": None})   # trendline recovered — no auto-activate needed
            _save_regime_state(rs)
            print(f"[Regime] TREND_DOWN OFF (trendline recovery) — "
                  f"price ${price:,.0f} within {TREND_DOWN_EXIT_GAP}×ATR "
                  f"of trendline ${trendline:,.0f}")
            # Fall through to other regime checks

        # ── Exit path 2: price stabilised — no new lows + meaningful bounce
        #    Clears TREND_DOWN autonomously when the trendline has not been
        #    updated to reflect new market structure (the common case).
        #    Requires BOTH conditions to avoid clearing on dead-cat bounces:
        #      • TD_STABLE_CYCLES consecutive cycles without a new low (~16 min)
        #      • Price bounced ≥ TD_BOUNCE_ATR × ATR above the episode low
        elif (rs.get("td_no_new_low_count", 0) >= TD_STABLE_CYCLES
              and price >= rs["td_low"] + atr * TD_BOUNCE_ATR):
            _td_low_snap    = rs["td_low"]
            _td_bounce_snap = round(price - _td_low_snap, 0)
            _td_stable_snap = rs["td_no_new_low_count"]
            rs.update({"trend_down_active": False, "below_tl_count": 0,
                       "td_low": None, "td_no_new_low_count": 0,
                       "td_last_low": _td_low_snap})  # preserved for trendline auto-activate
            _save_regime_state(rs)
            print(f"[Regime] TREND_DOWN AUTO-CLEAR — stabilised for "
                  f"{_td_stable_snap} cycles, bounced ${_td_bounce_snap:,.0f} "
                  f"from low ${_td_low_snap:,.0f} "
                  f"(threshold {TD_BOUNCE_ATR}×ATR = ${atr * TD_BOUNCE_ATR:,.0f})")
            # Fall through to other regime checks — engine recovery block will
            # redeploy the grid and notify. td_low stored above for retest tracking.

        else:
            _stable = rs.get("td_no_new_low_count", 0)
            _bounce = round(price - rs["td_low"], 0) if rs.get("td_low") else 0
            _needed_bounce = round(atr * TD_BOUNCE_ATR, 0)
            _needed_cycles = TD_STABLE_CYCLES - _stable
            print(f"[Regime] TREND_DOWN active — "
                  f"stable {_stable}/{TD_STABLE_CYCLES} cycles, "
                  f"bounce ${_bounce:,.0f}/${_needed_bounce:,.0f} needed "
                  f"(low ${rs.get('td_low', price):,.0f})")
            _save_regime_state(rs)
            return "TREND_DOWN"
    else:
        # Not in TREND_DOWN — count consecutive cycles below entry threshold
        entry_threshold = trendline - atr * TREND_DOWN_ENTRY_GAP
        if price < entry_threshold:
            rs["below_tl_count"] = rs.get("below_tl_count", 0) + 1
        else:
            if rs.get("below_tl_count", 0) > 0:
                rs["below_tl_count"] = 0

        if rs.get("below_tl_count", 0) >= 2:
            rs.update({"trend_down_active": True, "td_low": price,
                       "td_no_new_low_count": 0})
            _save_regime_state(rs)
            print(f"[Regime] TREND_DOWN ON — price ${price:,.0f} has been "
                  f">{TREND_DOWN_ENTRY_GAP}×ATR below trendline ${trendline:,.0f} "
                  f"for {rs['below_tl_count']} cycles")
            return "TREND_DOWN"

        _save_regime_state(rs)
    # ─────────────────────────────────────────────────────────────────────────

    # Require 3 consecutive candles below the 10th percentile to confirm
    # compression — prevents a single noisy candle triggering a regime flip.
    #
    # MOMENTUM OVERRIDE (1H): if price has moved >1.5×ATR over the last 3 closes,
    # force-exit compression even if BB/ATR haven't caught up yet.
    #
    # TRENDLINE GAP GUARD: if price is sitting >1.5×ATR above the trendline,
    # quiet BB/ATR is a bullish consolidation — ideal for grid bots, NOT a dead
    # market. Only allow COMPRESSION when price is near the trendline (gap_ratio
    # < 1.5) so the engine can't misread a tight Saturday range far above support
    # as a dead market and stop inner+mid unnecessarily.
    bb_threshold = df.bb_width.quantile(0.1)
    atr_mean = df.atr.mean()

    price_change_3h = abs(df.close.iloc[-1] - df.close.iloc[-4]) if len(df) >= 4 else 0
    momentum_exit = (atr > 0) and (price_change_3h > atr * 1.5)

    gap_ratio_now = (price - trendline) / atr if atr > 0 else 0
    above_trendline = gap_ratio_now > 1.5   # comfortable bullish range above support

    last_3_bb = df.bb_width.iloc[-3:]
    compression_confirmed = (
        (last_3_bb < bb_threshold).all() and atr < atr_mean
        and not momentum_exit
        and not above_trendline
    )

    if compression_confirmed:
        return "COMPRESSION"


    # TREND_UP: price well above trendline with expanding ATR and BB width
    bb_expanding = bb_width > df.bb_width.quantile(0.6)
    atr_expanding = atr > atr_mean

    if price > trendline * 1.03 and bb_expanding and atr_expanding:
        return "TREND_UP"

    return "RANGE"


def trend_strength(price, trendline, atr):
    """
    Returns a gap_ratio (price - trendline) / atr and derived trend flags.

    trending_up:   ENTRY when gap_ratio > 5.5 (price running hard above trendline).
                   EXIT  when gap_ratio < 4.5 (1.0× ATR hysteresis band).
                   Raised from 3.0 (Mar 16): 3x suppressed inner all morning during
                   consolidation at gap_ratio 4.8–5.8. 5.5x only parks inner on a
                   genuine acceleration.
                   Hysteresis added (Mar 21): prevents chop when gap_ratio sits on
                   the 5.5 threshold — inner was toggling ON/OFF every cycle in a
                   tight range where the trendline was far below price. State
                   persisted in regime_state.json.

    trending_down: price < trendline - 2.0×ATR — meaningful downside pressure.
                   Raised from 1.5× (Mar 18): -1.5× fired too aggressively on
                   normal pullbacks when the trendline was slightly optimistic.
                   The TREND_DOWN hysteresis (2-cycle, ATR×0.15) already guards
                   real downtrends; -2.0× avoids shutting inner off unnecessarily.

    Design principle: bots stay ON unless there is strong, confirmed evidence
    they are fighting the market. A false positive (unnecessary shutdown) costs
    more than a false negative (staying on through mild adverse move) because
    the outer bot always provides a safety net even when inner/mid are paused.
    """
    TRENDING_UP_ENTRY = 5.5   # gap_ratio threshold to enter trending_up
    TRENDING_UP_EXIT  = 4.5   # gap_ratio threshold to exit (hysteresis band)

    if atr and atr > 0:
        gap_ratio = (price - trendline) / atr
    else:
        gap_ratio = 0.0

    # Hysteresis: load current trending_up state, apply Schmitt-trigger logic
    rs = _load_regime_state()
    currently_up = rs.get("trending_up_active", False)

    if currently_up:
        # Only clear if gap_ratio has genuinely retreated (not just ticked below threshold)
        new_trending_up = gap_ratio >= TRENDING_UP_EXIT
    else:
        # Only enter on a proper break above entry threshold
        new_trending_up = gap_ratio > TRENDING_UP_ENTRY

    if new_trending_up != currently_up:
        rs["trending_up_active"] = new_trending_up
        _save_regime_state(rs)
        if new_trending_up:
            print(f"[TrendStrength] trending_up ON  — gap_ratio={gap_ratio:.2f}×ATR (entry>{TRENDING_UP_ENTRY})")
        else:
            print(f"[TrendStrength] trending_up OFF — gap_ratio={gap_ratio:.2f}×ATR (exit<{TRENDING_UP_EXIT})")

    return {
        "gap_ratio":     round(gap_ratio, 3),
        "trending_up":   new_trending_up,
        "trending_down": bool(gap_ratio < -2.0),
    }


def compression_exit_fast(df_5m, atr_1h):
    """Fast compression-exit check using 5m candles.

    Called every engine cycle when the 1H regime is COMPRESSION.
    Returns True if the 5m data shows momentum strong enough to
    justify exiting compression immediately — without waiting for the
    1H BB/ATR to catch up (which takes 1-3 hours).

    Triggers on any of:
      (a) Single large-candle body > 0.4× 1H ATR — fires within one engine cycle
      (b) Volume spike: last 5m candle volume > 2.5× rolling 20-period mean
      (c-1) ATR expansion: 5m ATR > 1.3× its own rolling mean (was 1.5×)
      (c-2) Price run: abs move over last 6 5m-candles > 0.35× 1H ATR (was 0.5×)

    (a) is highest priority — a single decisive candle exits compression in <5 min.
    (b) catches high-conviction moves on thin-candle structure.
    (c) lowered thresholds ensure the rolling checks don't lag behind the move.
    """
    if df_5m is None or len(df_5m) < 14:
        return False

    import ta as _ta

    # (a) Single large-candle body trigger
    if atr_1h and atr_1h > 0:
        candle_body = abs(df_5m["close"].iloc[-1] - df_5m["open"].iloc[-1])
        if candle_body > atr_1h * 0.4:
            return True

    # (b) Volume spike trigger
    if len(df_5m) >= 20:
        vol_mean = df_5m["volume"].rolling(20).mean().iloc[-1]
        if vol_mean > 0 and df_5m["volume"].iloc[-1] > vol_mean * 2.5:
            return True

    atr_series = _ta.volatility.average_true_range(
        df_5m["high"], df_5m["low"], df_5m["close"], window=14
    )
    atr_5m = atr_series.iloc[-1]
    atr_5m_mean = atr_series.mean()

    # (c-1) Short-term ATR expansion (lowered from 1.5× to 1.3×)
    if atr_5m_mean > 0 and atr_5m > atr_5m_mean * 1.3:
        return True

    # (c-2) Significant price run in last 30 minutes (lowered from 0.5× to 0.35×)
    if len(df_5m) >= 6 and atr_1h and atr_1h > 0:
        move_30m = abs(df_5m["close"].iloc[-1] - df_5m["close"].iloc[-6])
        if move_30m > atr_1h * 0.35:
            return True

    return False