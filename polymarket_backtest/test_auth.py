"""
Auth test using the correct Polymarket Builder API signing scheme.

Builder API keys (from polymarket.com/settings?tab=builder) use:
  POLY_BUILDER_API_KEY
  POLY_BUILDER_SIGNATURE  (urlsafe_b64encode of HMAC-SHA256)
  POLY_BUILDER_TIMESTAMP
  POLY_BUILDER_PASSPHRASE
  (no POLY_ADDRESS, no POLY_NONCE)

Secret is urlsafe_b64decoded before use as HMAC key.
Signature is urlsafe_b64encoded.
"""
import base64
import hashlib
import hmac
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("POLY_API_KEY", "")
SECRET     = os.getenv("POLY_SECRET", "")
PASSPHRASE = os.getenv("POLY_PASSPHRASE", "")
ADDRESS    = os.getenv("POLY_ADDRESS", "")  # kept for reference / fallback

MARKET = "0x62f31557b0e55475789b57a94ac385ee438ef9f800117fd1b823a0797b1fdd68"
PARAMS = {"market": MARKET, "limit": 2}
CLOB = "https://clob.polymarket.com"


def try_get(label, url, headers):
    r = requests.get(url, params=PARAMS, headers=headers, timeout=10)
    status = r.status_code
    snippet = r.text[:120] if status != 200 else f"{len(r.json().get('data', []))} trades"
    print(f"  [{status}] {label}: {snippet}")
    return status == 200


def build_sig(secret_str: str, timestamp: str, method: str, path: str, body=None) -> str:
    key = base64.urlsafe_b64decode(secret_str + "=" * (-len(secret_str) % 4))
    msg = timestamp + method + path
    if body:
        msg += str(body).replace("'", '"')
    raw = hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw).decode("utf-8")


print(f"API_KEY:    {API_KEY[:8]}...")
print(f"SECRET len: {len(SECRET)}")
print(f"ADDRESS:    {ADDRESS}")
print()

ts = str(int(time.time()))

# ── Builder headers (POLY_BUILDER_* scheme) ────────────────────────────────
print("=== Builder header scheme (POLY_BUILDER_*) ===")
for endpoint, path in [
    (f"{CLOB}/trades", "/trades"),
    (f"{CLOB}/data/trades", "/data/trades"),
]:
    sig = build_sig(SECRET, ts, "GET", path)
    headers = {
        "POLY_BUILDER_API_KEY":    API_KEY,
        "POLY_BUILDER_SIGNATURE":  sig,
        "POLY_BUILDER_TIMESTAMP":  ts,
        "POLY_BUILDER_PASSPHRASE": PASSPHRASE,
    }
    if try_get(f"BUILDER headers, path={path}", endpoint, headers):
        print(f"\n  ✓ WORKING")
        import sys; sys.exit(0)

# ── Standard L2 headers (POLY_API_KEY scheme) ─────────────────────────────
print()
print("=== Standard L2 scheme (POLY_API_KEY) ===")
for path in ["/trades"]:
    sig = build_sig(SECRET, ts, "GET", path)
    headers = {
        "POLY_ADDRESS":    ADDRESS,
        "POLY_SIGNATURE":  sig,
        "POLY_TIMESTAMP":  ts,
        "POLY_API_KEY":    API_KEY,
        "POLY_PASSPHRASE": PASSPHRASE,
    }
    if try_get(f"L2 headers, POLY_API_KEY", f"{CLOB}{path}", headers):
        print(f"\n  ✓ WORKING")
        import sys; sys.exit(0)

# ── Both sets of headers combined ─────────────────────────────────────────
print()
print("=== Both L2 + Builder headers ===")
sig = build_sig(SECRET, ts, "GET", "/trades")
headers = {
    "POLY_ADDRESS":            ADDRESS,
    "POLY_SIGNATURE":          sig,
    "POLY_TIMESTAMP":          ts,
    "POLY_API_KEY":            API_KEY,
    "POLY_PASSPHRASE":         PASSPHRASE,
    "POLY_BUILDER_API_KEY":    API_KEY,
    "POLY_BUILDER_SIGNATURE":  sig,
    "POLY_BUILDER_TIMESTAMP":  ts,
    "POLY_BUILDER_PASSPHRASE": PASSPHRASE,
}
if try_get("L2 + Builder combined", f"{CLOB}/trades", headers):
    print(f"\n  ✓ WORKING")
    import sys; sys.exit(0)

# ── No-auth baseline ───────────────────────────────────────────────────────
print()
print("=== No-auth baseline ===")
try_get("no headers", f"{CLOB}/trades", {})
