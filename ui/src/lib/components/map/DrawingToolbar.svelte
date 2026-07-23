<script lang="ts">
	/**
	 * Drawing toolbar — creates annotations and geofences on the map.
	 * Uses @mapbox/mapbox-gl-draw (compatible with MapLibre GL).
	 */
	import { onDestroy } from 'svelte';
	import { api } from '$lib/api/mock';
	import type { MapAdapter } from './engine/types';

	import Pencil from 'lucide-svelte/icons/pencil';
	import X from 'lucide-svelte/icons/x';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Route from 'lucide-svelte/icons/route';
	import Shield from 'lucide-svelte/icons/shield';
	import Pentagon from 'lucide-svelte/icons/pentagon';

	interface Props {
		adapter: MapAdapter;
		projectId?: string;
		onAnnotationCreated?: () => void;
		onGeofenceCreated?: () => void;
	}

	let { adapter, projectId = '', onAnnotationCreated = () => {}, onGeofenceCreated = () => {} }: Props = $props();

	let active = $state(false);
	let drawMode = $state<'pin' | 'route' | 'polygon' | 'geofence' | null>(null);
	let draw: any = null;
	let drawLoaded = $state(false);

	async function initDraw() {
		if (draw) return;
		const MapboxDraw = (await import('@mapbox/mapbox-gl-draw')).default;
		await import('@mapbox/mapbox-gl-draw/dist/mapbox-gl-draw.css');

		const map = adapter.getRawMap();

		draw = new MapboxDraw({
			displayControlsDefault: false,
			controls: {},
			styles: [
				// Polygon fill
				{ id: 'gl-draw-polygon-fill', type: 'fill', filter: ['all', ['==', '$type', 'Polygon'], ['!=', 'mode', 'static']],
				  paint: { 'fill-color': '#3b82f6', 'fill-opacity': 0.15 }},
				// Polygon outline
				{ id: 'gl-draw-polygon-stroke', type: 'line', filter: ['all', ['==', '$type', 'Polygon'], ['!=', 'mode', 'static']],
				  paint: { 'line-color': '#3b82f6', 'line-width': 2 }},
				// Line
				{ id: 'gl-draw-line', type: 'line', filter: ['all', ['==', '$type', 'LineString'], ['!=', 'mode', 'static']],
				  paint: { 'line-color': '#3b82f6', 'line-width': 3 }},
				// Point
				{ id: 'gl-draw-point', type: 'circle', filter: ['all', ['==', '$type', 'Point'], ['==', 'meta', 'feature']],
				  paint: { 'circle-radius': 6, 'circle-color': '#3b82f6', 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 }},
				// Vertex points
				{ id: 'gl-draw-vertex', type: 'circle', filter: ['all', ['==', '$type', 'Point'], ['==', 'meta', 'vertex']],
				  paint: { 'circle-radius': 4, 'circle-color': '#fff', 'circle-stroke-color': '#3b82f6', 'circle-stroke-width': 2 }},
				// Midpoints
				{ id: 'gl-draw-midpoint', type: 'circle', filter: ['all', ['==', '$type', 'Point'], ['==', 'meta', 'midpoint']],
				  paint: { 'circle-radius': 3, 'circle-color': '#3b82f6' }},
			],
		});

		map.addControl(draw, 'top-left');

		// Hide the default draw controls (we use our own buttons)
		const drawContainer = map.getContainer().querySelector('.mapboxgl-ctrl-group');
		if (drawContainer) drawContainer.style.display = 'none';

		map.on('draw.create', handleDrawCreate);
		drawLoaded = true;
	}

	async function handleDrawCreate(e: any) {
		if (!draw) return;
		const features = e.features;
		if (!features?.length) return;

		const feature = features[0];
		const geomType = feature.geometry.type;

		if (drawMode === 'geofence' && geomType === 'Polygon') {
			// Save as geofence
			const name = prompt('Geofence name:') || `Zone ${new Date().toLocaleTimeString()}`;
			const alertOn = prompt('Alert on: enter, exit, or both?', 'enter') || 'enter';

			try {
				// Convert GeoJSON polygon coords to the API format
				const coords = feature.geometry.coordinates[0]; // [[lng, lat], ...]
				await api.createGeofence({
					name,
					polygon: coords,
					alert_on: alertOn,
					color: '#ef4444',
					description: '',
					created_by: 'map-user',
				});
				onGeofenceCreated();
			} catch (err) {
				console.error('Failed to create geofence:', err);
			}
		} else {
			// Save as annotation
			let annotationType = 'pin';
			if (geomType === 'LineString') annotationType = 'route';
			else if (geomType === 'Polygon') annotationType = 'polygon';

			const title = prompt('Name this annotation:') || `${annotationType} ${new Date().toLocaleTimeString()}`;

			try {
				await api.createAnnotation({
					annotation_type: annotationType,
					geometry: feature.geometry,
					title,
					project_id: projectId || undefined,
					style: { color: '#3b82f6', weight: 2, fillOpacity: 0.15 },
				});
				onAnnotationCreated();
			} catch (err) {
				console.error('Failed to create annotation:', err);
			}
		}

		// Clear drawn features from draw control
		draw.deleteAll();
		drawMode = null;
	}

	async function startDraw(mode: 'pin' | 'route' | 'polygon' | 'geofence') {
		await initDraw();
		if (!draw) return;

		drawMode = mode;
		active = true;

		if (mode === 'pin') {
			draw.changeMode('draw_point');
		} else if (mode === 'route') {
			draw.changeMode('draw_line_string');
		} else if (mode === 'polygon' || mode === 'geofence') {
			draw.changeMode('draw_polygon');
		}
	}

	function stopDraw() {
		if (draw) {
			draw.changeMode('simple_select');
			draw.deleteAll();
		}
		active = false;
		drawMode = null;
	}

	let showMenu = $state(false);

	onDestroy(() => {
		if (draw) {
			try {
				const map = adapter.getRawMap();
				map.off('draw.create', handleDrawCreate);
				map.removeControl(draw);
			} catch {}
			draw = null;
		}
	});
