"""Production WSGI entrypoint with confidence and decision intelligence."""

import app as app_module

from intelligence_integration import install_intelligence_api
from tracker_confidence_integration import install_tracker_confidence

install_tracker_confidence(app_module)
install_intelligence_api(app_module)
app = app_module.app
