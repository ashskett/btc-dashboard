import ccxt
import pandas as pd
import time

exchange = ccxt.coinbase()

def get_btc_data(retries=3, delay=5):
    for attempt in range(retries):
        try:
            candles = exchange.fetch_ohlcv('BTC/USDC', timeframe='1h', limit=200)
            df = pd.DataFrame(candles, columns=["time","open","high","low","close","volume"])
            return df
        except Exception as e:
            if attempt < retries - 1:
                print(f"Warning: Coinbase fetch failed (attempt {attempt+1}/{retries}): {e} — retrying in {delay}s")
                time.sleep(delay)
            else:
                raise


def get_btc_data_short(timeframe='5m', limit=30, retries=3, delay=5):
    """Fetch short-timeframe candles for fast compression-exit detection.
    Default: last 30×5m = 2.5 hours of 5m data.
    """
    for attempt in range(retries):
        try:
            candles = exchange.fetch_ohlcv('BTC/USDC', timeframe=timeframe, limit=limit)
            df = pd.DataFrame(candles, columns=["time","open","high","low","close","volume"])
            return df
        except Exception as e:
            if attempt < retries - 1:
                print(f"Warning: Coinbase short-tf fetch failed (attempt {attempt+1}/{retries}): {e} — retrying in {delay}s")
                time.sleep(delay)
            else:
                raise
