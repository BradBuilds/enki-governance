import { error, redirect } from '@sveltejs/kit';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ params, fetch }) => {
	const res = await fetch(`/api/places/${params.id}`);

	if (res.status === 410) {
		const detail = await res.json().catch(() => ({}));
		const d = detail?.detail || detail || {};
		const target = d?.reclassified_to as string | undefined;
		const cat = d?.target_category as string | undefined;
		const slugByCat: Record<string, string> = {
			entity: 'entity',
			topic: 'topic',
			event: 'event',
			document: 'document',
		};
		if (target && cat) {
			const id = target.split(':', 2)[1];
			const slug = slugByCat[cat];
			if (slug && id) throw redirect(303, `/${slug}/${id}`);
		}
		return {
			place: null,
			reclassified: {
				target_category: cat,
				target_subtype: d?.target_subtype as string | undefined,
				pending: d?.pending === true,
				sweep_action: d?.sweep_action as string | undefined,
				sweep_reason: d?.sweep_reason as string | undefined,
			},
		};
	}

	if (res.status === 404) throw error(404, `Place ${params.id} not found`);
	if (!res.ok) throw error(res.status, `Failed to load place ${params.id}`);
	const place = await res.json();
	return { place, reclassified: null };
};
