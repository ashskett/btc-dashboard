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

def detect_regime(df, trendline):

    price = df.close.iloc[-1]
    bb_width = df.bb_width.iloc[-1]
    atr = df.atr.iloc[-1]

    # ── TREND_DOWN hysteresis ─────────────────────────────────────────────────
    # Require price to be at least ATR×0.15 below trendline (not just a tick)
    # AND for 2 consecutive cycles to filter out single-candle noise.
    # Entry 149 in the log was triggered by a $5 dip — this prevents that.
    min_gap   = atr * 0.15
    rs        = _load_regime_state()
    if price < trendline - min_gap:
        rs["below_tl_count"] = rs.get("below_tl_count", 0) + 1
    else:
        rs["below_tl_count"] = 0
    _save_regime_state(rs)

    if rs["below_tl_count"] >= 2:
        return "TREND_DOWN"
    # ─────────────────────────────────────────────────────────────────────────

    # Require 3 consecutive candles below the 10th percentile to confirm
    # compression — prevents a single noisy candle triggering a regime flip.
    #
    # MOMENTUM OVERRIDE (1H): if price has moved >1.5×ATR over the last 3 closes,
    # force-exit compression even if BB/ATR haven't caught up yet.
    bb_threshold = df.bb_width.quantile(0.1)
    atr_mean = df.atr.mean()

    price_change_3h = abs(df.close.iloc[-1] - df.close.iloc[-4]) if len(df) >= 4 else 0
    momentum_exit = (atr > 0) and (price_change_3h > atr * 1.5)

    last_3_bb = df.bb_width.iloc[-3:]
    compression_confirmed = (
        (last_3_bb < bb_threshold).all() and atr < atr_mean and not momentum_exit
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

    trending_up:   price > trendline + 3.0×ATR — price running away upward.
                   3x threshold never fired in 10hrs of data, so it only catches
                   genuine breakout moves, not normal ranging drift.

    trending_down: price < trendline - 1.5×ATR — meaningful downside pressure.
                   1.5x is tight enough to catch real dumps but the hysteresis
                   in detect_regime already filters single-candle noise, so these
                   two layers work together rather than duplicating each other.

    Design principle: bots stay ON unless there is strong, confirmed evidence
    they are fighting the market. A false positive (unnecessary shutdown) costs
    more than a false negative (staying on through mild adverse move) because
    the outer bot always provides a safety net even when inner/mid are paused.
    """
    if atr and atr > 0:
        gap_ratio = (price - trendline) / atr
    else:
        gap_ratio = 0.0

    return {
        "gap_ratio":     round(gap_ratio, 3),
        "trending_up":   bool(gap_ratio >  3.0),
        "trending_down": bool(gap_ratio < -1.5),
    }


def compression_exit_fast(df_5m, atr_1h):
    """Fast compression-exit check using 5m candles.

    Called every engine cycle when the 1H regime is COMPRESSION.
    Returns True if the 5m data shows momentum strong enough to
    justify exiting compression immediately — without waiting for the
    1H BB/ATR to catch up (which takes 1-3 hours).

    Triggers on either:
      (a) ATR expansion: 5m ATR > 1.5× its own rolling mean
          (volatility picked up on short timeframe)
      (b) Price run: abs move over last 6 5m-candles (30 min) > 0.5× 1H ATR
          (price already travelled half a normal 1H range in under 30 min)

    Both thresholds are deliberately conservative so we don't false-exit
    on normal noise, only genuine directional moves.
    """
    if df_5m is None or len(df_5m) < 14:
        return False

    import ta as _ta
    atr_5m = _ta.volatility.average_true_range(
        df_5m["high"], df_5m["low"], df_5m["close"], window=14
    ).iloc[-1]
    atr_5m_mean = _ta.volatility.average_true_range(
        df_5m["high"], df_5m["low"], df_5m["close"], window=14
    ).mean()

    # (a) Short-term ATR expansion
    if atr_5m_mean > 0 and atr_5m > atr_5m_mean * 1.5:
        return True

    # (b) Significant price run in last 30 minutes (6 × 5m candles)
    if len(df_5m) >= 6 and atr_1h and atr_1h > 0:
        move_30m = abs(df_5m["close"].iloc[-1] - df_5m["close"].iloc[-6])
        if move_30m > atr_1h * 0.5:
            return True

    return False