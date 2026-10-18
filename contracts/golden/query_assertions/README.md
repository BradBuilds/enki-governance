# Vector: Query Assertions (Authenticated)

**Endpoint**: `GET /query/assertions`

**Auth Required**: YES

**Expected Behavior**: Returns list of assertion events for authenticated tenant

**Status**: ⏳ **NOT YET EXECUTABLE** — Blocked by `AUTH.GRANT_SIGNATURE_INVALID` in test context. Auth signature verification needs debugging before this vector can execute.

**Contract Purpose**: Documents expected request/response shape for assertion query endpoint per CLIENT_CONTRACT.md v1.0.0. Validates GET request auth flow (empty body, body_sha256 of empty bytes).

**Prereq**: Enrolled device exists with valid grant
