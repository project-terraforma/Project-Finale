"""
Quick diagnostic to reveal data structure and extraction issues
"""

import pandas as pd
import json

# Load data
df = pd.read_parquet("data/project_c_samples.parquet")
print("=" * 80)
print("DATA STRUCTURE ANALYSIS")
print("=" * 80)
print(f"\nShape: {len(df):,} rows × {df.shape[1]} columns")
print(f"Columns: {df.columns.tolist()}\n")

# ─── 1. NAMES STRUCTURE ─────────────────────────────────────────────────────
print("[1] NAMES COLUMN")
print("-" * 80)
sample = df["names"].iloc[0]
print(f"Type: {type(sample).__name__}")
print(f"Sample (first 3):")
for i in range(min(3, len(df))):
    val = df["names"].iloc[i]
    print(f"  [{i}] Type={type(val).__name__:6} Value={repr(str(val)[:100])}")
    if isinstance(val, (dict, list)):
        if isinstance(val, dict):
            print(f"      Keys: {list(val.keys())}")
            if "primary" in val:
                print(f"        primary={repr(val['primary'])}")
        elif isinstance(val, list) and len(val) > 0:
            print(f"      [0] type={type(val[0]).__name__} keys={list(val[0].keys()) if isinstance(val[0], dict) else 'N/A'}")

# ─── 2. ADDRESSES & REGION ──────────────────────────────────────────────────
print("\n[2] ADDRESSES & REGION STRUCTURE")
print("-" * 80)
sample = df["addresses"].iloc[0]
print(f"Type: {type(sample).__name__}")
print(f"First address item (repr): {repr(sample[0] if sample else None)[:150]}")

def extract_region(addr):
    try:
        return addr[0]["region"] if addr and len(addr) > 0 else None
    except:
        return None

regions = df["addresses"].apply(extract_region)
print(f"\nRegion values (all unique):")
for r in sorted(regions.dropna().unique()):
    count = (regions == r).sum()
    print(f"  '{r}' — {count:6,} records")

# ─── 3. TEST EXTRACTION GATE ────────────────────────────────────────────────
print("\n[3] CURRENT EXTRACTION LOGIC TEST")
print("-" * 80)

# Current code (assumes names is dict)
extracted_names = df["names"].apply(
    lambda x: x.get("primary", "") if isinstance(x, dict) else ""
)
print(f"Names extracted (current logic):")
print(f"  Empty strings: {(extracted_names.str.len() == 0).sum():,}")
print(f"  Non-empty: {(extracted_names.str.len() > 0).sum():,}")
if (extracted_names.str.len() > 0).sum() > 0:
    print(f"  Samples: {extracted_names[extracted_names.str.len() > 0].head(3).tolist()}")

# Region to jurisdiction
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

print(f"Jurisdictions (current logic):")
print(f"  Valid lookup: {jurisdictions.notna().sum():,}")
print(f"  Failed (None): {jurisdictions.isna().sum():,}")
print(f"  Failed region samples: {regions_upper[(jurisdictions.isna()) & (regions_upper.str.len() > 0)].unique()[:5].tolist()}")

# API gate
has_name = extracted_names.str.len() > 0
has_juris = jurisdictions.notna()
passes_gate = has_name & has_juris
print(f"\nAPI Gate (name AND jurisdiction):")
print(f"  Passes: {passes_gate.sum():,} / {len(df):,}")
print(f"  FAILS: {(~passes_gate).sum():,}")

# ─── 4. COUNTRY ────────────────────────────────────────────────────────────
print("\n[4] COUNTRY DISTRIBUTION")
print("-" * 80)
def extract_country(addr):
    try:
        return addr[0]["country"] if addr and len(addr) > 0 else None
    except:
        return None

countries = df["addresses"].apply(extract_country)
for c, cnt in countries.value_counts().head(10).items():
    print(f"  {str(c):15} — {cnt:6,}")

print("\n" + "=" * 80)
