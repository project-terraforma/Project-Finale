"""
project_c — open/closed prediction (competition version)
Optimized for Matthews Correlation Coefficient (MCC)

Usage:
    python train_competition.py --data /path/to/full_dataset.parquet
    python train_competition.py --data project_c_samples.parquet  # sample only (no val split)

Outputs:
    outputs/submission.csv          — record_id, open ("open"/"closed")
    outputs/competition_model.json  — trained XGBoost model
    outputs/shap_importance.png     — SHAP feature importance
    outputs/mcc_threshold_curve.png — MCC vs threshold plot
    outputs/metrics_competition.json
"""

import argparse
import warnings
import json
from pathlib import Path

import numpy as np
import pandas as pd
from shapely.wkb import loads as wkb_loads
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import matthews_corrcoef, classification_report, confusion_matrix
from sklearn.preprocessing import LabelEncoder
import xgboost as xgb
import shap
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")

# ── CLI ────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--data", default="project_c_samples.parquet")
parser.add_argument("--out", default="outputs")
parser.add_argument("--n-folds", type=int, default=5)
parser.add_argument("--seed", type=int, default=42)
args = parser.parse_args()

OUT = Path(args.out)
OUT.mkdir(parents=True, exist_ok=True)
RNG = args.seed

print("=" * 65)
print("PROJECT C — COMPETITION MODEL  (metric: MCC)")
print("=" * 65)


# ── 1. LOAD ────────────────────────────────────────────────────────────────────
print("\n[1/6] Loading data …")
df = pd.read_parquet("data/project_c_samples.parquet")
print(f"    {len(df):,} rows × {df.shape[1]} columns")

# ── Handle both sample file (no eval_type) and full competition file ───────────
HAS_EVAL_TYPE = "eval_type" in df.columns
HAS_EXTERNAL = "open_external_signal" in df.columns

if HAS_EVAL_TYPE:
    # Normalize label: may come in as "open"/"closed" strings or 0/1 ints
    if df["open"].dtype == object:
        df["open_int"] = (df["open"] == "open").astype(int)
    else:
        df["open_int"] = df["open"].astype(int)

    train_df = df[df["eval_type"] == "train"].copy()
    val_df = df[df["eval_type"] == "validation"].copy()
    print(f"    Train: {len(train_df):,}  |  Validation: {len(val_df):,}")
else:
    # Sample file — treat everything as train, no submission generated
    if df["open"].dtype == object:
        df["open_int"] = (df["open"] == "open").astype(int)
    else:
        df["open_int"] = df["open"].astype(int)
    train_df = df.copy()
    val_df = pd.DataFrame()
    print(
        "    NOTE: No eval_type column found — running train-only mode (sample data)."
    )

print(f"    Train class balance: {train_df['open_int'].value_counts().to_dict()}")


# ── 2. FEATURE ENGINEERING ─────────────────────────────────────────────────────
print("\n[2/6] Engineering features …")


def safe_len(x):
    return len(x) if isinstance(x, (list, np.ndarray)) else 0


def addr_field(x, f):
    return x[0].get(f) if isinstance(x, (list, np.ndarray)) and len(x) > 0 else None


def parse_geom(wkb_bytes):
    try:
        g = wkb_loads(wkb_bytes)
        return g.y, g.x
    except Exception:
        return None, None


