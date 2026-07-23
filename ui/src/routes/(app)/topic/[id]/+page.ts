import { error, redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

// Load via SSR so the page hydrates cleanly (avoids the hydration
// mismatch that happens when onMount-driven pages render Loading on
// the server but populate state only on the client).
export const load: PageLoad = async ({ params, fetch }) => {
	const res = await fetch(`/api/topics/${params.id}`);

	// 410 Gone = topic has been reclassified to another substrate.
	// Mirror the entity dossier handling: navigate to the target if
	// a route exists, else render a placeholder explaining what happened.
	if (res.status === 410) {
		const detail = await res.json().catch(() => ({}));
		const d = detail?.detail || detail || {};
		const target = d?.reclassified_to as string | undefined;
		const cat = d?.target_category as string | undefined;
		const sub = d?.target_subtype as string | undefined;
		const pending = d?.pending === true;
		const sweep_action = d?.sweep_action as string | undefined;

		// Tier-2 migration with a target slug → redirect
		const slugByCat: Record<string, string> = {
			entity: 'entity',
			place: 'place',
			event: 'event',
			document: 'document',
		};
		if (target && cat) {
			const id = target.split(':', 2)[1];
			const slug = slugByCat[cat];
			if (slug && id) {
				throw redirect(303, `/${slug}/${id}`);
			}
		}
		return {
			topic: null,
			reclassified: {
				target_category: cat,
				target_subtype: sub,
				pending,
				sweep_action,
				sweep_reason: d?.sweep_reason as string | undefined,
			},
		};
	}

	if (res.status === 404) {
		throw error(404, `Topic ${params.id} not found`);
	}
	if (!res.ok) {
		throw error(res.status, `Failed to load topic ${params.id}`);
	}
	const topic = await res.json();
	return { topic, reclassified: null };
};
