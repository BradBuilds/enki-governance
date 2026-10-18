/**
 * Visual encoding functions for node radius, edge width, opacity.
 * Shared across KnowledgeGraph, Intelligence view, Map markers.
 */

import type { UniversalNode, UniversalEdge } from './types';

/** Node radius based on mention count + depth + focal status */
export function nodeRadius(
	node: UniversalNode,
	opts?: { focalId?: string; maxMentions?: number },
): number {
	if (opts?.focalId && node.id === opts.focalId) return 28;

	const mentions = node.mention_count || 1;
	const maxMentions = opts?.maxMentions || mentions;
	const ratio = maxMentions > 0 ? mentions / maxMentions : 0;

	const base = 10 + Math.sqrt(ratio) * 16;
	const depthScale = 1 - (node.traverse_depth || 0) * 0.12;
	return Math.max(8, Math.min(28, base * depthScale));
}

/** Node opacity based on traverse depth */
export function nodeOpacity(node: UniversalNode): number {
	const d = node.traverse_depth || 0;
	return Math.max(0.4, 1 - d * 0.15);
}

/** Node border style based on completeness tier */
export function nodeBorderDash(tier: string | undefined): string {
	if (tier === 'stub') return '2,3';
	if (tier === 'partial') return '4,2';
	return 'none';
}

/** Node label — truncated for graph display */
export function nodeLabel(title: string, maxLen = 20): string {
	if (title.length <= maxLen) return title;
	return title.slice(0, maxLen - 2) + '...';
}

/** Node icon emoji by subtype */
export function nodeIcon(subtype: string | null | undefined): string {
	if (!subtype) return '●';
	switch (subtype) {
		case 'person': return '👤';
		case 'organization': case 'org': return '🏢';
		case 'military_unit': return '🎖';
		case 'government_agency': return '🏛';
		case 'country': return '🌍';
		case 'region': return '🗺️';
		case 'city': return '🏙';
		case 'location': case 'facility': return '📍';
		case 'aircraft': case 'helicopter': case 'airliner': return '✈';
		case 'vessel': return '🚢';
		case 'vehicle': return '🚗';
		case 'weapon': return '⚔';
		case 'topic': return '💡';
		case 'event': return '⚡';
		case 'document': case 'source': return '📄';
		default: return '●';
	}
}

/** Edge width from strength or article count */
export function edgeWidth(edge: UniversalEdge): number {
	if (edge.article_count) {
		return Math.max(1, Math.min(6, edge.article_count / 3));
	}
	return Math.max(1, (edge.strength || 0.5) * 3);
}

/** Edge opacity */
export function edgeOpacity(edge: UniversalEdge): number {
	if (edge.article_count) {
		return 0.2 + Math.min(edge.article_count / 20, 0.5);
	}
	return 0.3 + (edge.strength || 0.5) * 0.4;
}

/** Edge dash pattern by relationship type */
export function edgeDash(relationship: string): string {
	if (relationship === 'contrasts' || relationship === 'questions' || relationship === 'opposes') return '4,3';
	if (relationship === 'extends' || relationship === 'references') return '2,2';
	return 'none';
}
