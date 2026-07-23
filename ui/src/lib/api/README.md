# `$lib/api` — shared API client

Every UI page should call the backend through this client. It's deliberately
generic — it knows nothing about specific entities, signals, libraries, or
domain concepts. Pages own their own response types.

## Files

| File | Purpose |
|---|---|
| `auth.ts` | Single source of truth for `accessToken`, `tenantId`, `identityId`. `setAuthContext()` is called once from `(app)/+layout.svelte` with server-provided data; `getAuthHeaders()` is read on every request. |
| `client.ts` | `api<T>(path, opts)` — the generic fetch wrapper. Plus `apiSafe`, `apiGet`, `apiPost`, `apiPut`, `apiDel`. Throws `ApiError` on non-2xx. |
| `mock.ts` | Legacy. Hybrid mock + facade. Will be retired in a later phase. New code should import from `client.ts`. |

## Usage

```ts
import { api, apiSafe, ApiError } from '$lib/api/client';

// GET with querystring
const feed = await api<{ items: any[] }>('/feed', { params: { hours: 24, limit: 50 } });

// POST with JSON body
await api('/governance/predicate-queue/42/approve', {
    method: 'POST',
    body: { reviewer: 'operator', edge_type: 'employs' },
});

// Optional / best-effort — null on failure, no throw
const geo = await apiSafe<{ latitude: number; longitude: number }>(`/entities/${id}/geo`);

// Error handling
try {
    await api('/some/path');
} catch (e) {
    if (e instanceof ApiError && e.status === 404) {
        // ...
    }
}
```

## What the client does for you

- **Path prefixing** — `'/feed'` → `'/api/feed'`. Pass `'/api/...'` if you want explicit control.
- **Tenant + identity headers** — `X-Tenant-ID` and `X-Identity-ID` are injected from `auth.ts`.
- **Bearer token** — `Authorization: Bearer …` is injected when a token is set.
- **JSON encode / decode** — pass `body: {...}` (an object). The client serialises and sets `Content-Type: application/json`. Use `rawBody: …` to send a string or `FormData` unchanged.
- **Querystring** — `params: { hours: 24, status: null }` becomes `?hours=24`. Null/undefined values are dropped.
- **Timeout** — 10 s default. Override via `timeoutMs`. Aborts via `AbortController`.
- **Retry on 429 / 503** — one retry with exponential backoff (200 ms, 800 ms). Honours `Retry-After`. Override count via `retry: 0`.
- **Structured errors** — `ApiError` carries `{ status, code, message, payload }`. The `code` is read from `error` or `code` in the JSON payload; `message` falls back through `message` → `detail` → `error` → `"HTTP ${status}"`.

## What it doesn't do

- **Response-shape validation** — pages own their own types.
- **Caching** — bring SWR later if needed.
- **Refresh-on-401** — currently throws on 401. Refresh logic will live here once the cloud-profile auth flow demands it (Phase G).
- **Real-time** — SSE / WebSocket are separate concerns.
