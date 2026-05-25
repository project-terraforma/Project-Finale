"""
external_enrichment.py — Two enrichment pipelines for project_c
================================================================

Handles both file schemas:
  - project_c_samples.parquet  (id, names dict, ...)
  - sample-open-prediction.parquet  (record_id, primary_name, ...)

Auto-detects schema and normalizes internally.

Pipeline A: OpenCorporates (free, all 50 US states)
    - US records only (non-US skipped automatically)
    - Legal entity status: active / dissolved / unknown
    - Formation date → company age in days

Pipeline B: Google Places API (New) — uncertain US records only
    - Uncertain = confidence < conf_max AND stale/missing Microsoft data
    - business_status: OPERATIONAL / CLOSED_TEMPORARILY / CLOSED_PERMANENTLY
    - Two-step: Text Search (find place_id) → Place Details (get status)

Usage:
    pip install requests pandas pyarrow rapidfuzz tqdm

    # OpenCorporates only (no Google key needed)
    python external_enrichment.py \\
        --data data/sample-open-prediction.parquet \\
        --skip-google

    # Both pipelines
    python external_enrichment.py \\
        --data data/sample-open-prediction.parquet \\
        --google-key YOUR_API_KEY

Outputs:
    outputs/opencorp_features.parquet
    outputs/google_features.parquet
    outputs/enrichment_combined.parquet  ← join this on 'record_id'
"""

import argparse
import time
import warnings
from pathlib import Path

import pandas as pd
import numpy as np
import requests
from rapidfuzz import fuzz
from tqdm import tqdm

warnings.filterwarnings("ignore")

# ── CLI ────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--data", default="data/sample-open-prediction.parquet")
parser.add_argument("--out", default="outputs")
parser.add_argument("--google-key", default=None)
parser.add_argument(
    "--opencorp-key",
    default=None,
    help="OpenCorporates API key (optional for free tier)",
)
parser.add_argument("--skip-google", action="store_true")
parser.add_argument("--skip-opencorp", action="store_true")
parser.add_argument(
    "--debug",
    action="store_true",
    help="Show API request/response details for first 10 calls",
)
parser.add_argument(
    "--conf-max",
    type=float,
    default=0.75,
    help="Confidence ceiling for 'uncertain' (default: 0.75)",
)
parser.add_argument(
    "--msft-age-min",
    type=int,
    default=365,
    help="Min MSFT staleness days for 'uncertain' (default: 365)",
)
parser.add_argument(
    "--google-limit",
    type=int,
    default=5000,
    help="Max Google API calls — budget guard (default: 5000)",
)
args = parser.parse_args()

OUT = Path(args.out)
OUT.mkdir(parents=True, exist_ok=True)

print("=" * 65)
print("EXTERNAL ENRICHMENT  (OpenCorporates + Google Places)")
print("=" * 65)


# ── 1. LOAD & NORMALISE SCHEMA ─────────────────────────────────────────────────
print("\n[1] Loading data …")
df = pd.read_parquet(args.data)
print(f"    {len(df):,} rows  |  columns: {df.columns.tolist()}")
print(f"\n    Data types:")
for col in df.columns[:8]:
    print(f"      {col:20} {df[col].dtype}")


# ── Schema detection ──────────────────────────────────────────────────────────
# Schema A: sample-open-prediction  → record_id, primary_name
# Schema B: project_c_samples       → id, names (dict)
if "record_id" in df.columns:
    df["_id"] = df["record_id"]
    df["_name"] = df["primary_name"].fillna("")
    print("    Detected schema: sample-open-prediction (record_id / primary_name)")
elif "id" in df.columns:
    df["_id"] = df["id"]
    df["_name"] = df["names"].apply(
        lambda x: x.get("primary", "") if isinstance(x, dict) else ""
    )
    print("    Detected schema: project_c_samples (id / names dict)")
