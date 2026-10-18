import { redirect } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url, cookies, fetch }) => {
	// Logto redirects here after authentication — exchange the code for tokens
	const origin = env.ORIGIN || url.origin;
	await locals.logtoClient.handleSignInCallback(`${origin}/callback`);

	// Check if this was a signup with an invite code
	const inviteCode = cookies.get('enki_invite_code');
	if (inviteCode && locals.user) {
		// Redeem the invite code — creates tenant + identity
		try {
			const res = await fetch('/api/invite/redeem', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					code: inviteCode,
					logto_sub: locals.user.sub,
					display_name: locals.user.name || locals.user.username || locals.user.email || 'User',
					email: locals.user.email || null,
				}),
			});

			if (res.ok) {
				// Clear the invite code cookie
				cookies.delete('enki_invite_code', { path: '/' });
			}
		} catch (e) {
			console.error('Failed to redeem invite code:', e);
		}
	}

	// Post-sign-in destination: Intelligence is the default landing.
	throw redirect(302, '/intelligence');
};
