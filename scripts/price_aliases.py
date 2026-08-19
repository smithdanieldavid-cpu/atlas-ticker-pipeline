#!/usr/bin/env python3
"""
Price the cost of shipping aliases in the Atlas ticker search payload.

The upstream dataset carries ~125k aliases. They are the biggest available
win on match quality ("commonwealth" -> CBA), but they are high-entropy text
like company names, so they may be expensive. This measures the real cost of
several inclusion strategies before we commit.

  python3 scripts/price_aliases.py --input ~/free-ticker-database/data/tickers.db

Strategies measured:
  0. baseline      - no aliases at all
  1. all           - every alias, as-is
  2. deduped       - drop aliases that add no match value (already findable
                     via the ticker or a substring of the name)
  3. deduped+lower - as above, lowercased and de-duplicated per listing
  4. pooled        - deduped+lower, but aliases stored once in a shared pool
                     with per-listing integer references

The script first INSPECTS how aliases are stored, since that varies, and
prints what it finds before measuring.
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


def gz(obj):
    if isinstance(obj, (bytes, bytearray)):
        data = obj
    else:
        data = json.dumps(obj, separators=(',', ':')).encode()
    return len(gzip.compress(data, compresslevel=9))


def inspect(cur):
    """Report how aliases are stored, since the format is not documented."""
    print("=" * 64)
    print("INSPECTION")
    print("=" * 64)

    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"Tables: {', '.join(tables)}\n")

    for t in ('aliases', 'core_aliases'):
        if t in tables:
            schema = cur.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (t,)).fetchone()
            n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"--- table `{t}` ({n:,} rows)")
            print(schema[0])
            sample = cur.execute(f"SELECT * FROM {t} LIMIT 3").fetchall()
            cols = [d[0] for d in cur.description]
            print(f"    columns: {cols}")
            for s in sample:
                print(f"    {tuple(s)}")
            print()

    # listings.aliases column format
    rows = cur.execute("""
        SELECT ticker, exchange, name, aliases FROM listings
        WHERE aliases IS NOT NULL AND aliases != '' LIMIT 5
    """).fetchall()
    print("--- listings.aliases column, non-empty samples")
    if not rows:
        print("    (none populated - aliases live only in the alias tables)\n")
    else:
        for t, e, n, a in rows:
            print(f"    {e}:{t}  name={n!r}")
            print(f"        aliases={a!r}")
        print()

    return tables


def split_aliases(raw):
    """Best-effort parse of the aliases column, whatever format it uses."""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith('['):
        try:
            return [str(x).strip() for x in json.loads(raw) if str(x).strip()]
        except Exception:
            pass
    for delim in ('|', ';', '\t', '~'):
        if delim in raw:
            return [p.strip() for p in raw.split(delim) if p.strip()]
    return [raw]


def adds_value(alias, ticker, name):
    """
    True if this alias could surface a result that ticker/name matching
    would miss. Drops aliases already reachable via existing fields.
    """
    a = alias.strip().lower()
    if not a:
        return False
    if a == ticker.lower():
        return False
    if a in name.lower():
        return False
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', required=True)
    a = p.parse_args()

    if not Path(a.input).exists():
        print(f"ERROR: {a.input} not found", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(a.input)
    cur = conn.cursor()

    tables = inspect(cur)

    placeholders = ','.join('?' for _ in TARGET_EXCHANGES)
    rows = cur.execute(f"""
        SELECT listing_key, ticker, exchange, name, aliases
        FROM listings WHERE exchange IN ({placeholders})
        ORDER BY exchange, ticker
    """, TARGET_EXCHANGES).fetchall()

    # If the column is empty, pull from the aliases table instead
    from_column = sum(1 for r in rows if r[4])
    by_key = {}
    if from_column == 0 and 'aliases' in tables:
        cols = [d[1] for d in cur.execute("PRAGMA table_info(aliases)").fetchall()]
        key_col = next((c for c in cols if 'listing' in c.lower() or c.lower() == 'key'), None)
        alias_col = next((c for c in cols if 'alias' in c.lower()), None)
        if key_col and alias_col:
            print(f"listings.aliases empty - reading from `aliases` "
                  f"table via {alias_col}/{key_col}\n")
            for al, lk in cur.execute(f"SELECT {alias_col}, {key_col} FROM aliases"):
                by_key.setdefault(lk, []).append(al)

    conn.close()

    names = [r[3] or '' for r in rows]
    tickers = [r[1] or '' for r in rows]

    all_al, dedup_al, lower_al = [], [], []
    for lk, ticker, exch, name, alias_raw in rows:
        parsed = split_aliases(alias_raw) if alias_raw else by_key.get(lk, [])
        all_al.append(parsed)
        kept = [x for x in parsed if adds_value(x, ticker or '', name or '')]
        dedup_al.append(kept)
        lower_al.append(sorted({x.strip().lower() for x in kept}))

    n_all = sum(len(x) for x in all_al)
    n_dedup = sum(len(x) for x in dedup_al)
    n_lower = sum(len(x) for x in lower_al)

    print("=" * 64)
    print("ALIAS COUNTS")
    print("=" * 64)
    print(f"  in scope (target exchanges): {n_all:,}")
    print(f"  after dropping redundant:    {n_dedup:,}  "
          f"({100*n_dedup/n_all:.1f}% kept)" if n_all else "  none found")
    print(f"  after lowercase + dedupe:    {n_lower:,}")
    print(f"  listings with >=1 alias:     "
          f"{sum(1 for x in lower_al if x):,} / {len(rows):,}")
    print()

    # pooled: shared string pool + integer refs
    pool, pool_idx = [], {}
    refs = []
    for lst in lower_al:
        r = []
        for x in lst:
            if x not in pool_idx:
                pool_idx[x] = len(pool)
                pool.append(x)
            r.append(pool_idx[x])
        refs.append(r)

    base_name = gz(names)
    base_ticker = gz(tickers)
    baseline = base_name + base_ticker

    variants = {
        'baseline (name+ticker)': 0,
        'all aliases':            gz(all_al),
        'deduped':                gz(dedup_al),
        'deduped+lower':          gz(lower_al),
        'pooled':                 gz(pool) + gz(refs),
    }

    print("=" * 64)
    print("COST (gzipped, added on top of name+ticker)")
    print("=" * 64)
    print(f"  name column:   {base_name:>9,}B")
    print(f"  ticker column: {base_ticker:>9,}B")
    print(f"  -> text total: {baseline:>9,}B\n")

    print(f"{'strategy':<24} {'added':>11} {'% of text':>11}")
    print("-" * 48)
    for k, v in variants.items():
        if v == 0:
            print(f"{k:<24} {'-':>11} {'-':>11}")
        else:
            print(f"{k:<24} {v:>10,}B {100*v/baseline:>10.1f}%")

    print()
    print(f"pool holds {len(pool):,} distinct alias strings")
    print()
    print("Add the chosen figure to the columnar total (340,729B) to get the")
    print("real shipped payload.")


if __name__ == '__main__':
    main()
