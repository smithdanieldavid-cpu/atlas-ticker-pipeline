# Atlas Ticker Pipeline

Automated weekly refresh of global ticker data for the Atlas Dashboard ticker search.

**Data source:** [free-ticker-database](https://github.com/adanos-software/free-ticker-database) (MIT licensed)  
**Coverage:** 50k+ instruments across USA, Australia, Asia (JP/HK/SG/KR/VN), Canada  
**Deployment:** GitHub Actions → S3 → CloudFront CDN → Atlas Dashboard  

---

## Overview

```
┌─────────────────────────────────────────┐
│ free-ticker-database (upstream)         │
│ 86 exchanges, 91 countries              │
└────────────────┬────────────────────────┘
                 │
                 ▼ (GitHub Actions)
        ┌────────────────────┐
        │ transform_tickers  │ SQLite → JSON
        │ validate.py        │ Quality checks
        └────────┬───────────┘
                 │
                 ▼
        ┌────────────────────┐
        │  S3 Bucket         │
        │ data-atlas-        │
        │ dashboard/         │
        │ instruments/       │
        │ {dev|prod}/        │
        └────────┬───────────┘
                 │
                 ▼
        ┌────────────────────┐
        │ CloudFront CDN     │
        │ (cache: 7 days)    │
        └────────┬───────────┘
                 │
                 ▼
        ┌────────────────────┐
        │ Atlas Dashboard    │
        │ ticker-search.js   │
        │ (in-browser)       │
        └────────────────────┘
```

---

## Workflow: Develop → Main

### Develop Branch (Deploy to dev/)

```bash
git checkout develop
git commit -m "feat: add new ticker validator"
git push origin develop

# → GitHub Actions auto-runs:
#   1. Clone free-ticker-database
#   2. Rebuild SQLite database
#   3. Transform → instruments.json
#   4. Validate (critical tickers, ISIN coverage, etc.)
#   5. Upload to s3://data-atlas-dashboard/instruments/dev/latest.json.gz
#   6. Invalidate CloudFront cache
#
# Check logs: https://github.com/atlas-data/atlas-ticker-pipeline/actions
```

### Main Branch (Deploy to prod/)

```bash
# After testing in dev:
git checkout main
git pull origin main
git merge develop
git push origin main

# → GitHub Actions auto-runs:
#   Same process, but deploys to s3://data-atlas-dashboard/instruments/prod/
```

---

## Local Development

### Clone & Setup

```bash
git clone https://github.com/atlas-data/atlas-ticker-pipeline.git
cd atlas-ticker-pipeline

# Clone upstream ticker database
git clone --depth 1 \
  https://github.com/adanos-software/free-ticker-database.git \
  /tmp/ticker-source

# Install Python deps
pip install pyarrow pandas requests
```

### Test Transform Locally

```bash
# 1. Rebuild upstream database
cd /tmp/ticker-source
python3 scripts/rebuild_dataset.py

# 2. Transform
cd ~/atlas-ticker-pipeline
python3 scripts/transform_tickers.py \
  --input /tmp/ticker-source/data/tickers.db \
  --output instruments.json

# Output:
# - instruments.json (uncompressed, for validation)
# - instruments.json.gz (gzipped, for S3)
```

### Test Validation

```bash
python3 scripts/validate.py --input instruments.json

# Checks:
#   ✓ Row count >= 50,000
#   ✓ Critical tickers: NVDA, BHP, PMGOLD, VGAD, VGS
#   ✓ Exchange coverage (US, AU, Asia, Canada)
#   ✓ ISIN coverage >= 95% (warn if lower, fail if <85%)
#   ✓ Schema valid
#   ✓ No truncation/corruption

# Exit codes:
#   0 = PASS (may have warnings)
#   1 = FAIL (critical issue)
```

---

## Validation Rules

### Fail (Block Deployment)

- ❌ Row count < 50,000
- ❌ Any critical ticker missing (NVDA, BHP, PMGOLD, VGAD, VGS)
- ❌ ISIN coverage < 85%
- ❌ Schema invalid (missing fields)
- ❌ No coverage for any of 4 regions

### Warn (Still Deploy)

- ⚠️ ISIN coverage 85–95%
- ⚠️ Data quality issues (truncated names, etc.)

All warnings are logged to GitHub Actions and saved to S3.

---

## AWS Setup

### 1. S3 Bucket

Already configured: `data-atlas-dashboard`

Folder structure:
```
s3://data-atlas-dashboard/
└── instruments/
    ├── dev/
    │   ├── latest.json.gz
    │   ├── validation_2026-08-17T020000Z.json
    │   └── ...
    └── prod/
        ├── latest.json.gz
        ├── validation_2026-08-17T020000Z.json
        └── ...
```

### 2. CloudFront Distributions

Already configured:

| Environment | Distribution ID  | Domain |
|---|---|---|
| Dev | `E3202GZ5910KGI` | `d-dev-id.cloudfront.net` |
| Prod | `E2OPE9NIDX9MEF` | `d-prod-id.cloudfront.net` |

### 3. GitHub Secrets

Configure in repo Settings → Secrets:

```
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
```

(These should have minimal permissions: S3 PutObject to `instruments/*`, CloudFront CreateInvalidation)

---

## URLs

### Dev Environment

```
S3:
s3://data-atlas-dashboard/instruments/dev/latest.json.gz

CloudFront:
https://d-dev-id.cloudfront.net/instruments/dev/latest.json.gz

Used by: atlas-dashboard-dev.com
```

### Prod Environment

```
S3:
s3://data-atlas-dashboard/instruments/prod/latest.json.gz

CloudFront:
https://d-prod-id.cloudfront.net/instruments/prod/latest.json.gz

Used by: atlas-dashboard.com
```

---

## Frontend Integration

In `atlas-dashboard/scripts/builder.html`:

```html
<script src="scripts/ticker-search.js"></script>

<input 
  type="text" 
  id="ticker-search-input"
  placeholder="Search for holdings (ticker or company name)..."
  oninput="handleTickerSearch(this.value)"
>
<div id="ticker-search-results"></div>
```

```javascript
// On page init
window.atlasTickerSearch = new TickerSearch();
await window.atlasTickerSearch.init();

// On search input
function handleTickerSearch(query) {
  const results = window.atlasTickerSearch.search(query, 5);
  const html = results
    .map(r => `<div onclick="addHoldingFromSearch(...)">
      ${r.ticker} · ${r.name} · ${r.exchange}
    </div>`)
    .join('');
  document.getElementById('ticker-search-results').innerHTML = html;
}

// On result click
function addHoldingFromSearch(instrument) {
  // Auto-populate new holding card
  // (See ticker-search.js for implementation)
}
```

---

## Monitoring

### GitHub Actions

Runs on every push/merge:

```
https://github.com/atlas-data/atlas-ticker-pipeline/actions
```

Each run shows:
- Row count: 52,347 instruments
- ISIN coverage: 97.6%
- Critical tickers: ✓ NVDA, BHP, PMGOLD, VGAD, VGS
- Deployment status: ✓ dev or prod

### Logs

Each workflow logs summary to GitHub Actions output:

```
════════════════════════════════════════
Deploy Summary (Production)
════════════════════════════════════════
Environment: prod
S3 Path: s3://data-atlas-dashboard/instruments/prod/latest.json.gz
Instruments: 52347
ISIN Coverage: 97.6%
════════════════════════════════════════
```

### Validation Reports

Stored in S3 for audit trail:

```
s3://data-atlas-dashboard/instruments/dev/validation_2026-08-17T020000Z.json
s3://data-atlas-dashboard/instruments/prod/validation_2026-08-17T020000Z.json
```

---

## Troubleshooting

### Workflow Failed: "Critical tickers missing"

**Cause:** One of NVDA, BHP, PMGOLD, VGAD, VGS not in upstream database.

**Fix:**
1. Check upstream repo: https://github.com/adanos-software/free-ticker-database
2. Verify ticker exists in their data
3. If missing, open issue in upstream repo or add to supplemental list

### Workflow Failed: "ISIN coverage < 85%"

**Cause:** Dataset has incomplete ISIN data.

**Fix:**
1. Check `validation_*.json` report in S3
2. Contact upstream maintainers or merge recent upstream changes

### CloudFront Cache Not Invalidating

**Cause:** AWS credentials missing or invalid.

**Fix:**
1. Check GitHub Secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
2. Verify IAM user has `cloudfront:CreateInvalidation` permission
3. Check CloudFront Distribution ID is correct

---

## Maintenance

### Add New Critical Ticker

Edit `scripts/validate.py`:

```python
CRITICAL_TICKERS = {
    'NVDA', 'BHP', 'PMGOLD', 'VGAD', 'VGS',
    'NEW_TICKER',  # ← Add here
}
```

### Adjust ISIN Threshold

Edit `scripts/validate.py`:

```python
MIN_ISIN_COVERAGE_WARN = 95.0  # ← Adjust
MIN_ISIN_COVERAGE_FAIL = 85.0  # ← Adjust
```

### Change CloudFront Distribution

Edit `.github/workflows/deploy-*.yml`:

```yaml
env:
  CLOUDFRONT_DIST_ID: E2OPE9NIDX9MEF  # ← Update
```

---

## License

MIT — Same as [free-ticker-database](https://github.com/adanos-software/free-ticker-database)

---

## References

- **Upstream Data:** https://github.com/adanos-software/free-ticker-database
- **AWS S3:** `data-atlas-dashboard` bucket
- **CloudFront:** Dev (E3202GZ5910KGI), Prod (E2OPE9NIDX9MEF)
- **GitHub:** https://github.com/atlas-data/atlas-ticker-pipeline
