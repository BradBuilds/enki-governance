# API Error Codes Registry

**Status:** Implementation Reference (Validated by `services/api/src/error_codes.py`)  
**Last Updated:** 2026-01-07  
**Purpose:** Document HTTP error codes and response envelopes for Phase 4 REST API

⚠️ **NOTE:** This documentation is auto-validated against the canonical registry in `services/api/src/error_codes.py`. The Python registry dict is the single source of truth; this markdown must match it exactly. Any drift is caught by test_api_error_code_registry.py.

This document lists error codes returned by the FastAPI application. These are **infrastructure codes** (not governance-locked spine validation codes), but they must remain stable for client integration.

---

## HTTP Error Codes (Phase 4 API)

All errors follow the **ErrorEnvelope** response model:

```json
{
  "status": "error",
  "error_code": "<CODE>",
  "detail": "<human-readable message>"
}
```

### Client Request Errors (4xx)

| Code | HTTP Status | Severity | Description | Usage |
|------|-------------|----------|-------------|-------|
| `API.BAD_REQUEST` | 400 | Validation | Invalid input format, limit constraints, or cursor validation | Query params, body parsing, pagination bounds |
| `API.PAYLOAD_TOO_LARGE` | 413 | Rejection | Request body exceeds configured size limit | POST /artifacts with body > max_artifact_bytes |

### Server Errors (5xx)

| Code | HTTP Status | Severity | Description | Usage |
|------|-------------|----------|-------------|-------|
| `API.TIMEOUT` | 503 | Transient | Request processing exceeded configured timeout | Long-running queries, database latency (future) |

### Authorization Errors (401)

These codes originate from `storage.auth` module, not API layer:

| Code | HTTP Status | Severity | Description | Usage |
|------|-------------|----------|-------------|-------|
| `AUTH.GRANT_EXPIRED` | 401 | Denial | Device grant past expiration date | Challenge resolution, route auth verification |
| `AUTH.GRANT_REVOKED` | 401 | Denial | Device grant has been revoked | Challenge resolution, route auth verification |

---

## HTTP Status Code Mapping

Determined by `_error_http_status()` function in `services/api/src/deps.py`:

```python
def _error_http_status(code: str) -> int:
    if code.startswith("AUTH."):
        return 401
    if code == "API.BAD_REQUEST":
        return 400
    if code == "API.PAYLOAD_TOO_LARGE":
        return 413
    if code == "API.TIMEOUT":
        return 503
    return 403  # fallback for undefined codes
```

---

## Configuration and Defaults

All API guardrails are environment-driven:

| Setting | Type | Default | Purpose |
|---------|------|---------|---------|
| `ENKI_API_DEFAULT_LIMIT` | int | 100 | Default pagination limit (bounds applied if not set) |
| `ENKI_API_MAX_LIMIT` | int | 1000 | Maximum pagination limit (enforced, returns 400 if exceeded) |
| `ENKI_API_MAX_ARTIFACT_BYTES` | int | 26214400 (25 MB) | Maximum artifact upload size (enforced, returns 413 if exceeded) |
| `ENKI_API_QUERY_TIMEOUT_MS` | int | 30000 (30 sec) | Query timeout (future: enforced, returns 503 if exceeded) |

Implementation: `services/api/src/config.py`

---

## Modification Guidelines

**Changes Allowed Without RFC:**
- ✅ Adding new API error codes (document here)
- ✅ Improving error message clarity
- ✅ Adding new configuration parameters
- ✅ Adjusting timeout/limit defaults

**Changes Requiring Review:**
- ⚠️ Removing codes (may break existing client monitoring)
- ⚠️ Changing HTTP status codes (breaking client contract)
- ⚠️ Removing required configuration options

**Forbidden:**
- ❌ Silently changing behavior (all guardrails must reject with clear errors)
- ❌ Moving API codes to spine validation registry (different governance domains)

---

## Implementation Notes

**Canonical Registry:** `services/api/src/error_codes.py`  
  - Python dict: `API_ERROR_CODE_REGISTRY`
  - Functions: `get_http_status(code)`, `is_registered(code)`, `all_error_codes()`
  - Single source of truth for all mappings

**Error Injection:** `services/api/src/deps.py` — `raise_enki_error(code, detail)`  
  - Uses canonical registry via `get_http_status(code)`
  - AUTH.* codes hardcoded to 401 (external module)
  - Fallback: 403 for policy/denial codes

**Response Model:** `services/api/src/models.py` — `ErrorEnvelope`  
  - JSON: `{"status": "error", "error_code": "<CODE>", "detail": "<msg>"}`
  - Used by all endpoints consistently

**Test Coverage:** `services/api/tests/test_api_error_code_registry.py`
  - Validates registry consistency (3 tests)
  - Checks that routes use only registered codes
  - Ensures no drift between Python registry and markdown doc

---

## Related Documentation

- [ERROR_CODES_LOCKED.md](../../core/packs/ERROR_CODES_LOCKED.md) — Governance-locked spine validation codes
- [INFRASTRUCTURE_ERROR_CODES.md](../../core/packs/INFRASTRUCTURE_ERROR_CODES.md) — Pack loader codes
- `services/api/src/config.py` — Guardrail configuration
- `services/api/src/error_codes.py` — **CANONICAL REGISTRY** (single source of truth)

---

**END OF API ERROR CODES REGISTRY**
