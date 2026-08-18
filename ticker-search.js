/**
 * Atlas Ticker Search
 * ===================
 *
 * Client-side search over the columnar instrument payload (~358KB gzipped,
 * 28,909 venue-level listings across US / AU / Asia / Canada).
 *
 * Two layers, deliberately separated:
 *
 *   AtlasTickerSearch  - the engine. Loading, decoding, matching, ranking,
 *                        faceting. No DOM. Testable in isolation.
 *   TickerSearchUI     - the presentation. Wires the engine to an input and
 *                        a results container. Rewrite this freely to match
 *                        Atlas's design system; the engine is the valuable bit.
 *
 * Design notes worth knowing before changing anything:
 *
 * - The payload is COLUMNAR (one array per field, index-aligned). Row `i` is
 *   assembled from columns[*][i]. This is 17% smaller gzipped than row objects
 *   and lets us scan one contiguous name array without touching other fields.
 *
 * - Matching is a LINEAR SCAN, not an inverted index. At 29k rows a scan runs
 *   in single-digit milliseconds, and it avoids a 100ms+ index build on load.
 *   If the universe grows past ~100k, revisit.
 *
 * - The upstream alias set was filtered on the assumption that this matcher
 *   does substring AND token matching on names. ~90% of aliases were dropped
 *   as redundant. If you narrow matching to prefix-only, recall will silently
 *   degrade - relax the filter in transform_tickers.py first.
 */

'use strict';

// ---------------------------------------------------------------------------
// Engine
// ---------------------------------------------------------------------------

const TIER = {
  TICKER_EXACT: 0,
  TICKER_QUALIFIED: 1,   // ASX:BHP
  TICKER_PREFIX: 2,
  NAME_PREFIX: 3,
  NAME_TOKENS: 4,
  ALIAS: 5,
};

class AtlasTickerSearch {
  /**
   * @param {object} opts
   * @param {string} [opts.url]            payload URL; defaults to same-origin
   * @param {string[]} [opts.priorityRegions]  ranking bias, most-preferred first
   * @param {number} [opts.limit]          max results returned
   */
  constructor(opts = {}) {
    this.url = opts.url || '/data/instruments.json.gz';
    this.priorityRegions = opts.priorityRegions || ['AU', 'US', 'ASIA', 'CANADA'];
    this.limit = opts.limit || 8;

    this.state = 'idle';   // idle | loading | ready | error
    this.error = null;
    this._loadPromise = null;

    this.count = 0;
    this.legend = null;
    this.col = null;       // decoded columns
    this._namesLower = null;
    this._tickersUpper = null;
    this._tickerIndex = null;   // exact ticker -> [row, ...]
    this._regionRank = null;    // row -> bias score
  }

  // -- loading -------------------------------------------------------------

