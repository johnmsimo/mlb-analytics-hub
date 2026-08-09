"""Production WSGI entrypoint with confidence and decision intelligence."""

import app as app_module

from canonical_consistency import (
    install_canonical_consistency_api,
)
from intelligence_integration import install_intelligence_api
from tracker_confidence_integration import install_tracker_confidence
from cache_warmup import cache_warmup_bp

install_tracker_confidence(app_module)
install_intelligence_api(app_module)
install_canonical_consistency_api(app_module)

# Phase 4.59: expose bounded warmup state once per worker.
if not getattr(app_module.app, "_phase_459_warmup_installed", False):
    app_module.app.register_blueprint(cache_warmup_bp)
    setattr(app_module.app, "_phase_459_warmup_installed", True)

app = app_module.app
