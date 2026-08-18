#!/usr/bin/env python3
"""
Transform free-ticker-database SQLite -> columnar JSON for Atlas ticker search.

Reads the `listings` table (venue-level, one row per listing_key) rather than
`tickers` (one globally-unique row per symbol). This matters for cross-listings:
BHP exists as both ASX::BHP (ISIN AU000000BHP4) and NYSE::BHP (US0886061086),
and an ASX holder must be offered the ASX line.

Emits TWO files:

  instruments.json      - columnar, enum-coded, aliases deduped. This is what
                          ships to the browser. ~369KB gzipped.
  instruments-full.json - row-oriented, every field including ISIN. Not shipped;
                          kept as the pipeline's own reference output so we
                          don't lose fidelity with upstream and can add fields
                          later without re-deriving them.

Encoding choices were measured, not guessed (see scripts/compare_encodings.py
and scripts/price_aliases.py):
  - columnar beat row-arrays and delimited text by 12-17%
  - name+ticker are 93% of the payload; the six categorical columns cost ~24KB
  - alias pooling LOST to plain deduped aliases, twice. Don't reintroduce it.

Usage:
  python3 scripts/transform_tickers.py \
    --input /path/to/free-ticker-database/data/tickers.db \
    --output instruments.json
"""

import sqlite3
import json
import gzip
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path


# Exchange codes exactly as they appear upstream. Verified against the dataset -
# do not guess these. JPX and TMX do NOT exist here: Japan is TSE, Canada is
# TSX/TSXV/NEO. NYSE ARCA matters because most US ETFs list there.
EXCHANGES_BY_REGION = {
    'US': ['NASDAQ', 'NYSE', 'NYSE ARCA', 'NYSE MKT', 'BATS'],
    'AU': ['ASX'],
    'ASIA': ['TSE', 'HKEX', 'SGX', 'KRX', 'KOSDAQ', 'HOSE', 'HNX', 'UPCOM'],
    'CANADA': ['TSX', 'TSXV', 'NEO'],
}
TARGET_EXCHANGES = sorted({e for v in EXCHANGES_BY_REGION.values() for e in v})

REGION_BY_EXCHANGE = {
    e: region for region, exchanges in EXCHANGES_BY_REGION.items() for e in exchanges
}

# Exchange -> Atlas asset_class, for prefilling the holdings dropdown.
# Keyed on exchange, NOT country: upstream `country` is the issuer's domicile,
# so NYSE::BHP reports "Australia" and would mis-prefill as au_equities.
ASSET_CLASS_BY_EXCHANGE = {
    'NASDAQ': 'us_equities',
    'NYSE': 'us_equities',
    'NYSE ARCA': 'us_equities',
    'NYSE MKT': 'us_equities',
    'BATS': 'us_equities',
    'ASX': 'au_equities',
}

# Fields stored as integer codes against a legend, rather than repeated strings
CATEGORICAL = ['exchange', 'assetType', 'assetClass', 'sector', 'category', 'country']


def parse_aliases(raw):
    """The aliases column is pipe-delimited, already lowercased upstream."""
    if not raw:
        return []
    return [a.strip() for a in raw.split('|') if a.strip()]


def alias_adds_value(alias, ticker, name):
    """
    Keep only aliases that a ticker or name search could not already find.

    IMPORTANT: this filter assumes the client matcher does substring AND token
    matching against `name`. Measured on the real dataset, it drops ~90% of
    aliases (28,158 -> 2,635) because most are just the registered name with
    corporate suffixes removed ("ping an bank" for "Ping An Bank Co Ltd").

    If the client search is ever narrowed to prefix-only matching, these
    aliases become load-bearing and this filter must be relaxed, or recall
    will silently degrade.
    """
    a = alias.strip().lower()
    if not a:
        return False
    if a == ticker.lower():
        return False
    if a in name.lower():
        return False
    # token containment: every alias word already present in the name
    name_tokens = set(name.lower().replace('-', ' ').replace('.', ' ').split())
    alias_tokens = a.replace('-', ' ').replace('.', ' ').split()
    if alias_tokens and all(t in name_tokens for t in alias_tokens):
        return False
    return True


