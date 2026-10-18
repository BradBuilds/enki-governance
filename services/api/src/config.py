from __future__ import annotations

import os


def get_api_default_limit() -> int:
    return int(os.environ.get('ENKI_API_DEFAULT_LIMIT', '100'))


def get_api_max_limit() -> int:
    return int(os.environ.get('ENKI_API_MAX_LIMIT', '1000'))


def get_api_max_artifact_bytes() -> int:
    # Default 25MB
    return int(os.environ.get('ENKI_API_MAX_ARTIFACT_BYTES', str(25 * 1024 * 1024)))


def get_api_query_timeout_ms() -> int:
    return int(os.environ.get('ENKI_API_QUERY_TIMEOUT_MS', '2000'))
