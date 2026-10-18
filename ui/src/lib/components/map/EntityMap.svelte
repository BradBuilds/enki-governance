<script lang="ts">
	/**
	 * Entity-aware map component. Plots contacts, assets, topic entities,
	 * signals, and knowledge nodes with type-specific colors and sizes.
	 * Used by Watch Topic, Contact, Case, and Project detail pages.
	 */
	import { onMount, onDestroy } from 'svelte';

	interface MapEntity {
		id: string;
		latitude: number;
		longitude: number;
		title: string;
		type: string;         // person, organization, country, region, location, asset, signal, event
		subtype?: string;     // extra detail (e.g. 'vehicle', 'facility', 'military_action')
		mention_count?: number;
		color?: string;       // override color
		onClick?: () => void;
	}

	interface Props {
		entities?: MapEntity[];
		height?: string;
		center?: [number, number];
		zoom?: number;
		showLegend?: boolean;
		fitPadding?: number;
	}

	let { entities = [], height = '400px', center = [39.8283, -98.5795], zoom = 3, showLegend = true, fitPadding = 60 }: Props = $props();

	let mapContainer: HTMLElement;
	let map = $state<any>(null);
	let maplibre: any = null;
	let markers: any[] = [];

	const typeStyles: Record<string, { color: string; border: string; size: number; shape: 'circle' | 'diamond' | 'square' }> = {
		person:       { color: '#8b5cf6', border: '#6d28d9', size: 10, shape: 'circle' },
		organization: { color: '#3b82f6', border: '#1d4ed8', size: 12, shape: 'square' },
		country:      { color: '#10b981', border: '#059669', size: 18, shape: 'circle' },
		region:       { color: '#14b8a6', border: '#0d9488', size: 14, shape: 'circle' },
		location:     { color: '#06b6d4', border: '#0891b2', size: 10, shape: 'diamond' },
		asset:        { color: '#f97316', border: '#ea580c', size: 10, shape: 'circle' },
		signal:       { color: '#6366f1', border: '#4f46e5', size: 8, shape: 'circle' },
		event:        { color: '#ef4444', border: '#dc2626', size: 12, shape: 'diamond' },
		facility:     { color: '#06b6d4', border: '#0891b2', size: 12, shape: 'diamond' },
	};

	function markerEl(ent: MapEntity): HTMLElement {
		const style = typeStyles[ent.type] || typeStyles.signal;
		const color = ent.color || style.color;
		const border = style.border;
		const size = Math.min(28, style.size + Math.sqrt(ent.mention_count || 1));

		// Wrapper for pin + label
		const wrapper = document.createElement('div');
		wrapper.style.cssText = 'display:flex;flex-direction:column;align-items:center;cursor:pointer;';
		wrapper.title = `${ent.title} (${ent.type}${ent.mention_count ? ` · ${ent.mention_count} mentions` : ''})`;

		// Pin element
		const pin = document.createElement('div');
		if (style.shape === 'diamond') {
			pin.style.cssText = `width:${size}px;height:${size}px;transform:rotate(45deg);background:${color};border:2px solid ${border};opacity:0.9;box-shadow:0 0 6px ${color}40;`;
		} else if (style.shape === 'square') {
			pin.style.cssText = `width:${size}px;height:${size}px;border-radius:3px;background:${color};border:2px solid ${border};opacity:0.9;box-shadow:0 0 6px ${color}40;`;
		} else {
			pin.style.cssText = `width:${size}px;height:${size}px;border-radius:50%;background:${color};border:2px solid ${border};opacity:0.9;box-shadow:0 0 8px ${color}50;`;
		}
		wrapper.appendChild(pin);

		// Text label for countries, regions, and locations with high mention counts
		const showLabel = ent.type === 'country' || ent.type === 'region' || (ent.mention_count && ent.mention_count >= 10);
		if (showLabel) {
			const label = document.createElement('div');
			label.textContent = ent.title;
			label.style.cssText = `font-size:10px;font-weight:600;color:white;text-shadow:0 1px 3px rgba(0,0,0,0.8),0 0 6px rgba(0,0,0,0.5);margin-top:2px;white-space:nowrap;pointer-events:none;`;
			wrapper.appendChild(label);
		}

		if (ent.onClick) wrapper.addEventListener('click', ent.onClick);
		return wrapper;
	}

	function plotMarkers() {
		if (!map || !maplibre) return;

		// Remove old markers
		for (const m of markers) m.remove();
		markers = [];

		const points: [number, number][] = [];

		for (const ent of entities) {
			if (ent.latitude == null || ent.longitude == null) continue;
			points.push([ent.longitude, ent.latitude]);
			const el = markerEl(ent);
			const m = new maplibre.Marker({ element: el }).setLngLat([ent.longitude, ent.latitude]).addTo(map);
			markers.push(m);
		}

		if (points.length > 0) {
			const bounds = new maplibre.LngLatBounds();
			for (const p of points) bounds.extend(p);
			if (points.length === 1) {
				map.flyTo({ center: points[0], zoom: 5 });
			} else {
				map.fitBounds(bounds, { padding: fitPadding, maxZoom: 12 });
			}
		}
	}

	onMount(async () => {
		maplibre = await import('maplibre-gl');
		await import('maplibre-gl/dist/maplibre-gl.css');

		map = new maplibre.Map({
			container: mapContainer,
			style: {
				version: 8 as const,
				sources: {
					'satellite': {
						type: 'raster' as const,
						tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
						tileSize: 256,
						maxzoom: 18,
					},
					'labels': {
						type: 'raster' as const,
						tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}'],
						tileSize: 256,
						maxzoom: 18,
					},
					'borders': {
						type: 'raster' as const,
						tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Reference_Overlay/MapServer/tile/{z}/{y}/{x}'],
						tileSize: 256,
						maxzoom: 18,
					},
				},
				layers: [
					{
						id: 'satellite-tiles',
						type: 'raster' as const,
						source: 'satellite',
						minzoom: 0,
						maxzoom: 22,
					},
					{
						id: 'border-tiles',
						type: 'raster' as const,
						source: 'borders',
						minzoom: 0,
						maxzoom: 22,
						paint: { 'raster-opacity': 0.6 },
					},
					{
						id: 'label-tiles',
						type: 'raster' as const,
						source: 'labels',
						minzoom: 0,
						maxzoom: 22,
						paint: { 'raster-opacity': 0.9 },
					},
				],
			} as any,
			center: [center[1], center[0]],
			zoom,
			interactive: true,
			attributionControl: false,
		});

		map.on('load', () => plotMarkers());
	});

	$effect(() => {
		if (map && maplibre && entities.length > 0) {
			plotMarkers();
		}
	});

	onDestroy(() => {
		for (const m of markers) m.remove();
		if (map) { map.remove(); map = null; }
	});

	// Legend data
	let legendTypes = $derived.by(() => {
		const types = new Set(entities.map(e => e.type));
		return [...types].map(t => ({ type: t, style: typeStyles[t] || typeStyles.signal, count: entities.filter(e => e.type === t).length }));
	});
</script>

<div class="rounded-xl border border-border bg-card overflow-hidden">
	<div
		bind:this={mapContainer}
		style="height: {height}; width: 100%; touch-action: none;"
		class="bg-muted"
	></div>

	{#if showLegend && legendTypes.length > 0}
		<div class="flex items-center gap-3 px-3 py-2 border-t border-border bg-muted/20 flex-wrap">
			{#each legendTypes as lt}
				<div class="flex items-center gap-1.5 text-[10px] text-muted-foreground">
					<span class="inline-block w-2.5 h-2.5 rounded-full shrink-0" style:background={lt.style.color}></span>
					<span class="capitalize">{lt.type}</span>
					<span class="text-foreground">({lt.count})</span>
				</div>
			{/each}
			<span class="text-[10px] text-muted-foreground ml-auto">{entities.filter(e => e.latitude && e.longitude).length} mapped</span>
		</div>
	{/if}
</div>
