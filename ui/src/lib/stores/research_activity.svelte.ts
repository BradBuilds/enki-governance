// Selection-research store.
//
// Wraps POST /api/research/from-selection which dispatches through
// core.research.selection_router — substrate-aware entity collapse.
// Replaces the legacy term:-research pipeline (retired June 4 2026).

export type SelectionResearchResult = {
	kind: 'entity' | 'multi' | 'topic' | 'unclassified';
	entity_id: number | null;
	node_id: string | null;
	canonical_name: string | null;
	canonical_type: string | null;
	is_new: boolean;
	resolved_via: string | null;
	edge_created: boolean;
	investigation_queued: boolean;
	message: string | null;
	candidates: any[];
};

export async function researchFromSelection(
	text: string,
	opts: { context?: string; parent_entity_id?: number } = {},
): Promise<SelectionResearchResult | null> {
	const payload = {
		text: text.trim(),
		context: opts.context,
		parent_entity_id: opts.parent_entity_id,
	};
	if (!payload.text) return null;
	try {
		const res = await fetch('/api/research/from-selection', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(payload),
		});
		if (!res.ok) {
			const detail = await res.json().catch(() => ({}));
			return {
				kind: 'unclassified',
				entity_id: null,
				node_id: null,
				canonical_name: null,
				canonical_type: null,
				is_new: false,
				resolved_via: null,
				edge_created: false,
				investigation_queued: false,
				message: detail?.error || `HTTP ${res.status}`,
				candidates: [],
			};
		}
		return await res.json();
	} catch (e) {
		console.error('researchFromSelection failed', e);
		return null;
	}
}
