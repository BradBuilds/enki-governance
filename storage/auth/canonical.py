# storage/auth/canonical.py
import json
from typing import Any

def canonical_json_bytes(obj: Any) -> bytes:
    """
    Deterministic JSON serialization:
    - sort_keys=True
    - separators without whitespace
    - UTF-8 bytes
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
