# ENKI Node API Changelog

## v1.0.0 — 2026-01-15

**Initial client contract lock**

### Defined
- Device auth headers: `X-Device-ID`, `X-Challenge-ID`, `X-Tenant-ID`, `X-Signature`, `X-Body-SHA256` (conditional)
- Canonical signing envelope with path+query rule
- Error envelope format: `{"error_code": "...", "detail": "...", "meta": {...}}`
- 11 endpoints (2 public, 9 authenticated)
- 6 error codes locked: `API.BAD_REQUEST`, `API.PAYLOAD_TOO_LARGE`, `API.TIMEOUT`, `API.UNAUTHORIZED`, `API.NOT_FOUND`, `SERVER.MISCONFIGURATION`

### Verified
- Missing auth headers → 401 `API.UNAUTHORIZED` with ENKI envelope
- Challenge response fields: `challenge_id`, `nonce`, `issued_at`, `expires_at`
- Path signing includes query string if present

### Status
- CONTRACT LOCKED v1.0.0 (pending golden vectors + harness)
- Micro-gate: PASSED
- Golden vectors: IN PROGRESS
- Contract harness: IN PROGRESS
