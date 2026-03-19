"""
One-time script: uses MetaMask private key to do L1 EIP-712 signing,
creates standard L2 API credentials via POST /auth/api-key, and prints them.

After running this, copy the printed credentials into .env and
delete POLY_PRIVATE_KEY from .env.
"""
import os
import time
import requests
from dotenv import load_dotenv
from eth_account import Account
from eth_utils import keccak
from eth_abi import encode

load_dotenv()

PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
if not PRIVATE_KEY:
    raise SystemExit("POLY_PRIVATE_KEY not set in .env")

if not PRIVATE_KEY.startswith("0x"):
    PRIVATE_KEY = "0x" + PRIVATE_KEY

CHAIN_ID = 137  # Polygon mainnet
CLOB = "https://clob.polymarket.com"
MSG_TO_SIGN = "This message attests that I control the given wallet"

acct = Account.from_key(PRIVATE_KEY)
address = acct.address
print(f"Wallet address: {address}")

timestamp = int(time.time())
nonce = 0

# ── Manual EIP-712 encoding (mirrors poly_eip712_structs behavior) ─────────
# ClobAuth struct: address=Address(), timestamp=String(), nonce=Uint(), message=String()
# Type string uses EIP-712 canonical names: "address", "string", "uint256"

# Domain type hash
domain_type = b"EIP712Domain(string name,string version,uint256 chainId)"
domain_type_hash = keccak(domain_type)

domain_separator = keccak(encode(
    ["bytes32", "bytes32", "bytes32", "uint256"],
    [
        domain_type_hash,
        keccak(b"ClobAuthDomain"),
        keccak(b"1"),
        CHAIN_ID,
    ],
))

# ClobAuth struct type hash — address is EIP-712 'address' type, not 'string'
clob_auth_type = (
    b"ClobAuth(address address,string timestamp,uint256 nonce,string message)"
)
clob_auth_type_hash = keccak(clob_auth_type)

# For EIP-712: address fields are ABI-encoded as address (20 bytes, left-padded)
# string fields are keccak256(utf-8 bytes)
struct_hash = keccak(encode(
    ["bytes32", "address", "bytes32", "uint256", "bytes32"],
    [
        clob_auth_type_hash,
        address,
        keccak(str(timestamp).encode("utf-8")),
        nonce,
        keccak(MSG_TO_SIGN.encode("utf-8")),
    ],
))

message_hash = keccak(b"\x19\x01" + domain_separator + struct_hash)

signed = Account._sign_hash(message_hash, PRIVATE_KEY)
signature = "0x" + signed.signature.hex()

headers = {
    "POLY_ADDRESS":   address,
    "POLY_SIGNATURE": signature,
    "POLY_TIMESTAMP": str(timestamp),
    "POLY_NONCE":     str(nonce),
    "Content-Type":   "application/json",
}

print(f"Requesting L2 credentials from {CLOB}/auth/api-key ...")
r = requests.post(f"{CLOB}/auth/api-key", headers=headers, json={}, timeout=15)
print(f"  POST /auth/api-key: [{r.status_code}] {r.text[:200]}")

if r.status_code != 200:
    # Try derive instead (for wallets that already have credentials)
    print(f"Trying GET /auth/derive-api-key ...")
    r = requests.get(f"{CLOB}/auth/derive-api-key", headers=headers, timeout=15)
    print(f"  GET /auth/derive-api-key: [{r.status_code}] {r.text[:200]}")

if r.status_code != 200:
    print(f"Both endpoints failed. Wallet {address} may not have a Polymarket account.")
    raise SystemExit(1)

creds = r.json()
api_key    = creds.get("apiKey", "")
secret     = creds.get("secret", "")
passphrase = creds.get("passphrase", "")

print()
print("=" * 60)
print("SUCCESS — add these to your .env:")
print("=" * 60)
print(f"POLY_ADDRESS={address}")
print(f"POLY_API_KEY={api_key}")
print(f"POLY_SECRET={secret}")
print(f"POLY_PASSPHRASE={passphrase}")
print("=" * 60)
print()
print("Then DELETE 'POLY_PRIVATE_KEY' from your .env.")
