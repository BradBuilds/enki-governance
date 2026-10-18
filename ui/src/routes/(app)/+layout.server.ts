import { redirect } from '@sveltejs/kit';
import type { LayoutServerLoad } from './$types';

export const load: LayoutServerLoad = async ({ locals, fetch }) => {
	const user = locals.user;
	const authMode = locals.authMode || 'node-local';

	// ── No user session ─────────────────────────────────────────────
	if (!user) {
		// In any non-dev mode: require login. Default is node-local, so
		// a missing or unrecognized mode redirects to /login — never
		// falls through to the dev-faked user. (May 28 2026 lockdown.)
		if (authMode !== 'dev') {
			throw redirect(302, '/login');
		}

		// Dev mode: allow unauth access with fallback user
		return {
			user: {
				sub: 'dev-user',
				email: 'dev@enki.local',
				name: 'Dev User',
				identity: {
					identity_id: 'person_dev',
					tenant_id: 'default',
					display_name: 'Dev User',
					role: 'admin',
					metadata: { email: 'dev@enki.local' },
				},
				preferences: {
					theme: 'dark',
					default_view: 'dashboard',
					timezone: 'UTC',
				},
				stats: { signal_count: 0, unseen_count: 0, project_count: 0 },
				projects: [],
				collections: [],
				active_cases: [],
			},
			auth: {
				accessToken: null,
				tenantId: 'default',
				identityId: 'person_dev',
				authMode,
			},
		};
	}

	// ── Resolve user identity from API ───────────────────────────────
	let enkiProfile = null;
	let accessToken: string | null = null;

	// Local-JWT modes (node-local, hardware): use the JWT token directly
	if (authMode === 'node-local' || authMode === 'hardware') {
		accessToken = locals.authToken || null;
	}

	// Logto mode: get access token from Logto client
	if (authMode === 'full' && locals.logtoClient) {
		try {
			const context = await locals.logtoClient.getContext({ getAccessToken: true });
			accessToken = context.accessToken || null;
		} catch {
			// Token fetch may fail on first load or expired session
		}
	}

	// Resolve identity via API
	try {
		const headers: Record<string, string> = { 'Content-Type': 'application/json' };
		if (accessToken) {
			headers['Authorization'] = `Bearer ${accessToken}`;
		}

		const res = await fetch('/api/auth/me', {
			method: 'POST',
			headers,
			body: JSON.stringify({
				logto_sub: user.sub,
				display_name: user.name || user.email || 'User',
				email: user.email || null,
			}),
		});
		if (res.ok) {
			enkiProfile = await res.json();
		}
	} catch (e) {
		console.error('Failed to resolve Enki identity:', e);
	}

	return {
		user: {
			sub: user.sub,
			email: user.email,
			name: user.name,
			identity: enkiProfile?.identity || null,
			preferences: enkiProfile?.preferences || null,
			stats: enkiProfile?.stats || null,
			projects: enkiProfile?.projects || [],
			collections: enkiProfile?.collections || [],
			active_cases: enkiProfile?.active_cases || [],
		},
		auth: {
			accessToken,
			tenantId: enkiProfile?.identity?.tenant_id || 'default',
			identityId: enkiProfile?.identity?.identity_id || user.sub || 'unknown',
			authMode,
		},
	};
};
