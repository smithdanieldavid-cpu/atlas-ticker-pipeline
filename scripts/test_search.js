/**
 * Exercise the search engine against a real instruments.json.
 *
 *   node scripts/test_search.js instruments.json
 *
 * No DOM, no network - stubs fetch to read from disk so the engine runs
 * exactly as it would in the browser.
 */

const fs = require('fs');
const path = require('path');

const file = process.argv[2] || 'instruments.json';
if (!fs.existsSync(file)) {
  console.error(`Not found: ${file}\nRun transform_tickers.py first.`);
  process.exit(1);
}

const payload = JSON.parse(fs.readFileSync(file, 'utf8'));

global.fetch = async () => ({ ok: true, status: 200, json: async () => payload });

const { AtlasTickerSearch, TIER } =
  require(path.resolve(__dirname, 'ticker-search.js'));

const TIER_NAME = Object.fromEntries(
  Object.entries(TIER).map(([k, v]) => [v, k]));

const QUERIES = [
  ['BHP',            'cross-listing: ASX must outrank NYSE'],
  ['ASX:BHP',        'exchange qualifier pins the venue'],
  ['NYSE:BHP',       'qualifier works the other way too'],
  ['NVDA',           'exact ticker'],
  ['NV',             'ticker prefix'],
  ['nvidia',         'company name, lowercase'],
  ['anz bank',       'alias only - registered name is ANZ GROUP HOLDINGS'],
  ['data 3',         'punctuation alias - name is DATA#3 LIMITED'],
  ['bank australia', 'token match, words out of order'],
  ['commonwealth',   'name substring'],
  ['perth mint',     'multi-token name match'],
  ['vanguard',       'broad - should trigger facet chips'],
  ['gold',           'broad, cross-market'],
  ['zzzznope',       'no matches'],
];

(async () => {
  const t0 = Date.now();
  const engine = new AtlasTickerSearch({ priorityRegions: ['AU', 'US', 'ASIA', 'CANADA'] });
  await engine.load();
  console.log(`Loaded ${engine.count.toLocaleString()} instruments in ${Date.now() - t0}ms`);
  console.log(`Payload built ${engine.meta.timestamp}\n`);

  let slowest = 0;
  for (const [q, why] of QUERIES) {
    const s = process.hrtime.bigint();
    const { results, total, facets } = engine.search(q);
    const ms = Number(process.hrtime.bigint() - s) / 1e6;
    slowest = Math.max(slowest, ms);

    console.log('='.repeat(72));
    console.log(`"${q}"  —  ${why}`);
    console.log(`${total.toLocaleString()} matches in ${ms.toFixed(1)}ms`);
    if (!results.length) { console.log('  (none)'); continue; }

    results.slice(0, 5).forEach((r, i) => {
      const badge = r.assetType === 'ETF' ? ' [ETF]' : '';
      console.log(
        `  ${i + 1}. ${(r.exchange + ':' + r.ticker).padEnd(18)} ` +
        `${r.name.slice(0, 40).padEnd(40)} ${TIER_NAME[r.tier]}${badge}`);
    });

    const chips = Object.entries(facets)
      .map(([f, v]) => `${f}(${v.length})`).join(' ');
    if (chips) console.log(`  facets: ${chips}`);
  }

  console.log('\n' + '='.repeat(72));
  console.log(`Slowest query: ${slowest.toFixed(1)}ms`);

  // Filter behaviour
  const unfiltered = engine.search('gold');
  const filtered = engine.search('gold', { exchange: 'ASX' });
  console.log(`\nFilter check: "gold" ${unfiltered.total} -> ` +
              `ASX-only ${filtered.total}`);
  const allASX = filtered.results.every((r) => r.exchange === 'ASX');
  console.log(`All filtered results on ASX: ${allASX ? 'yes' : 'NO - BUG'}`);

  // Region bias
  engine.setPriorityRegions(['US', 'AU', 'ASIA', 'CANADA']);
  const usFirst = engine.search('BHP').results[0];
  engine.setPriorityRegions(['AU', 'US', 'ASIA', 'CANADA']);
  const auFirst = engine.search('BHP').results[0];
  console.log(`\nRegion bias: US-first -> ${usFirst.exchange}, ` +
              `AU-first -> ${auFirst.exchange}`);
  console.log(usFirst.exchange !== auFirst.exchange
    ? 'Bias is working.' : 'Bias had no effect - check _regionRank.');
})();
