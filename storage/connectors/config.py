from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_CONFIG_PATH = Path("storage/connectors/config.json")
ENV_CONFIG_PATH = "ENKI_CONNECTOR_CONFIG_PATH"


@dataclass
class ConnectorPolicy:
    connectors_enabled: bool = True
    default_state: str = "ACTIVE_PROJECTION"
    assertion_submission_allowed_for_tiers: List[str] = None  # e.g., ["P", "K"]
    global_allowed_event_types: List[str] = None  # additional allowlist applied to all
    device_overrides: Dict[str, Dict[str, Any]] = None  # device_id -> {state, allowed_event_types}

    def __post_init__(self):
        if self.assertion_submission_allowed_for_tiers is None:
            self.assertion_submission_allowed_for_tiers = ["P", "K"]
        if self.global_allowed_event_types is None:
            self.global_allowed_event_types = []
        if self.device_overrides is None:
            self.device_overrides = {}


@dataclass
class ConnectorView:
    state: str
    allowed_event_types: List[str]


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_config_path() -> Optional[Path]:
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        p = Path(env)
        if p.is_file():
            return p
        # If env is set but file missing, treat as disabled via fallback
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def load_policy() -> ConnectorPolicy:
    path = _resolve_config_path()
    if not path:
        # If no config present, enable connectors but with empty allowlist -> restrictive behavior
        return ConnectorPolicy(
            connectors_enabled=True,
            default_state="ACTIVE_PROJECTION",
            assertion_submission_allowed_for_tiers=["P", "K"],
            global_allowed_event_types=[],
            device_overrides={},
        )

    data = _load_json(path)
    return ConnectorPolicy(
        connectors_enabled=bool(data.get("connectors_enabled", True)),
        default_state=data.get("default_state", "ACTIVE_PROJECTION"),
        assertion_submission_allowed_for_tiers=list(data.get("assertion_submission_allowed_for_tiers", ["P", "K"])),
        global_allowed_event_types=list(data.get("global_allowed_event_types", [])),
        device_overrides=dict(data.get("device_overrides", {})),
    )


def get_connector_view_for_device(device_id: str) -> ConnectorView:
    policy = load_policy()

    if not policy.connectors_enabled:
        # Disabled connectors: disallow writes via state gate
        return ConnectorView(state="DISABLED", allowed_event_types=[])

    override = policy.device_overrides.get(device_id, {})
    state = override.get("state", policy.default_state)
    allowed = set(policy.global_allowed_event_types)
    allowed.update(override.get("allowed_event_types", []) or [])

    return ConnectorView(state=state, allowed_event_types=sorted(allowed))
