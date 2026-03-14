import os
from dotenv import load_dotenv

load_dotenv()

# 3Commas account ID (set in .env)
ACCOUNT_ID = os.getenv("THREECOMMAS_ACCOUNT_ID", "")

# Currencies treated as quote/stable
QUOTE_CURRENCIES = {"USDT", "USDC", "USD", "BUSD", "DAI"}

# Maximum allowed inventory skew (±25%)
MAX_SKEW = 0.25
