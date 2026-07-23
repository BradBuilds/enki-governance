import { error, redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

// Universal address router. Takes a base-60 address (e.g. "0.0.2.26"
// for Alex Acme, "13.3.4" for the Acme library) and bounces
// to the typed dossier page. Falls through to a "not found" view if
// the API has no row at that address.
export const load: PageLoad = async ({ params, fetch }) => {
	const addr = params.base60;
	if (!addr) throw error(400, 'address missing');

	const res = await fetch(`/api/address/${encodeURIComponent(addr)}`);
	if (res.status === 404) {
		return { address: addr, hit: null };
	}
	if (!res.ok) {
		throw error(res.status, `address lookup failed (${res.status})`);
	}
	const hit = await res.json();
	if (hit?.redirect_path) {
		throw redirect(307, hit.redirect_path);
	}
	return { address: addr, hit };
};
