"""Explore /builder/trades endpoint now that auth is working."""
import requests, base64, hashlib, hmac, time, os, json
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv('POLY_API_KEY', '')
SECRET = os.getenv('POLY_SECRET', '')
PASSPHRASE = os.getenv('POLY_PASSPHRASE', '')

CLOB = 'https://clob.polymarket.com'
# Venezuela market condition_id
MARKET = '0x62f31557b0e55475789b57a94ac385ee438ef9f800117fd1b823a0797b1fdd68'
# Venezuela YES token id (from earlier discovery)
VENEZUELA_YES = '36250749475469589041076839935974244248049656942700423024547376176629971818495'


def build_sig(secret_str, timestamp, method, path):
    key = base64.urlsafe_b64decode(secret_str + '=' * (-len(secret_str) % 4))
    msg = timestamp + method + path
    raw = hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode('utf-8')


def get(path, params=None):
    ts = str(int(time.time()))
    hdrs = {
        'POLY_BUILDER_API_KEY': API_KEY,
        'POLY_BUILDER_SIGNATURE': build_sig(SECRET, ts, 'GET', path),
        'POLY_BUILDER_TIMESTAMP': ts,
        'POLY_BUILDER_PASSPHRASE': PASSPHRASE,
    }
    r = requests.get(CLOB + path, params=params, headers=hdrs, timeout=15)
    return r


print("=== /builder/trades with different params ===")

# 1. No params
r = get('/builder/trades')
print(f"no params: [{r.status_code}] {r.text[:200]}")

# 2. market (condition_id)
r = get('/builder/trades', {'market': MARKET, 'limit': 3})
print(f"market={MARKET[:16]}...: [{r.status_code}] {r.text[:200]}")

# 3. token_id (YES token)
r = get('/builder/trades', {'token_id': VENEZUELA_YES, 'limit': 3})
print(f"token_id=YES: [{r.status_code}] {r.text[:200]}")

# 4. Check if /data/trades has different behavior with builder headers
r = get('/data/trades', {'market': MARKET, 'limit': 3})
print(f"/data/trades market: [{r.status_code}] {r.text[:200]}")

# 5. Check what params the builder/trades endpoint accepts
# Try cursor pagination
r = get('/builder/trades', {'market': MARKET, 'limit': 5, 'cursor': ''})
print(f"with cursor: [{r.status_code}] {r.text[:300]}")