def engineer_features(frame: pd.DataFrame) -> pd.DataFrame:
    """All feature engineering in one place — call on train and val separately."""
    f = frame.copy()

    # ── Confidence ────────────────────────────────────────────────────────────
    f["confidence"] = f["confidence"].astype(float)
    conf_bins = [0.0, 0.5, 0.7, 0.85, 0.95, 1.01]
    f["confidence_bucket"] = pd.cut(
        f["confidence"], bins=conf_bins, labels=[0, 1, 2, 3, 4], right=False
    ).astype(float)

    # ── Source signals ────────────────────────────────────────────────────────
    f["source_count"] = f["sources"].apply(safe_len)
    f["has_meta"] = f["sources"].apply(
        lambda x: int(any(s["dataset"] == "meta" for s in x))
    )
    f["has_msft"] = f["sources"].apply(
        lambda x: int(any(s["dataset"] == "Microsoft" for s in x))
    )
    f["meta_conf"] = (
        f["sources"]
        .apply(
            lambda x: next(
                (s.get("confidence", np.nan) for s in x if s["dataset"] == "meta"),
                np.nan,
            )
        )
        .astype(float)
    )

    # Microsoft update age — older = higher risk of staleness / closure
    snapshot = pd.Timestamp("2025-02-24", tz="UTC")
    f["msft_update_dt"] = pd.to_datetime(
        f["sources"].apply(
            lambda x: next(
                (s.get("update_time") for s in x if s["dataset"] == "Microsoft"), None
            )
        ),
        errors="coerce",
        utc=True,
    )
    f["msft_age_days"] = (
        (snapshot - f["msft_update_dt"]).dt.days.fillna(-1).astype(float)
    )
    # Interaction: old MSFT record + low meta confidence = strong closure signal
    f["msft_age_x_conf"] = f["msft_age_days"].clip(lower=0) * (1 - f["confidence"])

    # ── Contact / presence ────────────────────────────────────────────────────
    f["has_phone"] = f["phones"].apply(lambda x: int(safe_len(x) > 0))
    f["phone_count"] = f["phones"].apply(safe_len)
    f["has_website"] = f["websites"].apply(lambda x: int(safe_len(x) > 0))
    f["website_count"] = f["websites"].apply(safe_len)
    f["has_social"] = f["socials"].apply(lambda x: int(safe_len(x) > 0))
    f["has_brand"] = f["brand"].apply(lambda x: int(x is not None))
    f["presence_score"] = (
        f["has_phone"] + f["has_website"] + f["has_social"] + f["has_brand"]
    )
    # Presence completeness ratio (0–1)
    f["presence_ratio"] = f["presence_score"] / 4.0

    # ── Address completeness ──────────────────────────────────────────────────
    for field in ["region", "locality", "postcode", "freeform"]:
        f[f"addr_has_{field}"] = f["addresses"].apply(
            lambda x, ff=field: int(addr_field(x, ff) is not None)
        )
    f["addr_completeness"] = (
        f["addr_has_region"]
        + f["addr_has_locality"]
        + f["addr_has_postcode"]
        + f["addr_has_freeform"]
    )
    f["addr_region_raw"] = f["addresses"].apply(
        lambda x: addr_field(x, "region") or "unknown"
    )

    # ── Name features ─────────────────────────────────────────────────────────
    f["name_primary"] = f["names"].apply(
        lambda x: x.get("primary", "") if isinstance(x, dict) else ""
    )
    f["name_len"] = f["name_primary"].apply(lambda x: len(x) if x else 0)
    f["name_words"] = f["name_primary"].apply(lambda x: len(x.split()) if x else 0)
    f["name_has_digit"] = f["name_primary"].apply(
        lambda x: int(any(c.isdigit() for c in x)) if x else 0
    )
    # Generic chain-like names (e.g. "Store #123") often survive even when closed in DBs
    f["name_has_hash"] = f["name_primary"].apply(lambda x: int("#" in x) if x else 0)

    # ── Geometry ──────────────────────────────────────────────────────────────
    coords = f["geometry"].apply(parse_geom)
    f["lat"] = coords.apply(lambda x: x[0])
    f["lon"] = coords.apply(lambda x: x[1])

    # ── Category features ─────────────────────────────────────────────────────
    f["primary_cat"] = f["categories"].apply(
        lambda x: x["primary"] if isinstance(x, dict) else "unknown"
    )
    f["alt_cats"] = f["categories"].apply(
        lambda x: (
            list(x["alternate"])
            if isinstance(x, dict) and x.get("alternate") is not None
            else []
        )
    )
    f["alt_cat_count"] = f["alt_cats"].apply(len)

    return f


