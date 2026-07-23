from __future__ import annotations

from dataclasses import dataclass
from typing import List

from storage.connectors.config import get_connector_view_for_device


@dataclass
class ConnectorConfig:
    state: str
    allowed_event_types: List[str]


def get_connector_config(device_id: str) -> ConnectorConfig:
    view = get_connector_view_for_device(device_id)
    return ConnectorConfig(state=view.state, allowed_event_types=view.allowed_event_types)
