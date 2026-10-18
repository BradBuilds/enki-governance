<script lang="ts">
	/**
	 * Renders saved map annotations (pins, routes, polygons, circles)
	 * from the annotations API via the MapAdapter.
	 */
	import { onDestroy } from 'svelte';
	import type { MapAdapter } from './engine/types';

	interface Props {
		adapter: MapAdapter;
		annotations?: any[];  // GeoJSON features
	}

	let { adapter, annotations = [] }: Props = $props();

	let handles: any[] = [];

	function renderAnnotations() {
		// Clear old
		for (const h of handles) adapter.remove(h);
		handles = [];

		for (const feat of annotations) {
			const geom = feat.geometry;
			const props = feat.properties || {};
			const style = props.style || {};
			const color = style.color || '#3b82f6';

			if (!geom) continue;

			if (geom.type === 'Point') {
				const [lon, lat] = geom.coordinates;
				const h = adapter.addMarker({
					lat, lon,
					color,
					size: 10,
					shape: 'circle',
					tooltip: props.title || 'Annotation',
				});
				handles.push(h);
			} else if (geom.type === 'LineString') {
				const coords: [number, number][] = geom.coordinates.map((c: number[]) => [c[1], c[0]]);
				const h = adapter.addPolyline({
					coords,
					color,
					weight: style.weight || 2,
					opacity: 0.8,
					tooltip: props.title || 'Route',
				});
				handles.push(h);
			} else if (geom.type === 'Polygon') {
				const ring = geom.coordinates[0];
				const coords: [number, number][] = ring.map((c: number[]) => [c[1], c[0]]);
				const h = adapter.addPolygon({
					coords,
					color,
					fillColor: color,
					fillOpacity: style.fillOpacity ?? 0.1,
					weight: style.weight || 2,
					popup: props.title || 'Area',
				});
				handles.push(h);
			}
		}
	}

	$effect(() => {
		if (adapter && annotations) {
			renderAnnotations();
		}
	});

	onDestroy(() => {
		for (const h of handles) adapter?.remove(h);
	});
</script>
