import { env } from '$env/dynamic/private';
import type { Actions } from './$types';

export const actions: Actions = {
	default: async ({ locals, url }) => {
		const origin = env.ORIGIN || url.origin;
		await locals.logtoClient.signOut(`${origin}/`);
	},
};