train_df = engineer_features(train_df)
if not val_df.empty:
    val_df = engineer_features(val_df)

print("    Feature engineering complete.")


# ── 3. ENCODE CATEGORICALS ─────────────────────────────────────────────────────
print("\n[3/6] Encoding categoricals …")

# Region: keep top-30 states, rest → "other"
top_states = train_df["addr_region_raw"].value_counts().head(30).index.tolist()
# Category: group rare cats (< 5 train samples) → "other"
cat_freq = train_df["primary_cat"].value_counts()
rare_cats = cat_freq[cat_freq < 5].index

for frame in [train_df] + ([val_df] if not val_df.empty else []):
    frame["addr_region"] = frame["addr_region_raw"].apply(
        lambda x: x if x in top_states else "other"
    )
    frame["primary_cat_grp"] = frame["primary_cat"].apply(
        lambda x: "other" if x in rare_cats else x
    )

le_cat = LabelEncoder().fit(train_df["primary_cat_grp"])
le_region = LabelEncoder().fit(train_df["addr_region"])

for frame in [train_df] + ([val_df] if not val_df.empty else []):
    frame["primary_cat_enc"] = le_cat.transform(
        frame["primary_cat_grp"].apply(lambda x: x if x in le_cat.classes_ else "other")
    )
    frame["addr_region_enc"] = le_region.transform(
        frame["addr_region"].apply(lambda x: x if x in le_region.classes_ else "other")
    )

STATIC_FEATURES = [
    "confidence",
    "confidence_bucket",
    "source_count",
    "has_meta",
    "has_msft",
    "meta_conf",
    "msft_age_days",
    "msft_age_x_conf",
    "has_phone",
    "phone_count",
    "has_website",
    "website_count",
    "has_social",
    "has_brand",
    "presence_score",
    "presence_ratio",
    "addr_completeness",
    "addr_has_region",
    "addr_has_locality",
    "addr_has_postcode",
    "addr_has_freeform",
    "name_len",
    "name_words",
    "name_has_digit",
    "name_has_hash",
    "lat",
    "lon",
    "alt_cat_count",
    "primary_cat_enc",
    "addr_region_enc",
]

# Will add OOF target-encoded cat_closure_rate in step 4
print(f"    Base features: {len(STATIC_FEATURES)}")


# ── 4. CROSS-VALIDATION WITH OOF TARGET ENCODING + MCC OPTIMIZATION ───────────
print("\n[4/6] Cross-validation (MCC) …")

y_train = train_df["open_int"].values
X_train_static = train_df[STATIC_FEATURES].astype(float)

neg = (y_train == 0).sum()
pos = (y_train == 1).sum()
scale_pos_weight = neg / pos
print(f"    Class ratio (neg/pos) = {scale_pos_weight:.2f}")

MODEL_PARAMS = dict(
    n_estimators=600,
    max_depth=5,
    learning_rate=0.04,
    subsample=0.8,
    colsample_bytree=0.75,
    min_child_weight=3,
    gamma=0.1,
    reg_alpha=0.1,
    reg_lambda=1.0,
    scale_pos_weight=scale_pos_weight,
    tree_method="hist",
    random_state=RNG,
    n_jobs=-1,
)

skf = StratifiedKFold(n_splits=args.n_folds, shuffle=True, random_state=RNG)
oof_probs = np.zeros(len(y_train))
# OOF cat closure rate (leak-free target encoding)
oof_cat_closure = np.zeros(len(y_train))

# For val predictions: average across folds
if not val_df.empty:
    val_probs = np.zeros(len(val_df))
    val_cat_closures = []

fold_mcc_by_threshold = {t: [] for t in np.arange(0.1, 0.7, 0.05)}

