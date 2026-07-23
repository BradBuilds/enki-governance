<script lang="ts">
	/**
	 * Renders knowledge graph edges as curved lines between connected nodes on the map.
	 * Fetches edges for visible nodes and draws polylines with relationship labels.
	 */
	import { onDestroy } from 'svelte';
	import type { MapAdapter } from './engine/types';

	interface Props {
		adapter: MapAdapter;
		nodes?: any[];       // GeoJSON features with node_id in properties
		edges?: any[];       // {source_node_id, target_node_id, relationship, strength}
	}

	let { adapter, nodes = [], edges = [] }: Props = $props();

	let handles: any[] = [];

	const relationshipColors: Record<string, string> = {
		supports: '#22c55e',
		contrasts: '#ef4444',
		defines: '#06b6d4',
		extends: '#3b82f6',
		references: '#a855f7',
		led_to: '#f59e0b',
		same_concept: '#6366f1',
		questions: '#ec4899',
		supersedes: '#f97316',
	};

	function renderEdges() {
		for (const h of handles) adapter.remove(h);
		handles = [];

		if (!nodes.length || !edges.length) return;

		// Build node location lookup
		const nodeLocs: Map<string, [number, number]> = new Map();
		for (const n of nodes) {
			const props = n.properties || n;
			const id = props.node_id;
			const lat = props.latitude;
			const lon = props.longitude;
			if (id && lat != null && lon != null) {
				nodeLocs.set(id, [lat, lon]);
			}
		}

		for (const edge of edges) {
			const srcLoc = nodeLocs.get(edge.source_node_id || edge.source);
			const tgtLoc = nodeLocs.get(edge.target_node_id || edge.target);
			if (!srcLoc || !tgtLoc) continue;

			// Create a curved line by adding a midpoint offset
			const midLat = (srcLoc[0] + tgtLoc[0]) / 2;
			const midLon = (srcLoc[1] + tgtLoc[1]) / 2;
			// Offset midpoint perpendicular to the line for a curve effect
			const dx = tgtLoc[1] - srcLoc[1];
			const dy = tgtLoc[0] - srcLoc[0];
			const dist = Math.sqrt(dx * dx + dy * dy);
			const offset = dist * 0.15; // 15% curve
			const curveLat = midLat + (dx / dist) * offset;
			const curveLon = midLon - (dy / dist) * offset;

			const color = relationshipColors[edge.relationship] || '#6b7280';
			const strength = edge.strength ?? 1.0;

			const h = adapter.addPolyline({
				coords: [srcLoc, [curveLat, curveLon], tgtLoc],
				color,
				weight: Math.max(1, strength * 3),
				opacity: 0.5 + strength * 0.3,
				tooltip: `${edge.relationship || 'connected'}${strength < 1 ? ` (${(strength * 100).toFixed(0)}%)` : ''}`,
			});
			handles.push(h);
		}
	}

	$effect(() => {
		if (adapter && nodes && edges) {
			renderEdges();
		}
	});

	onDestroy(() => {
		for (const h of handles) adapter?.remove(h);
	});
</script>
