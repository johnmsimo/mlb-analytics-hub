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

## Phase 4.86 baseline closeout

`data/champion_manifest.json` freezes every production model artifact by Git
blob identity. `scripts/accuracy_baseline.py` verifies those artifacts against
the complete market contract, the 2021–2024 versus 2025 temporal holdout,
ordered serve aliases, held-out AUC/Brier/log loss, and five current-season
calibration snapshots.

The deterministic result is committed at `data/accuracy_baseline.json`.
Pull-request quality runs it in check mode, while weekly regeneration refreshes
the proposed manifest and report before opening its review-gated PR. Phase 5
challengers must improve held-out Brier, avoid AUC/log-loss/ECE regression, and
retain every Phase 4.61 promotion gate. No model is automatically promoted.
