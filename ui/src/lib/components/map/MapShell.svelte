<script lang="ts">
	/**
	 * MapShell — single MapLibre GL v5 map with globe projection.
	 * Zoomed out = 3D globe with atmosphere. Zoomed in = detailed satellite tiles.
	 * One seamless experience, no engine switching.
	 */
	import { onMount, onDestroy } from 'svelte';
	import type { MapAdapter } from './engine/types';
	import SolarSystemOverlay from './SolarSystemOverlay.svelte';

	interface Props {
		height?: string;
		center?: [number, number];
		zoom?: number;
		onReady?: (adapter: MapAdapter) => void;
	}

	let {
		height = '100%',
		center = [39.8283, -98.5795],
		zoom = 3,
		onReady = () => {},
	}: Props = $props();

	let container: HTMLElement;
	let starCanvas: HTMLCanvasElement;
	let adapter: MapAdapter | null = null;
	let currentZoom = $state(zoom);

	// Generate a random star field on a canvas (much more realistic than CSS gradients)
	function renderStarField() {
		if (!starCanvas) return;
		const dpr = window.devicePixelRatio || 1;
		const w = starCanvas.clientWidth * dpr;
		const h = starCanvas.clientHeight * dpr;
		starCanvas.width = w;
		starCanvas.height = h;
		const ctx = starCanvas.getContext('2d');
		if (!ctx) return;

		// Deep space background
		const grad = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w * 0.7);
		grad.addColorStop(0, '#0a0a1a');
		grad.addColorStop(1, '#020206');
		ctx.fillStyle = grad;
		ctx.fillRect(0, 0, w, h);

		// Seeded random for consistency
		let seed = 42;
		function rng() { seed = (seed * 16807 + 0) % 2147483647; return seed / 2147483647; }

		// Dim background stars (~800)
		for (let i = 0; i < 800; i++) {
			const x = rng() * w;
			const y = rng() * h;
			const r = rng() * 0.8 + 0.2;
			const brightness = rng() * 0.5 + 0.1;
			ctx.beginPath();
			ctx.arc(x, y, r, 0, Math.PI * 2);
			ctx.fillStyle = `rgba(255,255,255,${brightness})`;
			ctx.fill();
		}

		// Medium stars with slight color variation (~200)
		for (let i = 0; i < 200; i++) {
			const x = rng() * w;
			const y = rng() * h;
			const r = rng() * 1.0 + 0.5;
			const brightness = rng() * 0.4 + 0.4;
			const hue = rng() < 0.3 ? '200,220,255' : rng() < 0.5 ? '255,240,200' : '255,255,255';
			ctx.beginPath();
			ctx.arc(x, y, r, 0, Math.PI * 2);
			ctx.fillStyle = `rgba(${hue},${brightness})`;
			ctx.fill();
		}

		// Bright prominent stars (~30)
		for (let i = 0; i < 30; i++) {
			const x = rng() * w;
			const y = rng() * h;
			const r = rng() * 1.5 + 1;
			const hue = rng() < 0.2 ? '#ffd4a0' : rng() < 0.4 ? '#a0c4ff' : '#ffffff';
			// Glow
			const glow = ctx.createRadialGradient(x, y, 0, x, y, r * 3);
			glow.addColorStop(0, hue);
			glow.addColorStop(0.3, hue + '88');
			glow.addColorStop(1, 'transparent');
			ctx.fillStyle = glow;
			ctx.fillRect(x - r * 3, y - r * 3, r * 6, r * 6);
			// Core
			ctx.beginPath();
			ctx.arc(x, y, r * 0.6, 0, Math.PI * 2);
			ctx.fillStyle = hue;
			ctx.fill();
		}

		// Faint nebula/milky way glow (subtle)
		const neb = ctx.createRadialGradient(w * 0.3, h * 0.6, w * 0.05, w * 0.3, h * 0.6, w * 0.3);
		neb.addColorStop(0, 'rgba(60,40,80,0.06)');
		neb.addColorStop(0.5, 'rgba(40,30,70,0.03)');
		neb.addColorStop(1, 'transparent');
		ctx.fillStyle = neb;
		ctx.fillRect(0, 0, w, h);
	}

	// Solar system overlay fades in when zoomed far out (zoom < 3)
	const solarSystemVisible = $derived(currentZoom < 3);
	const solarSystemOpacity = $derived(
		currentZoom < 1.5 ? 1 : currentZoom < 3 ? (3 - currentZoom) / 1.5 : 0
	);

	onMount(async () => {
		// Render star field background
		renderStarField();

		// Prevent browser page zoom (Ctrl+wheel / trackpad pinch) on the map container
		// This ensures wheel events only control map zoom, not page zoom
		container.addEventListener('wheel', (e: WheelEvent) => {
			if (e.ctrlKey) {
				e.preventDefault(); // Prevent browser zoom, MapLibre handles it
			}
		}, { passive: false });

		// Prevent touch-based page zoom on the map
		container.addEventListener('touchmove', (e: TouchEvent) => {
			if (e.touches.length > 1) {
				e.preventDefault(); // Prevent pinch page zoom
			}
		}, { passive: false });

		const maplibregl = await import('maplibre-gl');
		await import('maplibre-gl/dist/maplibre-gl.css');

		const style = {
			version: 8 as const,
			name: 'Enki Satellite Globe',
			projection: { type: 'globe' as const },
			sky: { 'sky-color': 'transparent', 'horizon-color': 'transparent' },
			sources: {
				'satellite': {
					type: 'raster' as const,
					tiles: [
						'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
					],
					tileSize: 256,
					maxzoom: 18,
					attribution: '&copy; Esri',
				},
			},
			layers: [
				{
					id: 'satellite-tiles',
					type: 'raster' as const,
					source: 'satellite',
					minzoom: 0,
					maxzoom: 22,
				},
			],
		};

		const map = new maplibregl.Map({
			container,
			style: style as any,
			center: [center[1], center[0]], // MapLibre uses [lng, lat]
			zoom,
			maxPitch: 85,
			antialias: true,
		});

		// Force globe projection
		map.on('style.load', () => {
			try {
				map.setProjection({ type: 'globe' });
			} catch (e) {
				console.warn('Globe projection not available:', e);
			}
		});

		map.on('load', () => {
			// Note: fog and terrain have limited globe support in MapLibre v5
			// They work in flat/mercator view and degrade gracefully on globe
		});

		// Track zoom for solar system overlay
		map.on('zoom', () => { currentZoom = map.getZoom(); });

		// Navigation controls (top-left to avoid overlap with layer panel top-right)
		map.addControl(new maplibregl.NavigationControl(), 'bottom-right');

		const { createMapLibreAdapter } = await import('./engine/maplibre-adapter');

		if (map.loaded()) {
			adapter = createMapLibreAdapter(map, maplibregl);
			onReady(adapter);
		} else {
			map.on('load', () => {
				adapter = createMapLibreAdapter(map, maplibregl);
				onReady(adapter);
			});
		}
	});

	onDestroy(() => {
		if (adapter) {
			try {
				adapter.clearAll();
				adapter.getRawMap()?.remove();
			} catch {}
			adapter = null;
		}
	});
</script>

<div class="star-field">
	<canvas bind:this={starCanvas} class="star-canvas"></canvas>
	<SolarSystemOverlay visible={solarSystemVisible} opacity={solarSystemOpacity} />
	<div
		bind:this={container}
		style="height: {height}; width: 100%; touch-action: none;"
		class="map-container"
	></div>
</div>

<style>
	.star-field {
		position: relative;
		height: 100%;
		width: 100%;
		background: #020206;
		overflow: hidden;
	}

	.star-canvas {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		z-index: 0;
		pointer-events: none;
	}

	.map-container {
		position: relative;
		z-index: 1;
		touch-action: none;
		-webkit-overflow-scrolling: auto;
		overscroll-behavior: none;
		background: transparent !important;
	}

	/* Make MapLibre canvas background transparent so stars show through */
	.map-container :global(.maplibregl-canvas) {
		background: transparent !important;
	}
</style>
