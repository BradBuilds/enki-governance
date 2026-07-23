/**
 * Universal node/edge contracts for all visualization surfaces.
 * Every graph, network view, and map marker speaks this language.
 */

export interface UniversalNode {
	// Identity (required)
	id: string;              // node_id — "entity:abc123", "place:def456"
	title: string;           // display name

	// Classification
	node_type: string;       // "entity" | "place" | "topic" | "document" | "source"
	subtype: string | null;  // "person" | "organization" | "country" | "city" | "aircraft"

	// Weight (drives radius)
	mention_count: number;

	// Quality (drives border style / opacity)
	completeness_tier: string;  // "stub" | "partial" | "solid" | "verified"
	completeness_score: number; // 0-1
	conflict_count: number;

	// Optional enrichment (hover card)
	dossier?: Record<string, any>;
	aliases?: string[];

	// Epistemic (for knowledge nodes)
	epistemic_type?: string;     // "fact" | "observation" | "claim"
	source_authority?: string;   // "verified" | "trusted" | "unverified" | "disputed"

	// Graph layout
	traverse_depth?: number;

	// Geospatial
	latitude?: number;
	longitude?: number;

	// d3-force simulation fields (injected at runtime)
	x?: number;
	y?: number;
	fx?: number | null;
	fy?: number | null;
}

export interface UniversalEdge {
	source: string | UniversalNode;
	target: string | UniversalNode;
	relationship: string;       // "mentions", "opposes", "co_occurs", "meets_with"
	strength: number;           // 0-1
	article_count?: number;     // for co-occurrence context
	evidence?: string;
	is_directed: boolean;
}