</script>

<div class="relative">
	{#if active}
		<button
			onclick={stopDraw}
			class="flex items-center gap-1.5 bg-primary/10 backdrop-blur-sm border border-primary/30 rounded-lg px-3 py-2 shadow-lg text-[10px] text-primary transition-colors"
		>
			<X class="w-3 h-3" />
			{#if drawMode === 'geofence'}Drawing Geofence...{:else}Drawing...{/if}
			<span class="text-[9px] text-muted-foreground ml-1">(click to finish, Esc to cancel)</span>
		</button>
	{:else}
		<button
			onclick={() => showMenu = !showMenu}
			class="flex items-center gap-1.5 bg-card/90 backdrop-blur-sm border border-border rounded-lg px-3 py-2 shadow-lg text-[10px] text-muted-foreground hover:text-foreground transition-colors"
		>
			<Pencil class="w-3 h-3" /> Draw
		</button>

		{#if showMenu}
			<!-- Backdrop -->
			<button onclick={() => showMenu = false} class="fixed inset-0 z-10" aria-label="Close"></button>

			<!-- Draw mode menu -->
			<div class="absolute bottom-full mb-2 left-0 bg-card/95 backdrop-blur-sm border border-border rounded-lg shadow-lg py-1 z-20 min-w-[160px]">
				<button onclick={() => { showMenu = false; startDraw('pin'); }}
					class="w-full flex items-center gap-2 px-3 py-2 text-xs text-foreground hover:bg-muted transition-colors text-left">
					<MapPin class="w-3.5 h-3.5 text-primary" /> Drop Pin
				</button>
				<button onclick={() => { showMenu = false; startDraw('route'); }}
					class="w-full flex items-center gap-2 px-3 py-2 text-xs text-foreground hover:bg-muted transition-colors text-left">
					<Route class="w-3.5 h-3.5 text-enki-green" /> Draw Route
				</button>
				<button onclick={() => { showMenu = false; startDraw('polygon'); }}
					class="w-full flex items-center gap-2 px-3 py-2 text-xs text-foreground hover:bg-muted transition-colors text-left">
					<Pentagon class="w-3.5 h-3.5 text-enki-cyan" /> Draw Area
				</button>
				<div class="border-t border-border my-1"></div>
				<button onclick={() => { showMenu = false; startDraw('geofence'); }}
					class="w-full flex items-center gap-2 px-3 py-2 text-xs text-foreground hover:bg-muted transition-colors text-left">
					<Shield class="w-3.5 h-3.5 text-enki-red" /> Create Geofence
					<span class="text-[9px] text-muted-foreground ml-auto">+ alert</span>
				</button>
			</div>
		{/if}
	{/if}
</div>
