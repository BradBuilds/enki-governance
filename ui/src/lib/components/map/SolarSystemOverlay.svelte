<script lang="ts">
	import { onMount } from 'svelte';
	import { planetPositions, orbitRadii, RAD } from '$lib/components/map/live/celestial';

	interface Props {
		visible: boolean;
		opacity: number;
	}

	let { visible, opacity }: Props = $props();

	let planets = $state(planetPositions());
	let orbits = orbitRadii();

	onMount(() => {
		const interval = setInterval(() => { planets = planetPositions(); }, 60000);
		return () => clearInterval(interval);
	});

	const VB = 35;

	function scaleRadius(au: number): number {
		if (au < 2) return au * 4;
		if (au < 6) return 8 + (au - 2) * 2.5;
		return 18 + (au - 6) * 0.6;
	}

	// Earth position (used to offset everything so Earth is at center)
	const earthData = $derived(planets.find(p => p.name === 'Earth'));
	const earthR = $derived(earthData ? scaleRadius(earthData.distance) : 4);
	const earthAngle = $derived(earthData ? earthData.angle * RAD : 0);
	const offsetX = $derived(-(earthR * Math.cos(earthAngle)));
	const offsetY = $derived(earthR * Math.sin(earthAngle));

	function pos(planet: typeof planets[0]) {
		const r = scaleRadius(planet.distance);
		const a = planet.angle * RAD;
		return { x: r * Math.cos(a) + offsetX, y: -r * Math.sin(a) + offsetY };
	}

	// Sun position (offset from center)
	const sunPos = $derived({ x: offsetX, y: offsetY });
</script>

