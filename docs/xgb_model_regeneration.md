# XGBoost Model Regeneration Guide

This document explains how to regenerate the production XGBoost model artifacts
used by **`xgb_prop_scorer.py`** and how to deploy them.

---

## Background

`xgb_prop_scorer.py` loads four `.pkl` artifacts at startup:

| File | Market |
|------|--------|
| `models/xgb_hits_over_0.5.pkl` | Batter hits ≥ 1 |
| `models/xgb_k_over_3.5.pkl` | Pitcher Ks > 3.5 |
| `models/xgb_k_over_4.5.pkl` | Pitcher Ks > 4.5 |
| `models/xgb_k_over_5.5.pkl` | Pitcher Ks > 5.5 |

Each artifact is a **joblib-serialised dict**:

```python
{
    "model":    <CalibratedClassifierCV wrapping XGBClassifier>,
    "features": ["sv_xba", "sv_xwoba", ...],   # ordered feature list
    "meta": {
        "xgboost_version": "3.2.0",            # version used during training
        "trained":  "<ISO-8601 timestamp>",
        "seasons":  [2021, 2022, 2023, 2024, 2025],
        ...
    }
}
```

At startup the scorer logs one line per loaded model including the recorded
`xgboost_version`.  If that version does not match the runtime version you
will see XGBoost's cross-version serialisation warning in your logs — the
cure is to retrain under the same version (see below).

---

## XGBoost version policy

**Pin `xgboost==3.2.0` in both the training notebook and `requirements.txt`.**

When the two versions differ, XGBoost emits a warning similar to:

```
UserWarning: If you are loading a serialized model (like pickle in Python…)
```

This does *not* crash the app, but it signals a compatibility risk.  Keeping
versions in sync eliminates the warning entirely.

---

## How to regenerate the artifacts

All paths below run the **same** code: `train_prop_models.train_all()`. The
notebook and the in-app **MODEL TRAINING** panel are thin wrappers around it,
so feature engineering can never drift from `xgb_prop_scorer.py`. This trains
all **7** markets (`hits`, `hr`, `tb`, `rbi`, `k_3.5/4.5/5.5`) with real
per-game targets and, for the K models, usage-weighted arsenal pitch-mix
features (`arsenal_whiff_pct`, `arsenal_putaway_pct`).

### Option A — Google Colab (recommended)

1. Open `notebooks/xgb_production_export_colab.ipynb` in Google Colab.
2. Run all cells top-to-bottom (**Runtime → Run all**).
   - Cell 1 installs `xgboost==3.2.0` (pinned).
   - Cell 2 makes the repo importable (git clone with `GITHUB_TOKEN`, or
     upload `train_prop_models.py` and set `REPO_DIR='/content'`).
   - Cells 3–4 call `train_prop_models.train_all(...)`, which pulls Statcast
     in memory-bounded windows, fetches Savant arsenal stats, and trains +
     calibrates every market into `models/`.
   - Cell 5 verifies each artifact (AUC/Brier, feature count, arsenal flag).
3. Download every `models/xgb_*.pkl` **and** `models/xgb_feature_cols.json`
   from the Colab **Files** panel.

### Option B — local Python environment (no notebook)

```bash
# Create a clean env with pinned xgboost
python -m venv .venv && source .venv/bin/activate
pip install xgboost==3.2.0 pybaseball pandas scikit-learn joblib pyarrow -q

# Train all markets straight to ./models (same code the notebook/UI run)
python train_prop_models.py                         # all 7 markets
python train_prop_models.py --market k_3.5 k_4.5 k_5.5   # K models only
```

### Option C — in-app MODEL TRAINING panel

Trigger `POST /api/training/run` (admin-token-gated) or click **RUN TRAINING**
on the dashboard/settings panel. It runs `train_all()` in a background thread
with live progress and writes the same artifacts to `models/`.

---

## Deploying the artifacts

After generating the files, copy them into the `models/` directory of the
repository:

```
models/
├── xgb_hits_over_0.5.pkl   ← replace with freshly generated file
├── xgb_hr_over_0.5.pkl     ← replace with freshly generated file
├── xgb_tb_over_1.5.pkl     ← replace with freshly generated file
├── xgb_rbi_over_0.5.pkl    ← replace with freshly generated file
├── xgb_k_over_3.5.pkl      ← replace with freshly generated file
├── xgb_k_over_4.5.pkl      ← replace with freshly generated file
├── xgb_k_over_5.5.pkl      ← replace with freshly generated file
└── xgb_feature_cols.json   ← regenerated alongside the .pkl files
```

Then:

1. Verify `requirements.txt` contains exactly `xgboost==3.2.0`.
2. Commit and push (or deploy via Render).
3. At startup you should see log lines like:
   ```
   [xgb_scorer] loaded hits from models/xgb_hits_over_0.5.pkl (xgboost_version=3.2.0)
   [xgb_scorer] loaded k_3.5 from models/xgb_k_over_3.5.pkl (xgboost_version=3.2.0)
   [xgb_scorer] loaded k_4.5 from models/xgb_k_over_4.5.pkl (xgboost_version=3.2.0)
   [xgb_scorer] loaded k_5.5 from models/xgb_k_over_5.5.pkl (xgboost_version=3.2.0)
   ```
   The `xgboost_version` must read `3.2.0`.

---

## Artifact format details

The export pipeline (`xgb_prop_pipeline.py` Cell 10 / `notebooks/xgb_production_export_colab.ipynb`
Cell 10) produces the payload dict using `joblib.dump(...)`.

`xgb_prop_scorer.py` loads with `joblib.load(...)` and detects the format:

- **Dict payload** (current format): extracts `model` and `features` from the dict.
- **Legacy direct-model** (older artifacts saved without the wrapper dict):
  accepted as-is; feature columns fall back to `models/xgb_feature_cols.json`
  if present.

---

## File ownership

| File | Role |
|------|------|
| `train_prop_models.py` | **Canonical training pipeline** — single source of truth for feature engineering, targets, and the 7 artifacts. Run directly, via the notebook, or via the MODEL TRAINING panel |
| `notebooks/xgb_production_export_colab.ipynb` | Colab wrapper that calls `train_prop_models.train_all()` (no separate feature logic) |
| `training_routes.py` | `/api/training/*` blueprint — runs `train_all()` in a background thread for the in-app panel |
| `xgb_prop_pipeline.py` | Older standalone pipeline (kept for reference/analysis) |
| `xgb_training_pipeline.py` | Legacy training script (uses `pickle`; different filenames — **not for production**) |
| `xgb_prop_scorer.py` | Production loader — consumes artifacts; reads each model's own saved `features` list |