def load_listings(input_db):
    conn = sqlite3.connect(input_db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    placeholders = ','.join('?' for _ in TARGET_EXCHANGES)
    cur.execute(f"""
        SELECT listing_key, ticker, exchange, name, asset_type,
               stock_sector, etf_category, country, country_code,
               isin, aliases
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

        name = (r['name'] or '').strip()
        asset_type = (r['asset_type'] or '').strip()

        kept = sorted({
            a.lower() for a in parse_aliases(r['aliases'])
            if alias_adds_value(a, ticker, name)
        })

        out.append({
            'listingKey': (r['listing_key'] or f'{exchange}::{ticker}').strip(),
            'ticker': ticker,
            'name': name,
            'exchange': exchange,
            'assetType': (
                'ETF' if asset_type == 'ETF'
                else 'Equity' if asset_type == 'Stock'
                else (asset_type or 'Other')
            ),
            'assetClass': ASSET_CLASS_BY_EXCHANGE.get(exchange, 'global_equities'),
            'region': REGION_BY_EXCHANGE.get(exchange, 'OTHER'),
            'sector': (r['stock_sector'] or '').strip(),
            'category': (r['etf_category'] or '').strip(),
            'country': (r['country'] or '').strip(),
            'countryCode': (r['country_code'] or '').strip(),
            'isin': (r['isin'] or '').strip(),
            'aliases': kept,
        })
    return out


def build_columnar(instruments):
    """
    Columnar layout: one array per field, all the same length and index-aligned.

    Chosen because gzip compresses adjacent similar values far better than
    interleaved ones, and because search scans a single contiguous column
    (name) without touching the rest.

    `id` is not stored: reconstruct client-side as exchange + '::' + ticker.
    `isin` is not stored: kept in the full reference file only.
    """
    legend, lookup = {}, {}
    for field in CATEGORICAL:
        values = sorted({i[field] for i in instruments})
        legend[field] = values
        lookup[field] = {v: idx for idx, v in enumerate(values)}

    columns = {
        'ticker': [i['ticker'] for i in instruments],
        'name': [i['name'] for i in instruments],
    }
    for field in CATEGORICAL:
        columns[field] = [lookup[field][i[field]] for i in instruments]

    # Sparse: most listings have no alias, so store index -> list rather than
    # a full-length array of empties. Measured cheaper than a dense column.
    columns['aliases'] = {
        str(idx): i['aliases']
        for idx, i in enumerate(instruments) if i['aliases']
    }

    return legend, columns


def write(path, payload):
    with open(path, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    raw = Path(path).stat().st_size

    gz_path = str(path).replace('.json', '.json.gz')
    with open(path, 'rb') as fin, gzip.open(gz_path, 'wb', compresslevel=9) as fout:
        fout.write(fin.read())
    gz = Path(gz_path).stat().st_size

    return raw, gz


def transform(input_db, output_json):
    print(f"Opening {input_db}")
    instruments = load_listings(input_db)
    total = len(instruments)
    if not total:
        raise RuntimeError(
            "No instruments matched the target exchanges - check exchange codes")

    print(f"Loaded {total:,} venue-level listings across "
          f"{len(TARGET_EXCHANGES)} exchanges")

    legend, columns = build_columnar(instruments)

    isin_count = sum(1 for i in instruments if i['isin'])
    alias_count = sum(len(i['aliases']) for i in instruments)
    exchange_counts = {}
    for i in instruments:
        exchange_counts[i['exchange']] = exchange_counts.get(i['exchange'], 0) + 1

    meta = {
        'version': '3.0',
        'encoding': 'columnar',
        'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'source': 'free-ticker-database (adanos-software, MIT)',
        'source_url': 'https://github.com/adanos-software/free-ticker-database',
        'source_table': 'listings',
        'count': total,
        'isin_coverage': isin_count,
        'isin_coverage_pct': round(isin_count / total * 100, 1),
        'alias_count': alias_count,
        'exchanges': dict(sorted(exchange_counts.items())),
        'regions': {
            region: sum(exchange_counts.get(e, 0) for e in exchanges)
            for region, exchanges in EXCHANGES_BY_REGION.items()
        },
    }

    # --- shipped payload ---
    search_payload = {**meta, 'legend': legend, 'columns': columns}
    raw, gz = write(output_json, search_payload)
    print(f"\nSearch payload -> {output_json}")
    print(f"  raw:     {raw:>10,}B")
    print(f"  gzipped: {gz:>10,}B")

    # --- full reference, not shipped ---
    full_path = str(output_json).replace('.json', '-full.json')
    full_payload = {**meta, 'encoding': 'rows', 'instruments': instruments}
    fraw, fgz = write(full_path, full_payload)
    print(f"\nFull reference -> {full_path}")
    print(f"  raw:     {fraw:>10,}B")
    print(f"  gzipped: {fgz:>10,}B")

    print()
    print(f"Instruments:   {total:,}")
    print(f"ISIN coverage: {meta['isin_coverage_pct']}%  (reference file only)")
    print(f"Aliases kept:  {alias_count:,} across "
          f"{len(columns['aliases']):,} listings")
    print(f"Regions:       " + ', '.join(
        f"{k} {v:,}" for k, v in meta['regions'].items()))
    print(f"Exchanges:     {', '.join(meta['exchanges'])}")

    return meta


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    p.add_argument('--output', default='instruments.json')
    a = p.parse_args()

    if not Path(a.input).exists():
        print(f"ERROR: {a.input} not found", file=sys.stderr)
        sys.exit(1)

    try:
        transform(a.input, a.output)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)