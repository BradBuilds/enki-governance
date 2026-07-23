/**
 * MapAdapter — generic interface for map engines.
 * Both Leaflet and MapLibre implement this.
 * Layer components use this instead of engine-specific APIs.
 */

export interface MapBounds {
	west: number;
	south: number;
	east: number;
	north: number;
}

export interface MarkerOptions {
	lat: number;
	lon: number;
	color?: string;
	size?: number;
	shape?: 'circle' | 'diamond' | 'square';
	html?: string;
	tooltip?: string;
	onClick?: () => void;
	/** Opacity when the point is on the far side of the globe (0–1). Default
	 *  MapLibre behaviour fades ground markers out; set to 1 for things that
	 *  live above the surface (satellites) so they don't vanish at the limb. */
	occludedOpacity?: number;
}

export interface PolylineOptions {
	coords: [number, number][]; // [lat, lon][]
	color?: string;
	weight?: number;
	opacity?: number;
	dashArray?: string;
	tooltip?: string;
}

export interface PolygonOptions {
	coords: [number, number][]; // [lat, lon][]
	color?: string;
	fillColor?: string;
	fillOpacity?: number;
	weight?: number;
	dashArray?: string;
	popup?: string;
}

export interface MapAdapter {
	/** Get current visible bounds */
	getBounds(): MapBounds;

	/** Fly/animate to a location */
	flyTo(lat: number, lon: number, zoom?: number): void;

	/** Fit view to contain all points */
	fitBounds(bounds: MapBounds, padding?: number): void;

	/** Add a marker, returns a handle for removal */
	addMarker(opts: MarkerOptions): any;

	/** Add a polyline, returns a handle for removal */
	addPolyline(opts: PolylineOptions): any;

	/** Add a polygon, returns a handle for removal */
	addPolygon(opts: PolygonOptions): any;

	/** Remove a previously added feature by handle */
	remove(handle: any): void;

	/** Remove all features */
	clearAll(): void;

	/** Register event handler */
	on(event: 'moveend' | 'contextmenu' | 'click', fn: (e: any) => void): void;

	/** Unregister event handler */
	off(event: string, fn: (e: any) => void): void;

	/** Get the raw underlying engine instance (escape hatch) */
	getRawMap(): any;

	/** Engine type identifier */
	readonly engine: 'leaflet' | 'maplibre';
}
