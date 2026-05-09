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

### Option A — Google Colab (recommended)

1. Open `notebooks/xgb_production_export_colab.ipynb` in Google Colab:
   - Upload the file, or open directly from GitHub via the Colab UI.
2. Run all cells top-to-bottom (**Runtime → Run all**).
   - Cell 1 installs `xgboost==3.2.0` and asserts the version.
   - Cells 3–5 pull Statcast + FanGraphs data via `pybaseball`.
   - Cells 6–9 build feature frames and train the four models.
   - Cell 10 exports the four `.pkl` artifacts to `/content/models/`.
3. Download the four `.pkl` files:
   - In the Colab file browser (**Files** panel on the left) navigate to
     `/content/models/`.
   - Right-click each `.pkl` → **Download**.

### Option B — local Python environment

```bash
# Create a clean env with pinned xgboost
python -m venv .venv && source .venv/bin/activate
pip install xgboost==3.2.0 pybaseball pandas scikit-learn shap joblib pyarrow -q

# Edit OUTPUT_DIR in the notebook to a local path, then run as a script:
jupyter nbconvert --to script notebooks/xgb_production_export_colab.ipynb
OUTPUT_DIR=./models python xgb_production_export_colab.py
```

---

## Deploying the artifacts

After generating the files, copy them into the `models/` directory of the
repository:

```
models/
├── xgb_hits_over_0.5.pkl   ← replace with freshly generated file
├── xgb_k_over_3.5.pkl      ← replace with freshly generated file
├── xgb_k_over_4.5.pkl      ← replace with freshly generated file
└── xgb_k_over_5.5.pkl      ← replace with freshly generated file
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
| `notebooks/xgb_production_export_colab.ipynb` | **Use this** to regenerate production artifacts |
| `xgb_prop_pipeline.py` | Full training pipeline (same export format, more analysis cells) |
| `xgb_training_pipeline.py` | Legacy training script (uses `pickle`; produces different filenames — **not recommended for production**) |
| `xgb_prop_scorer.py` | Production loader — consumes artifacts from the pipeline/notebook |
