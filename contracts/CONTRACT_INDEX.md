# CONTRACT_INDEX.md (REFERENCE — NOT AUTHORITY)

> **NOT AUTHORITY (2026-01-15)**  
> Protocol authority is: `contracts/CLIENT_CONTRACT.md` (v1.0.0).  
> This file remains as a convenient registry view, but it is not the contract law.

---

# External Contract Surface (Phase 4.4A Gate) - REFERENCE VIEW ONLY

Status: DRAFT (to be LOCKED when 4.4A completes)
Scope: Public API contract surface. Every public route must appear here before Phase 4.4A is marked complete.
Stability Legend: LOCKED = frozen; GROWABLE = additive-only; EXPERIMENTAL = unstable.

Device Auth Header Set (Phase 3.6 contract):
- X-Device-ID (req)
- X-Challenge-ID (req)
- X-Tenant-ID (req)
- X-Body-SHA256 (req for requests with body; omitted for GET)
- X-Signature (req)

List Response Envelope (for collection endpoints):
{ "items": [...], "next_cursor"?: string, "total"?: int }

Contract Strictness Legend: ENVELOPE_ONLY | MINIMAL_SHAPE | FULL_STRICT

| Endpoint | Method | Purpose | Auth Requirement | Request Schema (fields; req/opt) | Response Schema | Error Codes (public) | Stability | Contract Strictness |
|----------|--------|---------|------------------|----------------------------------|-----------------|----------------------|-----------|--------------------|
| /health | GET | Liveness/health probe | None | None | { status: string } | SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /admin/challenges/issue | GET | Issue a challenge nonce for device auth | None | query: device_id (req) | challenge payload (id, nonce, issued_at, expires_at) | API.BAD_REQUEST, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /admin/enroll_device | POST | Enroll a device grant | Device Auth Header Set + root key present; admin credentials | body: grant payload (JSON) | grant receipt | API.BAD_REQUEST, API.UNAUTHORIZED, API.FORBIDDEN, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /ingest/events | POST | Ingest audit/assertion events | Device Auth Header Set (includes X-Body-SHA256) | body: NDJSON events | receipt with counts | API.BAD_REQUEST, API.UNAUTHORIZED, API.FORBIDDEN, API.NOT_FOUND, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /query/assertions | GET | Query assertion events | Device Auth Header Set (no body hash) | query: filters | List Response Envelope of assertion events | API.BAD_REQUEST, API.UNAUTHORIZED, API.FORBIDDEN, API.NOT_FOUND, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /receipts/{txid} | GET | Fetch transaction receipt by txid | Device Auth Header Set (no body hash) | path: txid (req) | receipt | API.BAD_REQUEST, API.UNAUTHORIZED, API.FORBIDDEN, API.NOT_FOUND, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /audit-events | GET | Fetch audit events | Device Auth Header Set (no body hash) | query: filters | List Response Envelope of audit events | API.BAD_REQUEST, API.UNAUTHORIZED, API.FORBIDDEN, API.NOT_FOUND, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /chain/verify | GET | Verify proofchain integrity | Device Auth Header Set (no body hash) | query: txid? (optional) | verification result | API.BAD_REQUEST, API.UNAUTHORIZED, API.FORBIDDEN, API.NOT_FOUND, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /artifacts | POST | Upload artifact bytes | Device Auth Header Set (includes X-Body-SHA256) + Content-Type | body: bytes | artifact metadata (CID) | API.BAD_REQUEST, API.UNAUTHORIZED, API.FORBIDDEN, API.NOT_FOUND, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /artifacts/{cid} | GET | Fetch artifact metadata | Device Auth Header Set (no body hash) | path: cid | artifact metadata | API.BAD_REQUEST, API.UNAUTHORIZED, API.FORBIDDEN, API.NOT_FOUND, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |
| /artifacts/{cid}/content | GET | Fetch artifact content | Device Auth Header Set (no body hash) | path: cid | raw bytes | API.BAD_REQUEST, API.UNAUTHORIZED, API.FORBIDDEN, API.NOT_FOUND, SERVER.MISCONFIGURATION | GROWABLE | MINIMAL_SHAPE |

To-do for lock:
- Fill precise request/response schemas per endpoint (field names/types, required/optional, enums).
- Mark stability for each once frozen (expected LOCKED at 4.4A complete; some may remain GROWABLE with explicit additive rules).
- Ensure every public route is present; add pagination/filters explicitly where applicable.
