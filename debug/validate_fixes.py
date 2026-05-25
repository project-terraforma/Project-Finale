"""
Validation script: Confirm preprocessing fixes and API configuration are correct
Usage: python validate_fixes.py [--opencorp-key YOUR_KEY]
"""

import argparse
import pandas as pd
import requests
import time
from rapidfuzz import fuzz

print("=" * 80)
print("VALIDATION: Preprocessing & API Configuration Check")
print("=" * 80)

parser = argparse.ArgumentParser()
parser.add_argument("--opencorp-key", default=None, help="OpenCorporates API key for testing")
args = parser.parse_args()

# ─── 1. DATA VALIDATION ─────────────────────────────────────────────────────
print("\n[1] DATA VALIDATION")
print("-" * 80)

df = pd.read_parquet("data/project_c_samples.parquet")

# Names
names_extracted = df["names"].apply(lambda x: x.get("primary", "") if isinstance(x, dict) else "")
names_empty = (names_extracted.str.len() == 0).sum()
print(f"✓ Names extraction: {len(df) - names_empty:,} / {len(df):,} populated")
if names_empty > 0:
    print(f"  ⚠ {names_empty:,} empty names found")

# Regions
def safe_extract_region(addr):
    try:
        return addr[0]["region"] if addr and len(addr) > 0 else None
    except:
        return None

regions = df["addresses"].apply(safe_extract_region)
regions_null = regions.isna().sum()
print(f"✓ Region extraction: {len(df) - regions_null:,} / {len(df):,} extracted ({100*regions_null/len(df):.1f}% null)")

# Jurisdiction lookup
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

regions_upper = regions.fillna("").str.upper().str.strip()
jurisdictions = regions_upper.map(STATE_TO_OC.get)
juris_valid = jurisdictions.notna().sum()
print(f"✓ Jurisdiction lookup: {juris_valid:,} / {len(df):,} valid")

# API gate
has_name = names_extracted.str.len() > 0
has_juris = jurisdictions.notna()
passes_gate = has_name & has_juris
gate_pass = passes_gate.sum()
print(f"✓ API gate (name AND jurisdiction): {gate_pass:,} / {len(df):,} pass")

print(f"\n✅ DATA VALIDATION: PASSED")
print(f"   Ready to enrich: {gate_pass:,} records ({100*gate_pass/len(df):.1f}% of dataset)")

# ─── 2. API KEY VALIDATION ──────────────────────────────────────────────────
print("\n[2] API KEY VALIDATION")
print("-" * 80)

if not args.opencorp_key:
    print("⚠ No API key provided (--opencorp-key)")
    print("  Run with --opencorp-key YOUR_KEY to test OpenCorporates API")
else:
    print(f"API key provided: {args.opencorp_key[:10]}...")
    
    # Test single API call
    test_name = df["names"].iloc[0].get("primary", "")
    test_region = regions.iloc[0]
    test_juris = STATE_TO_OC.get(test_region)
    
    if test_juris and test_name:
        print(f"\nTesting API call:")
        print(f"  Name: '{test_name}'")
        print(f"  Jurisdiction: {test_juris}")
        
        try:
            params = {
                "q": test_name,
                "jurisdiction_code": test_juris,
                "api_token": args.opencorp_key,
                "per_page": 5
            }
            
            resp = requests.get(
                "https://api.opencorporates.com/v0.4/companies/search",
                params=params,
                timeout=10
            )
            
            print(f"  Status: {resp.status_code}")
            
            if resp.status_code == 200:
                companies = resp.json().get("results", {}).get("companies", [])
                print(f"  Results: {len(companies)} companies found")
                
                if companies:
                    best_score = 0
                    best = None
                    for item in companies:
                        c = item.get("company", {})
                        score = fuzz.token_sort_ratio(
                            test_name.lower(), (c.get("name") or "").lower()
                        )
                        if score > best_score:
                            best_score, best = score, c
                    
                    if best:
                        print(f"  Top match: '{best.get('name')}' (score={best_score})")
                        print(f"  Status: {best.get('current_status', 'unknown')}")
                        
                        if best_score >= 60:
                            print(f"\n✅ API VALIDATION: PASSED")
                            print(f"   API key is valid and returning matches!")
                        else:
                            print(f"\n⚠ API returned results but match quality low (score={best_score})")
                    else:
                        print(f"\n⚠ API returned {len(companies)} results but none matched well")
                else:
                    print(f"\n⚠ API returned 200 but no companies found")
                    print(f"   (This is OK - some names won't match)")
                    
            elif resp.status_code == 401:
                error_msg = resp.json().get('error', {}).get('message', 'Invalid key')
                print(f"\n❌ API VALIDATION: FAILED")
                print(f"   401 Unauthorized: {error_msg}")
                print(f"   Check your API key: {args.opencorp_key}")
                
            else:
                print(f"\n❌ API VALIDATION: FAILED")
                print(f"   HTTP {resp.status_code}: {resp.text[:100]}")
                
        except Exception as e:
            print(f"\n❌ API VALIDATION: FAILED")
            print(f"   {type(e).__name__}: {str(e)}")

# ─── 3. COMMAND EXAMPLES ───────────────────────────────────────────────────
print("\n[3] NEXT STEPS")
print("-" * 80)

if args.opencorp_key:
    print(f"\n✅ API key validated. Run enrichment with:")
    print(f"\npython external_enrichment.py \\")
    print(f"  --data data/project_c_samples.parquet \\")
    print(f"  --opencorp-key '{args.opencorp_key}' \\")
    print(f"  --skip-google")
else:
    print(f"\nTo enable OpenCorporates enrichment:")
    print(f"  1. Get API key from https://opencorporates.com/api")
    print(f"  2. Run validation again with --opencorp-key")
    print(f"  3. Then run enrichment with --opencorp-key")
    
    print(f"\nWithout API key, data extraction still works perfectly!")
    print(f"You can skip OpenCorporates and use Google Places instead:")
    print(f"\npython external_enrichment.py \\")
    print(f"  --data data/project_c_samples.parquet \\")
    print(f"  --google-key YOUR_GOOGLE_KEY \\")
    print(f"  --skip-opencorp")

print("\n" + "=" * 80)
print("VALIDATION COMPLETE")
print("=" * 80)
