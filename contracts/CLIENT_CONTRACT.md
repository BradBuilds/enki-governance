# ENKI Node Client Contract v1.0.0 (LOCKED)

**Authority**: This document defines the client protocol surface for ENKI Node v1.0.0.  
**Rule**: Node implementation MUST match this contract. Clients MUST implement this contract.  
**Coupling**: Clients MUST NOT import Node code. Node MUST NOT import client code.  
**Stability**: LOCKED — breaking changes require MAJOR version bump.  
**Status Date**: 2026-01-15

---

## 0. Versioning Policy (SemVer)

- **MAJOR**: breaking protocol changes (headers, envelope fields, endpoint paths, removed error codes)
- **MINOR**: additive changes (new endpoints, new optional fields, new error codes)
- **PATCH**: clarifications, bug fixes that do not change wire protocol

Changelog: `API_CHANGELOG.md`

---

## 1. Transport & Base Rules

### 1.1 Transport
- Development: HTTP is permitted.
- Production: HTTPS (TLS) is REQUIRED (reverse proxy acceptable).

### 1.2 Content Types
- JSON endpoints use `application/json`.
- Binary content endpoints return bytes (see endpoint list).

### 1.3 Authentication Model
ENKI Node uses **Device Authorization** based on:
- Challenge issuance (`/admin/challenges/issue`)
- Canonical signing envelope
- Ed25519 signature in `X-Signature`
- Device grants (scope enforcement may evolve; see §4.3)

---

## 2. Auth Header Set (v1.0.0)

### 2.1 When required
Auth headers are REQUIRED for all endpoints except:
- `GET /health`
- `GET /admin/challenges/issue`

### 2.2 Required headers (authenticated endpoints)
| Header | Required | Format | Notes |
|---|---:|---|---|
| `X-Device-ID` | YES | string | Device identifier |
| `X-Challenge-ID` | YES | string | Issued by challenge endpoint |
| `X-Tenant-ID` | YES | string | Tenant identifier |
| `X-Signature` | YES | base64 | Ed25519 signature of canonical envelope |
| `X-Body-SHA256` | Conditional | 64-char lowercase hex | Required for requests with a body; omitted for GET and for empty-body requests |

**Measured behavior (verified):**
- Missing required auth headers → **401** with ENKI error envelope and `API.UNAUTHORIZED`
- This is enforced even when FastAPI would otherwise return 422 (missing header validation), via a RequestValidationError handler.

---

## 3. Canonical Signing Envelope (v1.0.0)

**Authority**: `contracts/CANONICAL_ENVELOPE_SPEC.md`

Clients MUST:
1) Obtain a challenge (nonce + timestamps)
2) Construct the canonical envelope
3) Serialize deterministically
4) UTF-8 encode
5) Ed25519 sign
6) Base64 encode signature → `X-Signature`

---

## 4. Errors

### 4.1 Error Envelope (v1.0.0)
All contract-defined errors MUST be returned in the ENKI error envelope format:

```json
{
  "error_code": "NAMESPACE.CODE",
  "detail": "human-readable message",
  "meta": {}
}
```

**Notes:**
- `meta` MAY be omitted or empty when not applicable.
- If the implementation includes additional top-level fields (e.g., request_id), they MAY be present, but clients MUST rely only on the fields above for portability.
- **v1.0.0 Implementation Note**: Current FastAPI implementation wraps this envelope in an additional `detail` key: `{"detail": {"error_code": "...", "detail": "...", "meta": {}}}`. Golden vectors validate this actual wire behavior. Normalization to contract-defined shape may occur in v1.1.0 via middleware.

### 4.2 Error Codes (v1.0.0)

These error codes are part of the public contract and MUST remain stable within v1.x:

| Error Code | Typical HTTP | Meaning |
|---|---:|---|
| `API.BAD_REQUEST` | 400 | Invalid request (contract-defined) |
| `API.PAYLOAD_TOO_LARGE` | 413 | Body too large |
| `API.TIMEOUT` | 503 | Transient timeout condition |
| `API.UNAUTHORIZED` | 401 | Missing/invalid authentication |
| `API.NOT_FOUND` | 404 | Resource not found |
| `SERVER.MISCONFIGURATION` | 500 | Server missing required configuration |

**Measured behavior (verified):**
- Missing auth headers → 401 `API.UNAUTHORIZED` (ENKI envelope)
- Missing resource (404 path/resource) → ENKI infrastructure ready (should emit `API.NOT_FOUND`)
- Misconfiguration → 500 `SERVER.MISCONFIGURATION` (ENKI envelope)

### 4.3 Forbidden / Scope Enforcement (v1.0.0 note)

- `API.FORBIDDEN` (403) is registered in the server registry.
- Emission depends on scope/grant enforcement behavior.
- Clients MUST handle 403 if returned, but 403 is not required for v1.0.0 conformance until a stable "wrong scope" repro case is locked in golden vectors.

---

## 5. Endpoints (v1.0.0)

### 5.1 Public (No Auth)

#### `GET /health`
- 200 JSON response indicating service status.

#### `GET /admin/challenges/issue?device_id={id}`
- 200 JSON challenge response.
- Response fields MUST match implementation.
- Clients MUST treat the returned values as opaque strings copied into the signing envelope.

**Challenge response (verified):**
```json
{
  "challenge_id": "<string>",
  "nonce": "<base64 string>",
  "issued_at": "<ISO8601 timestamp with timezone>",
  "expires_at": "<ISO8601 timestamp with timezone>"
}
```

### 5.2 Authenticated (Auth Required)

- `POST /admin/enroll_device`
- `POST /ingest/events`
- `GET /query/assertions`
- `GET /receipts/{txid}`
- `GET /audit-events`
- `GET /chain/verify`
- `POST /artifacts`
- `GET /artifacts/{cid}`
- `GET /artifacts/{cid}/content` (binary)

**Response schemas:**
- If there is an existing schema index, it MAY be referenced.
- v1.0.0 contract lock does not require full field-level schemas for every endpoint; it requires:
  - paths + methods
  - auth requirements
  - error envelope + codes
  - canonical signing rules

---

## 6. Golden Vectors (v1.0.0)

**Location:** `contracts/golden/`

**Minimum required vectors:**
- 11 happy-path vectors (one per endpoint)
- 2 auth-failure vectors:
  - missing auth headers → 401 `API.UNAUTHORIZED`
  - wrong signature → 401 `API.UNAUTHORIZED` (or other auth error if implemented, but must be stable)

**Vectors MUST be executable:**
- No placeholder values unless the harness supports shape assertions.

---

## 7. Lock Statement

This contract is LOCKED as v1.0.0 upon:
1. Micro-gate passing (auth missing headers returns 401 ENKI envelope)
2. Golden vectors passing (v1.0.0 set)
3. Contract verification harness passing

**Status:** LOCKED v1.0.0 (pending harness + vectors)
