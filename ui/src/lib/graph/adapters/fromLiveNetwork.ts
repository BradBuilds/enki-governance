/**
 * Adapter: /feed/live/network response → UniversalNode[] + UniversalEdge[]
 *
 * The live network endpoint returns co-occurrence data with entity names
 * and optional node_ids. This adapter normalizes to the universal contract.
 */

import type { UniversalNode, UniversalEdge } from '../types';

interface LiveNetworkNode {
	name: string;
	type: string;       // "person", "organization", "country", "place", "topic"
	node_id?: string;
	count?: number;
}

interface LiveNetworkEdge {
	source: string;     // entity name
	target: string;     // entity name
	source_type?: string;
	target_type?: string;
	article_count?: number;
	strength?: number;
	sample_headline?: string;
}

interface LiveNetworkResponse {
	nodes: LiveNetworkNode[];
	edges: LiveNetworkEdge[];
	top_entities?: any[];
	emerged?: any[];
	surged?: any[];
	cooled?: any[];
}

function mapNodeType(type: string): string {
	if (['person', 'organization', 'org', 'military_unit', 'government_agency',
		'university', 'mass_media', 'research_institute'].includes(type)) return 'entity';
	if (['country', 'region', 'city', 'place', 'strait', 'bay', 'gulf',
		'canal', 'peninsula', 'island', 'mountain', 'desert', 'river',
		'lake', 'sea', 'ocean', 'airport', 'seaport'].includes(type)) return 'place';
	if (type === 'topic') return 'topic';
	return 'entity';
}

export function fromLiveNetwork(
	response: LiveNetworkResponse,
): { nodes: UniversalNode[]; edges: UniversalEdge[] } {
	// Build name → node_id lookup
	const nameToId = new Map<string, string>();
	for (const n of response.nodes) {
		nameToId.set(n.name, n.node_id || `name:${n.name}`);
	}

	const nodes = response.nodes.map(n => ({
		id: n.node_id || `name:${n.name}`,
		title: n.name,
		node_type: mapNodeType(n.type),
		subtype: n.type,
		// KnowledgeGraph component reads entity_type for coloring
		entity_type: n.type,
		mention_count: n.count || 0,
		completeness_tier: 'stub' as const,
		completeness_score: 0,
		conflict_count: 0,
	}));

	const edges: UniversalEdge[] = response.edges.map(e => ({
		source: nameToId.get(e.source) || `name:${e.source}`,
		target: nameToId.get(e.target) || `name:${e.target}`,
		relationship: 'co_occurs',
		strength: Math.min(1, (e.article_count || 1) / 10),
		article_count: e.article_count,
		evidence: e.sample_headline,
		is_directed: false,
	}));

	return { nodes, edges };
}
