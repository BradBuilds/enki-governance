import { redirect } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ locals, url, cookies }) => {
	const code = url.searchParams.get('code');
	if (code) {
		// Store the invite code in a cookie so we can use it after Logto callback
		cookies.set('enki_invite_code', code, {
			path: '/',
			httpOnly: true,
			sameSite: 'lax',
			maxAge: 60 * 30, // 30 minutes — enough time to complete signup
		});
	}

	const origin = env.ORIGIN || url.origin;
	const callbackUrl = `${origin}/callback`;
	await locals.logtoClient.signIn(callbackUrl);
	// signIn() triggers a redirect throw
	return new Response(null, { status: 500 });
};
