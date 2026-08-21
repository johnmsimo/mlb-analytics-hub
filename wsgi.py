"""Production WSGI entrypoint with confidence and decision intelligence."""

import app as app_module

from accuracy_control_plane import install_accuracy_control_plane
from canonical_consistency import (
    install_canonical_consistency_api,
)
from intelligence_integration import install_intelligence_api
from intelligence_control_plane import install_intelligence_control_plane
from tracker_confidence_integration import install_tracker_confidence
from cache_warmup import cache_warmup_bp
from monetization_growth import monetization_growth_bp
from product_hub import product_hub_bp
from public_verification import install_public_verification

install_tracker_confidence(app_module)
install_intelligence_api(app_module)
install_canonical_consistency_api(app_module)
install_public_verification(app_module)
install_accuracy_control_plane(app_module)
install_intelligence_control_plane(app_module)

# Phase 4.59: expose bounded warmup state once per worker.
if not getattr(app_module.app, "_phase_459_warmup_installed", False):
    app_module.app.register_blueprint(cache_warmup_bp)
    setattr(app_module.app, "_phase_459_warmup_installed", True)

# Phase 4.62: register the consolidated product workspace once per worker.
if not getattr(app_module.app, "_phase_462_product_hub_installed", False):
    app_module.app.register_blueprint(product_hub_bp)
    setattr(app_module.app, "_phase_462_product_hub_installed", True)

# Phase 5.11: expose read-only plan readiness and a fail-closed paid boundary.
if not getattr(app_module.app, "_phase_511_monetization_growth_installed", False):
    app_module.app.register_blueprint(monetization_growth_bp)
    setattr(app_module.app, "_phase_511_monetization_growth_installed", True)

app = app_module.app
