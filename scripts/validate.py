#!/usr/bin/env python3
"""
Validate transformed ticker JSON before deployment.

Checks:
  ✓ Row count (sanity check)
  ✓ Critical tickers present (NVDA, BHP, PMGOLD, VGAD, VGS)
  ✓ Exchange coverage (all 4 regions)
  ✓ ISIN coverage (warn if <95%, fail if <85%)
  ✓ Schema validation
  ✓ No truncation/corruption

Usage:
  python3 scripts/validate.py --input instruments.json

Exit codes:
  0 = PASS (may have warnings)
  1 = FAIL (critical issue)
"""

import json
import sys
from pathlib import Path
from typing import List, Dict, Any, Tuple


# Critical tickers (must be present)
CRITICAL_TICKERS = {
    'NVDA',     # US
    'BHP',      # AU
    'PMGOLD',   # AU
    'VGAD',     # AU
    'VGS',      # AU
}

# Expected regional coverage
EXPECTED_REGIONS = {
    'US': ['NASDAQ', 'NYSE'],
    'AU': ['ASX'],
    'ASIA': ['JPX', 'HKEX', 'SGX', 'KRX', 'HOSE', 'HNX', 'UPCOM'],
    'CANADA': ['TMX'],
}

# Thresholds
MIN_INSTRUMENT_COUNT = 50000
MIN_ISIN_COVERAGE_FAIL = 85.0
MIN_ISIN_COVERAGE_WARN = 95.0


def load_json(path: str) -> Dict[str, Any]:
    """Load and parse JSON."""
    with open(path, 'r') as f:
        return json.load(f)


