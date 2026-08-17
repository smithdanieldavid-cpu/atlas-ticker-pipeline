/**
 * Atlas Ticker Search
 * 
 * Fast in-browser ticker search with index lookup.
 * Loads from S3/CloudFront, maintains indices for <5ms search latency.
 * 
 * Usage in builder.html:
 *   <script src="scripts/ticker-search.js"></script>
 *   <input id="ticker-search-input" placeholder="Search tickers...">
 *   <div id="ticker-search-results"></div>
 */

class TickerSearch {
  constructor(options = {}) {
    this.instruments = [];
    this.byTicker = new Map();      // ticker → [instruments]
    this.byName = new Map();        // normalized name → instrument
    this.byExchange = new Map();    // exchange → [instruments]
    this.isReady = false;
    this.isLoading = false;
    
    this.options = {
      sourceUrl: options.sourceUrl || this._getDefaultSourceUrl(),
      cacheKey: options.cacheKey || 'atlas_tickers_cache',
      maxResults: options.maxResults || 5,
      ...options
    };
    
    this.init();
  }
  
  _getDefaultSourceUrl() {
    // Determine which environment based on current domain
    const hostname = window.location.hostname;
    
    if (hostname.includes('prod') || hostname === 'atlas-dashboard.com') {
      return 'https://d-prod-id.cloudfront.net/instruments/prod/latest.json.gz';
    } else {
      return 'https://d-dev-id.cloudfront.net/instruments/dev/latest.json.gz';
    }
  }
  
  async init() {
    if (this.isLoading) return;
    
    this.isLoading = true;
    
    try {
      // Try cache first
      const cached = this._getFromCache();
      if (cached) {
        this.instruments = cached;
        this._buildIndices();
        this.isReady = true;
        this.isLoading = false;
        console.log(`✓ Ticker search: ${this.instruments.length} instruments (cached)`);
        return;
      }
      
      // Fetch from S3
      console.log(`📥 Loading tickers from ${this.options.sourceUrl}...`);
      const resp = await fetch(this.options.sourceUrl, {
        method: 'GET',
        headers: {
          'Accept-Encoding': 'gzip',
        },
        credentials: 'omit'
      });
      
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
      }
      
      // Response is already auto-decompressed by browser
      const data = await resp.json();
      
      this.instruments = data.instruments || [];
      this._buildIndices();
      this._saveToCache();
      
      this.isReady = true;
      console.log(`✓ Ticker search: ${this.instruments.length} instruments loaded`);
      
    } catch (err) {
      console.error('❌ Ticker search failed to load:', err);
      this.isReady = false;
    } finally {
      this.isLoading = false;
    }
  }
  
  _buildIndices() {
    this.byTicker.clear();
    this.byName.clear();
    this.byExchange.clear();
    
    this.instruments.forEach(inst => {
      const ticker = inst.ticker.toUpperCase();
      const nameNorm = (inst.name || '').toUpperCase();
      const exchange = inst.exchange;
      
      // Index by ticker
      if (!this.byTicker.has(ticker)) {
        this.byTicker.set(ticker, []);
      }
      this.byTicker.get(ticker).push(inst);
      
      // Index by name
      if (nameNorm && !this.byName.has(nameNorm)) {
        this.byName.set(nameNorm, inst);
      }
      
      // Index by exchange
      if (!this.byExchange.has(exchange)) {
        this.byExchange.set(exchange, []);
      }
      this.byExchange.get(exchange).push(inst);
    });
  }
  
  _getFromCache() {
    try {
      const cached = sessionStorage.getItem(this.options.cacheKey);
      if (cached) {
        return JSON.parse(cached);
      }
    } catch (err) {
      console.warn('Cache read failed:', err);
    }
    return null;
  }
  
  _saveToCache() {
    try {
      sessionStorage.setItem(
        this.options.cacheKey,
        JSON.stringify(this.instruments)
      );
    } catch (err) {
      console.warn('Cache write failed:', err);
    }
  }
  
  /**
   * Search for instruments by ticker or name.
   * 
   * @param {string} query - Ticker or company name
   * @param {number} limit - Max results to return
   * @returns {Array} Array of matching instruments
   */
  search(query, limit = this.options.maxResults) {
    if (!this.isReady || !query) {
      return [];
    }
    
    const q = query.toUpperCase().trim();
    const results = [];
    const seen = new Set();
    
    // 1. Exact ticker match (highest priority)
    if (this.byTicker.has(q)) {
      this.byTicker.get(q).forEach(inst => {
        if (!seen.has(inst.id)) {
          results.push(inst);
          seen.add(inst.id);
        }
      });
    }
    
    // 2. Ticker prefix match (NVDA matches NV)
    if (results.length < limit) {
      this.byTicker.forEach((instruments, ticker) => {
        if (ticker.startsWith(q)) {
          instruments.forEach(inst => {
            if (!seen.has(inst.id) && results.length < limit) {
              results.push(inst);
              seen.add(inst.id);
            }
          });
        }
      });
    }
    
    // 3. Company name contains
    if (results.length < limit) {
      this.byName.forEach((inst, name) => {
        if (name.includes(q)) {
          if (!seen.has(inst.id) && results.length < limit) {
            results.push(inst);
            seen.add(inst.id);
          }
        }
      });
    }
    
    // 4. Broader name substring (slower, lower priority)
    if (results.length < limit) {
      this.instruments.forEach(inst => {
        const name = (inst.name || '').toUpperCase();
        if (name.includes(q) && !seen.has(inst.id)) {
          results.push(inst);
          seen.add(inst.id);
        }
      });
    }
    
    return results.slice(0, limit);
  }
  
  /**
   * Get instrument by composite ID (e.g. "NVDA-NASDAQ-US").
   */
  getById(id) {
    return this.instruments.find(inst => inst.id === id) || null;
  }
  
  /**
   * Get all instruments for an exchange.
   */
  getByExchange(exchange) {
    return this.byExchange.get(exchange.toUpperCase()) || [];
  }
  
  /**
   * Get summary statistics.
   */
  getStats() {
    return {
      total: this.instruments.length,
      exchanges: this.byExchange.size,
      byAssetType: this._countByAssetType(),
    };
  }
  
  _countByAssetType() {
    const counts = {};
    this.instruments.forEach(inst => {
      const type = inst.assetType || 'Unknown';
      counts[type] = (counts[type] || 0) + 1;
    });
    return counts;
  }
}