for fold, (tr_idx, va_idx) in enumerate(skf.split(X_train_static, y_train), 1):
    # ── OOF target encoding (leak-free) ──────────────────────────────────────
    tr_cats = train_df["primary_cat_grp"].iloc[tr_idx]
    tr_y = y_train[tr_idx]
    va_cats = train_df["primary_cat_grp"].iloc[va_idx]

    # Compute closure rate from training fold only (smoothed with global mean)
    global_mean = 1 - tr_y.mean()
    smooth_k = 10
    cat_stats = (
        pd.DataFrame({"cat": tr_cats, "y": tr_y})
        .groupby("cat")["y"]
        .agg(["mean", "count"])
    )
    cat_stats["closure_rate"] = 1 - (
        (cat_stats["mean"] * cat_stats["count"] + global_mean * smooth_k)
        / (cat_stats["count"] + smooth_k)
    )
    cat_closure_map = cat_stats["closure_rate"].to_dict()

    oof_cat_closure[va_idx] = va_cats.map(cat_closure_map).fillna(global_mean).values

    if not val_df.empty:
        val_cat_closures.append(
            val_df["primary_cat_grp"].map(cat_closure_map).fillna(global_mean).values
        )

    # ── Build fold feature matrices ───────────────────────────────────────────
    X_tr = X_train_static.iloc[tr_idx].copy()
    X_va = X_train_static.iloc[va_idx].copy()
    X_tr["cat_closure_rate"] = tr_cats.map(cat_closure_map).fillna(global_mean).values
    X_va["cat_closure_rate"] = va_cats.map(cat_closure_map).fillna(global_mean).values
    oof_cat_closure[va_idx] = X_va["cat_closure_rate"].values

    # ── External signal feature (train only — per spec) ───────────────────────
    if HAS_EXTERNAL:
        ext_tr = (
            (train_df["open_external_signal"].iloc[tr_idx] == "open")
            .astype(float)
            .values
        )
        ext_va = (
            (train_df["open_external_signal"].iloc[va_idx] == "open")
            .astype(float)
            .values
        )
        X_tr["external_signal"] = ext_tr
        X_va["external_signal"] = ext_va

    # ── Train ─────────────────────────────────────────────────────────────────
    model = xgb.XGBClassifier(**MODEL_PARAMS)
    model.fit(
        X_tr,
        y_train[tr_idx],
        eval_set=[(X_va, y_train[va_idx])],
        verbose=False,
    )

    probs = model.predict_proba(X_va)[:, 1]
    oof_probs[va_idx] = probs

    # MCC across thresholds
    for t in fold_mcc_by_threshold:
        preds = (probs >= t).astype(int)
        fold_mcc_by_threshold[t].append(matthews_corrcoef(y_train[va_idx], preds))

    # Val fold predictions
    if not val_df.empty:
        X_val_fold = val_df[STATIC_FEATURES].astype(float).copy()
        X_val_fold["cat_closure_rate"] = val_cat_closures[-1]
        if HAS_EXTERNAL:
            # External signal not available for val — use 0.5 (neutral)
            X_val_fold["external_signal"] = 0.5
        val_probs += model.predict_proba(X_val_fold)[:, 1] / args.n_folds

    fold_best = max(fold_mcc_by_threshold, key=lambda t: fold_mcc_by_threshold[t][-1])
    print(
        f"    Fold {fold} | best MCC={fold_mcc_by_threshold[fold_best][-1]:.4f} @ t={fold_best:.2f}"
    )

# ── Find optimal threshold ─────────────────────────────────────────────────────
mean_mcc = {t: np.mean(v) for t, v in fold_mcc_by_threshold.items()}
best_threshold = max(mean_mcc, key=mean_mcc.get)
best_mcc = mean_mcc[best_threshold]

