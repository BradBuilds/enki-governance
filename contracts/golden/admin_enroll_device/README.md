# Vector: Admin Enroll Device (Authenticated)

**Endpoint**: `POST /admin/enroll_device`

**Auth Required**: YES (Tier K admin device)

**Expected Behavior**: Enrolled admin creates new device grant, returns grant record

**Status**: ⏳ **NOT YET EXECUTABLE** — Blocked by `AUTH.GRANT_SIGNATURE_INVALID` in test context. Auth signature verification needs debugging before this vector can execute.

**Contract Purpose**: Documents expected request/response shape for device enrollment endpoint per CLIENT_CONTRACT.md v1.0.0.

**Prereq**: Enrolled Tier K device exists with valid grant
