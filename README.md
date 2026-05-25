# Closed/Open Business Prediction System (Project C)

Predict whether a business is OPEN or CLOSED using a multi-source signal stack, external enrichment, and an MCC-optimized XGBoost pipeline.

---

## Project Overview

- **Project name:** Project Finale (Project C)
- **Goal:** Predict business operational status using metadata, digital footprint signals, temporal recency, external enrichment, and interaction features
- **Primary metric:** Matthews Correlation Coefficient (MCC)
- **Differentiator:** Multi-source enrichment plus leak-aware design for robust open/closed classification

---

## How It Works

1. **Data sources**
   - Base dataset loaded from parquet files under `data/`
   - Enrichment dataset merged via stable identifiers
   - Supports schema variants with `record_id`/`primary_name` and `id`/`names`

2. **Feature engineering pipeline**
   - Normalize and clean raw fields
   - Extract temporal, presence, identity, geo, external, and interaction signals
   - Construct leak-aware features from available metadata

3. **External API enrichment**
   - Google Places text search for candidate business matches
   - Place details lookup for status, rating, hours, and moved-place signals
   - Optional OpenCorporates-style enrichment for legal status and age

4. **Model training**
   - XGBoost classifier trained with stratified cross-validation
   - Out-of-fold prediction generation for model selection
   - Class imbalance managed with weight adjustment and careful sampling

5. **Threshold optimization**
   - Sweep thresholds in a defined range
   - Select best threshold using OOF MCC
   - Use MCC rather than accuracy to optimize binary classification under class imbalance

6. **Prediction output**
   - Final model generates open/closed predictions
   - Outputs a submission-ready file and feature diagnostics

---

## Data & Labeling Strategy

### Base dataset schema

The base data contains business metadata and source provenance:
- `id` / `record_id`
- `names` (primary business name metadata)
- `categories`
- `addresses`
- `geometry`
- `confidence`
- `sources`
- `websites`
- `socials`
- `emails`
- `phones`
- `brand`
- `open`

### Enrichment dataset schema

External enrichment adds structured signals:
- `google_matched`
- `google_name_score`
- `google_business_status`
- `google_is_operational`
- `google_is_permanently_closed`
- `google_is_temporarily_closed`
- `google_rating`
- `google_rating_count`
- `google_has_hours`
- `google_has_moved`
- `oc_matched`
- `oc_is_dissolved`
- `oc_is_active`
- `oc_company_age_days`
- interaction features such as `any_external_closed`

### Merge strategy

- Merge enrichment data on a stable ID field: `record_id`
- Maintain schema unification across parquet variants
- Use defensive joins to avoid `KeyError` from missing columns
- Preserve base rows with left merge and fill missing enrichment signals safely

### Missing data and schema handling

- Explicitly normalize `names` and `addresses` schema variants
- Handle missing address fields with safe extraction helpers
- Replace missing numeric values with sentinel values before modeling
- Avoid direct assumptions on enrichment column presence

### Class imbalance

- Business closure prediction is often imbalanced
- Use MCC-focused selection and class-weight calibration
- Prefer metrics robust to skewed label distribution over simple accuracy

---

## Feature Engineering

### A. Temporal / Recency Features
- `msft_age_days`: staleness of the Microsoft source update
- `update_span_days`: range between earliest and latest source timestamps
- `days_since_latest_update`: recency of the last record change
- stale / old-data indicators for low-confidence records

### B. Presence Signals
- presence of phone numbers, websites, emails, and socials
- boolean flags for each digital footprint source
- aggregated presence score or ratio capturing digital footprint density

### C. Name & Identity Features
- original name length and token counts
- digit count and special-character presence
- presence of hash symbols, generics, legal business terms
- category collapse / grouping for business type signals
- brand-related features from `brand` and other identity metadata

### D. Geo Features
- latitude and longitude extraction from geometry
- regional indicators from address components
- local density or clustering features where applicable
- geo-derived consistency checks between address and coordinates

### E. External Enrichment Features
From Google Places:
- `google_business_status`
- `google_rating`
- `google_rating_count`
- `google_is_operational`
- `google_is_permanently_closed`
- `google_is_temporarily_closed`
- `google_name_score`