{#if visible}
<div class="solar-system-overlay" style="opacity: {opacity}; pointer-events: none;">
	<svg viewBox="{-VB} {-VB} {VB * 2} {VB * 2}" xmlns="http://www.w3.org/2000/svg">
		<defs>
			<!-- Sun glow -->
			<radialGradient id="sun-glow">
				<stop offset="0%" stop-color="#fbbf24" stop-opacity="1" />
				<stop offset="30%" stop-color="#f59e0b" stop-opacity="0.6" />
				<stop offset="60%" stop-color="#f59e0b" stop-opacity="0.1" />
				<stop offset="100%" stop-color="#f59e0b" stop-opacity="0" />
			</radialGradient>
			<!-- Mercury: gray rocky -->
			<radialGradient id="pg-mercury">
				<stop offset="0%" stop-color="#c0b8a8" /><stop offset="50%" stop-color="#8a8278" /><stop offset="100%" stop-color="#5a5248" />
			</radialGradient>
			<!-- Venus: yellow-white haze -->
			<radialGradient id="pg-venus">
				<stop offset="0%" stop-color="#f5e6b8" /><stop offset="50%" stop-color="#d4b86a" /><stop offset="100%" stop-color="#a08840" />
			</radialGradient>
			<!-- Mars: red-orange -->
			<radialGradient id="pg-mars">
				<stop offset="0%" stop-color="#e8a070" /><stop offset="40%" stop-color="#c05030" /><stop offset="100%" stop-color="#802010" />
			</radialGradient>
			<!-- Jupiter: banded orange/brown -->
			<radialGradient id="pg-jupiter">
				<stop offset="0%" stop-color="#e8c898" /><stop offset="40%" stop-color="#c4946a" /><stop offset="100%" stop-color="#8a6040" />
			</radialGradient>
			<!-- Saturn: golden with ring effect -->
			<radialGradient id="pg-saturn">
				<stop offset="0%" stop-color="#f0d890" /><stop offset="50%" stop-color="#c8a858" /><stop offset="100%" stop-color="#907838" />
			</radialGradient>
			<!-- Uranus: pale cyan -->
			<radialGradient id="pg-uranus">
				<stop offset="0%" stop-color="#b8e8f0" /><stop offset="50%" stop-color="#78c8d8" /><stop offset="100%" stop-color="#4898a8" />
			</radialGradient>
			<!-- Neptune: deep blue -->
			<radialGradient id="pg-neptune">
				<stop offset="0%" stop-color="#7898e8" /><stop offset="50%" stop-color="#4060c0" /><stop offset="100%" stop-color="#203880" />
			</radialGradient>
		</defs>

		<!-- Orbit rings (centered on Sun, which is offset) -->
		{#each orbits as orbit}
			{@const r = scaleRadius(orbit.radius)}
			<circle
				cx={sunPos.x} cy={sunPos.y} r={r}
				fill="none"
				stroke={orbit.name === 'Earth' ? '#4488ff' : orbit.color}
				stroke-width={orbit.name === 'Earth' ? '0.08' : '0.05'}
				stroke-opacity={orbit.name === 'Earth' ? '0.35' : '0.2'}
				stroke-dasharray={orbit.name === 'Earth' ? 'none' : '0.4,0.4'}
			/>
		{/each}

		<!-- Sun -->
		<circle cx={sunPos.x} cy={sunPos.y} r="3.5" fill="url(#sun-glow)" />
		<circle cx={sunPos.x} cy={sunPos.y} r="1.0" fill="#fbbf24" />
		<circle cx={sunPos.x} cy={sunPos.y} r="0.6" fill="#fff8e0" opacity="0.5" />

		<!-- Planets -->
		{#each planets as planet}
			{@const p = pos(planet)}
			{@const s = planet.size * 0.14}
			{@const gradId = `pg-${planet.name.toLowerCase()}`}

			{#if planet.name === 'Earth'}
				<!-- Earth is at center (0,0) — the MapLibre globe covers it -->
			{:else}
				<!-- Planet body -->
				<circle cx={p.x} cy={p.y} r={s} fill="url(#{gradId})" />
				<!-- Specular highlight -->
				<circle cx={p.x - s * 0.25} cy={p.y - s * 0.25} r={s * 0.35} fill="white" opacity="0.15" />
				<!-- Glow -->
				<circle cx={p.x} cy={p.y} r={s * 2.5} fill={planet.color} opacity="0.06" />

				{#if planet.name === 'Saturn'}
					<!-- Saturn's rings -->
					<ellipse cx={p.x} cy={p.y} rx={s * 2.2} ry={s * 0.6}
						fill="none" stroke="#c8a858" stroke-width="0.15" opacity="0.5"
						transform="rotate(-20, {p.x}, {p.y})" />
					<ellipse cx={p.x} cy={p.y} rx={s * 1.8} ry={s * 0.45}
						fill="none" stroke="#e0c878" stroke-width="0.08" opacity="0.3"
						transform="rotate(-20, {p.x}, {p.y})" />
				{/if}

				<!-- Label -->
				<text
					x={p.x} y={p.y + s + 1.0}
					fill={planet.color}
					font-size="0.75"
					font-family="Inter, sans-serif"
					text-anchor="middle"
					opacity="0.6"
				>{planet.name}</text>
			{/if}
		{/each}

		<!-- Jupiter bands (detail) -->
		{#each planets.filter(p => p.name === 'Jupiter') as jup}
			{@const p = pos(jup)}
			{@const s = jup.size * 0.14}
			<line x1={p.x - s * 0.7} y1={p.y - s * 0.3} x2={p.x + s * 0.7} y2={p.y - s * 0.3}
				stroke="#a07040" stroke-width="0.06" opacity="0.4" />
			<line x1={p.x - s * 0.8} y1={p.y + s * 0.15} x2={p.x + s * 0.8} y2={p.y + s * 0.15}
				stroke="#c08850" stroke-width="0.08" opacity="0.3" />
			<!-- Great Red Spot -->
			<ellipse cx={p.x + s * 0.3} cy={p.y + s * 0.15} rx="0.12" ry="0.08" fill="#c04020" opacity="0.5" />
		{/each}

		<!-- Mars polar caps -->
		{#each planets.filter(p => p.name === 'Mars') as mars}
			{@const p = pos(mars)}
			{@const s = mars.size * 0.14}
			<circle cx={p.x} cy={p.y - s * 0.7} r={s * 0.25} fill="white" opacity="0.2" />
		{/each}
	</svg>
</div>
{/if}

<style>
	.solar-system-overlay {
		position: absolute;
		inset: 0;
		z-index: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		transition: opacity 0.8s ease;
	}

	.solar-system-overlay svg {
		width: 95vmin;
		height: 95vmin;
		max-width: 1000px;
		max-height: 1000px;
	}
</style>
