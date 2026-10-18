<script lang="ts">
	// Flight-log network for a library: routes (weighted lines) + airports
	// (traffic-sized points) on a satellite globe. Data from
	// /api/libraries/{id}/flights (GeoJSON).
	import { onMount, onDestroy } from 'svelte';

	let { libraryId, height = '640px' }: { libraryId: number; height?: string } = $props();

	let mapContainer: HTMLDivElement;
	let map: any = null;
	let maplibre: any = null;
	let stats = $state<{ routes: number; airports: number } | null>(null);
	let loading = $state(true);

	onMount(async () => {
		maplibre = await import('maplibre-gl');
		await import('maplibre-gl/dist/maplibre-gl.css');

		const res = await fetch(`/api/libraries/${libraryId}/flights`);
		const data = res.ok ? await res.json() : { routes: { features: [] }, airports: { features: [] }, route_count: 0, airport_count: 0 };
		stats = { routes: data.route_count, airports: data.airport_count };

		map = new maplibre.Map({
			container: mapContainer,
			style: {
				version: 8,
				sources: {
					satellite: { type: 'raster', tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'], tileSize: 256, maxzoom: 18 },
					borders: { type: 'raster', tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}'], tileSize: 256, maxzoom: 18 },
				},
				layers: [
					{ id: 'sat', type: 'raster', source: 'satellite' },
					{ id: 'bord', type: 'raster', source: 'borders', paint: { 'raster-opacity': 0.5 } },
				],
			},
			center: [-72, 30], zoom: 2.4, attributionControl: false,
		});

		map.on('load', () => {
			map.addSource('routes', { type: 'geojson', data: data.routes });
			map.addSource('airports', { type: 'geojson', data: data.airports });

			// routes — width + opacity scale with flight count
			map.addLayer({
				id: 'routes', type: 'line', source: 'routes',
				layout: { 'line-cap': 'round' },
				paint: {
					'line-color': '#38bdf8',
					'line-width': ['interpolate', ['linear'], ['get', 'flights'], 1, 0.5, 100, 2, 2000, 6],
					'line-opacity': ['interpolate', ['linear'], ['get', 'flights'], 1, 0.15, 100, 0.4, 2000, 0.85],
				},
			});
			// airports — circle sized by traffic
			map.addLayer({
				id: 'airports', type: 'circle', source: 'airports',
				paint: {
					'circle-radius': ['interpolate', ['linear'], ['get', 'flights'], 1, 3, 500, 7, 10000, 16],
					'circle-color': '#fbbf24', 'circle-opacity': 0.85,
					'circle-stroke-width': 1, 'circle-stroke-color': '#7c2d12',
				},
			});
			map.addLayer({
				id: 'airport-labels', type: 'symbol', source: 'airports',
				filter: ['>', ['get', 'flights'], 300],
				layout: { 'text-field': ['get', 'code'], 'text-size': 11, 'text-offset': [0, 1.2] },
				paint: { 'text-color': '#fde68a', 'text-halo-color': '#000', 'text-halo-width': 1 },
			});

			const popup = new maplibre.Popup({ closeButton: false, closeOnClick: false });
			map.on('mouseenter', 'airports', (e: any) => {
				map.getCanvas().style.cursor = 'pointer';
				const p = e.features[0].properties;
				popup.setLngLat(e.lngLat).setHTML(`<b>${p.name}</b> (${p.code})<br/>${p.flights} flights`).addTo(map);
			});
			map.on('mouseleave', 'airports', () => { map.getCanvas().style.cursor = ''; popup.remove(); });
			map.on('mouseenter', 'routes', (e: any) => {
				const p = e.features[0].properties;
				popup.setLngLat(e.lngLat).setHTML(`${p.origin} → ${p.dest}<br/>${p.flights} flights`).addTo(map);
			});
			map.on('mouseleave', 'routes', () => popup.remove());
			loading = false;
		});
	});

	onDestroy(() => map?.remove());
</script>

<div class="relative rounded-xl overflow-hidden border border-border">
	<div bind:this={mapContainer} style="height: {height}; width: 100%;"></div>
	{#if stats}
		<div class="absolute top-2 left-2 bg-background/85 backdrop-blur rounded-md px-3 py-1.5 text-xs">
			<span class="text-primary font-semibold">{stats.routes}</span> routes ·
			<span class="text-amber-400 font-semibold">{stats.airports}</span> airports
			{#if loading}<span class="text-muted-foreground"> · loading…</span>{/if}
		</div>
	{/if}
</div>