print(f"\n    Threshold sweep:")
for t, m in sorted(mean_mcc.items()):
    marker = " ◄ BEST" if t == best_threshold else ""
    print(f"      t={t:.2f}  MCC={m:.4f}{marker}")

# OOF report
oof_preds = (oof_probs >= best_threshold).astype(int)
print(f"\n    OOF report @ threshold={best_threshold:.2f}:")
print(
    classification_report(y_train, oof_preds, target_names=["Closed", "Open"], digits=3)
)
cm = confusion_matrix(y_train, oof_preds)
print(f"    Confusion matrix:\n{cm}")
print(f"\n    OOF MCC: {matthews_corrcoef(y_train, oof_preds):.4f}")


# ── 5. FINAL MODEL + SUBMISSION ────────────────────────────────────────────────
print("\n[5/6] Training final model on full train set …")

# Final OOF cat closure (computed on full train)
global_mean_final = 1 - y_train.mean()
cat_stats_final = (
    pd.DataFrame({"cat": train_df["primary_cat_grp"], "y": y_train})
    .groupby("cat")["y"]
    .agg(["mean", "count"])
)
smooth_k = 10
cat_stats_final["closure_rate"] = 1 - (
    (cat_stats_final["mean"] * cat_stats_final["count"] + global_mean_final * smooth_k)
    / (cat_stats_final["count"] + smooth_k)
)
cat_closure_map_final = cat_stats_final["closure_rate"].to_dict()

X_final = X_train_static.copy()
X_final["cat_closure_rate"] = (
    train_df["primary_cat_grp"]
    .map(cat_closure_map_final)
    .fillna(global_mean_final)
    .values
)
if HAS_EXTERNAL:
    X_final["external_signal"] = (
        (train_df["open_external_signal"] == "open").astype(float).values
    )

ALL_FEATURES = list(X_final.columns)
print(f"    Total features: {len(ALL_FEATURES)}")

final_model = xgb.XGBClassifier(**MODEL_PARAMS)
final_model.fit(X_final, y_train, verbose=False)
final_model.save_model(str(OUT / "competition_model.json"))
print(f"    Model saved → {OUT}/competition_model.json")

# ── Submission CSV ─────────────────────────────────────────────────────────────
if not val_df.empty:
    val_preds_bin = (val_probs >= best_threshold).astype(int)
    submission = pd.DataFrame(
        {
            "record_id": val_df["id"],
            "open": ["open" if p == 1 else "closed" for p in val_preds_bin],
        }
    )
    sub_path = OUT / "submission.csv"
    submission.to_csv(sub_path, index=False)
    print(f"    Submission saved → {sub_path}  ({len(submission):,} rows)")
    print(
        f"    Predicted open: {(val_preds_bin==1).sum()}  closed: {(val_preds_bin==0).sum()}"
    )
else:
    print("    No validation split → no submission.csv generated (sample data mode).")


# ── 6. DIAGNOSTICS ─────────────────────────────────────────────────────────────
print("\n[6/6] Generating diagnostic plots …")

# Plot A — MCC vs threshold
fig, ax = plt.subplots(figsize=(7, 4))
ts = sorted(mean_mcc.keys())
ms = [mean_mcc[t] for t in ts]
stds = [np.std(fold_mcc_by_threshold[t]) for t in ts]
ax.plot(ts, ms, color="#1D9E75", lw=2, marker="o", markersize=4)
ax.fill_between(
    ts,
    [m - s for m, s in zip(ms, stds)],
    [m + s for m, s in zip(ms, stds)],
    alpha=0.2,
    color="#1D9E75",
)
ax.axvline(
    best_threshold,
    color="#E24B4A",
    lw=1.5,
    linestyle="--",
    label=f"Best t={best_threshold:.2f}, MCC={best_mcc:.3f}",
)
ax.set_xlabel("Decision threshold", fontsize=10)
ax.set_ylabel("Mean OOF MCC", fontsize=10)
ax.set_title("MCC vs threshold (5-fold OOF)", fontsize=12, pad=10)
ax.legend(fontsize=9)
ax.spines[["top", "right"]].set_visible(False)
plt.tight_layout()
plt.savefig(OUT / "mcc_threshold_curve.png", dpi=150, bbox_inches="tight")
plt.close()
print(f"    Saved → {OUT}/mcc_threshold_curve.png")