else:
    raise ValueError("Cannot detect schema — no 'id' or 'record_id' column found.")


# ── Address extraction (handles both list and numpy array containers) ──────────
def safe_addr_field(addr, field):
    """Extract a field from the first address entry regardless of container type."""
    try:
        if addr is None or len(addr) == 0:
            return None
        return addr[0][field]
    except Exception:
        return None


df["_country"] = df["addresses"].apply(lambda x: safe_addr_field(x, "country"))
df["_region"] = df["addresses"].apply(lambda x: safe_addr_field(x, "region"))
df["_locality"] = df["addresses"].apply(lambda x: safe_addr_field(x, "locality"))
df["_postcode"] = df["addresses"].apply(lambda x: safe_addr_field(x, "postcode"))

# ── MSFT staleness (used for uncertainty filter) ───────────────────────────────
SNAPSHOT = pd.Timestamp("2025-02-24", tz="UTC")


def get_msft_update(sources):
    try:
        for s in sources:
            if s["dataset"] == "Microsoft":
                return s.get("update_time")
    except Exception:
        pass
    return None


df["_msft_update_dt"] = pd.to_datetime(
    df["sources"].apply(get_msft_update), errors="coerce", utc=True
)
df["_msft_age_days"] = (SNAPSHOT - df["_msft_update_dt"]).dt.days.fillna(-1)

# Country breakdown
country_counts = df["_country"].value_counts(dropna=False)
print(f"\n    Country breakdown (top 10):")
for country, count in country_counts.head(10).items():
    print(f"      {country or 'unknown':>10}  {count:>6,}")

us_df = df[df["_country"] == "US"].copy()
non_us_df = df[df["_country"] != "US"].copy()
print(f"\n    US records:     {len(us_df):>6,}  (eligible for OpenCorporates + Google)")
print(
    f"    Non-US records: {len(non_us_df):>6,}  (will receive null enrichment features)"
)

# ── PREPROCESSING DIAGNOSTIC (Show extraction quality) ────────────────────────
print(f"\n    Preprocessing quality:")
name_empty = (df["_name"].fillna("").str.len() == 0).sum()
name_populated = len(df) - name_empty
print(f"      _name: {name_populated:,} populated, {name_empty:,} empty")
print(f"      Sample names (first 3): {df['_name'].dropna().head(3).tolist()}")

region_null = df["_region"].isna().sum()
region_populated = len(df) - region_null
print(f"      _region: {region_populated:,} extracted, {region_null:,} null")

if region_null == 0:
    print(f"      Sample regions: {sorted(df['_region'].unique())[:5]}")
