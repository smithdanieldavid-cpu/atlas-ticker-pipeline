#!/usr/bin/env python3
"""
Transform free-ticker-database SQLite → normalized JSON for Atlas ticker search.

Usage:
  python3 scripts/transform_tickers.py \
    --input /path/to/free-ticker-database/data/tickers.db \
    --output instruments.json

Output: instruments.json (uncompressed for validation)
        instruments.json.gz (gzipped for S3)
"""

import sqlite3
import json
import gzip
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any


# Target markets: USA, Australia, Asia (JP/HK/SG/KR/VN), Canada
TARGET_EXCHANGES = {
    # USA
    'NASDAQ',
    'NYSE',
    
    # Australia
    'ASX',
    
    # Asia
    'JPX',      # Japan
    'HKEX',     # Hong Kong
    'SGX',      # Singapore
    'KRX',      # South Korea
    'HOSE',     # Vietnam (Hanoi)
    'HNX',      # Vietnam (Hanoi)
    'UPCOM',    # Vietnam (UpCom)
    
    # Canada
    'TMX',
}

# Critical tickers (must be present for deployment)
CRITICAL_TICKERS = {
    'NVDA',     # US
    'BHP',      # AU
    'PMGOLD',   # AU
    'VGAD',     # AU
    'VGS',      # AU
}


def transform_tickers(input_db: str, output_json: str) -> Dict[str, Any]:
    """
    Transform SQLite database → normalized JSON.
    
    Returns validation metadata.
    """
    
    print(f"📖 Opening database: {input_db}")
    conn = sqlite3.connect(input_db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Fetch tickers for target exchanges
    print(f"🔍 Filtering to {len(TARGET_EXCHANGES)} target exchanges...")
    placeholders = ','.join(['?' for _ in TARGET_EXCHANGES])
    
    cursor.execute(f"""
        SELECT 
            ticker,
            name,
            exchange,
            country,
            country_code,
            isin,
            asset_type,
            stock_sector,
            etf_category
        FROM tickers
        WHERE exchange IN ({placeholders})
        ORDER BY exchange, ticker
    """, list(TARGET_EXCHANGES))
    
    rows = cursor.fetchall()
    print(f"✓ Fetched {len(rows)} rows from target exchanges")
    
    # Transform
    instruments = []
    isin_count = 0
    exchange_counts = {}
    
    for row in rows:
        ticker = row['ticker']
        name = row['name'] or ''
        exchange = row['exchange']
        country = row['country'] or row['country_code'] or 'UNKNOWN'
        isin = row['isin'] or ''
        asset_type = row['asset_type'] or 'Unknown'
        sector = row['stock_sector'] or ''
        category = row['etf_category'] or ''
        
        # Composite key: TICKER-EXCHANGE-COUNTRY
        composite_id = f"{ticker}-{exchange}-{country}".upper()
        
        # Track exchange counts
        exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1
        
        # Track ISIN coverage
        if isin:
            isin_count += 1
        
        # Determine asset type category (for frontend filtering)
        if asset_type == 'Stock':
            asset_category = 'Equity'
        elif asset_type == 'ETF':
            asset_category = 'ETF'
        elif asset_type == 'Index':
            asset_category = 'Index'
        else:
            asset_category = 'Other'
        
        instruments.append({
            'id': composite_id,
            'ticker': ticker,
            'name': name,
            'exchange': exchange,
            'country': country,
            'isin': isin,
            'assetType': asset_category,
            'sector': sector if asset_type == 'Stock' else '',
            'category': category if asset_type == 'ETF' else '',
        })
    
    conn.close()
    
    # Build output
    output = {
        'version': '1.0',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'source': 'free-ticker-database (adanos-software)',
        'source_url': 'https://github.com/adanos-software/free-ticker-database',
        'count': len(instruments),
        'isin_coverage': isin_count,
        'isin_coverage_pct': round(isin_count / len(instruments) * 100, 1) if instruments else 0,
        'exchanges': exchange_counts,
        'instruments': instruments,
    }
    
    # Write uncompressed JSON (for validation)
    print(f"✍️  Writing {output_json}...")
    with open(output_json, 'w') as f:
        json.dump(output, f, indent=2)
    
    json_size = Path(output_json).stat().st_size
    print(f"✓ Wrote {json_size:,} bytes")
    
    # Write gzipped JSON (for S3)
    gz_path = output_json.replace('.json', '.json.gz')
    print(f"📦 Compressing to {gz_path}...")
    with open(output_json, 'rb') as f_in:
        with gzip.open(gz_path, 'wb') as f_out:
            f_out.write(f_in.read())
    
    gz_size = Path(gz_path).stat().st_size
    print(f"✓ Compressed to {gz_size:,} bytes ({100*gz_size/json_size:.1f}% of original)")
    
    return output


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Transform free-ticker-database SQLite → Atlas JSON'
    )
    parser.add_argument(
        '--input',
        required=True,
        help='Path to tickers.db (from free-ticker-database)'
    )
    parser.add_argument(
        '--output',
        default='instruments.json',
        help='Output JSON path (default: instruments.json)'
    )
    
    args = parser.parse_args()
    
    # Validate input
    if not Path(args.input).exists():
        print(f"❌ Error: {args.input} not found")
        sys.exit(1)
    
    try:
        metadata = transform_tickers(args.input, args.output)
        
        print("\n" + "="*60)
        print("✅ Transform complete")
        print("="*60)
        print(f"Total instruments: {metadata['count']:,}")
        print(f"ISIN coverage: {metadata['isin_coverage_pct']}%")
        print(f"Exchanges: {len(metadata['exchanges'])}")
        print(f"Output: {args.output}")
        print(f"Output (gzip): {args.output.replace('.json', '.json.gz')}")
        
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
