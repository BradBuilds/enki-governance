# storage/auth/grant_store.py
"""
Filesystem-backed grant store (dev-safe, deterministic serialization).

- One file per device_id: <GRANTS_DIR>/<device_id>.json
- Loads all grants on startup into memory map
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from storage.auth.canonical import canonical_json_bytes
from storage.auth.grant import DeviceGrant


class GrantStore:
    def __init__(self, grants_dir: str):
        self.dir = Path(grants_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path_for_device(self, device_id: str) -> Path:
        return self.dir / f"{device_id}.json"

    def save(self, grant: DeviceGrant) -> None:
        p = self._path_for_device(grant.device_id)

        # deterministic JSON on disk
        p.write_bytes(canonical_json_bytes(grant.to_dict()))

    def load(self, device_id: str) -> Optional[DeviceGrant]:
        p = self._path_for_device(device_id)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return DeviceGrant(**data)

    def load_all(self) -> Dict[str, DeviceGrant]:
        out: Dict[str, DeviceGrant] = {}
        for p in self.dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                g = DeviceGrant(**data)
                out[g.device_id] = g
            except Exception:
                # ignore malformed files; ops can inspect
                continue
        return out

    def revoke(self, device_id: str, revoked_at_iso: str, reason: str) -> Optional[DeviceGrant]:
        grant = self.load(device_id)
        if not grant:
            return None
        grant.revoked_at = revoked_at_iso
        grant.revocation_reason = reason
        self.save(grant)
        return grant