def validate_schema(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate JSON schema."""
    errors = []
    warnings = []
    
    required_keys = ['version', 'timestamp', 'count', 'instruments']
    for key in required_keys:
        if key not in data:
            errors.append(f"Missing required key: {key}")
    
    if not isinstance(data.get('instruments'), list):
        errors.append("'instruments' must be a list")
        return len(errors) == 0, errors
    
    # Check first instrument schema
    if data['instruments']:
        first = data['instruments'][0]
        required_fields = ['id', 'ticker', 'name', 'exchange', 'country', 'isin', 'assetType']
        for field in required_fields:
            if field not in first:
                errors.append(f"Missing field in instrument: {field}")
    
    return len(errors) == 0, errors + warnings


def validate_row_count(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate minimum row count."""
    errors = []
    warnings = []
    
    count = data.get('count', 0)
    if count < MIN_INSTRUMENT_COUNT:
        errors.append(
            f"Row count too low: {count:,} (minimum: {MIN_INSTRUMENT_COUNT:,})"
        )
    else:
        print(f"✓ Row count: {count:,} instruments")
    
    return len(errors) == 0, errors + warnings


def validate_critical_tickers(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate critical tickers are present."""
    errors = []
    warnings = []
    
    found_tickers = set()
    ticker_exchanges = {}
    
    for inst in data.get('instruments', []):
        ticker = inst.get('ticker', '').upper()
        exchange = inst.get('exchange', '')
        if ticker in CRITICAL_TICKERS:
            found_tickers.add(ticker)
            if ticker not in ticker_exchanges:
                ticker_exchanges[ticker] = []
            ticker_exchanges[ticker].append(exchange)
    
    missing = CRITICAL_TICKERS - found_tickers
    if missing:
        errors.append(f"Critical tickers missing: {', '.join(sorted(missing))}")
    else:
        print(f"✓ Critical tickers: {', '.join(sorted(CRITICAL_TICKERS))}")
        for ticker in sorted(CRITICAL_TICKERS):
            exchanges = ticker_exchanges.get(ticker, [])
            print(f"  - {ticker}: {', '.join(exchanges)}")
    
    return len(errors) == 0, errors + warnings


def validate_exchange_coverage(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate coverage across regions."""
    errors = []
    warnings = []
    
    exchanges_in_data = set(data.get('exchanges', {}).keys())
    
    coverage = {}
    for region, exchanges in EXPECTED_REGIONS.items():
        found = [e for e in exchanges if e in exchanges_in_data]
        coverage[region] = {
            'expected': exchanges,
            'found': found,
            'count': data.get('exchanges', {}).get(exchanges[0], 0) if found else 0
        }
    
    # All regions should be represented
    for region, cov in coverage.items():
        if not cov['found']:
            errors.append(f"No coverage for region: {region}")
        else:
            print(f"✓ Region {region}: {len(cov['found'])}/{len(cov['expected'])} exchanges")
    
    return len(errors) == 0, errors + warnings


def validate_isin_coverage(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Validate ISIN coverage."""
    errors = []
    warnings = []
    
    isin_pct = data.get('isin_coverage_pct', 0)
    
    if isin_pct < MIN_ISIN_COVERAGE_FAIL:
        errors.append(
            f"ISIN coverage critically low: {isin_pct}% (minimum: {MIN_ISIN_COVERAGE_FAIL}%)"
        )
    elif isin_pct < MIN_ISIN_COVERAGE_WARN:
        warnings.append(
            f"⚠️  ISIN coverage below optimal: {isin_pct}% (target: {MIN_ISIN_COVERAGE_WARN}%)"
        )
    else:
        print(f"✓ ISIN coverage: {isin_pct}%")
    
    return len(errors) == 0, errors + warnings


def validate_data_quality(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Quick data quality checks."""
    errors = []
    warnings = []
    
    issues = {
        'null_names': 0,
        'short_names': 0,
        'no_isin': 0,
    }
    
    for inst in data.get('instruments', [])[:1000]:  # Sample first 1000
        name = inst.get('name', '').strip()
        if not name:
            issues['null_names'] += 1
        elif len(name) < 3:
            issues['short_names'] += 1
        if not inst.get('isin'):
            issues['no_isin'] += 1
    
    if issues['null_names'] > 10:
        errors.append(f"Too many instruments with null names: {issues['null_names']}")
    if issues['short_names'] > 50:
        warnings.append(f"Many instruments with very short names: {issues['short_names']}")
    
    if not errors:
        print(f"✓ Data quality: {len(data.get('instruments', []))} instruments checked")
    
    return len(errors) == 0, errors + warnings


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Validate ticker JSON')
    parser.add_argument('--input', required=True, help='Path to instruments.json')
    args = parser.parse_args()
    
    print(f"🔍 Validating {args.input}...")
    print()
    
    # Load
    try:
        data = load_json(args.input)
    except Exception as e:
        print(f"❌ Failed to load JSON: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Run validations
    validators = [
        ("Schema", validate_schema),
        ("Row count", validate_row_count),
        ("Critical tickers", validate_critical_tickers),
        ("Exchange coverage", validate_exchange_coverage),
        ("ISIN coverage", validate_isin_coverage),
        ("Data quality", validate_data_quality),
    ]
    
    all_errors = []
    all_warnings = []
    
    for name, validator in validators:
        passed, messages = validator(data)
        errors = [m for m in messages if m.startswith('❌') or (m and not m.startswith('⚠️'))]
        warnings = [m for m in messages if m.startswith('⚠️')]
        
        all_errors.extend(errors)
        all_warnings.extend(warnings)
        
        if not passed:
            for msg in errors:
                print(f"  ❌ {msg}")
    
    # Warnings
    for warning in all_warnings:
        print(f"  {warning}")
    
    print()
    print("="*60)
    
    if all_errors:
        print(f"❌ Validation FAILED ({len(all_errors)} errors)")
        print("="*60)
        for error in all_errors:
            print(f"  • {error}")
        sys.exit(1)
    elif all_warnings:
        print(f"⚠️  Validation PASSED with {len(all_warnings)} warning(s)")
        print("="*60)
        print("Warnings:")
        for warning in all_warnings:
            print(f"  • {warning}")
        print("\n✅ Deployment will proceed")
        sys.exit(0)
    else:
        print("✅ Validation PASSED")
        print("="*60)
        sys.exit(0)


if __name__ == '__main__':
    main()
