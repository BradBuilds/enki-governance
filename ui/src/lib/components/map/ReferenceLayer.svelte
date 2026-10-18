<script lang="ts">
	/**
	 * ReferenceLayer — rich reference overlays using native MapLibre layers.
	 * Renders countries (borders + labels), capitals (scaled markers + labels),
	 * rivers (styled lines), and lakes (filled polygons) directly via MapLibre
	 * GeoJSON sources for high performance.
	 */
	import type { MapAdapter } from './engine/types';

	interface Props {
		adapter: MapAdapter;
		showCountries?: boolean;
		showCapitals?: boolean;
		showRivers?: boolean;
		showLakes?: boolean;
	}

	let {
		adapter,
		showCountries = false,
		showCapitals = false,
		showRivers = false,
		showLakes = false,
	}: Props = $props();

	// Track which sources/layers we've added
	const layerIds: string[] = [];
	const sourceIds: string[] = [];

	function getMap(): any {
		return adapter?.getRawMap();
	}

	function safeRemoveLayer(map: any, id: string) {
		try { if (map.getLayer(id)) map.removeLayer(id); } catch {}
	}
	function safeRemoveSource(map: any, id: string) {
		try { if (map.getSource(id)) map.removeSource(id); } catch {}
	}

	function cleanupAll() {
		const map = getMap();
		if (!map) return;
		for (const id of layerIds) safeRemoveLayer(map, id);
		for (const id of sourceIds) safeRemoveSource(map, id);
		layerIds.length = 0;
		sourceIds.length = 0;
	}

	function addSource(map: any, id: string, spec: any) {
		if (!map.getSource(id)) {
			map.addSource(id, spec);
			sourceIds.push(id);
		}
	}

	function addLayer(map: any, spec: any) {
		if (!map.getLayer(spec.id)) {
			map.addLayer(spec);
			layerIds.push(spec.id);
		}
	}

	// ─── Countries ──────────────────────────────────────────────────
	async function renderCountries(map: any) {
		safeRemoveLayer(map, 'ref-countries-fill');
		safeRemoveLayer(map, 'ref-countries-line');
		safeRemoveLayer(map, 'ref-countries-label');
		safeRemoveSource(map, 'ref-countries');

		if (!showCountries) return;

		const data = await fetch('/geo/countries.geojson').then(r => r.json());

		addSource(map, 'ref-countries', { type: 'geojson', data });

		addLayer(map, {
			id: 'ref-countries-fill', type: 'fill', source: 'ref-countries',
			paint: {
				'fill-color': 'rgba(100, 160, 220, 0.06)',
			},
		});

		addLayer(map, {
			id: 'ref-countries-line', type: 'line', source: 'ref-countries',
			paint: {
				'line-color': 'rgba(140, 180, 220, 0.35)',
				'line-width': 1,
			},
		});

		addLayer(map, {
			id: 'ref-countries-label', type: 'symbol', source: 'ref-countries',
			layout: {
				'text-field': ['get', 'ADMIN'],
				'text-size': ['interpolate', ['linear'], ['zoom'], 2, 8, 4, 11, 6, 14],
				'text-transform': 'uppercase',
				'text-letter-spacing': 0.1,
				'text-max-width': 8,
				'text-allow-overlap': false,
			},
			paint: {
				'text-color': 'rgba(180, 200, 220, 0.6)',
				'text-halo-color': 'rgba(0, 0, 0, 0.7)',
				'text-halo-width': 1.5,
			},
		});
	}

	// ─── Capitals & Cities ──────────────────────────────────────────
	async function renderCapitals(map: any) {
		safeRemoveLayer(map, 'ref-capitals-circle');
		safeRemoveLayer(map, 'ref-capitals-label');
		safeRemoveLayer(map, 'ref-cities-circle');
		safeRemoveLayer(map, 'ref-cities-label');
		safeRemoveSource(map, 'ref-cities');

		if (!showCapitals) return;

		const data = await fetch('/geo/cities.geojson').then(r => r.json());

		// Enrich: add computed properties for styling
		for (const f of data.features) {
			const p = f.properties;
			p._isCapital = p.adm0cap === 1 ? 1 : 0;
			p._popM = Math.round((p.pop_max || 0) / 1e6 * 10) / 10;
			p._label = p._isCapital
				? `${p.name} ★`
				: p.name;
		}

		// Filter: capitals + cities > 500k
		data.features = data.features.filter((f: any) =>
			f.properties._isCapital === 1 || (f.properties.pop_max || 0) > 500000
		);

		addSource(map, 'ref-cities', { type: 'geojson', data });

		// Capital markers — larger amber circles with white stroke
		addLayer(map, {
			id: 'ref-capitals-circle', type: 'circle', source: 'ref-cities',
			filter: ['==', ['get', '_isCapital'], 1],
			paint: {
				'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, 3, 5, 5, 8, 8],
				'circle-color': '#f59e0b',
				'circle-stroke-color': '#ffffff',
				'circle-stroke-width': 1.5,
				'circle-opacity': 0.9,
			},
		});

		// Capital labels
		addLayer(map, {
			id: 'ref-capitals-label', type: 'symbol', source: 'ref-cities',
			filter: ['==', ['get', '_isCapital'], 1],
			layout: {
				'text-field': ['get', '_label'],
				'text-size': ['interpolate', ['linear'], ['zoom'], 3, 9, 6, 12, 10, 14],
				'text-offset': [0, 1.2],
				'text-anchor': 'top',
				'text-allow-overlap': false,
				'text-optional': true,
			},
			paint: {
				'text-color': '#fbbf24',
				'text-halo-color': 'rgba(0, 0, 0, 0.8)',
				'text-halo-width': 1.5,
			},
		});

		// Regular city markers — smaller gray circles
		addLayer(map, {
			id: 'ref-cities-circle', type: 'circle', source: 'ref-cities',
			filter: ['==', ['get', '_isCapital'], 0],
			paint: {
				'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 2, 6, 3.5, 10, 6],
				'circle-color': '#94a3b8',
				'circle-stroke-color': 'rgba(255,255,255,0.5)',
				'circle-stroke-width': 1,
				'circle-opacity': 0.7,
			},
		});

		// City labels (show at higher zoom)
		addLayer(map, {
			id: 'ref-cities-label', type: 'symbol', source: 'ref-cities',
			filter: ['==', ['get', '_isCapital'], 0],
			minzoom: 5,
			layout: {
				'text-field': ['get', 'name'],
				'text-size': ['interpolate', ['linear'], ['zoom'], 5, 9, 8, 11],
				'text-offset': [0, 1.0],
				'text-anchor': 'top',
				'text-allow-overlap': false,
				'text-optional': true,
			},
			paint: {
				'text-color': 'rgba(200, 210, 220, 0.7)',
				'text-halo-color': 'rgba(0, 0, 0, 0.7)',
				'text-halo-width': 1,
			},
		});
	}

	// ─── Rivers ─────────────────────────────────────────────────────
	async function renderRivers(map: any) {
		safeRemoveLayer(map, 'ref-rivers-line');
		safeRemoveLayer(map, 'ref-rivers-label');
		safeRemoveSource(map, 'ref-rivers');

		if (!showRivers) return;

		const data = await fetch('/geo/rivers.geojson').then(r => r.json());

		addSource(map, 'ref-rivers', { type: 'geojson', data });

		addLayer(map, {
			id: 'ref-rivers-line', type: 'line', source: 'ref-rivers',
			paint: {
				'line-color': 'rgba(56, 189, 248, 0.45)',
				'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.8, 6, 1.5, 10, 2.5],
			},
		});

		addLayer(map, {
			id: 'ref-rivers-label', type: 'symbol', source: 'ref-rivers',
			minzoom: 4,
			layout: {
				'text-field': ['get', 'name'],
				'text-size': 9,
				'symbol-placement': 'line',
				'text-allow-overlap': false,
				'text-optional': true,
			},
			paint: {
				'text-color': 'rgba(56, 189, 248, 0.6)',
				'text-halo-color': 'rgba(0, 0, 0, 0.6)',
				'text-halo-width': 1,
			},
		});
	}

	// ─── Lakes ──────────────────────────────────────────────────────
	async function renderLakes(map: any) {
		safeRemoveLayer(map, 'ref-lakes-fill');
		safeRemoveLayer(map, 'ref-lakes-line');
		safeRemoveLayer(map, 'ref-lakes-label');
		safeRemoveSource(map, 'ref-lakes');

		if (!showLakes) return;

		const data = await fetch('/geo/lakes.geojson').then(r => r.json());

		addSource(map, 'ref-lakes', { type: 'geojson', data });

		addLayer(map, {
			id: 'ref-lakes-fill', type: 'fill', source: 'ref-lakes',
			paint: {
				'fill-color': 'rgba(56, 189, 248, 0.12)',
			},
		});

		addLayer(map, {
			id: 'ref-lakes-line', type: 'line', source: 'ref-lakes',
			paint: {
				'line-color': 'rgba(56, 189, 248, 0.35)',
				'line-width': 1,
			},
		});

		addLayer(map, {
			id: 'ref-lakes-label', type: 'symbol', source: 'ref-lakes',
			minzoom: 4,
			layout: {
				'text-field': ['get', 'name'],
				'text-size': 9,
				'text-allow-overlap': false,
				'text-optional': true,
			},
			paint: {
				'text-color': 'rgba(56, 189, 248, 0.5)',
				'text-halo-color': 'rgba(0, 0, 0, 0.5)',
				'text-halo-width': 1,
			},
		});
	}

	// ─── Effects ─────────────────────────────────────────────────────
	$effect(() => { const m = getMap(); if (m) renderCountries(m); });
	$effect(() => { const m = getMap(); if (m) renderCapitals(m); });
	$effect(() => { const m = getMap(); if (m) renderRivers(m); });
	$effect(() => { const m = getMap(); if (m) renderLakes(m); });
</script>
