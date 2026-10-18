/**
 * Card Resolver — resolves path-based card recipes against signal payloads.
 *
 * The card template system produces recipes with field paths like "vitals.level"
 * instead of baked values like "87%". This module resolves those paths at render
 * time and provides formatting, sparkline extraction, and trackable series.
 */

// ─── Path Resolution ─────────────────────────────────────────────────────────

/**
 * Resolve a dot-path against an object.
 * resolvePath({vitals: {level: 87}}, "vitals.level") → 87
 * Returns undefined if any segment is missing.
 */
export function resolvePath(obj: Record<string, any>, path: string): any {
	if (!obj || !path) return undefined;
	const parts = path.split('.');
	let current: any = obj;
	for (const part of parts) {
		if (current == null || typeof current !== 'object') return undefined;
		current = current[part];
	}
	return current;
}

// ─── Value Formatting ────────────────────────────────────────────────────────

/**
 * Format a resolved value based on its field format hint.
 */
export function formatValue(value: any, format: string): string {
	if (value == null) return '';

	switch (format) {
		case 'numeric':
			if (typeof value === 'number') {
				return Math.abs(value) < 1000 ? value.toFixed(1) : value.toLocaleString();
			}
			return String(value);

		case 'percentage':
			return typeof value === 'number' ? `${value}%` : String(value);

		case 'badge':
			if (typeof value === 'boolean') return value ? 'Yes' : 'No';
			return String(value);

		case 'email':
		case 'url':
		case 'ip':
		case 'mac':
		case 'text':
			return String(value);

		case 'table':
			if (Array.isArray(value)) return `${value.length} items`;
			return String(value);

		case 'graph':
			if (Array.isArray(value)) return `${value.length} points`;
			return String(value);

		default:
			if (typeof value === 'number') return value.toFixed(1);
			if (typeof value === 'boolean') return value ? 'Yes' : 'No';
			if (Array.isArray(value)) return `${value.length} items`;
			if (typeof value === 'object') return `${Object.keys(value).length} fields`;
			return String(value);
	}
}

// ─── Trackable Series Extraction ─────────────────────────────────────────────

export interface TrackableSeries {
	path: string;
	label: string;
	unit: string;
	values: number[];
	timestamps: string[];
	current: number;
}

/**
 * Extract numeric series from signal history for each trackable field.
 * Used by sparklines and full graphs.
 */
export function extractTrackableSeries(
	trackable: Array<{ path: string; label: string; unit: string; current?: number }>,
	history: Array<{ payload: Record<string, any>; timestamp: string }>,
): TrackableSeries[] {
	if (!trackable || !history || history.length === 0) return [];

	return trackable
		.map((field) => {
			const values: number[] = [];
			const timestamps: string[] = [];

			// Walk history oldest-first for sparklines
			for (let i = history.length - 1; i >= 0; i--) {
				const signal = history[i];
				const val = resolvePath(signal.payload, field.path);
				if (typeof val === 'number' && isFinite(val)) {
					values.push(val);
					timestamps.push(signal.timestamp);
				}
			}

			return {
				path: field.path,
				label: field.label,
				unit: field.unit || '',
				values,
				timestamps,
				current: field.current ?? (values.length > 0 ? values[values.length - 1] : 0),
			};
		})
		.filter((s) => s.values.length >= 1);
}

// ─── Coordinate Extraction from History ──────────────────────────────────────

export interface TrackCoordinate {
	lat: number;
	lon: number;
	timestamp: string;
}

/**
 * Extract coordinate arrays from signal history for map track rendering.
 * Uses the lat_field/lon_field from the map media config.
 */
export function extractTrackCoordinates(
	latField: string,
	lonField: string,
	history: Array<{ payload: Record<string, any>; timestamp: string }>,
): TrackCoordinate[] {
	if (!history || history.length === 0) return [];

	const coords: TrackCoordinate[] = [];

	// Walk oldest-first for proper track ordering
	for (let i = history.length - 1; i >= 0; i--) {
		const signal = history[i];
		const lat = resolvePath(signal.payload, latField);
		const lon = resolvePath(signal.payload, lonField);
		if (typeof lat === 'number' && typeof lon === 'number' && isFinite(lat) && isFinite(lon)) {
			coords.push({ lat, lon, timestamp: signal.timestamp });
		}
	}

	return coords;
}

// ─── Sparkline SVG Path ──────────────────────────────────────────────────────

/**
 * Generate an SVG path string for a sparkline from numeric values.
 * Returns empty string if fewer than 2 values.
 */
export function sparklinePath(values: number[], width = 80, height = 20): string {
	if (values.length < 2) return '';
	const max = Math.max(...values);
	const min = Math.min(...values);
	const range = max - min || 1;
	const step = width / (values.length - 1);
	return values
		.map((v, i) => {
			const x = i * step;
			const y = height - ((v - min) / range) * height;
			return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
		})
		.join(' ');
}

// ─── Table Resolution ────────────────────────────────────────────────────────

/**
 * Resolve a table media block from a payload.
 * Takes the source_field path + column_keys and builds rows.
 */
export function resolveTable(
	payload: Record<string, any>,
	sourceField: string,
	columnKeys: string[],
): { headers: string[]; rows: string[][] } {
	const data = resolvePath(payload, sourceField);
	if (!Array.isArray(data) || data.length === 0) return { headers: [], rows: [] };

	// If no column keys specified, use keys from first object
	const keys = columnKeys.length > 0 ? columnKeys : Object.keys(data[0]).slice(0, 8);
	const headers = keys.map((k) => k.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()));
	const rows = data.slice(0, 15).map((item: Record<string, any>) => keys.map((k) => String(item[k] ?? '')));

	return { headers, rows };
}
