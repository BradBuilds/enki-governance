# Golden Test Vectors v1.0.0

**Purpose**: Executable test vectors proving CLIENT_CONTRACT.md conformance.

**Status**: ✅ **3 PASSING** (core vectors implemented)

---

## Structure

Each vector is a directory containing:
- `request.json` — Request details (method, path, query, headers, body if applicable)
- `response.json` — Expected response (status_code, body or body_shape)
- `README.md` — Human-readable description

**Shape markers** (in response.json):
- `"<string>"` — any string value (validated by presence/type only)
- `"<base64 string>"` — base64-encoded string
- `"<ISO8601 timestamp>"` — ISO8601 timestamp string
- Exact values — must match exactly

---

## Vectors (v1.0.0)

### Public Endpoints (No Auth) — 2 vectors
1. ✅ `health/` — GET /health → 200
2. ✅ `admin_challenges_issue/` — GET /admin/challenges/issue?device_id=test → 200 (proves path+query rule)

### Auth Failures — 1 vector
3. ✅ `auth_fail_missing_headers/` — Missing auth headers → 401 API.UNAUTHORIZED

### Authenticated Endpoints (Pending) — 10 vectors
4. ⏳ `admin_enroll_device/` — POST /admin/enroll_device
5. ⏳ `ingest_events/` — POST /ingest/events
6. ⏳ `query_assertions/` — GET /query/assertions
7. ⏳ `receipts_txid/` — GET /receipts/{txid}
8. ⏳ `audit_events/` — GET /audit-events
9. ⏳ `chain_verify/` — GET /chain/verify
10. ⏳ `artifacts_post/` — POST /artifacts
11. ⏳ `artifacts_cid_get/` — GET /artifacts/{cid}
12. ⏳ `artifacts_cid_content/` — GET /artifacts/{cid}/content
13. ⏳ `auth_fail_wrong_signature/` — Wrong signature → 401

---

## Execution

**Run all implemented vectors:**
```bash
pytest services/api/tests/test_contract_verification.py -m contract
```

**Result**: ✅ **3 passed, 10 skipped**

**v1.0.0 Lock Criteria**: Core vectors (health, challenge, auth-fail-missing) passing ✅

---

## Notes

- Pending vectors are stubbed in the harness with `@pytest.mark.skip`
- Authenticated endpoint vectors require:
  - Device enrollment (grant creation)
  - Challenge issuance + signing
  - Full auth headers in request.json
- These can be added post-lock without breaking v1.0.0 contract
