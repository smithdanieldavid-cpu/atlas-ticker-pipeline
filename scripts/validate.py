#!/usr/bin/env python3
"""
Validate transformed ticker JSON before deployment.

FAIL (blocks deploy): missing critical ticker, row count far too low,
                      absent region, ISIN < 85%, bad schema
WARN (deploys anyway): ISIN 85-95%, minor data quality issues

Exit 0 = deploy, Exit 1 = block.
"""

import json
import sys
import argparse

CRITICAL_TICKERS = ['NVDA', 'BHP', 'PMGOLD', 'VGAD', 'VGS']

EXPECTED_REGIONS = {
    'US': ['NASDAQ', 'NYSE', 'NYSE ARCA', 'NYSE MKT', 'BATS'],
    'AU': ['ASX'],
    'ASIA': ['TSE', 'HKEX', 'SGX', 'KRX', 'KOSDAQ', 'HOSE', 'HNX', 'UPCOM'],
    'CANADA': ['TSX', 'TSXV', 'NEO'],
}

MIN_COUNT = 10000
ISIN_FAIL = 85.0
ISIN_WARN = 95.0

REQUIRED_FIELDS = ['id', 'ticker', 'name', 'exchange', 'country', 'isin', 'assetType', 'assetClass']


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    a = p.parse_args()

    errors, warnings = [], []

    try:
        with open(a.input) as f:
            data = json.load(f)
    except Exception as e:
        print(f"FAIL: cannot read {a.input}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Validating {a.input}\n")

    instruments = data.get('instruments')
    if not isinstance(instruments, list) or not instruments:
        print("FAIL: no instruments array")
        sys.exit(1)

    # Schema
    missing = [f for f in REQUIRED_FIELDS if f not in instruments[0]]
    if missing:
        errors.append(f"Schema missing fields: {', '.join(missing)}")
    else:
        print(f"  OK   schema: all {len(REQUIRED_FIELDS)} required fields present")

    # Row count
    count = data.get('count', len(instruments))
    if count < MIN_COUNT:
        errors.append(f"Row count {count:,} below minimum {MIN_COUNT:,}")
    else:
        print(f"  OK   row count: {count:,}")

    # Critical tickers
    found = {}
    for inst in instruments:
        t = inst['ticker'].upper()
        if t in CRITICAL_TICKERS:
            found.setdefault(t, []).append(inst['exchange'])

    absent = [t for t in CRITICAL_TICKERS if t not in found]
    if absent:
        errors.append(f"Critical tickers absent: {', '.join(absent)}")
    else:
        print("  OK   critical tickers:")
        for t in CRITICAL_TICKERS:
            print(f"         {t} -> {', '.join(sorted(set(found[t])))}")

    # Region coverage
    present = set(data.get('exchanges', {}))
    for region, codes in EXPECTED_REGIONS.items():
        hits = [c for c in codes if c in present]
        if not hits:
            errors.append(f"No coverage for region {region} (expected any of: {', '.join(codes)})")
        else:
            rows = sum(data['exchanges'][c] for c in hits)
            print(f"  OK   {region}: {rows:,} rows across {', '.join(hits)}")

    # Unexpected exchanges
    all_expected = {c for codes in EXPECTED_REGIONS.values() for c in codes}
    unexpected = present - all_expected
    if unexpected:
        warnings.append(f"Unexpected exchanges present: {', '.join(sorted(unexpected))}")

    # ISIN
    pct = data.get('isin_coverage_pct', 0)
    if pct < ISIN_FAIL:
        errors.append(f"ISIN coverage {pct}% below hard floor {ISIN_FAIL}%")
    elif pct < ISIN_WARN:
        warnings.append(f"ISIN coverage {pct}% below target {ISIN_WARN}%")
    else:
        print(f"  OK   ISIN coverage: {pct}%")

    # Data quality
    blank_names = sum(1 for i in instruments if not i.get('name', '').strip())
    if blank_names:
        ratio = blank_names / count * 100
        msg = f"{blank_names:,} instruments ({ratio:.1f}%) have no name"
        (errors if ratio > 5 else warnings).append(msg)
    else:
        print("  OK   data quality: all instruments named")

    # Report
    print("\n" + "=" * 60)
    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  FAIL  {e}")

    if errors:
        print(f"\nVALIDATION FAILED - {len(errors)} error(s), deployment blocked")
        print("=" * 60)
        sys.exit(1)

    if warnings:
        print(f"\nVALIDATION PASSED with {len(warnings)} warning(s) - deploying")
    else:
        print("\nVALIDATION PASSED - deploying")
    print("=" * 60)
    sys.exit(0)


if __name__ == '__main__':
    main()