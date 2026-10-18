<script lang="ts">
	/**
	 * EntityAssetsLayer — renders entity_addresses (HQs, registered offices,
	 * asset locations) as MapLibre point markers. Phase 8b.6.5.
	 *
	 * Color-codes by address_type:
	 *   hq / principal_office  → emerald
	 *   registered             → cyan
	 *   asset_location         → amber
	 *   branch / facility      → muted blue
	 *   default                → grey
	 *
	 * Clicking a marker emits an `entitySelect` event with the entity's
	 * node_id so the map shell can open its dossier.
	 */
	import type { MapAdapter } from './engine/types';

	interface Props {
		adapter: MapAdapter;
		visible?: boolean;
		addressTypes?: string[]; // optional filter
		onEntitySelect?: (nodeId: string) => void;
	}

	let { adapter, visible = false, addressTypes, onEntitySelect }: Props = $props();

	const SOURCE_ID = 'entity-assets';
	const LAYER_ID = 'entity-assets-circle';
	const LABEL_ID = 'entity-assets-label';

	let lastVisible = false;
	let lastFilter: string | undefined;
	let abortController: AbortController | null = null;

	function getMap(): any {
		return adapter?.getRawMap();
	}

	function safeRemove() {
		const map = getMap();
		if (!map) return;
		try { if (map.getLayer(LABEL_ID)) map.removeLayer(LABEL_ID); } catch {}
		try { if (map.getLayer(LAYER_ID)) map.removeLayer(LAYER_ID); } catch {}
		try { if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID); } catch {}
	}

	async function loadAndRender() {
		const map = getMap();
		if (!map) return;
		safeRemove();
		if (!visible) return;

		abortController?.abort();
		abortController = new AbortController();

		const params = new URLSearchParams();
		if (addressTypes && addressTypes.length > 0) {
			params.set('address_types', addressTypes.join(','));
		}
		const url = `/nodes/map-assets${params.toString() ? '?' + params.toString() : ''}`;

		let data: any;
		try {
			const r = await fetch(url, { signal: abortController.signal });
			if (!r.ok) return;
			data = await r.json();
		} catch (e: any) {
			if (e?.name !== 'AbortError') console.warn('EntityAssetsLayer fetch failed', e);
			return;
		}

		if (!data || !data.features || data.features.length === 0) return;

		map.addSource(SOURCE_ID, { type: 'geojson', data });

		map.addLayer({
			id: LAYER_ID,
			type: 'circle',
			source: SOURCE_ID,
			paint: {
				'circle-radius': [
					'interpolate', ['linear'], ['zoom'],
					3, 3,
					8, 5,
					14, 8,
				],
				'circle-color': [
					'match', ['get', 'address_type'],
					'hq', '#10b981',
					'principal_office', '#10b981',
					'registered', '#06b6d4',
					'asset_location', '#f59e0b',
					'facility', '#3b82f6',
					'branch', '#3b82f6',
					'#94a3b8',
				],
				'circle-stroke-color': 'rgba(0,0,0,0.6)',
				'circle-stroke-width': 1,
				'circle-opacity': 0.85,
			},
		});

		map.addLayer({
			id: LABEL_ID,
			type: 'symbol',
			source: SOURCE_ID,
			minzoom: 8,
			layout: {
				'text-field': ['get', 'entity_name'],
				'text-size': 11,
				'text-offset': [0, 1.2],
				'text-anchor': 'top',
				'text-allow-overlap': false,
			},
			paint: {
				'text-color': '#e5e7eb',
				'text-halo-color': 'rgba(0,0,0,0.7)',
				'text-halo-width': 1.4,
			},
		});

		// Click → emit entitySelect with the node_id
		map.on('click', LAYER_ID, (ev: any) => {
			const f = ev?.features?.[0];
			const nodeId = f?.properties?.node_id;
			if (nodeId && onEntitySelect) onEntitySelect(nodeId);
		});
		map.on('mouseenter', LAYER_ID, () => {
			map.getCanvas().style.cursor = 'pointer';
		});
		map.on('mouseleave', LAYER_ID, () => {
			map.getCanvas().style.cursor = '';
		});
	}

	// Re-render whenever visibility or filter changes
	$effect(() => {
		const filterKey = (addressTypes || []).join(',');
		if (visible !== lastVisible || filterKey !== lastFilter) {
			lastVisible = visible;
			lastFilter = filterKey;
			loadAndRender();
		}
	});
</script>
