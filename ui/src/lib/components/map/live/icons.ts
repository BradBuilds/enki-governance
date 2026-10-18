/**
 * SVG icon paths for live map markers.
 * Each returns an SVG string that can be used as a marker icon.
 * Rotation is applied via CSS transform on the container.
 */

export function aircraftSvg(color: string, size: number = 20): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${color}" xmlns="http://www.w3.org/2000/svg">
		<path d="M12 2L10 8H4L3 10L10 13V20L8 22H10L12 20L14 22H16L14 20V13L21 10L20 8H14L12 2Z" />
	</svg>`;
}

export function vesselSvg(color: string, size: number = 18): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${color}" xmlns="http://www.w3.org/2000/svg">
		<path d="M3 17L5 8H9V4H15V8H19L21 17H3Z" opacity="0.9"/>
		<path d="M2 19C4 21 8 21 12 19C16 21 20 21 22 19" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round"/>
	</svg>`;
}

export function seismicSvg(color: string, size: number = 16): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
		<circle cx="12" cy="12" r="4" fill="${color}"/>
		<circle cx="12" cy="12" r="8" fill="none" stroke="${color}" stroke-width="1.5" opacity="0.5"/>
		<circle cx="12" cy="12" r="11" fill="none" stroke="${color}" stroke-width="1" opacity="0.25"/>
	</svg>`;
}

export function portSvg(color: string, size: number = 16): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${color}" xmlns="http://www.w3.org/2000/svg">
		<path d="M20 21c-1.39 0-2.78-.47-4-1.32-2.44 1.71-5.56 1.71-8 0C6.78 20.53 5.39 21 4 21H2v2h2c1.38 0 2.74-.35 4-.99 2.52 1.29 5.48 1.29 8 0 1.26.65 2.62.99 4 .99h2v-2h-2zM3.95 19H4c1.6 0 3.02-.88 4-2 .98 1.12 2.4 2 4 2s3.02-.88 4-2c.98 1.12 2.4 2 4 2h.05l1.89-6.68c.08-.26.06-.54-.06-.78s-.34-.42-.6-.5L20 10.62V6c0-1.1-.9-2-2-2h-3V1H9v3H6c-1.1 0-2 .9-2 2v4.62l-1.29.42c-.26.08-.48.26-.6.5s-.15.52-.06.78L3.95 19z" opacity="0.9"/>
	</svg>`;
}

export function airportSvg(color: string, size: number = 14): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${color}" xmlns="http://www.w3.org/2000/svg">
		<path d="M22 16v-2l-8.5-5V3.5c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5V9L2 14v2l8.5-2.5V19L8 20.5V22l4-1 4 1v-1.5L13.5 19v-5.5L22 16z"/>
	</svg>`;
}

export function cameraSvg(color: string, size: number = 14): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${color}" xmlns="http://www.w3.org/2000/svg">
		<path d="M15 8v8H5V8h10m1-2H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4V7c0-.55-.45-1-1-1z"/>
	</svg>`;
}

// Glow so a small marker stays visible on ANY background — a thin dark edge
// gives definition over bright land/ice, a soft white halo makes it pop over
// dark space and ocean.
const SAT_GLOW = 'filter: drop-shadow(0 0 1px rgba(0,0,0,.95)) drop-shadow(0 0 3px rgba(255,255,255,.85));';

export function satelliteSvg(color: string, size: number = 16): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" style="${SAT_GLOW}">
		<rect x="3.5" y="10" width="17" height="4" rx="1.2" fill="${color}"/>
		<rect x="10" y="3.5" width="4" height="17" rx="1.2" fill="${color}"/>
		<circle cx="12" cy="12" r="3.4" fill="${color}"/>
		<circle cx="12" cy="12" r="1.5" fill="#ffffff"/>
	</svg>`;
}

export function issSvg(color: string, size: number = 26): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" style="${SAT_GLOW}">
		<rect x="2" y="11" width="20" height="2.2" rx="1" fill="${color}"/>
		<rect x="10.9" y="6" width="2.2" height="12" rx="1" fill="${color}"/>
		<rect x="4.5" y="8" width="4" height="8" rx="1" fill="${color}"/>
		<rect x="15.5" y="8" width="4" height="8" rx="1" fill="${color}"/>
		<circle cx="12" cy="12" r="3" fill="${color}"/>
		<circle cx="12" cy="12" r="1.6" fill="#ffffff"/>
	</svg>`;
}

