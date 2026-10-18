/**
 * Envelope summary helpers — the single source of truth for extracting
 * chip data from a node envelope on the client.
 *
 * The backend returns `envelope_summary` on list endpoints. These helpers
 * turn that structure into render-ready chip arrays. When we add a new
 * chip type (media nodes, outlets, patterns) the helper file is the ONE
 * place to touch — no card component needs to know the envelope shape.
 *
 * Shape contract (matches core.extraction.envelope.build_envelope_summary):
 *
 *   {
 *     identity: { title_display, canonical_name },
 *     temporal: { event_time, precision },
 *     spatial:  { place_text, resolved_places: [{node_id,label}], coordinates },
 *     entities: [{ node_id, label, role?, weight? }],
 *     topics:   [{ node_id, label, weight? }],
 *     events:   [{ node_id }],
 *     source:   { outlet_name, url },
 *     evidence: { epistemic_type, source_count, corroborations, contradictions },
 *     completeness: { score, tier },
 *   }
 */

export type EnvelopeSummary = {
	identity?: { title_display?: string | null; canonical_name?: string | null };
	temporal?: { event_time?: string | null; precision?: string | null };
	spatial?: {
		place_text?: string[];
		resolved_places?: Array<{ node_id: string; label: string }>;
		coordinates?: { lat: number; lon: number } | null;
	};
	entities?: Array<{ node_id: string; label: string; role?: string | null; weight?: number | null }>;
	topics?: Array<{ node_id: string; label: string; weight?: number | null }>;
	events?: Array<{ node_id: string }>;
	media?: Array<{
		node_id: string;
		url?: string | null;
		width?: number | null;
		height?: number | null;
		role?: string | null;
	}>;
	source?: { outlet_name?: string | null; url?: string | null };
	evidence?: {
		epistemic_type?: string | null;
		source_count?: number | null;
		corroborations?: number | null;
		contradictions?: number | null;
	};
	completeness?: { score?: number | null; tier?: string | null };
};

export type Chip = {
	node_id: string;
	label: string;
	node_type: 'entity' | 'place' | 'topic' | 'event' | 'media';
	role?: string | null;
	/** Thumbnail URL (only set for media chips) — preview src. */
	thumbnail_url?: string | null;
};

/**
 * Entities as navigable chips. Sorted by weight DESC (nulls last) so the
 * most-relevant names render first inside a wrap row.
 */
export function getEntityChips(env: EnvelopeSummary | null | undefined): Chip[] {
	if (!env?.entities) return [];
	return [...env.entities]
		.sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0))
		.map(e => ({
			node_id: e.node_id,
			label: e.label,
			node_type: 'entity' as const,
			role: e.role ?? null
		}));
}

export function getTopicChips(env: EnvelopeSummary | null | undefined): Chip[] {
	if (!env?.topics) return [];
	return [...env.topics]
		.sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0))
		.map(t => ({
			node_id: t.node_id,
			label: t.label,
			node_type: 'topic' as const
		}));
}

export function getPlaceChips(env: EnvelopeSummary | null | undefined): Chip[] {
	const resolved = env?.spatial?.resolved_places ?? [];
	return resolved.map(p => ({
		node_id: p.node_id,
		label: p.label,
		node_type: 'place' as const
	}));
}

export function getEventChips(env: EnvelopeSummary | null | undefined): Chip[] {
	const events = env?.events ?? [];
	return events.map(e => ({
		node_id: e.node_id,
		label: e.node_id,           // titles not in summary; detail fetch resolves
		node_type: 'event' as const
	}));
}

/**
 * Media chips — rendered by NodeChip with a thumbnail preview. The
 * thumbnail_url points at the archived bytes served through the API,
 * which works even after the original outlet URL has gone dead.
 */
export function getMediaChips(env: EnvelopeSummary | null | undefined): Chip[] {
	const media = env?.media ?? [];
	return media.map(m => ({
		node_id: m.node_id,
		label: m.url ?? m.node_id,
		node_type: 'media' as const,
		role: m.role ?? null,
		thumbnail_url: `/api/media/${encodeURIComponent(m.node_id)}/image`
	}));
}

/** Any chip at all — useful for "is this card connected to anything?" tests. */
export function hasAnyChips(env: EnvelopeSummary | null | undefined): boolean {
	return (
		(env?.entities?.length ?? 0) > 0 ||
		(env?.topics?.length ?? 0) > 0 ||
		(env?.spatial?.resolved_places?.length ?? 0) > 0 ||
		(env?.events?.length ?? 0) > 0 ||
		(env?.media?.length ?? 0) > 0
	);
}
