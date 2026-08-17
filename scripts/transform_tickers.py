#!/usr/bin/env python3
"""
Transform free-ticker-database SQLite -> normalized JSON for Atlas ticker search.

Reads the `listings` table (venue-level, one row per listing_key) rather than
`tickers` (one globally-unique row per symbol). This matters for cross-listings:
BHP exists as both ASX::BHP (ISIN AU000000BHP4) and NYSE::BHP (ISIN
US0886061086), and an ASX holder must be offered the ASX line.

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
# do not guess. JPX/TMX do NOT exist here; Japan is TSE, Canada is TSX/TSXV/NEO.
EXCHANGES_BY_REGION = {
    'US': ['NASDAQ', 'NYSE', 'NYSE ARCA', 'NYSE MKT', 'BATS'],
    'AU': ['ASX'],
    'ASIA': ['TSE', 'HKEX', 'SGX', 'KRX', 'KOSDAQ', 'HOSE', 'HNX', 'UPCOM'],
    'CANADA': ['TSX', 'TSXV', 'NEO'],
}

TARGET_EXCHANGES = sorted({e for v in EXCHANGES_BY_REGION.values() for e in v})

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


def transform(input_db, output_json):
    print(f"Opening {input_db}")
    conn = sqlite3.connect(input_db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    placeholders = ','.join('?' for _ in TARGET_EXCHANGES)
    cur.execute(f"""
        SELECT listing_key, ticker, exchange, name, asset_type,
               stock_sector, etf_category, country, country_code, isin
        FROM listings
        WHERE exchange IN ({placeholders})
        ORDER BY exchange, ticker
    """, TARGET_EXCHANGES)

    rows = cur.fetchall()
    conn.close()
    print(f"Fetched {len(rows):,} venue-level listings across {len(TARGET_EXCHANGES)} exchanges")

    instruments = []
    isin_count = 0
    exchange_counts = {}
    seen_keys = set()

    for r in rows:
        listing_key = (r['listing_key'] or '').strip()
        ticker = (r['ticker'] or '').strip()
        exchange = (r['exchange'] or '').strip()

        if not listing_key or not ticker or not exchange:
            continue
        if listing_key in seen_keys:
            continue
        seen_keys.add(listing_key)

        isin = (r['isin'] or '').strip()
        asset_type = (r['asset_type'] or '').strip()

        exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1
        if isin:
            isin_count += 1

        instruments.append({
            'id': listing_key,
            'ticker': ticker,
            'name': (r['name'] or '').strip(),
            'exchange': exchange,
            'country': (r['country'] or '').strip(),
            'countryCode': (r['country_code'] or '').strip(),
            'isin': isin,
            'assetType': (
                'ETF' if asset_type == 'ETF'
                else 'Equity' if asset_type == 'Stock'
                else (asset_type or 'Other')
            ),
            'assetClass': ASSET_CLASS_BY_EXCHANGE.get(exchange, 'global_equities'),
            'sector': (r['stock_sector'] or '').strip(),
            'category': (r['etf_category'] or '').strip(),
        })

    total = len(instruments)
    if not total:
        raise RuntimeError("No instruments matched the target exchanges - check exchange codes")

    output = {
        'version': '2.0',
        'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'source': 'free-ticker-database (adanos-software, MIT)',
        'source_url': 'https://github.com/adanos-software/free-ticker-database',
        'source_table': 'listings',
        'count': total,
        'isin_coverage': isin_count,
        'isin_coverage_pct': round(isin_count / total * 100, 1),
        'exchanges': dict(sorted(exchange_counts.items())),
        'instruments': instruments,
    }

    with open(output_json, 'w') as f:
        json.dump(output, f, separators=(',', ':'))
    raw = Path(output_json).stat().st_size

    gz_path = output_json.replace('.json', '.json.gz')
    with open(output_json, 'rb') as fin, gzip.open(gz_path, 'wb', compresslevel=9) as fout:
        fout.write(fin.read())
    gz = Path(gz_path).stat().st_size

    print(f"Wrote {output_json} ({raw:,} bytes)")
    print(f"Wrote {gz_path} ({gz:,} bytes, {100*gz/raw:.1f}%)")
    print()
    print(f"Instruments:   {total:,}")
    print(f"ISIN coverage: {output['isin_coverage_pct']}%")
    print(f"Exchanges:     {', '.join(output['exchanges'])}")
    return output


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