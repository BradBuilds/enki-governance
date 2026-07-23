from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Dict, Any


BASE_DIR = Path("storage/artifacts")


def compute_bytes_cid(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _paths(tenant_id: str, cid: str) -> tuple[Path, Path]:
    tenant_dir = BASE_DIR / tenant_id
    content_path = tenant_dir / f"{cid}.bin"
    meta_path = tenant_dir / f"{cid}.json"
    return content_path, meta_path


def save_artifact(tenant_id: str, data: bytes, content_type: Optional[str]) -> Dict[str, Any]:
    cid = compute_bytes_cid(data)
    content_path, meta_path = _paths(tenant_id, cid)
    content_path.parent.mkdir(parents=True, exist_ok=True)

    # Write content
    with open(content_path, "wb") as f:
        f.write(data)

    # Write metadata
    meta = {
        "cid": cid,
        "byte_length": len(data),
        "content_type": content_type,
    }
    import json
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)

    return meta


def read_artifact_metadata(tenant_id: str, cid: str) -> Optional[Dict[str, Any]]:
    import json
    _, meta_path = _paths(tenant_id, cid)
    if not meta_path.exists():
        return None
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_artifact_content(tenant_id: str, cid: str) -> Optional[bytes]:
    content_path, _ = _paths(tenant_id, cid)
    if not content_path.exists():
        return None
    with open(content_path, "rb") as f:
        return f.read()
