/**
 * MapLibre GL implementation of MapAdapter.
 * Provides 3D globe view with the same interface as Leaflet.
 */

import type { MapAdapter, MapBounds, MarkerOptions, PolylineOptions, PolygonOptions } from './types';

export function createMapLibreAdapter(map: any, maplibregl: any): MapAdapter {
	const features: Set<any> = new Set();
	let sourceCounter = 0;

	function nextId(): string {
		return `enki_${++sourceCounter}`;
	}

	const adapter: MapAdapter = {
		engine: 'maplibre',

		getBounds(): MapBounds {
			const b = map.getBounds();
			return {
				west: b.getWest(),
				south: b.getSouth(),
				east: b.getEast(),
				north: b.getNorth(),
			};
		},

		flyTo(lat: number, lon: number, zoom = 14) {
			map.flyTo({ center: [lon, lat], zoom, duration: 2000 });
		},

		fitBounds(bounds: MapBounds, padding = 20) {
			map.fitBounds(
				[[bounds.west, bounds.south], [bounds.east, bounds.north]],
				{ padding }
			);
		},

		addMarker(opts: MarkerOptions): any {
			const el = document.createElement('div');
			const size = opts.size || 12;
			const color = opts.color || '#3b82f6';

			if (opts.html) {
				el.innerHTML = opts.html;
			} else if (opts.shape === 'diamond') {
				el.innerHTML = `<div style="
					background:${color}; width:${size}px; height:${size}px;
					transform:rotate(45deg); border:2px solid white;
					box-shadow:0 2px 4px rgba(0,0,0,0.3); border-radius:3px;
				"></div>`;
			} else if (opts.shape === 'square') {
				el.innerHTML = `<div style="
					background:${color}; width:${size}px; height:${size}px;
					border:2px solid white; border-radius:2px;
					box-shadow:0 1px 3px rgba(0,0,0,0.4);
				"></div>`;
			} else {
				el.innerHTML = `<div style="
					background:${color}; width:${size}px; height:${size}px;
					border-radius:50%; border:2px solid white;
					box-shadow:0 1px 3px rgba(0,0,0,0.4);
				"></div>`;
			}
			el.style.cursor = 'pointer';

			const marker = new maplibregl.Marker({
				element: el,
				// MapLibre's option is `opacityWhenCovered` (string) — the opacity
				// used when the point is behind the globe/terrain. Default '0.2'.
				...(opts.occludedOpacity != null ? { opacityWhenCovered: String(opts.occludedOpacity) } : {}),
			})
				.setLngLat([opts.lon, opts.lat])
				.addTo(map);

			if (opts.tooltip) {
				const popup = new maplibregl.Popup({
					offset: 12, closeButton: false, closeOnClick: false,
					className: 'enki-tooltip',
				}).setHTML(`<div style="
					background:rgba(15,15,25,0.92); color:#e2e8f0; padding:4px 8px;
					border-radius:6px; font-size:11px; font-family:Inter,sans-serif;
					border:1px solid rgba(255,255,255,0.1); backdrop-filter:blur(8px);
					white-space:nowrap; pointer-events:none;
				">${opts.tooltip}</div>`);
				marker.setPopup(popup);
				el.addEventListener('mouseenter', () => marker.togglePopup());
				el.addEventListener('mouseleave', () => { if (marker.getPopup()?.isOpen()) marker.togglePopup(); });
			}

			if (opts.onClick) {
				el.addEventListener('click', (e) => { e.stopPropagation(); opts.onClick!(); });
			}

			features.add(marker);
			return marker;
		},

		addPolyline(opts: PolylineOptions): any {
			const id = nextId();
			const coordinates = opts.coords.map(([lat, lon]) => [lon, lat]);

			map.addSource(id, {
				type: 'geojson',
				data: {
					type: 'Feature',
					geometry: { type: 'LineString', coordinates },
					properties: {},
				},
			});

			map.addLayer({
				id,
				type: 'line',
				source: id,
				paint: {
					'line-color': opts.color || '#3b82f6',
					'line-width': opts.weight || 3,
					'line-opacity': opts.opacity || 0.8,
				},
			});

			const handle = { type: 'layer', id };
			features.add(handle);
			return handle;
		},

		addPolygon(opts: PolygonOptions): any {
			const id = nextId();
			const coordinates = [opts.coords.map(([lat, lon]) => [lon, lat])];
			// Close the ring if not already closed
			const ring = coordinates[0];
			if (ring.length > 0 && (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1])) {
				ring.push([...ring[0]]);
			}

			map.addSource(id, {
				type: 'geojson',
				data: {
					type: 'Feature',
					geometry: { type: 'Polygon', coordinates },
					properties: {},
				},
			});

			// Fill
			map.addLayer({
				id: `${id}_fill`,
				type: 'fill',
				source: id,
				paint: {
					'fill-color': opts.fillColor || opts.color || '#ef4444',
					'fill-opacity': opts.fillOpacity ?? 0.15,
				},
			});

			// Outline
			map.addLayer({
				id: `${id}_line`,
				type: 'line',
				source: id,
				paint: {
					'line-color': opts.color || '#ef4444',
					'line-width': opts.weight || 2,
				},
			});

			const handle = { type: 'polygon', id, layers: [`${id}_fill`, `${id}_line`] };
			features.add(handle);
			return handle;
		},

		remove(handle: any) {
			if (!handle) return;

			if (handle.remove) {
				// MapLibre Marker
				handle.remove();
			} else if (handle.type === 'layer') {
				try {
					if (map.getLayer(handle.id)) map.removeLayer(handle.id);
					if (map.getSource(handle.id)) map.removeSource(handle.id);
				} catch { /* map already destroyed */ }
			} else if (handle.type === 'polygon') {
				try {
					for (const layerId of handle.layers) {
						if (map.getLayer(layerId)) map.removeLayer(layerId);
					}
					if (map.getSource(handle.id)) map.removeSource(handle.id);
				} catch { /* map already destroyed */ }
			}
			features.delete(handle);
		},

		clearAll() {
			for (const f of features) {
				adapter.remove(f);
			}
			features.clear();
		},

		on(event: string, fn: (e: any) => void) {
			const mapEvent = event === 'moveend' ? 'moveend' : event;
			map.on(mapEvent, (e: any) => {
				if (event === 'contextmenu') {
					fn({ lat: e.lngLat.lat, lon: e.lngLat.lng, originalEvent: e.originalEvent });
				} else if (event === 'click') {
					fn({ lat: e.lngLat.lat, lon: e.lngLat.lng });
				} else {
					fn(e);
				}
			});
		},

		off(event: string, fn: (e: any) => void) {
			map.off(event, fn);
		},

		getRawMap() {
			return map;
		},
	};

	return adapter;
}
