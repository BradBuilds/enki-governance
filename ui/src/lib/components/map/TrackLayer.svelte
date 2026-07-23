<script lang="ts">
	/**
	 * Renders polyline tracks on the map via MapAdapter.
	 * Each track is color-coded by entity and shows timestamps on hover.
	 * Optional: speed-gradient mode colors segments by speed (green→yellow→red).
	 */
	import { onDestroy } from 'svelte';
	import type { MapAdapter } from './engine/types';

	interface Props {
		map: any;
		tracks?: any[];
		adapter?: MapAdapter | null;
		speedGradient?: boolean;
	}

	let { map, tracks = [], adapter = null, speedGradient = false }: Props = $props();

	let handles: any[] = [];

	const trackColors = ['#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#a855f7', '#06b6d4', '#f97316', '#ec4899'];

	function speedToColor(speedKmh: number): string {
		// Green (slow) → Yellow (medium) → Red (fast)
		if (speedKmh <= 20) return '#22c55e';   // green - walking/stopped
		if (speedKmh <= 50) return '#84cc16';   // lime - city driving
		if (speedKmh <= 80) return '#f59e0b';   // amber - suburban
		if (speedKmh <= 110) return '#f97316';  // orange - highway
		return '#ef4444';                        // red - high speed
	}

	function renderTracks() {
		if (adapter) {
			for (const h of handles) adapter.remove(h);
		}
		handles = [];
		if (!adapter || !tracks.length) return;

		tracks.forEach((track, i) => {
			if (!track.geometry?.coordinates?.length) return;

			const coords: [number, number][] = track.geometry.coordinates.map((c: number[]) => [c[1], c[0]]);

			if (speedGradient && track.legs?.length) {
				// Speed-gradient mode: render each leg with speed-based color
				for (const leg of track.legs) {
					if (!leg.geometry?.length || leg.geometry.length < 2) continue;
					const legCoords: [number, number][] = leg.geometry.map((c: number[]) => [c[1], c[0]]);
					const speed = leg.speed_kmh || 0;
					const color = speedToColor(speed);
					const ph = adapter!.addPolyline({
						coords: legCoords,
						color,
						weight: 4,
						opacity: 0.9,
						tooltip: `${Math.round(speed)} km/h · ${leg.street || ''}`,
					});
					handles.push(ph);
				}
			} else {
				// Standard single-color mode
				const color = trackColors[i % trackColors.length];
				const polyHandle = adapter!.addPolyline({
					coords,
					color,
					weight: 3,
					opacity: 0.8,
					tooltip: `${track.display_name || track.entity_id} — ${track.signal_count} points`,
				});
				handles.push(polyHandle);
			}

			// Start/end markers
			if (coords.length >= 2) {
				const startHandle = adapter!.addMarker({
					lat: coords[0][0], lon: coords[0][1],
					color: '#22c55e', size: 10, shape: 'circle',
					tooltip: 'Start',
				});
				const endHandle = adapter!.addMarker({
					lat: coords[coords.length - 1][0], lon: coords[coords.length - 1][1],
					color: '#ef4444', size: 10, shape: 'circle',
					tooltip: 'End',
				});
				handles.push(startHandle, endHandle);
			}
		});
	}

	$effect(() => {
		if (adapter && tracks) {
			renderTracks();
		}
	});

	onDestroy(() => {
		if (adapter) {
			for (const h of handles) adapter.remove(h);
		}
	});
</script>
