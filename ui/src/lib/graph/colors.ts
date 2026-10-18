/**
 * Single source of truth for all visualization colors.
 * Imported by KnowledgeGraph, Intelligence page, Map markers, etc.
 */

// Entity subtype → color
export const ENTITY_COLORS: Record<string, string> = {
	// People
	person: '#8b5cf6',
	fictional_person: '#6b7280',

	// Organizations
	organization: '#3b82f6',
	military_unit: '#b91c1c',
	government_agency: '#1e40af',
	university: '#7c3aed',
	mass_media: '#f472b6',
	research_institute: '#c084fc',

	// Places
	country: '#10b981',
	region: '#14b8a6',
	city: '#22c55e',
	location: '#22c55e',
	facility: '#059669',

	// Geography (natural features)
	strait: '#06b6d4',
	bay: '#06b6d4',
	gulf: '#06b6d4',
	canal: '#06b6d4',
	peninsula: '#0d9488',
	island: '#0d9488',
	island_group: '#0d9488',
	archipelago: '#0d9488',
	mountain: '#78716c',
	mountain_range: '#78716c',
	desert: '#d97706',
	river: '#0284c7',
	lake: '#0284c7',
	sea: '#0284c7',
	ocean: '#0284c7',
	continent: '#059669',
	reef: '#06b6d4',

	// Infrastructure
	airport: '#06b6d4',
	seaport: '#0284c7',
	railway_station: '#6366f1',
	pipeline: '#78716c',
	bridge: '#78716c',
	dam: '#78716c',
	power_plant: '#eab308',
	refinery: '#78716c',

	// Equipment
	aircraft: '#06b6d4',
	helicopter: '#0891b2',
	airliner: '#06b6d4',
	vessel: '#0284c7',
	vehicle: '#6366f1',
	weapon: '#ef4444',
	spacecraft: '#8b5cf6',
	unmanned_aerial_vehicle: '#06b6d4',

	// Abstract
	event: '#fbbf24',
	topic: '#64748b',
	document: '#94a3b8',
	source: '#94a3b8',
	chronicle: '#78716c',
	assessment: '#f59e0b',
	pattern: '#a855f7',
	content_hash: '#6b7280',
};

// Epistemic type → color (for knowledge nodes without entity_type)
export const EPISTEMIC_COLORS: Record<string, string> = {
	fact: '#22c55e',
	observation: '#3b82f6',
	claim: '#eab308',
	hypothesis: '#a855f7',
	question: '#f97316',
	evidence: '#06b6d4',
};

// Edge relationship → color
export const EDGE_COLORS: Record<string, string> = {
	// Positive/supportive
	supports: '#22c55e',
	allied_with: '#22c55e',
	partners_with: '#22c55e',
	member_of: '#3b82f6',
	holds_position: '#3b82f6',
	leads: '#3b82f6',
	led: '#a855f7',
	employs: '#3b82f6',

	// Adversarial
	contrasts: '#ef4444',
	opposes: '#ef4444',
	condemned: '#ef4444',
	attacks: '#ef4444',
	sanctions: '#ef4444',
	targets: '#ef4444',

	// Neutral
	references: '#6b7280',
	associated_with: '#6b7280',
	co_occurs: '#6b7280',
	mentions: '#6b7280',
	met_with: '#6b7280',
	reports_on: '#6b7280',

	// Spatial
	located_in: '#22c55e',
	capital_of: '#10b981',
	borders: '#14b8a6',

	// Causal
	led_to: '#a855f7',
	defines: '#3b82f6',
	extends: '#3b82f6',
	supersedes: '#f97316',
	same_concept: '#6b7280',

	// Investigative
	inspected: '#06b6d4',
	investigates: '#06b6d4',
	questions: '#f97316',
	funds: '#eab308',
	owns: '#eab308',
};

// Fallback color
export const DEFAULT_COLOR = '#6b7280';

export function nodeColor(node: { entity_type?: string | null; subtype?: string | null; epistemic_type?: string }): string {
	const sub = node.subtype || node.entity_type;
	if (sub) return ENTITY_COLORS[sub] || DEFAULT_COLOR;
	return EPISTEMIC_COLORS[node.epistemic_type || 'observation'] || DEFAULT_COLOR;
}

export function edgeColor(relationship: string): string {
	return EDGE_COLORS[relationship] || DEFAULT_COLOR;
}
