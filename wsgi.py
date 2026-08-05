"""Production WSGI entrypoint with Phase 4.26 tracker confidence integration."""

import app as app_module

from tracker_confidence_integration import install_tracker_confidence

install_tracker_confidence(app_module)
app = app_module.app
