/**
 * Celestial position computations — sun, moon, day/night terminator.
 *
 * All computed from date/time using astronomical algorithms.
 * No external API needed. Based on Jean Meeus "Astronomical Algorithms".
 */

export const RAD = Math.PI / 180;
export const DEG = 180 / Math.PI;

/** Julian date from JS Date */
function toJD(date: Date): number {
	return date.getTime() / 86400000 + 2440587.5;
}

/** Centuries since J2000.0 */
function toT(jd: number): number {
	return (jd - 2451545.0) / 36525.0;
}

/** Normalize angle to 0-360 */
function norm360(a: number): number {
	return ((a % 360) + 360) % 360;
}

/**
 * Sun's ecliptic longitude (low-accuracy, ~0.01° error)
 */
function sunEclipticLon(T: number): number {
	const L0 = norm360(280.46646 + 36000.76983 * T + 0.0003032 * T * T);
	const M = norm360(357.52911 + 35999.05029 * T - 0.0001537 * T * T);
	const Mr = M * RAD;
	const C = (1.914602 - 0.004817 * T - 0.000014 * T * T) * Math.sin(Mr)
		+ (0.019993 - 0.000101 * T) * Math.sin(2 * Mr)
		+ 0.000289 * Math.sin(3 * Mr);
	return norm360(L0 + C);
}

/**
 * Obliquity of the ecliptic
 */
function obliquity(T: number): number {
	return 23.439291 - 0.0130042 * T - 0.00000016 * T * T;
}

/**
 * Sun sub-solar point — the lat/lon on Earth where the sun is directly overhead.
 */
export function sunPosition(date: Date = new Date()): { latitude: number; longitude: number } {
	const jd = toJD(date);
	const T = toT(jd);
	const lambda = sunEclipticLon(T) * RAD;
	const eps = obliquity(T) * RAD;

	// Right ascension and declination
	const sinLambda = Math.sin(lambda);
	const cosLambda = Math.cos(lambda);
	const ra = Math.atan2(Math.cos(eps) * sinLambda, cosLambda) * DEG;
	const dec = Math.asin(Math.sin(eps) * sinLambda) * DEG;

	// Greenwich Mean Sidereal Time
	const D = jd - 2451545.0;
	const GMST = norm360(280.46061837 + 360.98564736629 * D);

	// Sub-solar longitude = RA - GMST (normalized to -180..180)
	let lon = ra - GMST;
	if (lon > 180) lon -= 360;
	if (lon < -180) lon += 360;

	return { latitude: dec, longitude: lon };
}

/**
 * Moon sub-lunar point — approximate position (accuracy ~1-2°).
 */
export function moonPosition(date: Date = new Date()): { latitude: number; longitude: number } {
	const jd = toJD(date);
	const T = toT(jd);

	// Moon's mean elements
	const Lp = norm360(218.3165 + 481267.8813 * T); // mean longitude
	const D = norm360(297.8502 + 445267.1115 * T);  // mean elongation
	const M = norm360(357.5291 + 35999.0503 * T);   // sun mean anomaly
	const Mp = norm360(134.9634 + 477198.8676 * T);  // moon mean anomaly
	const F = norm360(93.2720 + 483202.0175 * T);   // argument of latitude

	const Dr = D * RAD, Mr = M * RAD, Mpr = Mp * RAD, Fr = F * RAD;

	// Ecliptic longitude (simplified)
	const lon_ecl = Lp
		+ 6.289 * Math.sin(Mpr)
		- 1.274 * Math.sin(2 * Dr - Mpr)
		- 0.658 * Math.sin(2 * Dr)
		+ 0.214 * Math.sin(2 * Mpr)
		- 0.186 * Math.sin(Mr)
		- 0.114 * Math.sin(2 * Fr);

	// Ecliptic latitude
	const lat_ecl = 5.128 * Math.sin(Fr)
		+ 0.281 * Math.sin(Mpr + Fr)
		- 0.278 * Math.sin(Fr - Mpr)
		- 0.173 * Math.sin(2 * Dr - Fr);

	const lambdaR = lon_ecl * RAD;
	const betaR = lat_ecl * RAD;
	const eps = obliquity(T) * RAD;

	// Ecliptic to equatorial
	const sinLam = Math.sin(lambdaR);
	const cosLam = Math.cos(lambdaR);
	const sinBet = Math.sin(betaR);
	const cosBet = Math.cos(betaR);

	const ra = Math.atan2(
		Math.cos(eps) * sinLam * cosBet - Math.sin(eps) * sinBet,
		cosLam * cosBet
	) * DEG;
	const dec = Math.asin(
		Math.sin(eps) * sinLam * cosBet + Math.cos(eps) * sinBet
	) * DEG;

	const Dd = jd - 2451545.0;
	const GMST = norm360(280.46061837 + 360.98564736629 * Dd);

	let lng = ra - GMST;
	if (lng > 180) lng -= 360;
	if (lng < -180) lng += 360;

	return { latitude: dec, longitude: lng };
}

