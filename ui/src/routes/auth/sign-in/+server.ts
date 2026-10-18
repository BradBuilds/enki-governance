import { env } from '$env/dynamic/private';
import { redirect } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ locals, url }) => {
	// Hardware mode: no Logto, redirect to local login
	const authMode = env.ENKI_AUTH_MODE || 'dev';
	if (authMode === 'hardware') {
		throw redirect(302, '/login');
	}

	// Dev mode: also redirect to local login
	if (authMode === 'dev') {
		throw redirect(302, '/login');
	}

	// Full mode: use Logto OIDC
	const origin = env.ORIGIN || url.origin;
	const callbackUrl = `${origin}/callback`;
	await locals.logtoClient.signIn(callbackUrl);
	// signIn() triggers a redirect(302) throw — this line is never reached
	return new Response(null, { status: 500 });
};
