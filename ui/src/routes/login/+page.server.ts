import { redirect } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch }) => {
	// Already logged in → go to app
	if (locals.user) {
		throw redirect(302, '/hub');
	}

	// Check if any users exist (login vs first-time signup)
	let needsSetup = false;
	try {
		const res = await fetch('/api/auth/local/status');
		if (res.ok) {
			const data = await res.json();
			needsSetup = !data.has_users;
		}
	} catch {
		// API unreachable — show login form, errors surface on submit
	}

	return { needsSetup };
};
