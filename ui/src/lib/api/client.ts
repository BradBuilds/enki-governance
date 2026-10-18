// Shared API client. Every page should call through `api()` instead of raw
// fetch(). The client handles:
//   - prefixing /api so callers can pass either '/feed' or '/api/feed'
//   - injecting tenant + identity + bearer headers (see ./auth.ts)
//   - JSON encode + decode with safe error parsing
//   - per-request AbortController + timeout (default 10 s)
//   - one retry on 429 / 503 with exponential backoff (200 ms, 800 ms)
//   - structured ApiError with { status, code, message, payload }
//
// Generic by construction: it knows nothing about specific entities, signals,
// or any other domain concept. Pages are responsible for their own types.

import { getAuthHeaders } from './auth';

const BASE = (import.meta.env?.PUBLIC_API_URL as string | undefined) || '/api';

export interface ApiOptions extends Omit<RequestInit, 'body'> {
	/** JSON-serialisable body. Set `rawBody` to send a string / FormData unchanged. */
	body?: unknown;
	/** Skip JSON.stringify on body and skip the Content-Type header. */
	rawBody?: BodyInit | null;
	/** Querystring params; null/undefined values are dropped. */
	params?: Record<string, string | number | boolean | null | undefined>;
	/** Per-request timeout in ms (default 10_000). */
	timeoutMs?: number;
	/** Retry count for 429 / 503 (default 1). */
	retry?: number;
}

export class ApiError extends Error {
	status: number;
	code: string | null;
	payload: unknown;
	constructor(status: number, code: string | null, message: string, payload?: unknown) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
		this.code = code;
		this.payload = payload;
	}
}

function resolvePath(path: string, params?: ApiOptions['params']): string {
	const root = path.startsWith('/api/') || path.startsWith('http')
		? path
		: `${BASE}${path.startsWith('/') ? path : `/${path}`}`;
	if (!params) return root;
	const usp = new URLSearchParams();
	for (const [k, v] of Object.entries(params)) {
		if (v === undefined || v === null) continue;
		usp.set(k, String(v));
	}
	const qs = usp.toString();
	if (!qs) return root;
	return root.includes('?') ? `${root}&${qs}` : `${root}?${qs}`;
}

function buildInit(opts: ApiOptions): RequestInit {
	const headers: Record<string, string> = {
		...getAuthHeaders(),
		...((opts.headers as Record<string, string>) || {}),
	};
	// Pull out client-only fields so we don't shove them into RequestInit;
	// `body` is also separated because our type widens it to `unknown`.
	const { body, rawBody, params: _p, timeoutMs: _t, retry: _r, headers: _h, ...rest } = opts;
	const init: RequestInit = { ...rest, headers };

	if (rawBody !== undefined) {
		init.body = rawBody;
	} else if (body !== undefined) {
		init.body = JSON.stringify(body);
		if (!headers['Content-Type']) headers['Content-Type'] = 'application/json';
	}
	return init;
}

async function parseJson(res: Response): Promise<unknown> {
	const text = await res.text();
	if (!text) return null;
	try { return JSON.parse(text); } catch { return text; }
}

async function throwFromResponse(res: Response): Promise<never> {
	const payload = await parseJson(res);
	const obj = (payload && typeof payload === 'object') ? payload as Record<string, unknown> : {};
	const code = (typeof obj.code === 'string' ? obj.code
		: typeof obj.error === 'string' ? obj.error : null);
	const message = (typeof obj.message === 'string' ? obj.message
		: typeof obj.detail === 'string' ? obj.detail
		: typeof obj.error === 'string' ? obj.error
		: `HTTP ${res.status}`);
	throw new ApiError(res.status, code, message, payload);
}

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

export async function api<T = unknown>(path: string, opts: ApiOptions = {}): Promise<T> {
	const url = resolvePath(path, opts.params);
	const timeoutMs = opts.timeoutMs ?? 10_000;
	const maxRetry = opts.retry ?? 1;

	let lastErr: unknown = null;
	for (let attempt = 0; attempt <= maxRetry; attempt++) {
		const ctrl = new AbortController();
		// Honour any user-supplied signal; otherwise just use ours.
		if (opts.signal) {
			opts.signal.addEventListener('abort', () => ctrl.abort(), { once: true });
		}
		const timer = setTimeout(() => ctrl.abort(), timeoutMs);

		let res: Response;
		try {
			res = await fetch(url, { ...buildInit(opts), signal: ctrl.signal });
		} catch (e) {
			lastErr = e;
			clearTimeout(timer);
			// Network or timeout — retry once for transient flakes
			if (attempt < maxRetry) { await sleep(200 * Math.pow(4, attempt)); continue; }
			if ((e as Error)?.name === 'AbortError') {
				throw new ApiError(0, 'TIMEOUT', `Request to ${path} timed out after ${timeoutMs} ms`);
			}
			throw new ApiError(0, 'NETWORK', (e as Error)?.message || 'Network error');
		}
		clearTimeout(timer);

		if (res.ok) {
			if (res.status === 204) return null as T;
			return (await parseJson(res)) as T;
		}

		// Retry on 429 (rate limited) or 503 (temporarily unavailable). Honour
		// Retry-After header if it's present, otherwise use exponential backoff.
		if ((res.status === 429 || res.status === 503) && attempt < maxRetry) {
			const ra = Number(res.headers.get('Retry-After'));
			const wait = Number.isFinite(ra) && ra > 0 ? ra * 1000 : 200 * Math.pow(4, attempt);
			await sleep(wait);
			continue;
		}

		await throwFromResponse(res);
	}
	// Unreachable in practice — the loop either returns or throws.
	throw lastErr ?? new ApiError(0, 'UNKNOWN', 'request failed');
}

/** Same as api() but returns null on error instead of throwing. Useful for
 *  optional / best-effort calls (e.g. /entities/{id}/geo where missing geo is
 *  not an error condition). */
export async function apiSafe<T = unknown>(path: string, opts: ApiOptions = {}): Promise<T | null> {
	try { return await api<T>(path, opts); } catch { return null; }
}

/** Convenience helpers — same options surface as `api()`. */
export const apiGet  = <T = unknown>(p: string, o: ApiOptions = {}) => api<T>(p, { ...o, method: 'GET' });
export const apiPost = <T = unknown>(p: string, o: ApiOptions = {}) => api<T>(p, { ...o, method: 'POST' });
export const apiPut  = <T = unknown>(p: string, o: ApiOptions = {}) => api<T>(p, { ...o, method: 'PUT' });
export const apiDel  = <T = unknown>(p: string, o: ApiOptions = {}) => api<T>(p, { ...o, method: 'DELETE' });
