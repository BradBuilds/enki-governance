import { error } from '@sveltejs/kit';
import type { PageLoad } from './$types';

// Run on both server and client so SSR has the data — eliminates the
// hydration mismatch we hit when this was fetched in onMount (the
// Loading state SSR'd differently than what the client tried to hydrate).
export const load: PageLoad = async ({ params, fetch }) => {
	const [docRes, pagesRes] = await Promise.all([
		fetch(`/api/documents/${params.id}`),
		fetch(`/api/documents/${params.id}/pages?limit=20&text_chars=4000`),
	]);

	if (docRes.status === 404) {
		throw error(404, `Document ${params.id} not found`);
	}
	if (!docRes.ok) {
		throw error(docRes.status, `Failed to load document ${params.id}`);
	}

	const doc = await docRes.json();
	const pages = pagesRes.ok ? (await pagesRes.json())?.pages ?? [] : [];

	return { doc, pages };
};
