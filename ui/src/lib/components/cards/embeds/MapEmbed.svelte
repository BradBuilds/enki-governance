<script lang="ts">
	/**
	 * Map embed — renders lat/lon as a static map tile with a pin.
	 * Supports three modes:
	 *   1. Point mode (default): single lat/lon with a pin
	 *   2. Track mode: array of coordinates renders a polyline connecting the dots
	 *   3. Both: latest point with a pin, plus track history as a polyline
	 *
	 * Uses OpenStreetMap static tiles (no API key needed).
	 * Clicking opens a larger map view.
	 */
	let { data }: { data: any } = $props();

	const lat = $derived(data?.lat || 0);
	const lon = $derived(data?.lon || 0);
	const label = $derived(data?.label || 'Location');
	const zoom = $derived(data?.zoom || 14);
	const track = $derived<Array<{lat: number; lon: number}>>(data?.track || []);
	const hasTrack = $derived(track.length >= 2);

	// OpenStreetMap URL for click-through
	const mapUrl = $derived(
		`https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=${zoom}/${lat}/${lon}`
	);

	function _lon2tile(lon: number, zoom: number): number {
		return Math.floor((lon + 180) / 360 * Math.pow(2, zoom));
	}
	function _lat2tile(lat: number, zoom: number): number {
		return Math.floor((1 - Math.log(Math.tan(lat * Math.PI / 180) + 1 / Math.cos(lat * Math.PI / 180)) / Math.PI) / 2 * Math.pow(2, zoom));
	}

	// Convert lat/lon to pixel position within the tile grid (3x3 = 768x768 at 256px tiles)
	function _coordToPixel(ptLat: number, ptLon: number, centerLat: number, centerLon: number, z: number): {x: number; y: number} {
		const n = Math.pow(2, z);
		const centerTileX = (centerLon + 180) / 360 * n;
		const centerTileY = (1 - Math.log(Math.tan(centerLat * Math.PI / 180) + 1 / Math.cos(centerLat * Math.PI / 180)) / Math.PI) / 2 * n;
		const ptTileX = (ptLon + 180) / 360 * n;
		const ptTileY = (1 - Math.log(Math.tan(ptLat * Math.PI / 180) + 1 / Math.cos(ptLat * Math.PI / 180)) / Math.PI) / 2 * n;
		// Pixel offset from center of 3x3 grid (each tile = 256px, grid = 768x768, center = 384,384)
		const x = 384 + (ptTileX - centerTileX) * 256;
		const y = 384 + (ptTileY - centerTileY) * 256;
		return {x, y};
	}

	// Build SVG polyline from track coordinates
	const trackSvgPoints = $derived.by(() => {
		if (!hasTrack) return '';
		return track.map(pt => {
			const {x, y} = _coordToPixel(pt.lat, pt.lon, lat, lon, zoom);
			return `${x},${y}`;
		}).join(' ');
	});

	// Start/end dot positions for track
	const startDot = $derived.by(() => {
		if (!hasTrack) return {x: 0, y: 0};
		return _coordToPixel(track[0].lat, track[0].lon, lat, lon, zoom);
	});
	const endDot = $derived.by(() => {
		if (!hasTrack) return {x: 0, y: 0};
		return _coordToPixel(track[track.length - 1].lat, track[track.length - 1].lon, lat, lon, zoom);
	});
</script>

<a href={mapUrl} target="_blank" rel="noopener" class="block relative rounded-lg overflow-hidden border border-border bg-muted hover:border-primary/30 transition-colors group">
	<div class="h-36 bg-muted flex items-center justify-center relative overflow-hidden">
		<!-- Static map tiles (3x3 grid for context) -->
		<div class="absolute inset-0 grid grid-cols-3 grid-rows-3 opacity-80">
			{#each [-1, 0, 1] as dy}
				{#each [-1, 0, 1] as dx}
					<img
						src="https://tile.openstreetmap.org/{zoom}/{_lon2tile(lon, zoom) + dx}/{_lat2tile(lat, zoom) + dy}.png"
						alt=""
						class="w-full h-full object-cover"
						loading="lazy"
					/>
				{/each}
			{/each}
		</div>

		<!-- Track polyline overlay -->
		{#if hasTrack}
			<svg class="absolute inset-0 w-full h-full z-[5] pointer-events-none" viewBox="0 0 768 768" preserveAspectRatio="none">
				<polyline
					points={trackSvgPoints}
					fill="none"
					stroke="#3b82f6"
					stroke-width="3"
					stroke-linejoin="round"
					stroke-linecap="round"
					opacity="0.8"
				/>
				<!-- Start dot (first point) -->
				<circle cx={startDot.x} cy={startDot.y} r="5" fill="#22c55e" stroke="white" stroke-width="2" />
				<!-- End dot (last point) -->
				<circle cx={endDot.x} cy={endDot.y} r="5" fill="#ef4444" stroke="white" stroke-width="2" />
			</svg>
		{/if}

		<!-- Center pin (current/latest position) -->
		{#if !hasTrack}
			<div class="relative z-10 flex flex-col items-center">
				<div class="w-6 h-6 rounded-full bg-enki-red border-2 border-white shadow-lg flex items-center justify-center">
					<div class="w-2 h-2 rounded-full bg-white"></div>
				</div>
				<div class="w-0.5 h-2 bg-enki-red"></div>
			</div>
		{/if}
	</div>
	<div class="px-3 py-2 flex items-center justify-between">
		<span class="text-[10px] text-muted-foreground">{label}{hasTrack ? ` · ${track.length} points` : ''}</span>
		<span class="text-[10px] font-mono text-muted-foreground/60">{lat.toFixed(4)}, {lon.toFixed(4)}</span>
	</div>
</a>
