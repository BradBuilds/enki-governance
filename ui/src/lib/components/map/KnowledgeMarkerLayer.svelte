<script lang="ts">
	/**
	 * Renders knowledge nodes on the map as epistemic-typed diamond markers.
	 * Visually distinct from signal circles — diamonds with epistemic-type colors.
	 */
	import { onDestroy } from 'svelte';

	interface Props {
		map: any;
		nodes?: any[];
		onNodeClick?: (node: any) => void;
	}

	let { map, nodes = [], onNodeClick = () => {} }: Props = $props();

	let markers: any[] = [];
	let L: any = null;

	const epistemicColors: Record<string, string> = {
		fact: '#22c55e',           // green — verified
		observation: '#3b82f6',    // blue — direct observation
		claim: '#f59e0b',          // amber — needs verification
		recommendation: '#a855f7', // purple
		opinion: '#ec4899',        // pink
		question: '#ef4444',       // red — open question
		definition: '#06b6d4',     // cyan
	};

	const epistemicIcons: Record<string, string> = {
		fact: '&#x2713;',          // checkmark
		observation: '&#x25C6;',   // diamond
		claim: '!',
		recommendation: '&#x2192;', // arrow
		opinion: '~',
		question: '?',
		definition: 'D',
	};

	async function renderMarkers() {
		if (!map) return;

		if (!L) {
			L = await import('leaflet');
		}

		// Clear old markers
		for (const m of markers) {
			map.removeLayer(m);
		}
		markers = [];

		for (const node of nodes) {
			const lat = node.properties?.latitude ?? node.latitude;
			const lon = node.properties?.longitude ?? node.longitude;
			if (lat == null || lon == null) continue;

			const type = node.properties?.epistemic_type ?? node.epistemic_type ?? 'observation';
			const color = epistemicColors[type] || '#6b7280';
			const symbol = epistemicIcons[type] || '&#x25C6;';
			const title = node.properties?.title ?? node.title ?? '';

			const icon = L.default.divIcon({
				html: `<div style="
					background: ${color};
					width: 24px; height: 24px;
					transform: rotate(45deg);
					border: 2px solid white;
					box-shadow: 0 2px 6px rgba(0,0,0,0.4);
					display: flex; align-items: center; justify-content: center;
					border-radius: 4px;
				"><span style="
					transform: rotate(-45deg);
					color: white;
					font-size: 10px;
					font-weight: 700;
					line-height: 1;
				">${symbol}</span></div>`,
				className: 'knowledge-marker',
				iconSize: [24, 24],
				iconAnchor: [12, 12],
			});

			const marker = L.default.marker([lat, lon], { icon });
			marker.bindTooltip(title, {
				direction: 'top',
				offset: [0, -14],
				className: 'knowledge-tooltip',
			});
			marker.on('click', () => onNodeClick(node));
			marker.addTo(map);
			markers.push(marker);
		}
	}

	$effect(() => {
		if (map && nodes) {
			renderMarkers();
		}
	});

	onDestroy(() => {
		for (const m of markers) {
			map?.removeLayer(m);
		}
	});
</script>

<svelte:head>
	<style>
		.knowledge-marker { background: none !important; border: none !important; }
		.knowledge-tooltip {
			background: rgba(0,0,0,0.85) !important;
			border: 1px solid rgba(255,255,255,0.2) !important;
			color: white !important;
			font-size: 11px !important;
			border-radius: 6px !important;
			padding: 4px 8px !important;
		}
		.knowledge-tooltip::before { border-top-color: rgba(0,0,0,0.85) !important; }
	</style>
</svelte:head>
