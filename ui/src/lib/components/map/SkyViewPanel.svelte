<script lang="ts">
	import { sunPosition, moonPosition, moonPhase } from '$lib/components/map/live/celestial';
	import X from 'lucide-svelte/icons/x';
	import Sun from 'lucide-svelte/icons/sun';
	import Moon from 'lucide-svelte/icons/moon';
	import Eye from 'lucide-svelte/icons/eye';

	interface Props {
		lat: number;
		lon: number;
		onClose: () => void;
	}

	let { lat, lon, onClose }: Props = $props();

	const now = new Date();
	const sun = sunPosition(now);
	const moon = moonPosition(now);
	const phase = moonPhase(now);

	// Angular distance from observer to sub-body point (approximate altitude proxy)
	function angularDist(lat1: number, lon1: number, lat2: number, lon2: number): number {
		const R = Math.PI / 180;
		const dLat = (lat2 - lat1) * R;
		const dLon = (lon2 - lon1) * R;
		const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * R) * Math.cos(lat2 * R) * Math.sin(dLon / 2) ** 2;
		return 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)) * (180 / Math.PI);
	}

	// Approximate elevation angle (90° - angular distance)
	function elevation(obsLat: number, obsLon: number, bodyLat: number, bodyLon: number): number {
		return Math.max(0, 90 - angularDist(obsLat, obsLon, bodyLat, bodyLon));
	}

	// Approximate azimuth from observer to body
	function azimuth(lat1: number, lon1: number, lat2: number, lon2: number): number {
		const R = Math.PI / 180;
		const dLon = (lon2 - lon1) * R;
		const y = Math.sin(dLon) * Math.cos(lat2 * R);
		const x = Math.cos(lat1 * R) * Math.sin(lat2 * R) - Math.sin(lat1 * R) * Math.cos(lat2 * R) * Math.cos(dLon);
		return ((Math.atan2(y, x) * 180 / Math.PI) + 360) % 360;
	}

	function compassDir(deg: number): string {
		const dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
		return dirs[Math.round(deg / 22.5) % 16];
	}

	const sunEl = elevation(lat, lon, sun.latitude, sun.longitude);
	const sunAz = azimuth(lat, lon, sun.latitude, sun.longitude);
	const moonEl = elevation(lat, lon, moon.latitude, moon.longitude);
	const moonAz = azimuth(lat, lon, moon.latitude, moon.longitude);
	const isNight = sunEl < 0.1;
	const isDusk = sunEl > 0 && sunEl < 18;

	// Time info
	const utcStr = now.toUTCString().slice(0, -4);

	// Known bright planets with approximate current zodiac positions
	// (very rough — for a real implementation we'd use JPL Horizons)
	const planets = [
		{ name: 'Mercury', visible: sunEl < 10 && sunEl > -12, desc: 'Near horizon at dawn/dusk' },
		{ name: 'Venus', visible: sunEl < 20 && sunEl > -18, desc: 'Bright evening/morning star' },
		{ name: 'Mars', visible: true, desc: 'Visible when above horizon' },
		{ name: 'Jupiter', visible: true, desc: 'Bright, visible most of the night' },
		{ name: 'Saturn', visible: true, desc: 'Visible when above horizon' },
	];
</script>

<div class="absolute bottom-4 left-4 z-[600] w-72 rounded-xl bg-card/95 backdrop-blur-sm border border-border shadow-2xl overflow-hidden">
	<!-- Header -->
	<div class="flex items-center justify-between px-3 py-2 border-b border-border bg-muted/30">
		<div class="flex items-center gap-2">
			<Eye size={14} class="text-primary" />
			<span class="text-xs font-semibold text-foreground">Sky View</span>
		</div>
		<div class="flex items-center gap-2">
			<span class="text-[9px] text-muted-foreground">{lat.toFixed(2)}°, {lon.toFixed(2)}°</span>
			<button onclick={onClose} class="p-0.5 rounded hover:bg-muted"><X size={12} /></button>
		</div>
	</div>

	<div class="p-3 space-y-3">
		<!-- Sky condition -->
		<div class="text-center py-2 rounded-lg {isNight ? 'bg-indigo-950/50' : isDusk ? 'bg-orange-950/30' : 'bg-sky-950/30'}">
			<p class="text-sm font-medium text-foreground">
				{isNight ? 'Night' : isDusk ? (sunEl < 6 ? 'Civil Twilight' : sunEl < 12 ? 'Nautical Twilight' : 'Golden Hour') : 'Daylight'}
			</p>
			<p class="text-[10px] text-muted-foreground">{utcStr} UTC</p>
		</div>

		<!-- Sun -->
		<div class="flex items-start gap-2">
			<Sun size={16} class="text-yellow-400 mt-0.5 shrink-0" />
			<div class="flex-1">
				<div class="flex justify-between">
					<span class="text-xs font-medium text-foreground">Sun</span>
					<span class="text-[10px] text-muted-foreground">{sunEl > 0 ? 'Above horizon' : 'Below horizon'}</span>
				</div>
				<div class="flex gap-3 text-[10px] text-muted-foreground">
					<span>El: {sunEl.toFixed(1)}°</span>
					<span>Az: {sunAz.toFixed(0)}° {compassDir(sunAz)}</span>
				</div>
			</div>
		</div>

		<!-- Moon -->
		<div class="flex items-start gap-2">
			<Moon size={16} class="text-gray-300 mt-0.5 shrink-0" />
			<div class="flex-1">
				<div class="flex justify-between">
					<span class="text-xs font-medium text-foreground">Moon</span>
					<span class="text-[10px] text-muted-foreground">{moonEl > 0 ? 'Above horizon' : 'Below horizon'}</span>
				</div>
				<div class="flex gap-3 text-[10px] text-muted-foreground">
					<span>El: {moonEl.toFixed(1)}°</span>
					<span>Az: {moonAz.toFixed(0)}° {compassDir(moonAz)}</span>
				</div>
				<div class="text-[10px] text-muted-foreground">
					{phase.name} — {Math.round(phase.illumination * 100)}% illuminated
				</div>
			</div>
		</div>

		<!-- Planets -->
		{#if isNight || isDusk}
		<div class="pt-2 border-t border-border/50">
			<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1">Potentially Visible Planets</p>
			{#each planets.filter(p => p.visible) as planet}
				<div class="flex items-center gap-2 py-0.5">
					<span class="w-1.5 h-1.5 rounded-full bg-amber-300"></span>
					<span class="text-[10px] text-foreground">{planet.name}</span>
					<span class="text-[9px] text-muted-foreground flex-1 text-right">{planet.desc}</span>
				</div>
			{/each}
		</div>
		{/if}

		<!-- Visibility note -->
		<p class="text-[9px] text-muted-foreground/60 italic">
			{isNight ? 'Best stargazing conditions. Check for light pollution and cloud cover.' : 'Stars and planets become visible after sunset.'}
		</p>
	</div>
</div>
