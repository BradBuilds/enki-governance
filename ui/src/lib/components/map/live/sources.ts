/**
 * Live source registry — defines how each data source renders on the map.
 *
 * Each source has: icon generator, color logic, size logic, rotation field,
 * label field, detail fields, and available filters.
 */

import { aircraftSvg, vesselSvg, seismicSvg, weatherSvg, cameraSvg, portSvg, airportSvg, satelliteSvg, issSvg, fireballSvg, asteroidSvg, solarFlareSvg, gdeltSvg } from './icons';

export interface LiveSourceFilter {
	key: string;
	label: string;
	type: 'range' | 'search';
	min?: number;
	max?: number;
	unit?: string;
}

export interface LiveDetailField {
	key: string;
	label: string;
	unit?: string;
}

export interface LiveSourceConfig {
	name: string;
	iconFn: (item: any) => string;
	rotationField: string | null;
	labelField: string;
	colorFn: (item: any) => string;
	sizeFn: (item: any) => number;
	trailColor: string;
	filters: LiveSourceFilter[];
	detailFields: LiveDetailField[];
	observableTypes: string[];
}

// GDELT V1.5TONE runs -100 (very negative) → +100 (very positive). Most
// articles cluster between -10 and +5. Map: negative red, neutral grey,
// positive emerald, so the map reads as sentiment density at a glance.
function _gdeltToneColor(tone: number | null | undefined): string {
	if (tone == null) return '#a855f7';        // purple = unknown tone
	if (tone <= -5) return '#ef4444';
	if (tone <= -1) return '#f97316';
	if (tone >= 5)  return '#10b981';
	if (tone >= 1)  return '#84cc16';
	return '#94a3b8';                          // neutral
}

function altitudeColor(alt: number | null): string {
	if (alt == null) return '#94a3b8';
	if (alt < 1000) return '#22c55e';
	if (alt < 5000) return '#84cc16';
	if (alt < 15000) return '#eab308';
	if (alt < 30000) return '#f97316';
	return '#3b82f6';
}

