# ERROR_CONTRACT.md (DEPRECATED)

> **DEPRECATED (2026-01-15)**  
> Superseded by: `contracts/CLIENT_CONTRACT.md` → **Error Handling** section.  
> Reason: this file described future error codes that were not implemented in the canonical registry at the time.  
> This file remains only as historical reference.

---

# External Error Contract (Phase 4.4A Gate) - HISTORICAL REFERENCE ONLY

Status: DRAFT (to be LOCKED at 4.4A completion)
Authority: services/api/src/error_codes.py (canonical registry, read-only via MappingProxyType)
Tests: services/api/tests/test_api_error_code_registry.py (governance + immutability)

## Envelope Shape (public)
```
{
  "detail": {
    "error_code": "<NAMESPACE.CODE>",
    "message": "<human-readable>",
    "meta": { ... }
  },
  "request_id": "<uuid-like>"
}
```
- `detail.error_code`: required, string, matches registry (API.* or SERVER.* for external).
- `detail.message`: required, string, stable per code, no dynamic data.
- `detail.meta`: optional, structured, may include safe field-level info; never required for stability.
- `request_id`: required, string, opaque; deterministic in tests via harness injection.

## Public vs Internal Codes
- Public namespaces: API.*, SERVER.* (exposed to external callers)
- Internal-only codes MUST NOT appear externally.
- Unknown API codes map to deterministic 500 with `SERVER.MISCONFIGURATION` (see below).

## 4xx Policy
- Only API.* codes appear for client-caused errors.
- No stack traces, no internal identifiers.
- Messages are stable and static; any variable details go in `detail.meta`.
- 401 API.UNAUTHORIZED: missing/invalid auth credentials.
- 403 API.FORBIDDEN: auth valid but not allowed (scope/tenant/policy).
- 404 API.NOT_FOUND: resource not found (e.g., txid, cid) — framework 404s are out of scope unless globally wrapped.

## 5xx Policy (Deterministic 500)
- Unknown/unregistered API codes → 500 with `SERVER.MISCONFIGURATION`.
- detail.message for 500: deterministic, operator-safe, no stack traces.
- request_id is always present; harness may fix it for golden vectors.

## Header Semantics
- Content-Type: application/json for error envelopes.
- Status codes: align with existing contract (400, 401, 403, 404, 500). 429/503 not used unless explicitly added and documented here first.

## Lock Criteria for 4.4A
- Tests assert envelope shape for all 4xx and 5xx responses.
- Registry immutability enforced (MappingProxyType) and verified by tests.
- All public routes in CONTRACT_INDEX reference only registry codes; no ad-hoc strings.
- Golden vectors cover representative 4xx and forced 5xx paths per endpoint.

## Notes
- Pydantic v2 deprecation warning is known/non-blocking; migration scheduled Phase 5.