else:
    print(
        f"      Sample regions: {df[df['_region'].notna()]['_region'].unique()[:5].tolist()}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE A — OpenCorporates (US records only, free)
# ══════════════════════════════════════════════════════════════════════════════

STATE_TO_OC = {
    "AL": "us_al",
    "AK": "us_ak",
    "AZ": "us_az",
    "AR": "us_ar",
    "CA": "us_ca",
    "CO": "us_co",
    "CT": "us_ct",
    "DE": "us_de",
    "FL": "us_fl",
    "GA": "us_ga",
    "HI": "us_hi",
    "ID": "us_id",
    "IL": "us_il",
    "IN": "us_in",
    "IA": "us_ia",
    "KS": "us_ks",
    "KY": "us_ky",
    "LA": "us_la",
    "ME": "us_me",
    "MD": "us_md",
    "MA": "us_ma",
    "MI": "us_mi",
    "MN": "us_mn",
    "MS": "us_ms",
    "MO": "us_mo",
    "MT": "us_mt",
    "NE": "us_ne",
    "NV": "us_nv",
    "NH": "us_nh",
    "NJ": "us_nj",
    "NM": "us_nm",
    "NY": "us_ny",
    "NC": "us_nc",
    "ND": "us_nd",
    "OH": "us_oh",
    "OK": "us_ok",
    "OR": "us_or",
    "PA": "us_pa",
    "RI": "us_ri",
    "SC": "us_sc",
    "SD": "us_sd",
    "TN": "us_tn",
    "TX": "us_tx",
    "UT": "us_ut",
    "VT": "us_vt",
    "VA": "us_va",
    "WA": "us_wa",
    "WV": "us_wv",
    "WI": "us_wi",
    "WY": "us_wy",
    "DC": "us_dc",
}

ACTIVE_STATUSES = {
    "active",
    "good standing",
    "in good standing",
    "registered",
    "current",
    "subsisting",
    "live",
    "existing",
}
DISSOLVED_STATUSES = {
    "dissolved",
    "cancelled",
    "revoked",
    "forfeited",
    "inactive",
    "withdrawn",
    "terminated",
    "expired",
    "delinquent",
    "struck off",
    "administratively dissolved",
    "involuntarily dissolved",
}


def oc_status_label(raw):
    if not raw:
        return "unknown"
    s = raw.lower().strip()
    if any(a in s for a in ACTIVE_STATUSES):
        return "active"
    if any(d in s for d in DISSOLVED_STATUSES):
        return "dissolved"
    return "unknown"


OC_NULL = {
    "oc_matched": 0,
    "oc_status_raw": None,
    "oc_status": "unknown",
    "oc_name_score": 0,
    "oc_incorporation_date": None,
    "oc_dissolution_date": None,
    "oc_company_age_days": np.nan,
    "oc_is_dissolved": 0,
    "oc_is_active": 0,
}


def query_opencorporates(name, jurisdiction, api_key=None, retries=2, debug=False):
    base = "https://api.opencorporates.com/v0.4/companies/search"
    params = {"q": name, "jurisdiction_code": jurisdiction, "per_page": 5}

    # Add API key if provided
    if api_key:
        params["api_token"] = api_key

    last_error = None

    for attempt in range(retries):
        try:
            resp = requests.get(base, params=params, timeout=10)

            # Debug first call attempt
            if debug and attempt == 0:
                print(f"    [DEBUG] Query: name='{name[:40]}' juris='{jurisdiction}'")
                print(f"            Status: {resp.status_code}")

            # Handle authentication errors
            if resp.status_code == 401:
                error_msg = "Invalid API key or no authentication provided"
                try:
                    error_msg = resp.json().get("error", {}).get("message", error_msg)
                except:
                    pass
                last_error = f"401 Unauthorized: {error_msg}"
                if debug:
                    print(f"            ⚠ {last_error}")
                    if not api_key:
                        print(f"            💡 Tip: Run with --opencorp-key YOUR_KEY")
                return OC_NULL.copy()

            # Handle rate limiting
            if resp.status_code == 429:
                if debug and attempt == 0:
                    print(f"            ⚠ 429 Rate Limited — backing off")
                time.sleep(2**attempt * 2)
                continue

            # Handle other errors
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                if debug:
                    print(f"            Error: {last_error}")
                return OC_NULL.copy()

            # Parse successful response
            companies = resp.json().get("results", {}).get("companies", [])
            if debug and attempt == 0:
                print(f"            Results: {len(companies)} companies found")

            if not companies:
                return OC_NULL.copy()

            # Find best match by fuzzy name matching
            best_score, best = 0, None
            for item in companies:
                c = item.get("company", {})
                score = fuzz.token_sort_ratio(
                    name.lower(), (c.get("name") or "").lower()
                )
                if score > best_score:
                    best_score, best = score, c

            # Check if match quality passes threshold (60)
            if best_score < 60 or best is None:
                if debug and attempt == 0 and best_score > 0:
                    print(f"            Match score {best_score} < 60 threshold")
                return OC_NULL.copy()

            # Extract data from successful match
            raw_status = best.get("current_status") or ""
            inc_date = best.get("incorporation_date")
            dis_date = best.get("dissolution_date")
            age_days = np.nan
            if inc_date:
                try:
                    age_days = (
                        SNAPSHOT.tz_localize(None) - pd.to_datetime(inc_date)
                    ).days
                except Exception:
                    pass

            normalized = oc_status_label(raw_status)

            if debug and attempt == 0:
                print(
                    f"            ✓ Matched: '{best.get('name')[:40]}' score={best_score} status='{normalized}'"
                )

            return {
                "oc_matched": 1,
                "oc_status_raw": raw_status,
                "oc_status": normalized,
                "oc_name_score": best_score,
                "oc_incorporation_date": inc_date,
                "oc_dissolution_date": dis_date,
                "oc_company_age_days": age_days,
                "oc_is_dissolved": int(normalized == "dissolved"),
                "oc_is_active": int(normalized == "active"),
            }
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:50]}"
            if attempt == retries - 1:
                if debug:
                    print(f"            Exception: {last_error}")
                return OC_NULL.copy()
            time.sleep(1)

    return OC_NULL.copy()


