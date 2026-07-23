/**
 * Client-side satellite position computation from TLE data.
 *
 * Uses satellite.js (SGP4/SDP4 implementation) to compute real-time
 * lat/lon/altitude from Two-Line Element sets. Runs entirely in the browser
 * — no server round-trips needed for position updates.
 *
 * Dynamic import to avoid SSR/IIFE top-level await issues with WASM.
 */

export interface SatellitePosition {
	latitude: number;
	longitude: number;
	altitude_km: number;
	velocity_kms: number;
}

// Lazy-load satellite.js to avoid SSR bundling issues
let satLib: any = null;
async function getSatLib() {
	if (!satLib) {
		satLib = await import('satellite.js');
	}
	return satLib;
}

// Log only the FIRST propagation failure of each kind — propagating
// 300+ TLEs at a time would otherwise spam the console with repeats.
const loggedErrors = new Set<string>();
function logOnce(kind: string, detail: any) {
	if (loggedErrors.has(kind)) return;
	loggedErrors.add(kind);
	// eslint-disable-next-line no-console
	console.warn(`[satellite-propagator] ${kind}:`, detail);
}

/**
 * Compute current position of a satellite from TLE lines.
 *
 * Returns null on any failure. Errors are logged once per failure-kind so
 * the user can see WHY propagation is failing without the console getting
 * flooded with 300 identical messages.
 *
 * Handles satellite.js v7's contract:
 *   - sgp4 / propagate may return `null` (not just throw) on numerical
 *     instability, eccentricity errors, or epoch decay. Old code only
 *     guarded against `typeof position === 'boolean'` (v3 era) and would
 *     throw 'Cannot read properties of null' when result itself was null.
 *   - eciToGeodetic returns { latitude, longitude, height } in radians;
 *     we convert via degreesLat / degreesLong.
 */
export async function propagateTLE(
	tle1: string,
	tle2: string,
	date: Date = new Date()
): Promise<SatellitePosition | null> {
	let lib: any;
	try {
		lib = await getSatLib();
	} catch (e) {
		logOnce('import-failed', e);
		return null;
	}

	const { twoline2satrec, propagate, gstime, eciToGeodetic, degreesLat, degreesLong } = lib;

	if (typeof twoline2satrec !== 'function') {
		logOnce('twoline2satrec-missing', { keys: Object.keys(lib) });
		return null;
	}

	let satrec: any;
	try {
		satrec = twoline2satrec(tle1, tle2);
	} catch (e) {
		logOnce('twoline2satrec-threw', { tle1, error: e });
		return null;
	}
	if (!satrec || satrec.error) {
		logOnce('satrec-error', { tle1, satrecError: satrec?.error });
		return null;
	}

	let result: any;
	try {
		result = propagate(satrec, date);
	} catch (e) {
		logOnce('propagate-threw', e);
		return null;
	}

	if (!result || !result.position || typeof result.position === 'boolean') {
		logOnce('propagate-no-position', { tle1, gotResult: !!result });
		return null;
	}

	let geo: any;
	try {
		const gmst = gstime(date);
		geo = eciToGeodetic(result.position, gmst);
	} catch (e) {
		logOnce('eciToGeodetic-threw', e);
		return null;
	}

	const velocity = result.velocity && typeof result.velocity !== 'boolean'
		? Math.sqrt(result.velocity.x ** 2 + result.velocity.y ** 2 + result.velocity.z ** 2)
		: 0;

	return {
		latitude: degreesLat(geo.latitude),
		longitude: degreesLong(geo.longitude),
		altitude_km: geo.height,
		velocity_kms: velocity,
	};
}

/**
 * Batch-propagate multiple satellites to current time.
 * Returns an array parallel to input (null entries for propagation failures).
 */
export async function propagateBatch(
	satellites: Array<{ tle_line1: string; tle_line2: string; [key: string]: any }>,
	date: Date = new Date()
): Promise<Array<SatellitePosition | null>> {
	const results = await Promise.all(
		satellites.map(sat =>
			sat.tle_line1 && sat.tle_line2
				? propagateTLE(sat.tle_line1, sat.tle_line2, date)
				: Promise.resolve(null)
		)
	);
	return results;
}
