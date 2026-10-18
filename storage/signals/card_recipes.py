"""
Card Recipe Generation — thin wrapper around the card_templates system.

This module previously contained 17 hardcoded extractor functions that
manually built card recipes for each signal type. It has been replaced
by the adaptive card_templates system which:

- Walks any payload recursively, classifying fields automatically
- Detects coordinates, tables, gauges, graphs from the data
- Persists templates per signal type so user visibility prefs survive
- Auto-merges new fields when payloads change over time
- Supports AI-assisted layout suggestion
- Produces path-based recipes with trackable fields for graphing

This wrapper exists so that any code still calling generate_card_recipe()
continues to work — it delegates to ensure_template() + render_card().
"""

from __future__ import annotations
from typing import Optional

from storage.signals.card_templates import ensure_template, render_card


def generate_card_recipe(
    signal_type: str,
    payload: dict,
    metadata: dict = None,
    observables: list = None,
    signal_id: str = "",
    lat: float = None,
    lon: float = None,
    tenant_id: str = "default",
) -> dict:
    """Generate a card recipe for a signal.

    Delegates to the card_templates system:
    1. ensure_template() — get or create the persistent template
    2. render_card() — produce a recipe from the template + payload

    Args:
        signal_type: GDU signal type (e.g., signal.device.sensor_reading)
        payload: The signal's parsed payload dict
        metadata: Signal metadata (unused — kept for backwards compat)
        observables: Extracted observables (unused — kept for backwards compat)
        signal_id: Signal ID for content URLs
        lat: Latitude if known
        lon: Longitude if known
        tenant_id: Tenant ID for template lookup

    Returns:
        Card recipe dict with fields, media, trackable, summary.
    """
    if not payload:
        payload = {}

    template = ensure_template(signal_type, payload, tenant_id)
    return render_card(template, payload, signal_id, lat, lon)
