"""Install confidence enrichment at the tracker API serialization boundary."""

from __future__ import annotations

from types import ModuleType
from typing import Any, Callable, Mapping

from confidence_service import enrich_pick_confidence

_INSTALL_FLAG = "_tracker_confidence_integration_installed"


def install_tracker_confidence(app_module: ModuleType) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Wrap ``app._tracker_pick_payload`` once and return the active serializer.

    The wrapper is deliberately installed after the legacy app module finishes
    importing. Existing tracker fields remain unchanged; confidence metadata is
    added to every response that passes through the shared serializer.
    """
    current = getattr(app_module, "_tracker_pick_payload")
    if getattr(app_module, _INSTALL_FLAG, False):
        return current

    def tracker_pick_payload_with_confidence(row: Mapping[str, Any]) -> dict[str, Any]:
        return enrich_pick_confidence(current(row))

    tracker_pick_payload_with_confidence.__name__ = current.__name__
    tracker_pick_payload_with_confidence.__doc__ = (
        "Serialize a tracker pick and append deterministic confidence metadata."
    )
    setattr(app_module, "_tracker_pick_payload", tracker_pick_payload_with_confidence)
    setattr(app_module, _INSTALL_FLAG, True)
    return tracker_pick_payload_with_confidence
