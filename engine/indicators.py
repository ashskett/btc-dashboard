import ta

def add_indicators(df):

    df["atr"] = ta.volatility.average_true_range(
        df["high"],
        df["low"],
        df["close"],
        window=14
    )

    bb = ta.volatility.BollingerBands(df["close"])

    df["bb_width"] = bb.bollinger_wband()

    return df