if not args.skip_opencorp:
    print("\n[A] OpenCorporates enrichment (US records only) …")

    if args.opencorp_key:
        print(f"    Using API key: {args.opencorp_key[:10]}...")
    else:
        print(f"    No API key provided (--opencorp-key) — will attempt free tier")

    oc_results = []
    debug_count = 0

    for idx, (_, row) in enumerate(
        tqdm(df.iterrows(), total=len(df), desc="    OpenCorporates")
    ):
        rec = {"record_id": row["_id"]}

        # Skip non-US records — fill nulls immediately
        if row["_country"] != "US":
            rec.update(OC_NULL)
            oc_results.append(rec)
            continue

        name = row["_name"]
        state = (
            (str(row["_region"]) if pd.notna(row["_region"]) else "").upper().strip()
        )
        jurisdiction = STATE_TO_OC.get(state)

        if not name or not jurisdiction:
            rec.update(OC_NULL)
            oc_results.append(rec)
            continue

        # Enable debug logging for first N successful API attempts
        show_debug = args.debug and debug_count < 10
        if show_debug:
            debug_count += 1

        result = query_opencorporates(
            name, jurisdiction, api_key=args.opencorp_key, debug=show_debug
        )
        rec.update(result)
        oc_results.append(rec)
        time.sleep(0.15)  # ~6 req/s — within free rate limit

    oc_df = pd.DataFrame(oc_results)

    matched = oc_df["oc_matched"].sum()
    dissolved = oc_df["oc_is_dissolved"].sum()
    print(f"\n    Matched:   {matched:,} / {len(us_df):,} US records")
    print(f"    Dissolved: {dissolved:,}  |  Active: {oc_df['oc_is_active'].sum():,}")
    print(f"    Unknown:   {(oc_df['oc_status'] == 'unknown').sum():,}")
    print(f"    Non-US (nulled): {len(non_us_df):,}")

    if matched == 0 and not args.opencorp_key:
        print(
            f"\n    ⚠ Zero matches found. If using OpenCorporates API, provide --opencorp-key"
        )
        print(f"      See DIAGNOSTIC_FINDINGS.md for details")

    oc_path = OUT / "opencorp_features.parquet"
    oc_df.to_parquet(oc_path, index=False)
    print(f"    Saved → {oc_path}")

else:
    print("\n[A] Skipping OpenCorporates (--skip-opencorp)")
    oc_df = pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE B — Google Places API (uncertain US records only)
# ══════════════════════════════════════════════════════════════════════════════

GOOGLE_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

GOOGLE_NULL = {
    "google_matched": 0,
    "google_name_score": 0,
    "google_business_status": None,
    "google_is_permanently_closed": 0,
    "google_is_temporarily_closed": 0,
    "google_is_operational": 0,
    "google_rating": np.nan,
    "google_rating_count": np.nan,
    "google_has_hours": 0,
    "google_has_moved": 0,
}