export const LIVE_SOURCES: Record<string, LiveSourceConfig> = {
	adsb: {
		name: 'Aircraft (ADS-B)',
		iconFn: (item) => aircraftSvg(altitudeColor(item.altitude), 22),
		rotationField: 'heading',
		labelField: 'label',
		colorFn: (item) => altitudeColor(item.altitude),
		sizeFn: () => 22,
		trailColor: '#60a5fa',
		filters: [
			{ key: 'altitude', label: 'Altitude', type: 'range', min: 0, max: 60000, unit: 'ft' },
			{ key: 'speed', label: 'Speed', type: 'range', min: 0, max: 600, unit: 'kt' },
			{ key: 'label', label: 'Callsign', type: 'search' },
		],
		detailFields: [
			{ key: 'payload.r', label: 'Registration' },
			{ key: 'payload.t', label: 'Type' },
			{ key: 'altitude', label: 'Altitude', unit: 'ft' },
			{ key: 'speed', label: 'Speed', unit: 'kt' },
			{ key: 'heading', label: 'Heading', unit: '°' },
			{ key: 'payload.squawk', label: 'Squawk' },
		],
		observableTypes: ['aircraft_registration', 'icao_hex', 'callsign'],
	},

	ais: {
		name: 'Vessels (AIS)',
		iconFn: (item) => vesselSvg('#06b6d4', 20),
		rotationField: 'heading',
		labelField: 'label',
		colorFn: () => '#06b6d4',
		sizeFn: () => 20,
		trailColor: '#06b6d4',
		filters: [
			{ key: 'label', label: 'Vessel Name', type: 'search' },
			{ key: 'speed', label: 'Speed', type: 'range', min: 0, max: 30, unit: 'kt' },
		],
		detailFields: [
			{ key: 'payload.mmsi', label: 'MMSI' },
			{ key: 'payload.imo', label: 'IMO' },
			{ key: 'payload.shipname', label: 'Name' },
			{ key: 'speed', label: 'Speed', unit: 'kt' },
			{ key: 'heading', label: 'Course', unit: '°' },
		],
		observableTypes: ['vessel_mmsi', 'vessel_imo', 'vessel_name'],
	},

	seismic: {
		name: 'Earthquakes',
		iconFn: (item) => {
			const age = Date.now() - new Date(item.cached_at || '').getTime();
			const color = age < 3600000 ? '#ef4444' : age < 21600000 ? '#f97316' : '#eab308';
			const mag = item.payload?.properties?.mag ?? 3;
			return seismicSvg(color, Math.max(14, mag * 4));
		},
		rotationField: null,
		labelField: 'label',
		colorFn: (item) => {
			const age = Date.now() - new Date(item.cached_at || '').getTime();
			return age < 3600000 ? '#ef4444' : age < 21600000 ? '#f97316' : '#eab308';
		},
		sizeFn: (item) => {
			const mag = item.payload?.properties?.mag ?? 3;
			return Math.max(14, mag * 4);
		},
		trailColor: '#ef4444',
		filters: [],
		detailFields: [
			{ key: 'payload.properties.mag', label: 'Magnitude' },
			{ key: 'payload.properties.place', label: 'Location' },
			{ key: 'payload.properties.type', label: 'Type' },
		],
		observableTypes: [],
	},

	weather: {
		name: 'Weather',
		iconFn: () => weatherSvg('#06b6d4', 16),
		rotationField: null,
		labelField: 'label',
		colorFn: () => '#06b6d4',
		sizeFn: () => 16,
		trailColor: '#06b6d4',
		filters: [],
		detailFields: [],
		observableTypes: [],
	},

	ports: {
		name: 'Ports',
		iconFn: (item) => {
			const size = item?.harbor_size;
			const s = size === 'L' || size === 'Large' ? 20 : size === 'M' || size === 'Medium' ? 16 : 12;
			return portSvg('#14b8a6', s);
		},
		rotationField: null,
		labelField: 'label',
		colorFn: () => '#14b8a6',
		sizeFn: (item) => {
			const size = item?.harbor_size;
			return size === 'L' || size === 'Large' ? 20 : size === 'M' || size === 'Medium' ? 16 : 12;
		},
		trailColor: '#14b8a6',
		filters: [
			{ key: 'label', label: 'Port Name', type: 'search' },
		],
		detailFields: [
			{ key: 'name', label: 'Port Name' },
			{ key: 'country', label: 'Country' },
			{ key: 'harbor_size', label: 'Harbor Size' },
			{ key: 'harbor_type', label: 'Harbor Type' },
			{ key: 'shelter', label: 'Shelter' },
		],
		observableTypes: [],
	},

	airports: {
		name: 'Airports',
		iconFn: (item) => {
			const t = item?.type || '';
			const mil = item?.military;
			const color = mil ? '#ef4444' : t === 'large_airport' ? '#f59e0b' : t === 'medium_airport' ? '#fbbf24' : '#d4a017';
			const size = t === 'large_airport' ? 20 : t === 'medium_airport' ? 16 : t === 'small_airport' ? 12 : 10;
			return airportSvg(color, size);
		},
		rotationField: null,
		labelField: 'label',
		colorFn: (item) => {
			const t = item?.type || '';
			if (item?.military) return '#ef4444';
			if (t === 'large_airport') return '#f59e0b';
			if (t === 'medium_airport') return '#fbbf24';
			return '#d4a017';
		},
		sizeFn: (item) => {
			const t = item?.type || '';
			if (t === 'large_airport') return 20;
			if (t === 'medium_airport') return 16;
			if (t === 'small_airport') return 12;
			return 10;
		},
		trailColor: '#f59e0b',
		filters: [
			{ key: 'label', label: 'Airport Name', type: 'search' },
		],
		detailFields: [
			{ key: 'name', label: 'Name' },
			{ key: 'airport_id', label: 'Identifier' },
			{ key: 'icao', label: 'ICAO' },
			{ key: 'city', label: 'City' },
			{ key: 'payload.country', label: 'Country' },
			{ key: 'elevation_ft', label: 'Elevation', unit: 'ft' },
			{ key: 'type', label: 'Type' },
		],
		observableTypes: [],
	},

	osm_cameras: {
		name: 'OSM Cameras (Global)',
		iconFn: () => cameraSvg('#ef4444', 10),
		rotationField: null,
		labelField: 'label',
		colorFn: () => '#ef4444',
		sizeFn: () => 10,
		trailColor: '#ef4444',
		filters: [],
		detailFields: [
			{ key: 'payload.camera:type', label: 'Type' },
			{ key: 'payload.manufacturer', label: 'Manufacturer' },
			{ key: 'payload.surveillance:zone', label: 'Zone' },
		],
		observableTypes: [],
	},

	cameras: {
		name: 'Cameras',
		iconFn: () => cameraSvg('#ef4444', 14),
		rotationField: null,
		labelField: 'label',
		colorFn: () => '#ef4444',
		sizeFn: () => 14,
		trailColor: '#ef4444',
		filters: [
			{ key: 'label', label: 'Camera Name', type: 'search' },
		],
		detailFields: [
			{ key: 'label', label: 'Name' },
			{ key: 'source_provider', label: 'Provider' },
			{ key: 'image_url', label: 'Feed URL' },
		],
		observableTypes: [],
	},

	// ─── Space & Astronomy ──────────────────────────────────────────────

	satellites: {
		name: 'Satellites (CelesTrak)',
		iconFn: (item) => {
			const c = item?.constellation || '';
			if (c === 'ISS') return issSvg('#fbbf24', 26);
			const orbit = item?.altitude_km || item?.altitude || 0;
			const color = orbit > 35000 ? '#e879f9' : orbit > 1000 ? '#38bdf8' : '#4ade80';
			return satelliteSvg(color, 16);
		},
		rotationField: null,
		labelField: 'label',
		colorFn: (item) => {
			if (item?.constellation === 'ISS') return '#fbbf24';
			const orbit = item?.altitude_km || item?.altitude || 0;
			return orbit > 35000 ? '#e879f9' : orbit > 1000 ? '#38bdf8' : '#4ade80';
		},
		sizeFn: (item) => item?.constellation === 'ISS' ? 26 : 16,
		trailColor: '#e879f9',
		filters: [
			{ key: 'label', label: 'Satellite Name', type: 'search' },
			{ key: 'altitude_km', label: 'Altitude', type: 'range', min: 0, max: 40000, unit: 'km' },
		],
		detailFields: [
			{ key: 'payload.name', label: 'Name' },
			{ key: 'payload.satellite_id', label: 'NORAD ID' },
			{ key: 'payload.constellation', label: 'Constellation' },
			{ key: 'altitude_km', label: 'Altitude', unit: 'km' },
			{ key: 'velocity_kms', label: 'Velocity', unit: 'km/s' },
			{ key: 'payload.orbit_type', label: 'Orbit' },
			{ key: 'payload.country_code', label: 'Country' },
		],
		observableTypes: ['norad_id', 'satellite_name'],
	},

	fireballs: {
		name: 'Fireballs (NASA)',
		iconFn: (item) => {
			const energy = item?.payload?.impact_energy_kt || item?.impact_energy_kt || 0.1;
			return fireballSvg('#f97316', Math.min(28, Math.max(12, energy * 15)));
		},
		rotationField: null,
		labelField: 'label',
		colorFn: () => '#f97316',
		sizeFn: (item) => {
			const energy = item?.payload?.impact_energy_kt || item?.impact_energy_kt || 0.1;
			return Math.min(28, Math.max(12, energy * 15));
		},
		trailColor: '#f97316',
		filters: [],
		detailFields: [
			{ key: 'payload.date', label: 'Date' },
			{ key: 'payload.velocity_kms', label: 'Velocity', unit: 'km/s' },
			{ key: 'payload.altitude_km', label: 'Altitude', unit: 'km' },
			{ key: 'payload.impact_energy_kt', label: 'Impact Energy', unit: 'kt' },
		],
		observableTypes: [],
	},

	gdelt: {
		name: 'News mentions (GDELT)',
		iconFn: (item) => gdeltSvg(_gdeltToneColor(item?.tone), 14),
		rotationField: null,
		labelField: 'label',
		colorFn: (item) => _gdeltToneColor(item?.tone),
		sizeFn: () => 14,
		trailColor: '#a855f7',
		filters: [
			{ key: 'label', label: 'Place', type: 'search' },
		],
		detailFields: [
			{ key: 'payload.country_code', label: 'Country' },
			{ key: 'payload.published_at',  label: 'Published' },
			{ key: 'tone', label: 'Tone' },
			{ key: 'payload.source',  label: 'Source' },
			{ key: 'payload.themes',  label: 'Themes' },
			{ key: 'payload.url',     label: 'URL' },
		],
		observableTypes: [],
	},

	space_weather: {
		name: 'Space Weather (DONKI)',
		iconFn: (item) => {
			const cls = item?.payload?.class_type || item?.class_type || '';
			const color = cls.startsWith('X') ? '#ef4444' : cls.startsWith('M') ? '#f97316' : '#f59e0b';
			return solarFlareSvg(color, 16);
		},
		rotationField: null,
		labelField: 'label',
		colorFn: (item) => {
			const cls = item?.payload?.class_type || item?.class_type || '';
			if (cls.startsWith('X')) return '#ef4444';
			if (cls.startsWith('M')) return '#f97316';
			return '#f59e0b';
		},
		sizeFn: () => 16,
		trailColor: '#f59e0b',
		filters: [],
		detailFields: [
			{ key: 'payload.event_type', label: 'Event Type' },
			{ key: 'payload.class_type', label: 'Class' },
			{ key: 'payload.start_time', label: 'Start Time' },
			{ key: 'payload.peak_time', label: 'Peak Time' },
			{ key: 'payload.source_location', label: 'Source' },
		],
		observableTypes: [],
	},
};

/**
 * Get a nested value from an object using dot notation.
 * "payload.r" → obj.payload.r
 */
export function getNestedValue(obj: any, path: string): any {
	return path.split('.').reduce((o, k) => o?.[k], obj);
}
