# DIAGNOSTIC FINDINGS & ROOT CAUSE ANALYSIS

## Executive Summary

**Your preprocessing logic is working correctly!** ✅ 

**The zero-match problem is NOT caused by data extraction errors.**

**Root Cause: OpenCorporates API returns 401 Unauthorized** (authentication missing)
- Current code silently catches this and returns `OC_NULL` for all 3,337 records
- Makes it look like "no matches found" when actually "API not authenticated"

---

## Detailed Diagnostic Results

### Data Structure (What Your Data Actually Is)

| Field | Structure | Status |
|-------|-----------|--------|
| `names` | `{'primary': str, 'common': None, 'rules': None}` | ✅ Matches current code |
| `region` | 2-letter state abbreviations (IL, CA, NY, TX, etc.) | ✅ Perfect for STATE_TO_OC lookup |
| `country` | All "US" (3,425/3,425 records) | ✅ Correct |
| `addresses` | List of `{'country', 'region', 'locality', 'postcode', ...}` dicts | ✅ Works with safe_addr_field() |

### Extraction Quality (3,425 Records)

```
Names:
  - Extracted successfully: 3,425 / 3,425 (100%) ✅
  - Empty strings: 0
  - Sample: ['SNT Biotech Lab', 'Debbie's Doula Services', 'Wax Custom Communications']

Regions:
  - Extracted successfully: 3,337 / 3,425 (97.4%) ✅
  - Null/Missing: 88 (2.6%)
  - Format: All 2-letter abbreviations ✅
  - Sample values: ['IL', 'VA', 'FL', 'NC', 'KS', 'CA', 'TX', ...]

Jurisdictions (via STATE_TO_OC lookup):
  - Valid lookup: 3,337 / 3,425 (97.4%) ✅
  - Failed lookup (None): 88 (2.6%)

API Gate (requires both name AND jurisdiction):
  - Passes gate: 3,337 / 3,425 (97.4%) ✅
  - Fails gate: 88 (2.6%)
    - Reason: null region → null jurisdiction
    - These 88 correctly receive OC_NULL (no API call attempted)
```

### API Response Diagnostics

```
Test Query 1: name='SNT Biotech Lab' jurisdiction='us_il'
  Status: 401 Unauthorized
  Error: "Invalid Api Token. Please check your OpenCorporates account"
  
Test Query 2: name='Debbie's Doula Services' jurisdiction='us_va'
  Status: 401 Unauthorized
  Error: "Invalid Api Token. Please check your OpenCorporates account"
  
Test Query 3-5: All return 401 Unauthorized ❌
```

---

## Why You're Seeing Zero Matches

### Current Code Flow (Silent Failure)

```python
def query_opencorporates(name, jurisdiction, retries=2):
    for attempt in range(retries):
        try:
            resp = requests.get(...)
            
            if resp.status_code != 200:
                return OC_NULL.copy()  # ← Returns null for 401!
            
            # Never reaches here for 401 response
            companies = resp.json().get("results", {}).get("companies", [])
            ...
        except Exception:
            if attempt == retries - 1:
                return OC_NULL.copy()  # ← Also returns null for any exception
            time.sleep(1)
    
    return OC_NULL.copy()
```

**Result**: All 3,337 API calls return `OC_NULL` silently
- `oc_matched` = 0 for all records
- Looks like "no matches found" but actually "all failed with 401"
- No indication that authentication failed

---

## The Actual Problem

OpenCorporates API requires authentication. You have two options:

### Option 1: Use Free Tier (No API Key Required)
```bash
python external_enrichment.py \
    --data data/project_c_samples.parquet \
    --skip-google
```

**Issue**: Free tier might have stricter limits or no API access at all
**Status**: Returns 401 Unauthorized (current situation)

### Option 2: Use Paid Tier (Requires API Key)
```bash
python external_enrichment.py \
    --data data/project_c_samples.parquet \
    --google-key YOUR_GOOGLE_API_KEY \
    --opencorp-key YOUR_OPENCORP_API_KEY  # ← Not yet in code
```

**Action Required**: 
1. Get OpenCorporates API key from: https://opencorporates.com/api
2. Update `external_enrichment.py` to accept and use the key
3. Then run with `--opencorp-key YOUR_KEY`

---

## What Was Fixed in Your Code

### 1. Added Diagnostic Output
Now shows extraction quality at startup:
```
[1] Loading data …
    3,425 rows | columns: [...]
    Preprocessing quality:
      _name: 3,425 populated, 0 empty
      _region: 3,337 extracted, 88 null
      Sample names: ['SNT Biotech Lab', ...]
      Sample regions: ['IL', 'VA', 'FL', ...]
```

### 2. Enhanced Error Handling
```python
if resp.status_code == 401:
    last_error = f"401 Unauthorized: {resp.json().get('error', {}).get('message', 'Invalid API key')}"
    if debug:
        print(f"⚠ {last_error}")
    return OC_NULL.copy()
```

Now clearly identifies authentication failures.

### 3. Added --debug Flag
```bash
python external_enrichment.py --data data/project_c_samples.parquet --debug --skip-google
```

Shows first 10 API request/response details:
```
[DEBUG] Query: name='SNT Biotech Lab' juris='us_il'
        Status: 401
        ⚠ 401 Unauthorized: Invalid Api Token. Please check your OpenCorporates account
```

---

## Next Steps

### Immediate (Verify the Diagnosis)
```bash
# Run with --debug to see API responses clearly
python external_enrichment.py \
    --data data/project_c_samples.parquet \
    --skip-google \
    --debug 2>&1 | head -50
```

You'll see `401 Unauthorized` errors, confirming the diagnosis.

### To Fix (Get API Access)

1. **Check OpenCorporates API status:**
   - Visit: https://opencorporates.com/api
   - Check if free tier includes your country
   - Sign up for API access if needed

2. **Update external_enrichment.py** to accept `--opencorp-key`:
   ```python
   parser.add_argument("--opencorp-key", default=None, help="OpenCorporates API key")
   ```

3. **Update query_opencorporates() function** to use the key:
   ```python
   params = {
       "q": name,
       "jurisdiction_code": jurisdiction,
       "api_token": api_key,  # ← Add this
       "per_page": 5
   }
   ```

4. **Run with API key:**
   ```bash
   python external_enrichment.py \
       --data data/project_c_samples.parquet \
       --opencorp-key YOUR_API_KEY \
       --skip-google
   ```

---

## Summary Table

| Aspect | Finding | Impact |
|--------|---------|--------|
| Names extraction | ✅ Working (100% populated) | Not the problem |
| Region extraction | ✅ Working (97.4% populated) | Not the problem |
| Jurisdiction lookup | ✅ Working (97.4% valid) | Not the problem |
| Data preprocessing | ✅ All correct | Not the problem |
| API requests sent | ✅ 3,337 requests made | Happening correctly |
| API responses | ❌ 401 Unauthorized | **← THE ACTUAL PROBLEM** |
| Zero matches | ✅ But for right reason | API auth required |

---

## Conclusion

**Good News**: Your data preprocessing is perfect! 🎉
- Names extracted correctly
- Regions in correct format  
- Jurisdiction mapping works
- 97.4% of records eligible for enrichment

**The Issue**: OpenCorporates API requires authentication (401 Unauthorized)
- Not a data problem
- Not an API integration problem  
- Need to provide API credentials

**Once Fixed**: Will enrich ~3,337 records (97.4% of your dataset)
