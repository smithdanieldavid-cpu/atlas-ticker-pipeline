#!/usr/bin/env python3
"""
Validate the columnar ticker payload before deployment.

FAIL (blocks deploy): missing critical ticker, row count too low, absent
                      region, malformed columns, bad schema
WARN (deploys anyway): low alias coverage, unexpected exchanges, minor
                       data quality issues

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
CATEGORICAL = ['exchange', 'assetType', 'assetClass', 'sector', 'category', 'country']
TEXT_COLUMNS = ['ticker', 'name']


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

    if data.get('encoding') != 'columnar':
        errors.append(f"Expected columnar encoding, got {data.get('encoding')!r}")

    columns = data.get('columns')
    legend = data.get('legend')
    if not isinstance(columns, dict) or not isinstance(legend, dict):
        print("FAIL: missing columns or legend")
        sys.exit(1)

    # --- schema ---
    missing = [c for c in TEXT_COLUMNS + CATEGORICAL if c not in columns]
    if missing:
        errors.append(f"Missing columns: {', '.join(missing)}")
    else:
        print(f"  OK   schema: all {len(TEXT_COLUMNS + CATEGORICAL)} columns present")

    # --- column alignment: every column must be the same length ---
    count = data.get('count', 0)
    lengths = {c: len(columns[c]) for c in TEXT_COLUMNS + CATEGORICAL if c in columns}
    bad = {c: n for c, n in lengths.items() if n != count}
    if bad:
        errors.append(
            f"Column length mismatch (expected {count:,}): " +
            ', '.join(f'{c}={n:,}' for c, n in bad.items()))
    else:
        print(f"  OK   alignment: all columns {count:,} long")

    # --- codes must be in range for their legend ---
    for field in CATEGORICAL:
        if field not in columns or field not in legend:
            continue
        size = len(legend[field])
        oob = [v for v in columns[field] if not isinstance(v, int) or v < 0 or v >= size]
        if oob:
            errors.append(
                f"{field}: {len(oob)} codes outside legend range 0..{size-1}")
    if not any('codes outside' in e for e in errors):
        print(f"  OK   legend codes: all in range")

    # --- row count ---
    if count < MIN_COUNT:
        errors.append(f"Row count {count:,} below minimum {MIN_COUNT:,}")
    else:
        print(f"  OK   row count: {count:,}")

    # --- critical tickers (decode exchange to check venue coverage) ---
    if 'ticker' in columns and 'exchange' in columns and 'exchange' in legend:
        ex_legend = legend['exchange']
        found = {}
        for idx, t in enumerate(columns['ticker']):
            tu = t.upper()
            if tu in CRITICAL_TICKERS:
                found.setdefault(tu, []).append(ex_legend[columns['exchange'][idx]])

        absent = [t for t in CRITICAL_TICKERS if t not in found]
        if absent:
            errors.append(f"Critical tickers absent: {', '.join(absent)}")
        else:
            print("  OK   critical tickers:")
            for t in CRITICAL_TICKERS:
                print(f"         {t} -> {', '.join(sorted(set(found[t])))}")
            # BHP must carry its ASX line, not just NYSE - this is the
            # cross-listing regression that moving to `listings` fixed
            if 'BHP' in found and 'ASX' not in found['BHP']:
                errors.append(
                    "BHP has no ASX listing - cross-listing regression, "
                    "check the transform is reading `listings` not `tickers`")

    # --- region coverage ---
    present = set(data.get('exchanges', {}))
    for region, codes in EXPECTED_REGIONS.items():
        hits = [c for c in codes if c in present]
        if not hits:
            errors.append(f"No coverage for region {region}")
        else:
            rows = sum(data['exchanges'][c] for c in hits)
            print(f"  OK   {region}: {rows:,} rows across {', '.join(hits)}")

    all_expected = {c for codes in EXPECTED_REGIONS.values() for c in codes}
    unexpected = present - all_expected
    if unexpected:
        warnings.append(f"Unexpected exchanges: {', '.join(sorted(unexpected))}")

    # --- data quality ---
    if 'name' in columns:
        blank = sum(1 for n in columns['name'] if not n.strip())
        if blank:
            ratio = blank / count * 100
            msg = f"{blank:,} instruments ({ratio:.1f}%) have no name"
            (errors if ratio > 5 else warnings).append(msg)
        else:
            print("  OK   data quality: all instruments named")

    # --- aliases (sparse dict, indices must be valid) ---
    aliases = columns.get('aliases', {})
    if isinstance(aliases, dict):
        bad_idx = [k for k in aliases if not k.isdigit() or int(k) >= count]
        if bad_idx:
            errors.append(f"{len(bad_idx)} alias keys outside row range")
        else:
            n = sum(len(v) for v in aliases.values())
            print(f"  OK   aliases: {n:,} across {len(aliases):,} listings")
            if len(aliases) < count * 0.02:
                warnings.append(
                    f"Alias coverage low ({100*len(aliases)/count:.1f}% of listings)")

    # --- report ---
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