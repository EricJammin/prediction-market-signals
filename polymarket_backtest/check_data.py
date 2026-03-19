import os, json, datetime

data_dir = 'data/raw_trades'
for f in sorted(os.listdir(data_dir)):
    if not f.endswith('.json'):
        continue
    path = os.path.join(data_dir, f)
    with open(path) as fp:
        trades = json.load(fp)
    if not trades:
        print(f'{f}: EMPTY')
        continue
    # Try to find timestamp field
    keys = list(trades[0].keys())
    ts_field = None
    for field in ['timestamp', 'created_at', 'time', 'matched_time', 'match_time']:
        if field in keys:
            ts_field = field
            break
    if ts_field:
        def to_str(v):
            if isinstance(v, (int, float)):
                return datetime.datetime.utcfromtimestamp(v).strftime('%Y-%m-%d')
            return str(v)[:10]
        ts_vals = [t[ts_field] for t in trades if ts_field in t]
        ts_strs = [to_str(v) for v in ts_vals]
        print(f'{f}: {len(trades)} trades | {min(ts_strs)} to {max(ts_strs)}')
    else:
        print(f'{f}: {len(trades)} trades | keys: {keys[:6]}')