/**
 * Generate day/night terminator as a GeoJSON polygon.
 * Returns coordinates for the "night" polygon (the half of Earth away from the sun).
 */
export function terminatorGeoJSON(date: Date = new Date()): GeoJSON.Feature {
	const sun = sunPosition(date);
	// The terminator is a great circle 90° from the sub-solar point
	const sunLatR = sun.latitude * RAD;
	const sunLonR = sun.longitude * RAD;

	// Generate terminator points (great circle 90° from sun)
	const points: [number, number][] = [];
	for (let i = 0; i <= 360; i += 2) {
		const bearing = i * RAD;
		const lat = Math.asin(
			Math.sin(sunLatR) * Math.cos(Math.PI / 2) +
			Math.cos(sunLatR) * Math.sin(Math.PI / 2) * Math.cos(bearing)
		);
		const lon = sunLonR + Math.atan2(
			Math.sin(bearing) * Math.sin(Math.PI / 2) * Math.cos(sunLatR),
			Math.cos(Math.PI / 2) - Math.sin(sunLatR) * Math.sin(lat)
		);
		points.push([lon * DEG, lat * DEG]);
	}

	// Build a polygon that covers the night side
	// We need to determine which side is night (opposite the sun)
	// Night polygon: terminator line → extend to pole opposite the sun → close
	const nightPole = sun.latitude >= 0 ? -90 : 90;

	// Sort terminator points by longitude for clean polygon construction
	const sorted = [...points].sort((a, b) => a[0] - b[0]);

	// Build polygon: go along terminator, then along the bottom/top edge
	const poly: [number, number][] = [];
	for (const p of sorted) {
		poly.push(p);
	}
	// Close along the night-side edge
	poly.push([180, sorted[sorted.length - 1][1]]);
	poly.push([180, nightPole]);
	poly.push([-180, nightPole]);
	poly.push([-180, sorted[0][1]]);
	poly.push(sorted[0]); // close

	return {
		type: 'Feature',
		properties: { type: 'night' },
		geometry: {
			type: 'Polygon',
			coordinates: [poly],
		},
	};
}

/**
 * Moon phase (0 = new, 0.5 = full, 1 = new again)
 */
export function moonPhase(date: Date = new Date()): { phase: number; name: string; illumination: number } {
	const jd = toJD(date);
	const T = toT(jd);
	const D = norm360(297.8502 + 445267.1115 * T) * RAD;
	const M = norm360(357.5291 + 35999.0503 * T) * RAD;
	const Mp = norm360(134.9634 + 477198.8676 * T) * RAD;

	// Phase angle
	const i = norm360(180 - D * DEG
		- 6.289 * Math.sin(Mp)
		+ 2.1 * Math.sin(M)
		- 1.274 * Math.sin(2 * D - Mp)
		- 0.658 * Math.sin(2 * D)
		- 0.214 * Math.sin(2 * Mp)
		- 0.11 * Math.sin(D));

	const phase = i / 360; // 0-1
	const illumination = (1 + Math.cos(i * RAD)) / 2;

	let name = 'Waxing Crescent';
	if (phase < 0.025 || phase > 0.975) name = 'New Moon';
	else if (phase < 0.225) name = 'Waxing Crescent';
	else if (phase < 0.275) name = 'First Quarter';
	else if (phase < 0.475) name = 'Waxing Gibbous';
	else if (phase < 0.525) name = 'Full Moon';
	else if (phase < 0.725) name = 'Waning Gibbous';
	else if (phase < 0.775) name = 'Last Quarter';
	else name = 'Waning Crescent';

	return { phase, name, illumination };
}


// ─── Planet Positions (Keplerian orbital elements from JPL) ──────────────

/**
 * Orbital elements for each planet (J2000 + rate per century).
 * Source: JPL "Keplerian Elements for Approximate Positions of the Major Planets"
 * https://ssd.jpl.nasa.gov/planets/approx_pos.html
 */
interface OrbitalElements {
	a: number;  aR: number;   // semi-major axis (AU) + rate
	e: number;  eR: number;   // eccentricity + rate
	I: number;  IR: number;   // inclination (deg) + rate
	L: number;  LR: number;   // mean longitude (deg) + rate
	w: number;  wR: number;   // longitude of perihelion (deg) + rate
	O: number;  OR: number;   // longitude of ascending node (deg) + rate
}