These are used to infer external closure signals and cross-check base data.

### F. Interaction Features
- stale record + low presence combinations
- external closure agreement signals (`oc_is_dissolved` OR `google_is_permanently_closed`)
- confidence interactions with external status signals
- cross-product features between recency and presence indicators

---

## External Data Enrichment (Google Places API)

### Two-step enrichment
1. **Text search**
   - Query string combines business name and formatted address
   - Limit results with `maxResultCount=3`
   - Retrieve candidate `place_id`, display name, and business status

2. **Place details**
   - Fetch detailed record for the selected `place_id`
   - Mask fields for efficiency
   - Extract rating, review count, opening hours, and moved-place signal

### Field masking optimization
- Use `X-Goog-FieldMask` to request only needed fields
- Reduce response size and API processing overhead
- Keep results consistent across text search and details requests

### Cost-efficient strategy
- Only process a bounded number of candidates
- Apply a hard cap on API calls
- Avoid redundant requests by validating candidate quality before details lookup

### Signal parsing
- Normalize Google status into boolean labels
- Compute `google_name_score` with fuzzy matching
- Reject low-confidence matches below threshold
- Store structured output even when partial responses occur

### Failure handling
- Handle non-200 responses gracefully
- Detect missing or malformed JSON
- Fall back to null-safe defaults for missing enrichment fields
- Preserve pipeline stability under API failure

---

## Model Training

- Use XGBoost binary classification
- Train with stratified K-Fold cross-validation
- Generate out-of-fold (OOF) predictions for model evaluation
- Clean feature matrix by converting NaNs to sentinel values (e.g., `-1`)
- Encode categorical values with label encoding where needed
- Handle imbalance with `scale_pos_weight` or similar weighting

---

## Optimization Strategy (MCC Focus)

- Perform threshold sweep over a defined range (for example, `0.1` to `0.7`)
- Compute OOF MCC for each candidate threshold
- Select the threshold maximizing MCC
- Use MCC to account for class imbalance and balanced predictive power
- Report confusion matrix and classification metrics at the chosen threshold

---

## Evaluation Outputs

- **OOF MCC** as the primary evaluation metric
- **SHAP feature importance** for model interpretability
- **MCC vs threshold curve** to justify threshold selection
- **Submission file format** compatible with downstream evaluation
- Detailed reports on match quality, feature impact, and error cases

---

## Key Engineering Challenges

- Parquet schema mismatches across raw and enrichment datasets
- Inconsistent ID fields (`id`, `record_id`) requiring schema unification
- Avoiding `KeyError` on optional enrichment columns such as `oc_matched`
- Handling mixed-type address concatenation safely
- Maintaining safe merge logic across base and enrichment pipelines
- Controlling API rate limits and cost during enrichment
- Preventing feature leakage from future or duplicate status signals
- Ensuring stable production behavior under partial API failure

---

## Repository Structure

- `train_competition.py` — main model training and evaluation pipeline
- `external_enrichment.py` — external enrichment pipeline for Google Places and OpenCorporates-style features
- `data/` — source parquet datasets
- `outputs/` — generated feature and model output files
- `debug/` — utility and validation scripts for enrichment and API testing
- `README.md` — project documentation
- `LICENSE` — license file

---

## Results

- **Primary metric:** MCC
- **Best threshold:** 0.15
- **OOF MCC:** 0.2715
- **Top features:** `confidence`, `cat_closure_rate`, `name_len`, `lon`, `primary_cat_enc`, `lat`, `msft_age_x_conf`, `meta_conf`, `addr_completeness`, `msft_age_days`
- **Notes:** Model results are generated from stratified OOF evaluation and threshold selection on the validation sweep

---

## Future Improvements

- Add additional enrichment APIs: Yelp, OpenStreetMap, Yelp Fusion
- Improve temporal modeling with sequence or decay-based features
- Build graph-based business network signals
- Use embeddings for business name similarity and entity matching
- Add active learning for uncertain predictions and human review loops
