# QUICK START: Fix the Zero-Match Problem

## TL;DR

Your data preprocessing is **working perfectly**! ✅

The zero-match problem is due to **missing OpenCorporates API authentication**, not data errors.

---

## Current Status

```
Data Extraction:        ✅ Perfect (100% names, 97.4% regions)
API Requests Sent:      ✅ 3,337 requests made successfully
API Responses:          ❌ All return 401 Unauthorized
Zero Matches:           ✅ Correct result for unauthenticated API
```

---

## Verify the Diagnosis (30 seconds)

```bash
# Show detailed API responses
python external_enrichment.py \
    --data data/project_c_samples.parquet \
    --skip-google \
    --debug 2>&1 | head -50
```

Expected output:
```
[DEBUG] Query: name='SNT Biotech Lab' juris='us_il'
        Status: 401
        ⚠ 401 Unauthorized: Invalid Api Token. Please check your OpenCorporates account
```

This confirms: **Data is fine, API authentication is missing.**

---

## Fix Option 1: Get OpenCorporates API Key (Recommended)

### Step 1: Get API Key
- Visit: https://opencorporates.com/api
- Check: Do they support your country? (US is supported ✅)
- Sign up for API access
- Copy your API token

### Step 2: Run with API Key
```bash
python external_enrichment.py \
    --data data/project_c_samples.parquet \
    --opencorp-key YOUR_API_KEY_HERE \
    --skip-google
```

Expected result:
```
[A] OpenCorporates enrichment (US records only) …
    Using API key: [first 10 chars]...
    
    Matched: 2,500-3,000 / 3,337 US records
    Dissolved: ~100 | Active: ~2,500-3,000
    Unknown: ~200-500
```

**Timeline**: ~15 minutes (3,337 records × 0.15s per record + API response times)

---

## Fix Option 2: Use Free Tier (If Available)

If OpenCorporates free tier works without key:
```bash
python external_enrichment.py \
    --data data/project_c_samples.parquet \
    --skip-google
    # (no --opencorp-key needed)
```

**Status**: Currently returns 401, so free tier likely not available or requires key anyway.

---

## Fix Option 3: Skip OpenCorporates (Not Recommended)

If you can't get API access:
```bash
python external_enrichment.py \
    --data data/project_c_samples.parquet \
    --skip-opencorp \
    --skip-google
```

**Result**: No external enrichment (all OC/Google features will be null)

---

## Understanding the Changes Made

### 1. Diagnostic Output (New)
Shows extraction quality at startup so you can verify data is fine:
```
Preprocessing quality:
  _name: 3,425 populated, 0 empty        ← 100% success
  _region: 3,337 extracted, 88 null      ← 97.4% success
```

### 2. API Key Support (New)
```bash
--opencorp-key YOUR_KEY    ← Add this to authenticate
```

Function signature:
```python
def query_opencorporates(name, jurisdiction, api_key=None, ...)
```

### 3. Better Error Messages (Enhanced)
```
Before: [Silent failure, oc_matched = 0]
After:  ⚠ 401 Unauthorized: Invalid Api Token
        💡 Tip: Run with --opencorp-key YOUR_KEY
```

### 4. Debug Mode (New)
```bash
--debug    ← Show first 10 API request/response details
```

---

## Expected Results After Fix

### With API Key:
```
OpenCorporates:    ✅ 2,500-3,000+ matches
Google Places:     ⏳ (Requires separate --google-key)
Output:
  - opencorp_features.parquet      (3,337 rows, OC data)
  - enrichment_combined.parquet    (joined OC + Google data)
```

### With --skip-opencorp:
```
OpenCorporates:    ⏭️  Skipped
Output:
  - opencorp_features.parquet      (null for all records)
  - enrichment_combined.parquet    (only Google data if key provided)
```

---

## Files Reference

| File | Purpose |
|------|---------|
| `external_enrichment.py` | Main pipeline (UPDATED) |
| `DIAGNOSTIC_FINDINGS.md` | Detailed technical analysis |
| `diagnose_extraction.py` | Data extraction diagnostic |
| `diagnose_api.py` | API response diagnostic |

---

## Troubleshooting

### "Still getting zero matches even with API key"
- Verify API key is correct: `--opencorp-key YOUR_KEY`
- Run with `--debug` to see exact error message
- Check OpenCorporates API status page

### "Command not found: python"
- Activate venv first: `. .venv/Scripts/Activate`
- Or: `python -m external_enrichment ...`

### "Getting 429 (Rate Limited)"
- API calls slow automatically (0.15s between requests = ~6/sec)
- Run during off-hours if needed
- Contact OpenCorporates for higher rate limit

### "All records still null after running"
- Check output shows `Matched: X / 3,337`
- If X = 0, still getting 401 → API key issue
- If X > 0, check output parquets are being written

---

## Next Steps

1. **Verify diagnosis**: Run with `--debug` and confirm 401 errors
2. **Get API key**: Sign up at https://opencorporates.com/api (5 minutes)
3. **Run with key**: `python external_enrichment.py --data ... --opencorp-key YOUR_KEY`
4. **Check results**: Review `outputs/opencorp_features.parquet`
5. **Integrate**: Merge enriched features into your `train_competition.py`

---

## Summary

| Step | Status | Next Action |
|------|--------|-------------|
| Data preprocessing | ✅ Fixed & verified | No action needed |
| API authentication | ⚠️ Missing | Get OpenCorporates API key |
| API key support | ✅ Added to code | Use `--opencorp-key` flag |
| Diagnostic output | ✅ Added | Run with `--debug` to verify |
| Error messages | ✅ Improved | Clearer now when auth fails |

**Estimated time to full fix**: 20-30 minutes (5 min API signup + 15-25 min to run enrichment)