def google_text_search(name, address, api_key):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.businessStatus",
    }
    body = {"textQuery": f"{name} {address}".strip(), "maxResultCount": 3}
    try:
        resp = requests.post(
            GOOGLE_TEXT_SEARCH_URL, json=body, headers=headers, timeout=10
        )
        if resp.status_code != 200:
            return None, None, None
        places = resp.json().get("places", [])
        if not places:
            return None, None, None
        top = places[0]
        return (
            top.get("id"),
            top.get("displayName", {}).get("text", ""),
            top.get("businessStatus"),
        )
    except Exception:
        return None, None, None


def google_place_details(place_id, api_key):
    url = GOOGLE_PLACE_DETAILS_URL.format(place_id=place_id)
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "id,displayName,businessStatus,rating,"
            "userRatingCount,regularOpeningHours,movedPlace,movedPlaceId"
        ),
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        return resp.json() if resp.status_code == 200 else {}
    except Exception:
        return {}


def parse_google_result(place_id, g_name, text_status, details, query_name):
    if not place_id:
        return GOOGLE_NULL.copy()
    status = details.get("businessStatus") or text_status or ""
    g_display = details.get("displayName", {}).get("text", g_name or "")
    name_score = fuzz.token_sort_ratio(query_name.lower(), (g_display or "").lower())
    if name_score < 50:
        return GOOGLE_NULL.copy()
    return {
        "google_matched": 1,
        "google_name_score": name_score,
        "google_business_status": status,
        "google_is_permanently_closed": int(status == "CLOSED_PERMANENTLY"),
        "google_is_temporarily_closed": int(status == "CLOSED_TEMPORARILY"),
        "google_is_operational": int(status == "OPERATIONAL"),
        "google_rating": details.get("rating", np.nan),
        "google_rating_count": details.get("userRatingCount", np.nan),
        "google_has_hours": int(bool(details.get("regularOpeningHours"))),
        "google_has_moved": int(bool(details.get("movedPlaceId"))),
    }


if not args.skip_google:
    if not args.google_key:
        print("\n[B] Skipping Google Places — no --google-key provided")
        google_df = pd.DataFrame()
    else:
        print(f"\n[B] Google Places enrichment (uncertain US records only) …")
        print(
            f"    Thresholds: confidence < {args.conf_max}"
            f"  AND  msft_age >= {args.msft_age_min} days (or no MSFT record)"
        )

        # Filter to uncertain US records only
        low_conf = df["confidence"].astype(float) < args.conf_max
        stale_msft = (df["_msft_age_days"] < 0) | (
            df["_msft_age_days"] >= args.msft_age_min
        )
        is_us = df["_country"] == "US"
        uncertain = df[is_us & low_conf & stale_msft].copy()

        print(f"    Uncertain US records: {len(uncertain):,} / {len(df):,} total")

        if len(uncertain) > args.google_limit:
            print(
                f"    Budget guard: capping at {args.google_limit:,} "
                f"(lowest confidence first)"
            )
            uncertain = uncertain.nsmallest(args.google_limit, "confidence")

        n = len(uncertain)
        est_cost = n * 2 * 0.017
        print(
            f"    Est. API calls: {n*2:,}  |  Est. cost: ${est_cost:.2f}"
            f"  |  Free credit used: {100*est_cost/200:.1f}% of $200/month"
        )

        google_results = []
        calls_made = 0

        for _, row in tqdm(
            uncertain.iterrows(), total=len(uncertain), desc="    Google Places"
        ):
            if calls_made >= args.google_limit:
                print("API limit reached — stopping Google enrichment safely.")
                break

            name = row["_name"]
            address = " ".join(
                str(x).strip()
                for x in [
                    row["_locality"],
                    row["_region"],
                    row["_postcode"],
                ]
                if pd.notna(x) and str(x).strip() != "" and str(x).lower() != "nan"
            )

            place_id, g_name, text_status = google_text_search(
                name, address, args.google_key
            )
            calls_made += 1
            time.sleep(0.05)

            details = {}
            if place_id:
                details = google_place_details(place_id, args.google_key)
                calls_made += 1
                time.sleep(0.05)

            result = parse_google_result(place_id, g_name, text_status, details, name)
            result["record_id"] = row["_id"]
            google_results.append(result)

        google_df = pd.DataFrame(google_results)

        print(f"\n    API calls made: {calls_made:,}")
        print(f"    Matched:              {google_df['google_matched'].sum():,}")
        print(
            f"    CLOSED_PERMANENTLY:   {google_df['google_is_permanently_closed'].sum():,}"
        )
        print(
            f"    CLOSED_TEMPORARILY:   {google_df['google_is_temporarily_closed'].sum():,}"
        )
        print(f"    OPERATIONAL:          {google_df['google_is_operational'].sum():,}")
        print(f"    No match / discarded: {(google_df['google_matched']==0).sum():,}")

        g_path = OUT / "google_features.parquet"
        google_df.to_parquet(g_path, index=False)
        print(f"    Saved → {g_path}")
