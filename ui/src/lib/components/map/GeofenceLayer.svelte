<script lang="ts">
	/**
	 * Renders geofence polygon overlays via MapAdapter.
	 * Semi-transparent fill with configurable color.
	 */
	import { onDestroy } from 'svelte';
	import type { MapAdapter } from './engine/types';

	interface Props {
		map: any; // Raw map instance (kept for backward compat)
		geofences?: any[];
		adapter?: MapAdapter | null;
	}

	let { map, geofences = [], adapter = null }: Props = $props();

	let handles: any[] = [];

	function renderGeofences() {
		if (adapter) {
			for (const h of handles) adapter.remove(h);
		}
		handles = [];

		if (!adapter) return;

		for (const gf of geofences) {
			if (!gf.geometry?.coordinates?.length) continue;

			// GeoJSON polygon: [[[lon, lat], ...]]
			const ring = gf.geometry.coordinates[0];
			const coords: [number, number][] = ring.map((c: number[]) => [c[1], c[0]]);

			const handle = adapter.addPolygon({
				coords,
				color: gf.color || '#ef4444',
				fillColor: gf.color || '#ef4444',
				fillOpacity: 0.15,
				weight: 2,
				dashArray: gf.enabled === false ? '5, 10' : undefined,
				popup: `<strong>${gf.name}</strong>${gf.description ? `<br><span style="color:#999">${gf.description}</span>` : ''}<br><span style="font-size:11px;color:#666">Alert: ${gf.alert_on || 'enter'}</span>`,
			});
			handles.push(handle);
		}
	}

	$effect(() => {
		if (adapter && geofences) {
			renderGeofences();
		}
	});

	onDestroy(() => {
		if (adapter) {
			for (const h of handles) adapter.remove(h);
		}
	});
</script>
