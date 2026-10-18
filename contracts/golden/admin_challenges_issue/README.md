# Vector: Challenge Issuance (No Auth Required)

**Endpoint**: `GET /admin/challenges/issue?device_id=test`

**Auth Required**: NO

**Expected Behavior**: Returns 200 with challenge fields

**Contract Rule**: Public endpoint, proves path signing includes query string

**Key Verification**: Response contains `challenge_id`, `nonce`, `issued_at`, `expires_at`
