# Vector: Auth Failure - Wrong Signature

**Endpoint**: `GET /query/assertions` (any authenticated endpoint)

**Auth Required**: YES

**Expected Behavior**: Returns 401 with `API.UNAUTHORIZED` in ENKI envelope

**Contract Rule**: Invalid signature MUST return 401 with authentication error

**Specification**: Same challenge, same device ID, signature byte flipped at position 0 (ensures failure is signature verification, not challenge/device mismatch)
