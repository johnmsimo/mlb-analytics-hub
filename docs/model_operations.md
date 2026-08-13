# Phase 4.61 model operations

The weekly regeneration workflow remains the only routine path that creates
new model artifacts. Phase 4.61 adds a fail-closed operations contract before
that workflow opens its pull request.

Each candidate must pass four independent gates:

1. **Held-out skill** — test AUC is at least 0.53, test Brier beats the
   base-rate Brier, and a held-out sample exists.
2. **Calibration** — the artifact identifies a calibrated estimator and its
   calibration method.
3. **Serve parity** — the candidate feature order matches every scorer alias
   for that market.
4. **Market validation** — target, line, and tracker market match the
   canonical market contract.

`model_operations.py` also provides metadata-only challenger comparison and
explicit promotion/rollback records. It does not load or replace pickle
artifacts, and it never auto-promotes a blocked candidate. Review and merge
remain required before deployment.

The CI report is written to `data/model_gate_report.json`. A failed gate exits
the weekly regeneration job before its pull request is opened.
