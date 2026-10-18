/**
 * Leaflet implementation of MapAdapter.
 * Wraps Leaflet API behind the generic interface.
 */

import type { MapAdapter, MapBounds, MarkerOptions, PolylineOptions, PolygonOptions } from './types';

export function createLeafletAdapter(map: any, L: any): MapAdapter {
	const features: Set<any> = new Set();

	function buildIcon(opts: MarkerOptions): any {
		if (opts.html) {
			return L.divIcon({
				html: opts.html,
				className: 'custom-marker',
				iconSize: [opts.size || 12, opts.size || 12],
				iconAnchor: [(opts.size || 12) / 2, (opts.size || 12) / 2],
			});
		}

		const size = opts.size || 12;
		const color = opts.color || '#3b82f6';

		if (opts.shape === 'diamond') {
			return L.divIcon({
				html: `<div style="
					background:${color}; width:${size}px; height:${size}px;
					transform:rotate(45deg); border:2px solid white;
					box-shadow:0 2px 4px rgba(0,0,0,0.3); border-radius:3px;
				"></div>`,
				className: 'custom-marker',
				iconSize: [size, size],
				iconAnchor: [size / 2, size / 2],
			});
		}

		// Default: circle
		return L.divIcon({
			html: `<div style="
				background:${color}; width:${size}px; height:${size}px;
				border-radius:50%; border:2px solid white;
				box-shadow:0 1px 3px rgba(0,0,0,0.4);
			"></div>`,
			className: 'custom-marker',
			iconSize: [size, size],
			iconAnchor: [size / 2, size / 2],
		});
	}

	const adapter: MapAdapter = {
		engine: 'leaflet',

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
			map.flyTo([lat, lon], zoom, { duration: 1 });
		},

		fitBounds(bounds: MapBounds, padding = 20) {
			map.fitBounds(
				[[bounds.south, bounds.west], [bounds.north, bounds.east]],
				{ padding: [padding, padding] }
			);
		},

		addMarker(opts: MarkerOptions): any {
			const icon = buildIcon(opts);
			const marker = L.marker([opts.lat, opts.lon], { icon });

			if (opts.tooltip) {
				marker.bindTooltip(opts.tooltip, { direction: 'top', offset: [0, -8] });
			}
			if (opts.onClick) {
				marker.on('click', opts.onClick);
			}

			marker.addTo(map);
			features.add(marker);
			return marker;
		},

		addPolyline(opts: PolylineOptions): any {
			const polyline = L.polyline(opts.coords, {
				color: opts.color || '#3b82f6',
				weight: opts.weight || 3,
				opacity: opts.opacity || 0.8,
				dashArray: opts.dashArray,
			});

			if (opts.tooltip) {
				polyline.bindTooltip(opts.tooltip, { sticky: true });
			}

			polyline.addTo(map);
			features.add(polyline);
			return polyline;
		},

		addPolygon(opts: PolygonOptions): any {
			const polygon = L.polygon(opts.coords, {
				color: opts.color || '#ef4444',
				fillColor: opts.fillColor || opts.color || '#ef4444',
				fillOpacity: opts.fillOpacity ?? 0.15,
				weight: opts.weight || 2,
				dashArray: opts.dashArray,
			});

			if (opts.popup) {
				polygon.bindPopup(opts.popup);
			}

			polygon.addTo(map);
			features.add(polygon);
			return polygon;
		},

		remove(handle: any) {
			if (handle) {
				map.removeLayer(handle);
				features.delete(handle);
			}
		},

		clearAll() {
			for (const f of features) {
				map.removeLayer(f);
			}
			features.clear();
		},

		on(event: string, fn: (e: any) => void) {
			map.on(event, (e: any) => {
				if (event === 'contextmenu') {
					fn({ lat: e.latlng.lat, lon: e.latlng.lng, originalEvent: e.originalEvent });
				} else if (event === 'click') {
					fn({ lat: e.latlng.lat, lon: e.latlng.lng });
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
