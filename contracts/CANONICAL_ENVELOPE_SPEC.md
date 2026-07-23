# Canonical Signing Envelope Specification (v1.0.0)

**Authority**: Implementations MUST follow this algorithm exactly.  
**Purpose**: Deterministic Ed25519 signing payload for request authorization.  
**Status**: LOCKED for v1.0.0

---

## 1. Envelope Fields

Clients MUST construct an envelope object with these keys:

```json
{
  "body_sha256": "",
  "challenge_id": "",
  "device_id": "",
  "expires_at": "",
  "issued_at": "",
  "method": "",
  "nonce": "",
  "path": "",
  "tenant_id": ""
}
```

### Field rules

- **`challenge_id`, `nonce`, `issued_at`, `expires_at`**: copied exactly from challenge response
- **`device_id`**: from `X-Device-ID`
- **`tenant_id`**: from `X-Tenant-ID`
- **`method`**: uppercase HTTP method, e.g. `GET`, `POST`
- **`path`**: request path WITH query string if present
  - Example with query: `/admin/challenges/issue?device_id=test`
  - Example without query: `/query/assertions`
  - **Rule**: If `request.url.query` is present, path MUST be `{path}?{query}`
- **`body_sha256`**:
  - GET or no body: `""` (empty string)
  - body present: lowercase hex sha256 of raw request body bytes

---

## 2. Canonicalization

### JSON serialization
- Keys MUST be serialized in alphabetical order
- JSON MUST be compact: no extra whitespace
- UTF-8 encoding

### Reference algorithm (language-agnostic intent):

1. Construct envelope dictionary
2. Serialize as compact JSON with sorted keys
3. Encode UTF-8 bytes

**Example (Python shape only, not authoritative for clients):**

```python
canonical_json = json.dumps(envelope, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
canonical_bytes = canonical_json.encode("utf-8")
```

---

## 3. Signature

1. Sign `canonical_bytes` using Ed25519 with the device private key
2. Base64 encode signature bytes
3. Send in `X-Signature`

---

## 4. Path Rule (v1.0.0 — LOCKED)

**VERIFIED BEHAVIOR:**
- Server expects path WITH query string if query is present.
- Implementation (verified): `path = request.url.path if not request.url.query else f"{request.url.path}?{request.url.query}"`

**Client rule:**
- If making request to `/admin/challenges/issue?device_id=test`, envelope `path` MUST be `/admin/challenges/issue?device_id=test`
- If making request to `/query/assertions` (no query), envelope `path` MUST be `/query/assertions`

**Enforcement:**
- Golden vector for challenge issuance MUST include query string in envelope path.

---

## 5. Minimal Test Vector (shape only)

Use `contracts/golden/admin_challenges_issue/ok/` to confirm:
- Challenge fields match contract
- Path includes query string
- Signing inputs produce valid signature
