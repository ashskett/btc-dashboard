from datetime import datetime, timezone


def get_session():

    hour = datetime.now(timezone.utc).hour

    if 0 <= hour < 7:
        return "ASIA"

    elif 7 <= hour < 13:
        return "EUROPE"

    elif 13 <= hour < 21:
        return "US"

    else:
        return "ASIA"