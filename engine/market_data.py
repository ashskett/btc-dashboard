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
