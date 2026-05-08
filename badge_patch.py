"""
badge_patch.py — Changelog only, no code changes needed here.

BUG 1 — Missing badge CSS class (fixed in app.py template context)
  Symptom : Prop-card confidence badges rendered without background colour.
  Root cause: render_template() omitted the `badge_class` variable.
  Fix: badge_class=get_badge_class(confidence) added to render_template() call.

BUG 2 — NRFI edge sign flip (fixed in nrfi_odds.py)
  Symptom : Positive model edge displayed as negative.
  Root cause: edge = fair_prob - model_prob (should be model - market).
  Fix: nrfi_edge = round(float(model_nrfi_prob) - fair_nrfi_prob, 4)
"""
