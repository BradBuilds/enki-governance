# Vector: Ingest Events (Authenticated)

**Endpoint**: `POST /ingest/events`

**Auth Required**: YES

**Expected Behavior**: Accepts JSONL event batch, returns ingestion receipt with chain hash

**Status**: ⏳ **NOT YET EXECUTABLE** — Blocked by `AUTH.GRANT_SIGNATURE_INVALID` in test context. Auth signature verification needs debugging before this vector can execute.

**Contract Purpose**: Documents expected request/response shape for event ingestion endpoint per CLIENT_CONTRACT.md v1.0.0. Receipt contains `<TXID>` (receipt_id) for Tier 3 vectors.

**Prereq**: Enrolled device exists with valid grant
