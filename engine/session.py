from datetime import datetime, timezone


# Weekend = Saturday (weekday 5) or Sunday (weekday 6).
# During market hours (Mon–Fri) the standard 3-session structure applies.
# On weekends BTC still trades 24/7 but liquidity is thinner — the engine
# treats these as reduced-activity windows rather than full trading sessions.
#
# Weekend session names use the WKD_ prefix so grid_logic.py can detect them
# and apply wider grid widths / higher thresholds where needed.

def get_session() -> str:
    now     = datetime.now(timezone.utc)
    hour    = now.hour
    weekend = now.weekday() >= 5   # 5=Sat, 6=Sun

    if weekend:
        if 0 <= hour < 8:
            return "WKD_ASIA"
        elif 8 <= hour < 16:
            return "WKD_EU"
        else:
            return "WKD_US"

    # Weekday sessions
    if 0 <= hour < 7:
        return "ASIA"
    elif 7 <= hour < 13:
        return "EUROPE"
    elif 13 <= hour < 21:
        return "US"
    else:
        return "ASIA"


def is_weekend() -> bool:
    """Convenience helper — True on Saturday or Sunday UTC."""
    return datetime.now(timezone.utc).weekday() >= 5
