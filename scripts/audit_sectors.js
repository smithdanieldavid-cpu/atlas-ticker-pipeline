#!/usr/bin/env node
/* ---------------------------------------------------------------------------
 * Atlas Dashboard - Sector Classification Audit
 * Copyright (c) 2026 Atlas Dashboard (ABN 30 782 536 570)
 * ---------------------------------------------------------------------------
 *
 * Finds listings whose GICS sector looks inconsistent with their company name,
 * to gather evidence before raising an upstream data issue.
 *
 * This is a HEURISTIC, not a source of truth. A name containing "Gold" does
 * not prove the sector is wrong — Gold Fields is Materials, but "Golden Gate
 * Capital" would legitimately be Financials. Every hit needs eyeballing before
 * it goes in a bug report.
 *
 *   node scripts/audit_sectors.js instruments.json
 */

const fs = require('fs');

const file = process.argv[2] || 'instruments.json';
if (!fs.existsSync(file)) {
  console.error(`Not found: ${file}`);
  process.exit(1);
}

const data = JSON.parse(fs.readFileSync(file, 'utf8'));
const c = data.columns;
const L = data.legend;

// Words that strongly imply a sector, and the sector they imply.
// Deliberately conservative — only terms that are rarely metaphorical.
const SIGNALS = [
  { sector: 'Materials', words: [
    'mining', 'minerals', 'resources', 'exploration', 'gold', 'silver',
    'copper', 'nickel', 'lithium', 'iron ore', 'zinc', 'uranium', 'cobalt',
    'rare earth', 'graphite', 'potash', 'steel', 'cement', 'smelting',
  ]},
  { sector: 'Energy', words: [
    'petroleum', 'oil & gas', 'oil and gas', 'drilling', 'coal', 'gas ltd',
    'offshore energy', 'lng',
  ]},
  { sector: 'Financials', words: [
    'bancorp', 'bankshares', 'savings bank', 'insurance', 'reinsurance',
    'asset management', 'capital management',
  ]},
  { sector: 'Real Estate', words: [
    'reit', 'property trust', 'realty',
  ]},
  { sector: 'Health Care', words: [
    'pharmaceutical', 'biotech', 'therapeutics', 'medical', 'health care',
    'healthcare', 'diagnostics',
  ]},
];

const rows = [];
for (let i = 0; i < data.count; i++) {
  // Only stocks carry a GICS sector; ETFs use category instead.
  if (L.assetType[c.assetType[i]] !== 'Equity') continue;

  const sector = L.sector[c.sector[i]] || '';
  if (!sector) continue;

  const name = c.name[i].toLowerCase();

  for (const sig of SIGNALS) {
    if (sig.sector === sector) continue;          // already agrees
    const hit = sig.words.find(w => name.includes(w));
    if (hit) {
      rows.push({
        listing: `${L.exchange[c.exchange[i]]}:${c.ticker[i]}`,
        name: c.name[i],
        sector,
        expected: sig.sector,
        matched: hit,
      });
      break;
    }
  }
}

// Group by the mismatch pattern so systematic errors stand out from one-offs
const groups = new Map();
rows.forEach(r => {
  const key = `${r.sector} -> ${r.expected}`;
  if (!groups.has(key)) groups.set(key, []);
  groups.get(key).push(r);
});

const sorted = [...groups.entries()].sort((a, b) => b[1].length - a[1].length);

console.log(`Audited ${data.count.toLocaleString()} listings from ${data.source}`);
console.log(`Payload built ${data.timestamp}\n`);
console.log(`${rows.length} listings where the name suggests a different sector\n`);

for (const [pattern, hits] of sorted) {
  console.log('='.repeat(72));
  console.log(`classified ${pattern}  (${hits.length})`);
  hits.slice(0, 12).forEach(r => {
    console.log(`  ${r.listing.padEnd(16)} ${r.name.slice(0, 44).padEnd(44)} [${r.matched}]`);
  });
  if (hits.length > 12) console.log(`  ... and ${hits.length - 12} more`);
}

// Per-exchange rate, to see whether one source feed is worse than others
console.log('\n' + '='.repeat(72));
console.log('Rate by exchange (suspect / total equities with a sector)\n');
const totals = {}, suspects = {};
for (let i = 0; i < data.count; i++) {
  if (L.assetType[c.assetType[i]] !== 'Equity') continue;
  if (!L.sector[c.sector[i]]) continue;
  const ex = L.exchange[c.exchange[i]];
  totals[ex] = (totals[ex] || 0) + 1;
}
rows.forEach(r => {
  const ex = r.listing.split(':')[0];
  suspects[ex] = (suspects[ex] || 0) + 1;
});
Object.keys(totals).sort((a, b) =>
  (suspects[b] || 0) / totals[b] - (suspects[a] || 0) / totals[a]
).forEach(ex => {
  const s = suspects[ex] || 0;
  const pct = (100 * s / totals[ex]).toFixed(1);
  console.log(`  ${ex.padEnd(12)} ${String(s).padStart(4)} / ${String(totals[ex]).padStart(5)}  ${pct.padStart(5)}%`);
});