  /**
   * Fetch and decode. Safe to call repeatedly - concurrent callers share one
   * request, and a completed load resolves immediately.
   */
  load() {
    if (this.state === 'ready') return Promise.resolve(this);
    if (this._loadPromise) return this._loadPromise;

    this.state = 'loading';
    this._loadPromise = fetch(this.url, { credentials: 'omit' })
      .then((resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status} fetching ${this.url}`);
        return resp.json();   // browser transparently gunzips
      })
      .then((data) => {
        this._decode(data);
        this.state = 'ready';
        return this;
      })
      .catch((err) => {
        this.state = 'error';
        this.error = err;
        this._loadPromise = null;   // allow retry
        throw err;
      });

    return this._loadPromise;
  }

  _decode(data) {
    if (data.encoding !== 'columnar') {
      throw new Error(`Unexpected encoding: ${data.encoding}`);
    }

    this.count = data.count;
    this.legend = data.legend;
    this.meta = {
      timestamp: data.timestamp,
      source: data.source,
      exchanges: data.exchanges,
      regions: data.regions,
    };

    const c = data.columns;
    this.col = c;

    // Precompute the case-folded forms once, rather than 29k toLowerCase()
    // calls on every keystroke.
    this._namesLower = c.name.map((n) => n.toLowerCase());
    this._tickersUpper = c.ticker.map((t) => t.toUpperCase());

    // Exact-ticker lookup. Several rows share a ticker across venues, so the
    // value is a list - this is exactly the BHP-on-ASX-and-NYSE case.
    this._tickerIndex = new Map();
    for (let i = 0; i < this.count; i++) {
      const t = this._tickersUpper[i];
      const hit = this._tickerIndex.get(t);
      if (hit) hit.push(i);
      else this._tickerIndex.set(t, [i]);
    }

    // Region bias, resolved once. Lower is better.
    const exLegend = this.legend.exchange;
    const regionOf = AtlasTickerSearch.REGION_BY_EXCHANGE;
    this._regionRank = new Int8Array(this.count);
    for (let i = 0; i < this.count; i++) {
      const region = regionOf[exLegend[c.exchange[i]]] || 'OTHER';
      const rank = this.priorityRegions.indexOf(region);
      this._regionRank[i] = rank === -1 ? 99 : rank;
    }

    // Sparse aliases arrive as { "rowIndex": [...] }
    this._aliases = c.aliases || {};
  }

  /** Re-rank without reloading, e.g. once the user's holdings are known. */
  setPriorityRegions(regions) {
    this.priorityRegions = regions;
    if (this.state === 'ready') {
      const exLegend = this.legend.exchange;
      const regionOf = AtlasTickerSearch.REGION_BY_EXCHANGE;
      for (let i = 0; i < this.count; i++) {
        const region = regionOf[exLegend[this.col.exchange[i]]] || 'OTHER';
        const rank = regions.indexOf(region);
        this._regionRank[i] = rank === -1 ? 99 : rank;
      }
    }
  }

  // -- row access ----------------------------------------------------------

  /** Assemble row `i` into a plain object. */
  get(i) {
    const c = this.col;
    const L = this.legend;
    const exchange = L.exchange[c.exchange[i]];
    const ticker = c.ticker[i];
    return {
      index: i,
      id: `${exchange}::${ticker}`,     // reconstructed, not stored
      ticker,
      name: c.name[i],
      exchange,
      assetType: L.assetType[c.assetType[i]],
      assetClass: L.assetClass[c.assetClass[i]],
      sector: L.sector[c.sector[i]] || '',
      category: L.category[c.category[i]] || '',
      country: L.country[c.country[i]] || '',
      aliases: this._aliases[i] || [],
    };
  }

  // -- matching ------------------------------------------------------------

  /**
   * @param {string} query
   * @param {object} [filters] { exchange, assetType, sector, category }
   *                           values are display strings, not codes
   * @returns {{ results: object[], total: number, facets: object }}
   */
  search(query, filters = {}) {
    if (this.state !== 'ready') return { results: [], total: 0, facets: {} };

    const parsed = this._parseQuery(query);
    if (!parsed) return { results: [], total: 0, facets: {} };

    const { qUpper, qLower, tokens, exchangeHint } = parsed;
    const c = this.col;
    const L = this.legend;

    // Resolve filter display values to legend codes once, outside the loop.
    const codeFilters = [];
    for (const field of ['exchange', 'assetType', 'sector', 'category']) {
      const want = filters[field];
      if (!want) continue;
      const code = L[field].indexOf(want);
      if (code === -1) return { results: [], total: 0, facets: {} };
      codeFilters.push([field, code]);
    }

    const exchangeHintCode = exchangeHint
      ? L.exchange.indexOf(exchangeHint)
      : -1;
    if (exchangeHint && exchangeHintCode === -1) {
      return { results: [], total: 0, facets: {} };
    }

    const hits = [];
    for (let i = 0; i < this.count; i++) {
      if (exchangeHintCode !== -1 && c.exchange[i] !== exchangeHintCode) continue;

      let skip = false;
      for (let f = 0; f < codeFilters.length; f++) {
        if (c[codeFilters[f][0]][i] !== codeFilters[f][1]) { skip = true; break; }
      }
      if (skip) continue;

      const tier = this._tier(i, qUpper, qLower, tokens, exchangeHintCode !== -1);
      if (tier !== -1) hits.push([i, tier]);
    }

    // tier, then region bias, then shorter name (proxy for the primary
    // listing rather than a structured product referencing it)
    hits.sort((a, b) => {
      if (a[1] !== b[1]) return a[1] - b[1];
      const ra = this._regionRank[a[0]], rb = this._regionRank[b[0]];
      if (ra !== rb) return ra - rb;
      return this.col.name[a[0]].length - this.col.name[b[0]].length;
    });

    return {
      total: hits.length,
      results: hits.slice(0, this.limit).map(([i, tier]) => {
        const row = this.get(i);
        row.tier = tier;
        return row;
      }),
      facets: this._facets(hits),
    };
  }

  _parseQuery(query) {
    let q = (query || '').trim();
    if (!q) return null;

    // TradingView-style exchange qualifier: "ASX:BHP" or "asx:bh"
    let exchangeHint = null;
    const colon = q.indexOf(':');
    if (colon > 0) {
      const candidate = q.slice(0, colon).trim().toUpperCase();
      if (this.legend.exchange.includes(candidate)) {
        exchangeHint = candidate;
        q = q.slice(colon + 1).trim();
      }
    }
    if (!q) return null;

    const qLower = q.toLowerCase();
    return {
      qUpper: q.toUpperCase(),
      qLower,
      tokens: qLower.split(/[\s.\-#&/]+/).filter(Boolean),
      exchangeHint,
    };
  }

  /** Best matching tier for row `i`, or -1 for no match. */
  _tier(i, qUpper, qLower, tokens, qualified) {
    const ticker = this._tickersUpper[i];

    if (ticker === qUpper) {
      return qualified ? TIER.TICKER_QUALIFIED : TIER.TICKER_EXACT;
    }
    if (ticker.startsWith(qUpper)) return TIER.TICKER_PREFIX;

    const name = this._namesLower[i];
    if (name.startsWith(qLower)) return TIER.NAME_PREFIX;

    // Token match: every query token appears somewhere in the name, in any
    // order. This is what lets "bank australia" find "National Australia
    // Bank", and it subsumes most of the aliases we filtered out upstream.
    if (tokens.length) {
      let all = true;
      for (let t = 0; t < tokens.length; t++) {
        if (name.indexOf(tokens[t]) === -1) { all = false; break; }
      }
      if (all) return TIER.NAME_TOKENS;
    }

    const aliases = this._aliases[i];
    if (aliases) {
      for (let a = 0; a < aliases.length; a++) {
        if (aliases[a].indexOf(qLower) !== -1) return TIER.ALIAS;
      }
    }

    return -1;
  }

  /**
   * Distinct values present in the full hit set, with counts. Drives the
   * progressive filter chips - we only offer refinements that would actually
   * narrow something.
   */
  _facets(hits) {
    const out = {};
    for (const field of ['exchange', 'assetType', 'sector', 'category']) {
      const counts = new Map();
      for (let h = 0; h < hits.length; h++) {
        const code = this.col[field][hits[h][0]];
        counts.set(code, (counts.get(code) || 0) + 1);
      }
      const values = [];
      for (const [code, n] of counts) {
        const label = this.legend[field][code];
        if (label) values.push({ value: label, count: n });
      }
      values.sort((a, b) => b.count - a.count);
      if (values.length > 1) out[field] = values;
    }
    return out;
  }
}

AtlasTickerSearch.REGION_BY_EXCHANGE = {
  NASDAQ: 'US', NYSE: 'US', 'NYSE ARCA': 'US', 'NYSE MKT': 'US', BATS: 'US',
  ASX: 'AU',
  TSE: 'ASIA', HKEX: 'ASIA', SGX: 'ASIA', KRX: 'ASIA', KOSDAQ: 'ASIA',
  HOSE: 'ASIA', HNX: 'ASIA', UPCOM: 'ASIA',
  TSX: 'CANADA', TSXV: 'CANADA', NEO: 'CANADA',
};

AtlasTickerSearch.TIER = TIER;

// ---------------------------------------------------------------------------
// UI controller
// ---------------------------------------------------------------------------

const FACET_LABELS = {
  exchange: 'Market',
  assetType: 'Type',
  sector: 'Sector',
  category: 'Category',
};

// Below this many results, filters are noise rather than help.
const FACET_THRESHOLD = 15;

class TickerSearchUI {
  /**
   * @param {object} opts
   * @param {HTMLInputElement} opts.input
   * @param {HTMLElement} opts.results       container for the dropdown
   * @param {AtlasTickerSearch} opts.engine
   * @param {function} opts.onSelect         receives the chosen instrument
   * @param {function} [opts.getRecent]      -> instrument[] for the empty state
   * @param {number} [opts.debounce]
   */
  constructor(opts) {
    this.input = opts.input;
    this.resultsEl = opts.results;
    this.engine = opts.engine;
    this.onSelect = opts.onSelect || (() => {});
    this.getRecent = opts.getRecent || (() => []);
    this.debounceMs = opts.debounce ?? 120;

    this.filters = {};
    this.active = -1;
    this.current = [];
    this._timer = null;

    this._bind();
  }

  _bind() {
    // Lazy load: the payload is only fetched when the user shows intent.
    this.input.addEventListener('focus', () => {
      this._ensureLoaded();
      if (!this.input.value.trim()) this._renderEmpty();
    }, { once: false });

    this.input.addEventListener('input', () => {
      clearTimeout(this._timer);
      this._timer = setTimeout(() => this._run(), this.debounceMs);
    });

    this.input.addEventListener('keydown', (e) => this._onKey(e));

    document.addEventListener('click', (e) => {
      if (!this.resultsEl.contains(e.target) && e.target !== this.input) {
        this.close();
      }
    });
  }

  _ensureLoaded() {
    if (this.engine.state === 'ready') return;
    if (this.engine.state === 'loading') return;

    this._renderStatus('Loading markets…');
    this.engine.load()
      .then(() => { if (this.input.value.trim()) this._run(); else this._renderEmpty(); })
      .catch(() => this._renderStatus('Could not load market data. Try again shortly.'));
  }

  _run() {
    const q = this.input.value.trim();
    if (!q) { this.filters = {}; this._renderEmpty(); return; }
    if (this.engine.state !== 'ready') { this._ensureLoaded(); return; }

    const { results, total, facets } = this.engine.search(q, this.filters);
    this.current = results;
    this.active = -1;
    this._render(results, total, facets);
  }

  // -- rendering -----------------------------------------------------------

  _render(results, total, facets) {
    if (!results.length) {
      this._renderStatus(
        Object.keys(this.filters).length
          ? 'No matches with these filters.'
          : 'No matches.');
      return;
    }

    const parts = [];

    // Filters appear only once the result set is genuinely ambiguous.
    const showFacets = total > FACET_THRESHOLD || Object.keys(this.filters).length;
    if (showFacets) parts.push(this._facetHTML(facets));

    parts.push('<ul class="ats-list" role="listbox">');
    results.forEach((r, idx) => {
      parts.push(`
        <li class="ats-item" role="option" data-idx="${idx}" aria-selected="false">
          <div class="ats-line">
            <span class="ats-ticker">${esc(r.ticker)}</span>
            <span class="ats-badge">${esc(r.exchange)}</span>
            ${r.assetType === 'ETF' ? '<span class="ats-badge ats-etf">ETF</span>' : ''}
          </div>
          <div class="ats-name">${esc(r.name)}</div>
          ${(r.sector || r.category)
            ? `<div class="ats-meta">${esc(r.sector || r.category)}</div>` : ''}
        </li>`);
    });
    parts.push('</ul>');

    if (total > results.length) {
      parts.push(
        `<div class="ats-more">${total.toLocaleString()} matches — ` +
        `refine to narrow further</div>`);
    }

    this.resultsEl.innerHTML = parts.join('');
    this.resultsEl.hidden = false;
    this._bindResults();
  }

  _facetHTML(facets) {
    const chips = [];
    for (const [field, values] of Object.entries(facets)) {
      const activeVal = this.filters[field];
      if (activeVal) {
        chips.push(
          `<button class="ats-chip ats-chip-on" data-field="${field}" data-value="">` +
          `${esc(FACET_LABELS[field])}: ${esc(activeVal)} ×</button>`);
      } else {
        values.slice(0, 4).forEach((v) => {
          chips.push(
            `<button class="ats-chip" data-field="${field}" ` +
            `data-value="${esc(v.value)}">${esc(v.value)} ` +
            `<span class="ats-chip-n">${v.count}</span></button>`);
        });
      }
    }
    return chips.length ? `<div class="ats-chips">${chips.join('')}</div>` : '';
  }

  _renderEmpty() {
    const recent = this.getRecent() || [];
    if (!recent.length) { this.close(); return; }

    this.current = recent;
    this.active = -1;
    const items = recent.slice(0, 5).map((r, idx) => `
      <li class="ats-item" role="option" data-idx="${idx}">
        <div class="ats-line">
          <span class="ats-ticker">${esc(r.ticker)}</span>
          <span class="ats-badge">${esc(r.exchange)}</span>
        </div>
        <div class="ats-name">${esc(r.name)}</div>
      </li>`).join('');

    this.resultsEl.innerHTML =
      `<div class="ats-head">Recently added</div>` +
      `<ul class="ats-list" role="listbox">${items}</ul>`;
    this.resultsEl.hidden = false;
    this._bindResults();
  }

  _renderStatus(msg) {
    this.resultsEl.innerHTML = `<div class="ats-status">${esc(msg)}</div>`;
    this.resultsEl.hidden = false;
  }

  _bindResults() {
    this.resultsEl.querySelectorAll('.ats-item').forEach((el) => {
      el.addEventListener('mousedown', (e) => {
        e.preventDefault();   // keep focus so blur doesn't close first
        this._choose(Number(el.dataset.idx));
      });
    });
    this.resultsEl.querySelectorAll('.ats-chip').forEach((el) => {
      el.addEventListener('mousedown', (e) => {
        e.preventDefault();
        const { field, value } = el.dataset;
        if (value) this.filters[field] = value;
        else delete this.filters[field];
        this._run();
      });
    });
  }

  // -- interaction ---------------------------------------------------------

  _onKey(e) {
    if (e.key === 'Escape') { this.close(); return; }
    if (!this.current.length) return;

    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const delta = e.key === 'ArrowDown' ? 1 : -1;
      this.active = (this.active + delta + this.current.length) % this.current.length;
      this._highlight();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      this._choose(this.active === -1 ? 0 : this.active);
    }
  }

  _highlight() {
    this.resultsEl.querySelectorAll('.ats-item').forEach((el, i) => {
      const on = i === this.active;
      el.classList.toggle('ats-item-on', on);
      el.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on) el.scrollIntoView({ block: 'nearest' });
    });
  }

  _choose(idx) {
    const picked = this.current[idx];
    if (!picked) return;
    this.onSelect(picked);
    this.input.value = '';
    this.filters = {};
    this.close();
  }

  close() {
    this.resultsEl.hidden = true;
    this.resultsEl.innerHTML = '';
    this.current = [];
    this.active = -1;
  }
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ---------------------------------------------------------------------------
// Exports - works as an ES module, a CommonJS module, or a plain script tag
// ---------------------------------------------------------------------------

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { AtlasTickerSearch, TickerSearchUI, TIER };
} else if (typeof window !== 'undefined') {
  window.AtlasTickerSearch = AtlasTickerSearch;
  window.TickerSearchUI = TickerSearchUI;
}