export function fireballSvg(color: string, size: number = 14): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${color}" xmlns="http://www.w3.org/2000/svg">
		<path d="M12 2C9 7 5 9 5 14c0 3.87 3.13 7 7 7s7-3.13 7-7c0-5-4-7-7-12z" opacity="0.9"/>
		<path d="M12 9c-1.5 3-3 4-3 6.5C9 17.43 10.34 19 12 19s3-1.57 3-3.5c0-2.5-1.5-3.5-3-6.5z" fill="white" opacity="0.4"/>
	</svg>`;
}

export function asteroidSvg(color: string, size: number = 14): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${color}" xmlns="http://www.w3.org/2000/svg">
		<path d="M14.5 3L16 5.5L19.5 4.5L20 8.5L23 10.5L21 13.5L22.5 17L19 17.5L17.5 21L14 19.5L11 21.5L9 18.5L5.5 19L5 15.5L1.5 13.5L3.5 10.5L2 7L5.5 6L7 3L10.5 4.5L14.5 3Z" opacity="0.85"/>
		<circle cx="10" cy="10" r="1.5" fill="white" opacity="0.2"/>
		<circle cx="15" cy="13" r="1" fill="white" opacity="0.15"/>
	</svg>`;
}

export function solarFlareSvg(color: string, size: number = 16): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${color}" xmlns="http://www.w3.org/2000/svg">
		<circle cx="12" cy="12" r="5" fill="${color}"/>
		<path d="M12 1L13.5 5.5L12 4L10.5 5.5L12 1Z M12 23L13.5 18.5L12 20L10.5 18.5L12 23Z M1 12L5.5 10.5L4 12L5.5 13.5L1 12Z M23 12L18.5 10.5L20 12L18.5 13.5L23 12Z" opacity="0.7"/>
		<path d="M4.22 4.22L7.4 8L6 7L5 8L4.22 4.22Z M19.78 4.22L16.6 8L18 7L19 8L19.78 4.22Z M4.22 19.78L8 16.6L7 18L8 19L4.22 19.78Z M19.78 19.78L16 16.6L17 18L16 19L19.78 19.78Z" opacity="0.5"/>
	</svg>`;
}

export function sunMarkerSvg(size: number = 28): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
		<circle cx="12" cy="12" r="5" fill="#fbbf24"/>
		<circle cx="12" cy="12" r="5" fill="none" stroke="#fde68a" stroke-width="1" opacity="0.5"/>
		<path d="M12 2V5M12 19V22M2 12H5M19 12H22M4.93 4.93L7.05 7.05M16.95 16.95L19.07 19.07M4.93 19.07L7.05 16.95M16.95 7.05L19.07 4.93" fill="none" stroke="#fbbf24" stroke-width="1.5" stroke-linecap="round"/>
	</svg>`;
}

export function moonMarkerSvg(illumination: number, size: number = 22): string {
	const gray = Math.round(200 * illumination + 55);
	const color = `rgb(${gray},${gray},${Math.min(255, gray + 20)})`;
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
		<circle cx="12" cy="12" r="7" fill="${color}" opacity="0.9"/>
		<circle cx="12" cy="12" r="7" fill="none" stroke="${color}" stroke-width="0.5" opacity="0.4"/>
		<circle cx="10" cy="10" r="1.5" fill="rgba(0,0,0,0.1)"/>
		<circle cx="14.5" cy="13" r="1" fill="rgba(0,0,0,0.08)"/>
		<circle cx="9" cy="14" r="0.8" fill="rgba(0,0,0,0.06)"/>
	</svg>`;
}

export function weatherSvg(color: string, size: number = 16): string {
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${color}" xmlns="http://www.w3.org/2000/svg">
		<circle cx="12" cy="12" r="5" opacity="0.8"/>
		<path d="M12 2V5M12 19V22M2 12H5M19 12H22M4.93 4.93L7.05 7.05M16.95 16.95L19.07 19.07M4.93 19.07L7.05 16.95M16.95 7.05L19.07 4.93" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round"/>
	</svg>`;
}

export function gdeltSvg(color: string, size: number = 14): string {
	// Newspaper / mention dot — small circle with a tiny "page" tick so it
	// reads as "article location" even at zoom-out density.
	return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
		<circle cx="12" cy="12" r="7" fill="${color}" opacity="0.85" stroke="rgba(0,0,0,0.55)" stroke-width="1"/>
		<rect x="9" y="8" width="6" height="8" rx="0.5" fill="rgba(255,255,255,0.85)"/>
		<line x1="10" y1="10" x2="14" y2="10" stroke="${color}" stroke-width="0.8"/>
		<line x1="10" y1="12" x2="14" y2="12" stroke="${color}" stroke-width="0.8"/>
		<line x1="10" y1="14" x2="13" y2="14" stroke="${color}" stroke-width="0.8"/>
	</svg>`;
}
