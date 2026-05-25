import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from rapidfuzz import fuzz

MAX_API_CALLS = 10
OUTPUT_PATH = Path("outputs/google_test_results.parquet")
DATA_PATH = Path("data/project_c_samples.parquet")


def safe_addr_field(addr, field):
    """Extract a field from the first address entry safely."""
    try:
        if addr is None:
            return None
        if len(addr) == 0:
            return None
        first = addr[0]
        if isinstance(first, dict):
            return first.get(field)
    except Exception:
        return None
    return None


def google_text_search(name, address, api_key):
    """Perform a Google Places text search and return the top place metadata."""
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.businessStatus",
    }
    body = {"textQuery": f"{name} {address}".strip(), "maxResultCount": 3}

    try:
        response = requests.post(url, json=body, headers=headers, timeout=10)
        status = response.status_code
        print(f"TEXT SEARCH: query='{body['textQuery']}' status={status}")
        if status != 200:
            print(f"  Warning: non-200 response from text search: {status}")
            return None, None, None

        payload = response.json()
        places = payload.get("places") or []
        print(f"  Places found: {len(places)}")
        if not places:
            return None, None, None

        top = places[0]
        place_id = top.get("id")
        display_name = top.get("displayName", {}).get("text", "")
        business_status = top.get("businessStatus")
        return place_id, display_name, business_status

    except requests.exceptions.RequestException as exc:
        print(f"  Error: request failed during text search: {exc}")
        return None, None, None
    except ValueError as exc:
        print(f"  Error: invalid JSON from text search: {exc}")
        return None, None, None


def google_place_details(place_id, api_key):
    """Fetch Google Place details for a place_id."""
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "id,displayName,businessStatus,rating," 
            "userRatingCount,regularOpeningHours,movedPlaceId"
        ),
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        status = response.status_code
        print(f"DETAILS: place_id='{place_id}' status={status}")
        if status != 200:
            print(f"  Warning: non-200 response from place details: {status}")
            return {}

        return response.json()

    except requests.exceptions.RequestException as exc:
        print(f"  Error: request failed during place details: {exc}")
        return {}
    except ValueError as exc:
        print(f"  Error: invalid JSON from place details: {exc}")
        return {}


def parse_google_result(place_id, google_display_name, google_status, details, original_name):
    """Construct final Google enrichment result from API outputs."""
    if not place_id:
        return {
            "google_display_name": None,
            "google_name_score": 0,
            "google_business_status": None,
            "google_is_operational": 0,
            "google_is_permanently_closed": 0,
            "google_is_temporarily_closed": 0,
            "google_rating": None,
            "google_rating_count": None,
            "google_has_hours": 0,
            "google_has_moved": 0,
            "matched": 0,
        }

    display_name = google_display_name or details.get("displayName", {}).get("text", "")
    score = fuzz.token_sort_ratio(
        str(original_name).lower(), str(display_name).lower()
    )
    if score < 50:
        print(f"  Rejecting match: score={score} < 50 for displayName='{display_name}'")
        return {
            "google_display_name": display_name,
            "google_name_score": score,
            "google_business_status": google_status,
            "google_is_operational": 0,
            "google_is_permanently_closed": 0,
            "google_is_temporarily_closed": 0,
            "google_rating": None,
            "google_rating_count": None,
            "google_has_hours": 0,
            "google_has_moved": 0,
            "matched": 0,
        }

    status = details.get("businessStatus") or google_status or ""
    has_hours = int(bool(details.get("regularOpeningHours")))
    has_moved = int(bool(details.get("movedPlaceId")))
    rating = details.get("rating")
    rating_count = details.get("userRatingCount")

    return {
        "google_display_name": display_name,
        "google_name_score": score,
        "google_business_status": status,
        "google_is_operational": int(status == "OPERATIONAL"),
        "google_is_permanently_closed": int(status == "CLOSED_PERMANENTLY"),
        "google_is_temporarily_closed": int(status == "CLOSED_TEMPORARILY"),
        "google_rating": rating,
        "google_rating_count": rating_count,
        "google_has_hours": has_hours,
        "google_has_moved": has_moved,
        "matched": 1,
    }


def main():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY environment variable is not set.")
        print("Set GOOGLE_API_KEY and rerun: export GOOGLE_API_KEY=your_key")
        sys.exit(1)

    if not DATA_PATH.exists():
        print(f"ERROR: Input file not found: {DATA_PATH}")
        sys.exit(1)

    df = pd.read_parquet(DATA_PATH)
    print(f"Loaded {len(df):,} rows from {DATA_PATH}")

    valid_rows = []
    for _, row in df.iterrows():
        name = None
        try:
            name = row["names"]["primary"]
        except Exception:
            name = None

        address = row.get("addresses")
        country = safe_addr_field(address, "country")
        locality = safe_addr_field(address, "locality")
        region = safe_addr_field(address, "region")
        postcode = safe_addr_field(address, "postcode")

        if not name or not country or country != "US":
            continue

        if not locality and not region and not postcode:
            continue

        formatted_address = " ".join(
            part for part in [locality, region, postcode] if part
        ).strip()
        if not formatted_address:
            continue

        valid_rows.append(
            {
                "record_id": row["id"],
                "original_name": name,
                "address": formatted_address,
            }
        )
        if len(valid_rows) >= 5:
            break

    if not valid_rows:
        print("ERROR: No valid US businesses found in the first rows.")
        sys.exit(1)

    results = []
    calls_made = 0
    successful_matches = 0
    operational_count = 0
    permanently_closed_count = 0

    for entry in valid_rows:
        if calls_made >= MAX_API_CALLS:
            print("WARNING: API call cap reached. Stopping further requests.")
            break

        record_id = entry["record_id"]
        name = entry["original_name"]
        address = entry["address"]
        query = f"{name} {address}".strip()

        print(f"\n=== Testing business {record_id} ===")
        print(f"Query: {query}")

        place_id, display_name, text_status = google_text_search(name, address, api_key)
        calls_made += 1
        time.sleep(0.1)

        details = {}
        if place_id and calls_made < MAX_API_CALLS:
            details = google_place_details(place_id, api_key)
            calls_made += 1
            time.sleep(0.1)
        elif place_id:
            print("WARNING: API call cap reached before place details request.")

        result = parse_google_result(place_id, display_name, text_status, details, name)
        result["record_id"] = record_id
        result["original_name"] = name
        results.append(result)

        if result["matched"]:
            successful_matches += 1
            if result["google_is_operational"]:
                operational_count += 1
            if result["google_is_permanently_closed"]:
                permanently_closed_count += 1

    if calls_made >= MAX_API_CALLS:
        print("WARNING: Reached MAX_API_CALLS limit.")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out_df = pd.DataFrame(results)
    out_df.to_parquet(OUTPUT_PATH, index=False)
    print(f"\nSaved results to {OUTPUT_PATH}")

    print("\nSummary")
    print("-------")
    print(f"Total businesses tested: {len(results)}")
    print(f"Successful matches: {successful_matches}")
    print(f"Permanently closed count: {permanently_closed_count}")
    print(f"Operational count: {operational_count}")
    print(f"API calls made: {calls_made}")


if __name__ == "__main__":
    main()