/**
 * Global instance (initialize on page load).
 */
window.atlasTickerSearch = null;

document.addEventListener('DOMContentLoaded', async () => {
  window.atlasTickerSearch = new TickerSearch();
  await window.atlasTickerSearch.init();
});

/**
 * Helper: Format a result for display.
 * Usage in HTML: renderTickerResult(instrument)
 */
function renderTickerResult(inst) {
  if (!inst) return '';
  
  return `
    <div class="ticker-search-result" data-id="${inst.id}">
      <div class="ticker-search-result-header">
        <span class="ticker-symbol">${inst.ticker}</span>
        <span class="ticker-exchange">${inst.exchange}</span>
      </div>
      <div class="ticker-search-result-name">${inst.name}</div>
      ${inst.isin ? `<div class="ticker-search-result-isin">ISIN: ${inst.isin}</div>` : ''}
    </div>
  `;
}

/**
 * Helper: Map assetType to Atlas asset_class field.
 * Usage: mapAssetTypeToClass(instrument.assetType)
 */
function mapAssetTypeToClass(assetType) {
  const mappings = {
    'Equity': {
      'US': 'us_equities',
      'AU': 'au_equities',
      'Other': 'global_equities',
    },
    'ETF': {
      'US': 'us_equities',
      'AU': 'au_equities',
      'Other': 'global_equities',
    },
    'Index': 'global_equities',
  };
  
  return mappings[assetType] || 'global_equities';
}

/**
 * Helper: Auto-populate holding card after search result click.
 * Usage: addHoldingFromSearch(instrument)
 */
function addHoldingFromSearch(inst) {
  if (!inst) return;
  
  // Create new holding
  const holding = {
    id: ++window.holdingId,
    ticker: inst.ticker,
    asset_class: mapAssetTypeToClass(inst.assetType, inst.country),
    market: inst.exchange,
    units: '',
    purchase_price: '',
    notes: '',
    notesVisible: false,
  };
  
  // Add to holdings array
  window.holdings.push(holding);
  
  // Re-render
  renderHoldings();
  updateState();
  
  // Focus on units field
  setTimeout(() => {
    document.getElementById(`units-${holding.id}`)?.focus();
  }, 100);
}