const PLANETS: Record<string, OrbitalElements & { color: string; size: number; label: string }> = {
	mercury: { a: 0.38710, aR: 0, e: 0.20563, eR: 0.00002, I: 7.005, IR: -0.0060, L: 252.251, LR: 149472.675, w: 77.457, wR: 0.1600, O: 48.331, OR: -0.1254, color: '#a0a0a0', size: 3, label: 'Mercury' },
	venus:   { a: 0.72333, aR: 0, e: 0.00677, eR: -0.00005, I: 3.395, IR: -0.0008, L: 181.980, LR: 58517.816, w: 131.564, wR: 0.0048, O: 76.680, OR: -0.2780, color: '#e8c56d', size: 4, label: 'Venus' },
	earth:   { a: 1.00000, aR: 0, e: 0.01671, eR: -0.00004, I: 0.000, IR: -0.0130, L: 100.464, LR: 35999.373, w: 102.937, wR: 0.3225, O: 0.000, OR: 0, color: '#4488ff', size: 5, label: 'Earth' },
	mars:    { a: 1.52368, aR: 0, e: 0.09340, eR: 0.00008, I: 1.850, IR: -0.0003, L: 355.453, LR: 19140.300, w: 336.060, wR: 0.4439, O: 49.558, OR: -0.2950, color: '#e05030', size: 4, label: 'Mars' },
	jupiter: { a: 5.20260, aR: 0, e: 0.04849, eR: 0.00016, I: 1.303, IR: -0.0020, L: 34.351, LR: 3034.906, w: 14.331, wR: 0.2153, O: 100.464, OR: 0.1769, color: '#d4a56a', size: 8, label: 'Jupiter' },
	saturn:  { a: 9.55491, aR: 0, e: 0.05551, eR: -0.00035, I: 2.489, IR: 0.0025, L: 50.077, LR: 1222.114, w: 93.057, wR: 0.5665, O: 113.666, OR: -0.2500, color: '#e8d07a', size: 7, label: 'Saturn' },
	uranus:  { a: 19.2184, aR: 0, e: 0.04638, eR: -0.00003, I: 0.773, IR: -0.0003, L: 314.055, LR: 428.467, w: 173.005, wR: 0.0893, O: 74.006, OR: 0.0475, color: '#88ccdd', size: 5, label: 'Uranus' },
	neptune: { a: 30.1104, aR: 0, e: 0.00946, eR: 0.00001, I: 1.770, IR: 0.0003, L: 304.349, LR: 218.486, w: 48.124, wR: 0.0295, O: 131.784, OR: -0.0061, color: '#4466ee', size: 5, label: 'Neptune' },
};

export interface PlanetPosition {
	name: string;
	x: number;       // heliocentric X (AU) — ecliptic plane
	y: number;       // heliocentric Y (AU)
	angle: number;    // angle from Sun in degrees (for SVG positioning)
	distance: number; // distance from Sun (AU)
	color: string;
	size: number;
}

/**
 * Compute heliocentric positions of all planets for a given date.
 * Returns x,y in AU on the ecliptic plane (top-down solar system view).
 */
export function planetPositions(date: Date = new Date()): PlanetPosition[] {
	const jd = toJD(date);
	const T = toT(jd);

	const results: PlanetPosition[] = [];

	for (const [key, p] of Object.entries(PLANETS)) {
		// Current orbital elements
		const a = p.a + p.aR * T;
		const e = p.e + p.eR * T;
		const L = norm360(p.L + p.LR * T);
		const w = norm360(p.w + p.wR * T);

		// Mean anomaly
		const M = norm360(L - w);
		const Mr = M * RAD;

		// Solve Kepler's equation: E - e*sin(E) = M (Newton's method)
		let E = Mr;
		for (let i = 0; i < 10; i++) {
			E = E - (E - e * Math.sin(E) - Mr) / (1 - e * Math.cos(E));
		}

		// True anomaly
		const xv = a * (Math.cos(E) - e);
		const yv = a * Math.sqrt(1 - e * e) * Math.sin(E);

		// Heliocentric distance and true anomaly angle
		const r = Math.sqrt(xv * xv + yv * yv);
		const v = Math.atan2(yv, xv);

		// Heliocentric ecliptic coordinates (simplified — ignoring inclination for 2D view)
		const lonPeri = w * RAD;
		const x = r * Math.cos(v + lonPeri);
		const y = r * Math.sin(v + lonPeri);

		const angle = Math.atan2(y, x) * DEG;

		results.push({
			name: p.label,
			x, y,
			angle: norm360(angle),
			distance: r,
			color: p.color,
			size: p.size,
		});
	}

	return results;
}

/** Get just the orbital radii for drawing orbit rings (in AU) */
export function orbitRadii(): { name: string; radius: number; color: string }[] {
	return Object.entries(PLANETS).map(([_, p]) => ({
		name: p.label,
		radius: p.a,
		color: p.color,
	}));
}
