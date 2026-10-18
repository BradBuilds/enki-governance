import { env } from '$env/dynamic/private';
import { dev } from '$app/environment';
import { sequence } from '@sveltejs/kit/hooks';
import type { Handle } from '@sveltejs/kit';

// ─── Auth Mode Detection ─────────────────────────────────────────────────────
// ENKI_AUTH_MODE controls which auth system the UI uses:
//   "node-local" → local email+password+TOTP (no Logto, no hardware key)
//   "hardware"   → local password auth + HWKEY hardware key (evidentiary node)
//   "full"       → Logto OIDC (cloud nodes, multi-user)
//   "dev"        → dev fallback (no real auth) — explicit opt-in only
//
// node-local and hardware share the same UI auth path: read JWT from the
// enki_session cookie, no external IdP. The difference is at the API
// layer (hardware enforces HWKEY device-key headers).
//
// Default is "node-local" (secure-by-default). Setting ENKI_AUTH_MODE=dev
// must be explicit — a missing env var must NOT fall through to dev mode
// in production. (May 28 2026 lockdown.)

const authMode = env.ENKI_AUTH_MODE || 'node-local';
const isLocalJwtMode = authMode === 'node-local' || authMode === 'hardware';

// ─── Hardware Mode: Local Auth (no Logto) ────────────────────────────────────
// Reads JWT from enki_session cookie, validates locally.
// No external OIDC provider needed. Fully offline capable.

const localAuthHandle: Handle = async ({ event, resolve }) => {
	const token = event.cookies.get('enki_session');

	if (token) {
		try {
			// Decode JWT payload (validation happens server-side on API calls)
			const parts = token.split('.');
			if (parts.length === 3) {
				const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));

				// Check expiry
				if (payload.exp && payload.exp > Date.now() / 1000) {
					event.locals.user = {
						sub: payload.sub,
						email: payload.email,
						name: payload.email, // display name resolved later
					};
					event.locals.authToken = token;
					event.locals.authMode = authMode;
				}
			}
		} catch {
			// Invalid token — treat as not logged in
		}
	}

	event.locals.authMode = event.locals.authMode || authMode;
	return resolve(event);
};

// ─── Dev Mode: No Auth ───────────────────────────────────────────────────────

const devAuthHandle: Handle = async ({ event, resolve }) => {
	event.locals.user = {
		sub: 'dev-user',
		email: 'dev@enki.local',
		name: 'Dev User',
	};
	event.locals.authMode = 'dev';
	return resolve(event);
};

// ─── Logto Mode: Full OIDC ───────────────────────────────────────────────────

let logtoHandle: Handle | null = null;

if (authMode === 'full') {
	try {
		const { handleLogto } = await import('@logto/sveltekit');

		const endpoint = env.LOGTO_ENDPOINT || '';
		const appId = env.LOGTO_APP_ID || '';
		const appSecret = env.LOGTO_APP_SECRET || '';
		const encryptionKey = env.LOGTO_COOKIE_ENCRYPTION_KEY || '';

		if (endpoint && appId) {
			logtoHandle = handleLogto(
				{ endpoint, appId, appSecret },
				{ encryptionKey }
			);
		}
	} catch {
		// Logto package not available — fall back to local auth
	}
}

// ─── Select Auth Handler ─────────────────────────────────────────────────────

const authHandle: Handle = async (input) => {
	if (isLocalJwtMode) {
		return localAuthHandle(input);
	}
	if (authMode === 'dev') {
		return devAuthHandle(input);
	}
	if (authMode === 'full' && logtoHandle) {
		return logtoHandle(input);
	}
	// Unknown mode — fall through to local auth (secure default)
	return localAuthHandle(input);
};

export const handle = sequence(authHandle);
