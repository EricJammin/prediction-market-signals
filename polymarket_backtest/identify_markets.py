"""Identify all cached markets and discover Iran/Venezuela markets via tags."""
import requests, json

GAMMA = 'https://gamma-api.polymarket.com'

# Map condition_ids we have data for
condition_ids = [
    '0x1b2b69401b202d313f8909800ca7e5b1c631de6782d0b74e7cc827e1873abfb0',
    '0x1b6f76e5b8587ee896c35847e12d11e75290a8c3934c5952e8a9d6e4c6f03cfa',
    '0x2e94bb8dd09931d12e6e656fe4fe6ceb3922bc3d6eab864bb6cd24773cf67269',
    '0x41e47408f8ab39b46a9d9e3c9b15ebd62f1d795eb072ff46df3d376c09eb583e',
    '0x5cb20a760bc2bba3b87fae547a25cbac73f702abfa30e4a801e65f8b9f15d8ff',
    '0x61ce3773237a948584e422de72265f937034af418a8b703e3a860ea62e59ff36',
    '0x62f31557b0e55475789b57a94ac385ee438ef9f800117fd1b823a0797b1fdd68',
    '0x70909f0ba8256a89c301da58812ae47203df54957a07c7f8b10235e877ad63c2',
    '0xb3ebf217cf2f393a66030c072b04b893268506923e01b23f1bcf3504c3d319c2',
    '0xd5e2c76090cc15dc1e613fd61b9a2cee9b76c9097a6c313f256df55d2df5149c',
]

# Look up by slug-search approach: browse tag 102304 (Khamenei/Iran/Venezuela)
print("=== Markets with tag_id=102304 ===")
offset = 0
found = []
while True:
    r = requests.get(f'{GAMMA}/markets', params={'tag_id': 102304, 'limit': 100, 'offset': offset}, timeout=15)
    data = r.json()
    if not isinstance(data, list) or not data:
        break
    for m in data:
        cid = m.get('conditionId', '')
        q = m.get('question') or '?'
        vol = float(m.get('volume') or 0)
        end = (m.get('endDate') or '')[:10]
        slug = m.get('slug') or '?'
        tokens_raw = m.get('clobTokenIds') or '[]'
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        in_cache = cid in condition_ids
        star = ' *' if in_cache else ''
        found.append((vol, cid, slug, q, end, tokens))
        if in_cache or vol > 100000:
            print(f'  [vol={vol:>12.0f}]{star} {slug[:50]:50s} end={end}')
            print(f'           cid={cid[:20]}...')
    if len(data) < 100:
        break
    offset += 100

print()
print("=== Cached condition_ids NOT found in tag 102304 ===")
found_cids = {c for _, c, *_ in found}
for cid in condition_ids:
    if cid not in found_cids:
        print(f'  {cid[:20]}... — not in tag 102304, trying direct slug lookup')
        r = requests.get(f'{GAMMA}/markets', params={'slug': cid}, timeout=10)
        print(f'    result: {r.text[:100]}')
