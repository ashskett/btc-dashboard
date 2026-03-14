# test_connection.py — raw HTTP diagnostic
import os, json, base64, requests
from urllib.parse import urlencode
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

load_dotenv()
KEY    = os.getenv("THREECOMMAS_API_KEY", "")
SECRET = os.getenv("THREECOMMAS_API_SECRET", "")
ACCT   = os.getenv("THREECOMMAS_ACCOUNT_ID", "")
BOTS   = [b.strip() for b in os.getenv("GRID_BOT_IDS", "").split(",") if b.strip()]

def load_private_key(pem_str):
    if os.path.exists(pem_str.strip()):
        pem_str = open(pem_str.strip()).read()
    return serialization.load_pem_private_key(
        pem_str.encode(), password=None, backend=default_backend()
    )

private_key = load_private_key(SECRET)

def signed(method, path, params=None, body=None):
    query_string = ("?" + urlencode(params)) if params else ""
    full_path    = path + query_string
    payload      = json.dumps(body) if body else ""
    sign_target  = (full_path + payload).encode()
    sig = base64.b64encode(
        private_key.sign(sign_target, padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    headers = {"Apikey": KEY, "Signature": sig, "Content-Type": "application/json"}
    r = requests.request(method, "https://api.3commas.io" + full_path,
                         headers=headers, data=payload, timeout=15)
    return r

print("=== RAW RESPONSE HEADERS (to find redirect or proxy clues) ===")
r = signed("GET", "/ver1/accounts")
print(f"Status:  {r.status_code}")
print(f"Headers: {dict(r.headers)}")
print(f"Body:    '{r.text[:200]}'")

print()
print("=== NO-AUTH ENDPOINT TEST ===")
# This needs zero auth — pure public endpoint
r2 = requests.get("https://api.3commas.io/ver1/time", timeout=15)
print(f"/ver1/time (no auth):  {r2.status_code}  {r2.text[:80]}")

r3 = requests.get("https://api.3commas.io/ver1/ping", timeout=15)
print(f"/ver1/ping (no auth):  {r3.status_code}  {r3.text[:80]}")

# Try the NEW 3Commas API domain
print()
print("=== TRY NEW API DOMAIN (api.3commas.io vs app) ===")
for base in ["https://api.3commas.io", "https://app.3commas.io"]:
    try:
        r4 = requests.get(f"{base}/ver1/time", timeout=10)
        print(f"  {base}/ver1/time  =>  {r4.status_code}  {r4.text[:60]}")
    except Exception as e:
        print(f"  {base}/ver1/time  =>  ERROR: {e}")

print()
print("=== ACCOUNTS WITH VERBOSE DEBUG ===")
import http.client as http_client
import logging
http_client.HTTPConnection.debuglevel = 1
logging.basicConfig()
logging.getLogger().setLevel(logging.DEBUG)
requests_log = logging.getLogger("requests.packages.urllib3")
requests_log.setLevel(logging.DEBUG)
requests_log.propagate = True

r5 = signed("GET", "/ver1/accounts")
print(f"\nFinal status: {r5.status_code}")
