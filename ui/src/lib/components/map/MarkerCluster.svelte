<script lang="ts">
	/**
	 * Marker clustering for signal points on the map.
	 * Rebuilds cluster group when signals change.
	 * Color-codes markers by signal type domain.
	 */
	import { onDestroy } from 'svelte';

	interface Props {
		map: any;
		signals?: any[];
		onMarkerClick?: (signal: any) => void;
	}

	let { map, signals = [], onMarkerClick = () => {} }: Props = $props();

	let clusterGroup: any = null;
	let L: any = null;

	const domainColors: Record<string, string> = {
		geospatial: '#3b82f6',  // blue
		security: '#ef4444',     // red
		communications: '#a855f7', // purple
		personal: '#22c55e',     // green
		financial: '#f59e0b',    // amber
		environmental: '#06b6d4', // cyan
		infrastructure: '#f97316', // orange
		vehicle: '#6366f1',      // indigo
		intelligence: '#ec4899', // pink
	};

	function getDomainColor(signalType: string): string {
		const parts = signalType?.split('.') || [];
		const domain = parts[1] || '';
		return domainColors[domain] || '#6b7280';
	}

	async function rebuildClusters() {
		if (!map) return;

		if (!L) {
			L = await import('leaflet');
			await import('leaflet.markercluster');
		}

		if (clusterGroup) {
			map.removeLayer(clusterGroup);
		}

		clusterGroup = (L.default as any).markerClusterGroup({
			maxClusterRadius: 50,
			spiderfyOnMaxZoom: true,
			showCoverageOnHover: false,
			iconCreateFunction: (cluster: any) => {
				const count = cluster.getChildCount();
				const size = count < 10 ? 'small' : count < 50 ? 'medium' : 'large';
				const sizes: Record<string, number> = { small: 30, medium: 40, large: 50 };
				return L.default.divIcon({
					html: `<div class="cluster-icon cluster-${size}">${count}</div>`,
					className: 'custom-cluster',
					iconSize: L.default.point(sizes[size], sizes[size]),
				});
			},
		});

		for (const sig of signals) {
			const lat = sig.properties?.latitude ?? sig.latitude;
			const lon = sig.properties?.longitude ?? sig.longitude;
			if (lat == null || lon == null) continue;

			const color = getDomainColor(sig.properties?.signal_type ?? sig.signal_type ?? '');

			const icon = L.default.divIcon({
				html: `<div style="background:${color};width:12px;height:12px;border-radius:50%;border:2px solid white;box-shadow:0 1px 3px rgba(0,0,0,0.4);"></div>`,
				className: 'custom-marker',
				iconSize: [12, 12],
				iconAnchor: [6, 6],
			});

			const marker = L.default.marker([lat, lon], { icon });
			marker.on('click', () => onMarkerClick(sig));
			clusterGroup.addLayer(marker);
		}

		map.addLayer(clusterGroup);
	}

	$effect(() => {
		if (map && signals) {
			rebuildClusters();
		}
	});

	onDestroy(() => {
		if (map && clusterGroup) {
			map.removeLayer(clusterGroup);
		}
	});
</script>

<svelte:head>
	<style>
		.custom-cluster .cluster-icon {
			display: flex;
			align-items: center;
			justify-content: center;
			border-radius: 50%;
			font-size: 11px;
			font-weight: 600;
			color: white;
			border: 2px solid rgba(255,255,255,0.8);
			box-shadow: 0 2px 6px rgba(0,0,0,0.3);
		}
		.cluster-small { background: rgba(59,130,246,0.8); }
		.cluster-medium { background: rgba(245,158,11,0.8); }
		.cluster-large { background: rgba(239,68,68,0.8); }
		.custom-marker { background: none !important; border: none !important; }
	</style>
</svelte:head>