else:
    print("\n[B] Skipping Google Places (--skip-google)")
    google_df = pd.DataFrame()


# ── Merge and save combined enrichment file ────────────────────────────────────
print("\n[Merge] Building combined enrichment file …")

combined = df[["_id"]].rename(columns={"_id": "record_id"}).copy()

if not oc_df.empty:
    combined = combined.merge(oc_df, on="record_id", how="left")

if not google_df.empty:
    combined = combined.merge(google_df, on="record_id", how="left")
    # Fill Google null features for records that weren't queried
    google_cols = [c for c in google_df.columns if c != "record_id"]
    for col in google_cols:
        if col not in combined.columns:
            combined[col] = np.nan

# Pre-compute interaction features
if "oc_is_dissolved" in combined.columns:
    combined["oc_dissolved_low_conf"] = (
        (combined["oc_is_dissolved"] == 1)
        & (df["confidence"].astype(float) < 0.75).values
    ).astype(int)

if "google_is_permanently_closed" in combined.columns:
    combined["google_closed_high_conf_conflict"] = (
        (combined["google_is_permanently_closed"] == 1)
        & (df["confidence"].astype(float) > 0.7).values
    ).astype(int)

if (
    "oc_is_dissolved" in combined.columns
    and "google_is_permanently_closed" in combined.columns
):
    combined["any_external_closed"] = (
        (combined["oc_is_dissolved"].fillna(0) == 1)
        | (combined["google_is_permanently_closed"].fillna(0) == 1)
    ).astype(int)

out_path = OUT / "enrichment_combined.parquet"
combined.to_parquet(out_path, index=False)
print(f"    Saved → {out_path}  ({len(combined):,} rows, {combined.shape[1]} cols)")

print("\n" + "=" * 65)
print("DONE")
print("=" * 65)
print("""
── HOW TO INTEGRATE INTO train_competition.py ───────────────────

# After loading df, before engineer_features():

enriched = pd.read_parquet("outputs/enrichment_combined.parquet")
df = df.merge(enriched, on="record_id", how="left")

# Add to STATIC_FEATURES:

ENRICHMENT_FEATURES = [
    "oc_matched",
    "oc_is_dissolved",            # ★  legally dissolved → very likely closed
    "oc_is_active",
    "oc_company_age_days",
    "oc_dissolved_low_conf",      # ★  interaction: dissolved + low confidence
    "google_matched",
    "google_is_permanently_closed",  # ★★ strongest single new signal
    "google_is_temporarily_closed",
    "google_is_operational",
    "google_rating_count",
    "google_has_moved",
    "google_closed_high_conf_conflict",
    "any_external_closed",        # ★  OR of all external closure signals
]
""")
