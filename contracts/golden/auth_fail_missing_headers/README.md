# Vector: Auth Failure - Missing Headers

**Endpoint**: `GET /query/assertions` (any authenticated endpoint)

**Auth Required**: YES

**Expected Behavior**: Returns 401 with `API.UNAUTHORIZED` in ENKI envelope

**Contract Rule**: Missing required auth headers MUST return 401 (not 422)

**Micro-Gate Verification**: Proves RequestValidationError handler works
