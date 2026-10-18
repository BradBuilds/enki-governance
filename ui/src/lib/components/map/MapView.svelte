<script lang="ts">
	/**
	 * Reusable embedded map component (MapLibre v5).
	 * Used by dashboard, case detail, and device detail views.
	 * Optionally plots signal markers and knowledge node markers.
	 */
	import { onMount, onDestroy } from 'svelte';

	interface Props {
		height?: string;
		center?: [number, number];
		zoom?: number;
		signals?: any[];
		nodes?: any[];
		onMapReady?: (map: any) => void;
	}

	let { height = '100%', center = [39.8283, -98.5795], zoom = 4, signals = [], nodes = [], onMapReady = () => {} }: Props = $props();

	let mapContainer: HTMLElement;
	let map = $state<any>(null);
	let maplibre: any = null;

	function plotMarkers() {
		if (!map || !maplibre) return;

		// Collect all points for bounds fitting
		const points: [number, number][] = [];

		// Plot signal markers
		for (const sig of signals) {
			const lat = sig.latitude ?? sig.properties?.latitude;
			const lon = sig.longitude ?? sig.properties?.longitude;
			if (lat == null || lon == null) continue;
			points.push([lon, lat]);

			const el = document.createElement('div');
			el.style.cssText = 'width:10px;height:10px;border-radius:50%;background:#3b82f6;border:2px solid #1e40af;cursor:pointer;';
			el.title = sig.signal_type || sig.title || 'Signal';
			new maplibre.Marker({ element: el }).setLngLat([lon, lat]).addTo(map);
		}

		// Plot knowledge node markers
		for (const node of nodes) {
			const lat = node.latitude ?? node.properties?.latitude;
			const lon = node.longitude ?? node.properties?.longitude;
			if (lat == null || lon == null) continue;
			points.push([lon, lat]);

			const el = document.createElement('div');
			el.style.cssText = 'width:12px;height:12px;transform:rotate(45deg);background:#22c55e;border:2px solid #166534;cursor:pointer;';
			el.title = node.title || 'Node';
			new maplibre.Marker({ element: el }).setLngLat([lon, lat]).addTo(map);
		}

		// Fit bounds to show all markers
		if (points.length > 0) {
			const bounds = new maplibre.LngLatBounds();
			for (const p of points) bounds.extend(p);
			map.fitBounds(bounds, { padding: 50, maxZoom: 14 });
		}
	}

	onMount(async () => {
		maplibre = await import('maplibre-gl');
		await import('maplibre-gl/dist/maplibre-gl.css');

		map = new maplibre.Map({
			container: mapContainer,
			style: {
				version: 8 as const,
				sources: {
					'satellite': {
						type: 'raster' as const,
						tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
						tileSize: 256,
						maxzoom: 18,
					},
				},
				layers: [{
					id: 'satellite-tiles',
					type: 'raster' as const,
					source: 'satellite',
					minzoom: 0,
					maxzoom: 22,
				}],
			} as any,
			center: [center[1], center[0]],
			zoom,
			interactive: true,
			attributionControl: false,
		});

		map.on('load', () => {
			plotMarkers();
			onMapReady(map);
		});
	});

	// Re-plot when signals or nodes change
	$effect(() => {
		if (map && maplibre && (signals.length > 0 || nodes.length > 0)) {
			plotMarkers();
		}
	});

	onDestroy(() => {
		if (map) { map.remove(); map = null; }
	});
</script>

<div
	bind:this={mapContainer}
	style="height: {height}; width: 100%; touch-action: none;"
	class="rounded-lg bg-muted"
></div>