# Plot B — SHAP feature importance
explainer = shap.TreeExplainer(final_model)
shap_vals = explainer.shap_values(X_final)
mean_shap = pd.Series(np.abs(shap_vals).mean(axis=0), index=ALL_FEATURES).sort_values(
    ascending=True
)

fig, ax = plt.subplots(figsize=(9, max(5, len(ALL_FEATURES) * 0.3)))
colors = ["#1D9E75" if v >= mean_shap.median() else "#B4B2A9" for v in mean_shap]
bars = ax.barh(mean_shap.index, mean_shap.values, color=colors, height=0.65)
ax.set_xlabel("Mean |SHAP value|", fontsize=10)
ax.set_title("Feature importance — mean absolute SHAP", fontsize=12, pad=10)
ax.spines[["top", "right"]].set_visible(False)
for bar, val in zip(bars, mean_shap.values):
    ax.text(
        val + 0.0002,
        bar.get_y() + bar.get_height() / 2,
        f"{val:.4f}",
        va="center",
        fontsize=7.5,
        color="#3d3d3a",
    )
plt.tight_layout()
plt.savefig(OUT / "shap_importance.png", dpi=150, bbox_inches="tight")
plt.close()
print(f"    Saved → {OUT}/shap_importance.png")

# Plot C — SHAP beeswarm (top 20)
shap.summary_plot(
    shap_vals,
    X_final,
    feature_names=ALL_FEATURES,
    show=False,
    max_display=20,
    plot_size=None,
)
plt.title("SHAP beeswarm (top 20 features)", fontsize=12, pad=10)
plt.tight_layout()
plt.savefig(OUT / "shap_beeswarm.png", dpi=150, bbox_inches="tight")
plt.close()
print(f"    Saved → {OUT}/shap_beeswarm.png")

# ── Save metrics ───────────────────────────────────────────────────────────────
metrics_out = {
    "n_train": int(len(train_df)),
    "n_val": int(len(val_df)) if not val_df.empty else 0,
    "n_features": len(ALL_FEATURES),
    "features": ALL_FEATURES,
    "class_balance_train": {"open": int(pos), "closed": int(neg)},
    "best_threshold": float(best_threshold),
    "oof_mcc": float(matthews_corrcoef(y_train, oof_preds)),
    "mcc_by_threshold": {f"{k:.2f}": float(v) for k, v in mean_mcc.items()},
    "top_10_features_by_shap": mean_shap.sort_values(ascending=False)
    .head(10)
    .index.tolist(),
    "model_params": {
        k: (v if not isinstance(v, float) or not np.isnan(v) else None)
        for k, v in MODEL_PARAMS.items()
    },
    "has_external_signal": HAS_EXTERNAL,
}
with open(OUT / "metrics_competition.json", "w") as f:
    json.dump(metrics_out, f, indent=2)
print(f"    Saved → {OUT}/metrics_competition.json")

print("\n" + "=" * 65)
print("DONE")
print(f"  Outputs in: {OUT}/")
print(f"    competition_model.json   — XGBoost model")
print(f"    submission.csv           — competition submission")
print(f"    mcc_threshold_curve.png  — MCC vs threshold sweep")
print(f"    shap_importance.png      — SHAP bar chart")
print(f"    shap_beeswarm.png        — SHAP beeswarm")
print(f"    metrics_competition.json — all metrics + config")
print(
    f"\n  OOF MCC: {matthews_corrcoef(y_train, oof_preds):.4f}  (threshold={best_threshold:.2f})"
)
print("=" * 65)
