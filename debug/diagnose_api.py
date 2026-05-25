"""
Enhanced diagnostic: test actual OpenCorporates API calls and identify zero-match root cause
"""

import pandas as pd
import requests
from rapidfuzz import fuzz
import time
import json

df = pd.read_parquet("data/project_c_samples.parquet")

# Extract data
def extract_region(addr):
    try:
        return addr[0]["region"] if addr and len(addr) > 0 else None
    except:
        return None

# Current extraction
extracted_names = df["names"].apply(lambda x: x.get("primary", "") if isinstance(x, dict) else "")
extracted_regions = df["addresses"].apply(extract_region)

STATE_TO_OC = {
    "AL": "us_al", "AK": "us_ak", "AZ": "us_az", "AR": "us_ar", "CA": "us_ca",
    "CO": "us_co", "CT": "us_ct", "DE": "us_de", "FL": "us_fl", "GA": "us_ga",
    "HI": "us_hi", "ID": "us_id", "IL": "us_il", "IN": "us_in", "IA": "us_ia",
    "KS": "us_ks", "KY": "us_ky", "LA": "us_la", "ME": "us_me", "MD": "us_md",
    "MA": "us_ma", "MI": "us_mi", "MN": "us_mn", "MS": "us_ms", "MO": "us_mo",
    "MT": "us_mt", "NE": "us_ne", "NV": "us_nv", "NH": "us_nh", "NJ": "us_nj",
    "NM": "us_nm", "NY": "us_ny", "NC": "us_nc", "ND": "us_nd", "OH": "us_oh",
    "OK": "us_ok", "OR": "us_or", "PA": "us_pa", "RI": "us_ri", "SC": "us_sc",
    "SD": "us_sd", "TN": "us_tn", "TX": "us_tx", "UT": "us_ut", "VT": "us_vt",
    "VA": "us_va", "WA": "us_wa", "WV": "us_wv", "WI": "us_wi", "WY": "us_wy",
    "DC": "us_dc",
}

regions_upper = extracted_regions.fillna("").str.upper().str.strip()
jurisdictions = regions_upper.map(STATE_TO_OC.get)

# Find records that pass API gate
has_name = extracted_names.str.len() > 0
has_juris = jurisdictions.notna()
passes_gate = has_name & has_juris

print("=" * 80)
print("OPENCORPORATES API TEST")
print("=" * 80)

# ─── 1. Show 88 failing records ─────────────────────────────────────────────
print("\n[1] 88 RECORDS WITH INVALID REGION (fail API gate)")
print("-" * 80)
failing = df[~passes_gate].copy()
failing["_extracted_region"] = extracted_regions[~passes_gate]
print(f"Total failing: {len(failing)}")
print(f"\nRegion values in failing records:")
for r in failing["_extracted_region"].value_counts().items():
    print(f"  {repr(r[0]):20} — {r[1]:4,} records")

# ─── 2. Test 5 API calls with passing records ──────────────────────────────
print("\n[2] TEST ACTUAL OPENCORPORATES API CALLS (first 5 passing records)")
print("-" * 80)

test_df = df[passes_gate].head(5).copy()
test_df["_name"] = extracted_names[passes_gate].head(5).values
test_df["_region"] = extracted_regions[passes_gate].head(5).values
test_df["_jurisdiction"] = jurisdictions[passes_gate].head(5).values

for idx, (_, row) in enumerate(test_df.iterrows()):
    name = row["_name"]
    jurisdiction = row["_jurisdiction"]
    
    print(f"\n[Test {idx+1}] name='{name[:50]}' jurisdiction='{jurisdiction}'")
    
    try:
        base = "https://api.opencorporates.com/v0.4/companies/search"
        params = {"q": name, "jurisdiction_code": jurisdiction, "per_page": 5}
        
        print(f"  URL: {base}")
        print(f"  Params: {params}")
        
        resp = requests.get(base, params=params, timeout=10)
        print(f"  Status: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            companies = data.get("results", {}).get("companies", [])
            print(f"  Results: {len(companies)} companies found")
            
            if companies:
                for i, item in enumerate(companies[:2]):
                    c = item.get("company", {})
                    c_name = c.get("name", "")
                    score = fuzz.token_sort_ratio(name.lower(), c_name.lower())
                    status = c.get("current_status", "unknown")
                    print(f"    [{i}] name='{c_name[:40]}' score={score} status='{status}'")
            else:
                print(f"    (No companies returned)")
        else:
            print(f"  Error: {resp.status_code}")
            if resp.status_code == 429:
                print(f"    ⚠ RATE LIMITED")
            print(f"  Response: {resp.text[:200]}")
        
        time.sleep(0.5)  # Rate limit
        
    except Exception as e:
        print(f"  Exception: {type(e).__name__}: {str(e)[:100]}")

# ─── 3. Check if problem is 60% name match threshold ──────────────────────
print("\n[3] NAME MATCH SCORE ANALYSIS (current threshold: 60)")
print("-" * 80)

# Manually query a few and check match scores
test_cases = [
    ("SNT Biotech Lab", "us_il"),
    ("Debbie's Doula Services", "us_il"),
    ("Wax Custom Communications", "us_il"),
]

print("\nManually testing fuzzy match scores for sample queries:")
for name, juris in test_cases:
    try:
        resp = requests.get(
            "https://api.opencorporates.com/v0.4/companies/search",
            params={"q": name, "jurisdiction_code": juris, "per_page": 5},
            timeout=10
        )
        if resp.status_code == 200:
            companies = resp.json().get("results", {}).get("companies", [])
            print(f"\nQuery: '{name}' in {juris}")
            print(f"  API returned: {len(companies)} results")
            
            if companies:
                for i, item in enumerate(companies[:3]):
                    c = item.get("company", {})
                    c_name = c.get("name", "")
                    score = fuzz.token_sort_ratio(name.lower(), c_name.lower())
                    print(f"    {i+1}. '{c_name}' — score={score}  {'✓ PASS' if score >= 60 else '✗ FAIL'}")
            else:
                print(f"  ⚠ ZERO results from API")
        else:
            print(f"\n'{name}': HTTP {resp.status_code}")
        
        time.sleep(0.5)
    except Exception as e:
        print(f"\n'{name}': {type(e).__name__}: {str(e)[:50]}")

print("\n" + "=" * 80)
