// Shared auth context for both the legacy mock.ts facade and the new client.
//
// `setAuthContext` is called once from the app layout when page data loads.
// `getAuthHeaders` is read on every request. Both `client.ts` and the older
// `mock.ts` import from here so they stay in sync — there is only one place
// the access token, tenant id, and identity id live.

export interface AuthContext {
	accessToken?: string | null;
	tenantId?: string;
	identityId?: string;
}

let _ctx: AuthContext = {};

export function setAuthContext(ctx: AuthContext): void {
	_ctx = ctx;
}

export function getAuthContext(): AuthContext {
	return _ctx;
}

export function getAuthHeaders(): Record<string, string> {
	const h: Record<string, string> = {
		'X-Tenant-ID': _ctx.tenantId || 'default',
		'X-Identity-ID': _ctx.identityId || 'dev-user',
	};
	if (_ctx.accessToken) h['Authorization'] = `Bearer ${_ctx.accessToken}`;
	return h;
}
