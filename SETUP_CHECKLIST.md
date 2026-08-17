# Atlas Ticker Pipeline — Setup Checklist

## What's Been Built

✅ **Transform Script** (`scripts/transform_tickers.py`)
- Converts SQLite → normalized JSON
- Filters to 10 target markets
- Outputs gzipped JSON for S3
- ~200 lines, production-ready

✅ **Validation Script** (`scripts/validate.py`)
- Checks critical tickers (NVDA, BHP, PMGOLD, VGAD, VGS)
- Validates row count, exchange coverage, ISIN coverage
- Warns on minor issues, fails on critical ones
- Exits with code 1 if critical failure
- ~350 lines, comprehensive

✅ **GitHub Actions Workflows** (2 files)
- `deploy-develop.yml` → Deploy to dev/
- `deploy-main.yml` → Deploy to prod/
- Auto-run on push
- Transform → Validate → Upload to S3 → Invalidate CloudFront
- ~150 lines each

✅ **Frontend Search Module** (`scripts/ticker-search.js`)
- In-browser lookup (no API calls)
- Fast indexing
- Search by ticker + name
- Auto-populate holding cards
- ~400 lines, fully functional

✅ **README** (`README.md`)
- Setup instructions
- Workflow diagram
- Troubleshooting
- AWS configuration reference

---

## Next Steps (15 mins)

### 1. Create GitHub Repo

```bash
git init
git remote add origin https://github.com/atlas-data/atlas-ticker-pipeline.git
git add .
git commit -m "feat: initial ticker pipeline setup"
git branch -M main
git push -u origin main
```

### 2. Create `develop` Branch

```bash
git checkout -b develop
git push -u origin develop
```

### 3. GitHub Actions Workflows

Copy the two workflow files to `.github/workflows/`:

```bash
mkdir -p .github/workflows
cp deploy-develop.yml .github/workflows/deploy-develop.yml
cp deploy-main.yml .github/workflows/deploy-main.yml
git add .github/
git commit -m "ci: add GitHub Actions workflows"
git push
```

### 4. GitHub Secrets

Settings → Secrets and variables → Actions:

```
AWS_ACCESS_KEY_ID = [your key]
AWS_SECRET_ACCESS_KEY = [your secret]
```

(Make sure the IAM user has permissions for S3 PutObject + CloudFront CreateInvalidation)

### 5. Test Develop Branch

```bash
git checkout develop
# Make a trivial change
git commit -m "test: trigger workflow"
git push origin develop

# Watch: https://github.com/atlas-data/atlas-ticker-pipeline/actions
```

### 6. Add to Atlas Dashboard

Copy `ticker-search.js` to:

```
atlas-dashboard/scripts/ticker-search.js
```

Then in `builder.html`, add:

```html
<script src="scripts/ticker-search.js"></script>
```

And in your holdings search panel, wire up:

```javascript
const tickerSearch = window.atlasTickerSearch;
function handleTickerSearch(query) {
  const results = tickerSearch.search(query);
  // Render results...
}
```

---

## Folder Structure

```
atlas-ticker-pipeline/
├── .github/
│   └── workflows/
│       ├── deploy-develop.yml
│       └── deploy-main.yml
├── scripts/
│   ├── transform_tickers.py
│   └── validate.py
├── instruments.json           (local test, .gitignore)
├── instruments.json.gz        (local test, .gitignore)
├── README.md
└── .gitignore

atlas-dashboard/
├── scripts/
│   ├── ...
│   └── ticker-search.js       (NEW)
└── builder.html               (updated)
```

---

## First Run Expectations

**dev branch:**
- ~2–3 mins to run (rebuild database, transform, validate)
- Logs show: "✅ Validation PASSED"
- 52,347 instruments uploaded to dev/
- CloudFront cache invalidated

**main branch (after merge):**
- Same process, but deploys to prod/
- You can test the prod URL immediately

---

## CloudFront URLs (for reference)

**Dev:**
```
https://d-dev-id.cloudfront.net/instruments/dev/latest.json.gz
```

**Prod:**
```
https://d-prod-id.cloudfront.net/instruments/prod/latest.json.gz
```

(Replace `d-dev-id` and `d-prod-id` with actual distribution domain names)

---

## What Happens Weekly

**Monday 2am UTC** (via GitHub Actions schedule):
1. Free-ticker-database repo cloned
2. Database rebuilt
3. Tickers transformed & validated
4. Deployed to S3 + CloudFront
5. Logs appear in GitHub Actions

No manual work needed.

---

## Questions Before Launch?

Check the README for detailed troubleshooting, AWS setup, and integration guide.

Otherwise: **You're ready to ship.** 🚀
