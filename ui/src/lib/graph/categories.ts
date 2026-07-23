/**
 * Entity type categorization — maps subtypes to visual grouping categories.
 * Used for graph sector layout, filter toggles, and entity browsers.
 */

export type EntityCategory = 'people' | 'organizations' | 'places' | 'equipment' | 'topics' | 'documents' | 'other';

const PEOPLE_TYPES = new Set(['person', 'fictional_person']);

const ORG_TYPES = new Set([
	'organization', 'org', 'military_unit', 'government_agency',
	'university', 'mass_media', 'research_institute',
]);

const PLACE_TYPES = new Set([
	'location', 'country', 'region', 'city', 'facility',
	'strait', 'bay', 'gulf', 'canal', 'peninsula',
	'island', 'island_group', 'archipelago',
	'mountain', 'mountain_range', 'desert', 'river', 'lake',
	'sea', 'ocean', 'continent', 'reef',
	'airport', 'seaport', 'railway_station', 'pipeline',
	'bridge', 'dam', 'power_plant', 'refinery',
]);

const EQUIPMENT_TYPES = new Set([
	'aircraft', 'helicopter', 'airliner', 'vessel', 'vehicle',
	'weapon', 'spacecraft', 'unmanned_aerial_vehicle',
]);

const TOPIC_TYPES = new Set(['topic']);
const DOC_TYPES = new Set(['document', 'source', 'chronicle']);

export function entityCategory(subtype: string | null | undefined): EntityCategory {
	if (!subtype) return 'other';
	const t = subtype.toLowerCase();
	if (PEOPLE_TYPES.has(t)) return 'people';
	if (ORG_TYPES.has(t)) return 'organizations';
	if (PLACE_TYPES.has(t)) return 'places';
	if (EQUIPMENT_TYPES.has(t)) return 'equipment';
	if (TOPIC_TYPES.has(t)) return 'topics';
	if (DOC_TYPES.has(t)) return 'documents';
	return 'other';
}

/** Ordered sectors for radial layout */
export const SECTOR_ORDER: EntityCategory[] = [
	'people', 'organizations', 'places', 'equipment', 'topics', 'documents', 'other',
];

/** Display labels for UI */
export const CATEGORY_LABELS: Record<EntityCategory, { label: string; icon: string }> = {
	people: { label: 'People', icon: '👤' },
	organizations: { label: 'Organizations', icon: '🏢' },
	places: { label: 'Places', icon: '🌍' },
	equipment: { label: 'Equipment', icon: '✈' },
	topics: { label: 'Topics', icon: '💡' },
	documents: { label: 'Documents', icon: '📄' },
	other: { label: 'Other', icon: '●' },
};
