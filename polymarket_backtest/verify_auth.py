"""Explore /data/trades endpoint parameters."""
import base64, hashlib, hmac, os, time, requests, json
from dotenv import load_dotenv

load_dotenv(override=True)

ADDRESS    = os.getenv("POLY_ADDRESS", "")
API_KEY    = os.getenv("POLY_API_KEY", "")
SECRET     = os.getenv("POLY_SECRET", "")
PASSPHRASE = os.getenv("POLY_PASSPHRASE", "")

CLOB = "https://clob.polymarket.com"

# Venezuela market
CONDITION_ID = "0x62f31557b0e55475789b57a94ac385ee438ef9f800117fd1b823a0797b1fdd68"
# Venezuela YES token (from gamma API earlier)
YES_TOKEN = "28510305071001528588232263061858620884071686412926518442255373887747921822222"


def auth_headers(method, path):
    ts = str(int(time.time()))
    key = base64.urlsafe_b64decode(SECRET + "=" * (-len(SECRET) % 4))
    raw = hmac.new(key, (ts + method + path).encode(), hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(raw).decode()
    return {
        "POLY_ADDRESS":    ADDRESS,
        "POLY_SIGNATURE":  sig,
        "POLY_TIMESTAMP":  ts,
        "POLY_API_KEY":    API_KEY,
        "POLY_PASSPHRASE": PASSPHRASE,
    }


def get(path, params=None):
    r = requests.get(CLOB + path, params=params, headers=auth_headers("GET", path), timeout=15)
    if r.status_code == 200:
        d = r.json()
        return f"[200] count={d.get('count')} cursor={d.get('next_cursor')} first={json.dumps(d.get('data', [{}])[0])[:120] if d.get('data') else 'none'}"
    return f"[{r.status_code}] {r.text[:150]}"


print("No filter (first page):")
print(" ", get("/data/trades", {"limit": 3}))

print("market=condition_id:")
print(" ", get("/data/trades", {"market": CONDITION_ID, "limit": 3}))

print("token_id=YES_TOKEN:")
print(" ", get("/data/trades", {"token_id": YES_TOKEN, "limit": 3}))

print("asset_id=YES_TOKEN:")
print(" ", get("/data/trades", {"asset_id": YES_TOKEN, "limit": 3}))

print("maker_address=ADDRESS:")
print(" ", get("/data/trades", {"maker_address": ADDRESS, "limit": 3}))
