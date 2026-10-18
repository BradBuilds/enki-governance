import { env } from '$env/dynamic/private';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ locals, url }) => {
	const origin = env.ORIGIN || url.origin;
	await locals.logtoClient.signOut(`${origin}/`);
	return new Response(null, { status: 500 });
};
