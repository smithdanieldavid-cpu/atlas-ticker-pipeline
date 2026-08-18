#!/usr/bin/env python3
"""
Measure payload size across four encodings of the same instrument data.

Run this against the real database to get actual byte counts before
committing to an encoding:

  python3 scripts/compare_encodings.py --input ~/free-ticker-database/data/tickers.db

Encodings compared (all drop `id` and `isin`, all enum-code categoricals):
  1. objects   - array of objects            (current shape, baseline)
  2. rows      - array of arrays             + shared field list
  3. columnar  - struct of arrays            (one array per field)
  4. delimited - newline/pipe delimited text (no JSON per row)

Writes each to /tmp so you can eyeball them, and prints a size table.
"""

import sqlite3
import json
import gzip
import argparse
import sys
from pathlib import Path


EXCHANGES_BY_REGION = {
    'US': ['NASDAQ', 'NYSE', 'NYSE ARCA', 'NYSE MKT', 'BATS'],
    'AU': ['ASX'],
    'ASIA': ['TSE', 'HKEX', 'SGX', 'KRX', 'KOSDAQ', 'HOSE', 'HNX', 'UPCOM'],
    'CANADA': ['TSX', 'TSXV', 'NEO'],
}
TARGET_EXCHANGES = sorted({e for v in EXCHANGES_BY_REGION.values() for e in v})

ASSET_CLASS_BY_EXCHANGE = {
    'NASDAQ': 'us_equities', 'NYSE': 'us_equities', 'NYSE ARCA': 'us_equities',
    'NYSE MKT': 'us_equities', 'BATS': 'us_equities',
    'ASX': 'au_equities',
}

# Order matters: this is the field order for rows/columnar/delimited
FIELDS = ['ticker', 'name', 'exchange', 'assetType', 'assetClass',
          'sector', 'category', 'country']

CATEGORICAL = ['exchange', 'assetType', 'assetClass', 'sector', 'category', 'country']


def load(input_db):
    conn = sqlite3.connect(input_db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    placeholders = ','.join('?' for _ in TARGET_EXCHANGES)
    cur.execute(f"""
        SELECT ticker, name, exchange, asset_type,
               stock_sector, etf_category, country
        FROM listings
        WHERE exchange IN ({placeholders})
        ORDER BY exchange, ticker
    """, TARGET_EXCHANGES)
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        ticker = (r['ticker'] or '').strip()
        exchange = (r['exchange'] or '').strip()
        if not ticker or not exchange:
            continue
        at = (r['asset_type'] or '').strip()
        out.append({
            'ticker': ticker,
            'name': (r['name'] or '').strip(),
            'exchange': exchange,
            'assetType': 'ETF' if at == 'ETF' else 'Equity' if at == 'Stock' else (at or 'Other'),
            'assetClass': ASSET_CLASS_BY_EXCHANGE.get(exchange, 'global_equities'),
            'sector': (r['stock_sector'] or '').strip(),
            'category': (r['etf_category'] or '').strip(),
            'country': (r['country'] or '').strip(),
        })
    return out


def build_legend(instruments):
    """Map each categorical field's distinct values to integer codes."""
    legend, lookup = {}, {}
    for field in CATEGORICAL:
        values = sorted({i[field] for i in instruments})
        legend[field] = values
        lookup[field] = {v: idx for idx, v in enumerate(values)}
    return legend, lookup


def encode(instruments, legend, lookup, meta):
    """Return {name: serialized_bytes} for each candidate encoding."""
    out = {}

    # 1. objects - baseline, enum-coded
    objs = []
    for i in instruments:
        o = {'ticker': i['ticker'], 'name': i['name']}
        for f in CATEGORICAL:
            o[f] = lookup[f][i[f]]
        objs.append(o)
    out['objects'] = json.dumps(
        {**meta, 'legend': legend, 'instruments': objs},
        separators=(',', ':')).encode()

    # 2. rows - array of arrays
    rows = [
        [i['ticker'], i['name']] + [lookup[f][i[f]] for f in CATEGORICAL]
        for i in instruments
    ]
    out['rows'] = json.dumps(
        {**meta, 'legend': legend, 'fields': FIELDS, 'rows': rows},
        separators=(',', ':')).encode()

    # 3. columnar - struct of arrays
    columns = {
        'ticker': [i['ticker'] for i in instruments],
        'name': [i['name'] for i in instruments],
    }
    for f in CATEGORICAL:
        columns[f] = [lookup[f][i[f]] for i in instruments]
    out['columnar'] = json.dumps(
        {**meta, 'legend': legend, 'fields': FIELDS, 'columns': columns},
        separators=(',', ':')).encode()

    # 4. delimited - header JSON then one pipe-delimited line per instrument
    header = json.dumps({**meta, 'legend': legend, 'fields': FIELDS},
                        separators=(',', ':'))
    lines = [header]
    for i in instruments:
        # names contain commas but not pipes; strip any that appear
        name = i['name'].replace('|', ' ')
        codes = '|'.join(str(lookup[f][i[f]]) for f in CATEGORICAL)
        lines.append(f"{i['ticker']}|{name}|{codes}")
    out['delimited'] = '\n'.join(lines).encode()

    return out


def gz(data):
    return len(gzip.compress(data, compresslevel=9))


def field_cost(instruments, legend, lookup, meta):
    """Gzipped cost of each field in isolation, columnar-encoded."""
    costs = {}
    for f in FIELDS:
        if f in CATEGORICAL:
            col = [lookup[f][i[f]] for i in instruments]
        else:
            col = [i[f] for i in instruments]
        costs[f] = gz(json.dumps(col, separators=(',', ':')).encode())
    return costs


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True, help='path to tickers.db')
    p.add_argument('--outdir', default='/tmp/atlas-encodings')
    a = p.parse_args()

    if not Path(a.input).exists():
        print(f"ERROR: {a.input} not found", file=sys.stderr)
        sys.exit(1)

    instruments = load(a.input)
    print(f"Loaded {len(instruments):,} instruments\n")

    legend, lookup = build_legend(instruments)
    meta = {'version': '3.0', 'count': len(instruments)}

    print("Legend sizes (distinct values per categorical field):")
    for f in CATEGORICAL:
        print(f"  {f:12s} {len(legend[f]):>4d}")
    print()

    encodings = encode(instruments, legend, lookup, meta)

    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"{'encoding':<12} {'raw':>12} {'gzipped':>12} {'vs baseline':>12}")
    print("-" * 52)

    baseline_gz = None
    for name in ['objects', 'rows', 'columnar', 'delimited']:
        data = encodings[name]
        g = gz(data)
        if baseline_gz is None:
            baseline_gz = g
            delta = 'baseline'
        else:
            delta = f"{100 * g / baseline_gz - 100:+.1f}%"

        ext = 'txt' if name == 'delimited' else 'json'
        path = outdir / f"{name}.{ext}"
        path.write_bytes(data)
        (outdir / f"{name}.{ext}.gz").write_bytes(gzip.compress(data, compresslevel=9))

        print(f"{name:<12} {len(data):>11,}B {g:>11,}B {delta:>12}")

    print()
    print("Per-field gzipped cost (columnar, in isolation):")
    costs = field_cost(instruments, legend, lookup, meta)
    total = sum(costs.values())
    for f, c in sorted(costs.items(), key=lambda kv: -kv[1]):
        print(f"  {f:12s} {c:>9,}B  {100*c/total:>5.1f}%")

    print()
    print(f"Files written to {outdir}/")


if __name__ == '__main__':
    main()
