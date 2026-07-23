<script lang="ts">
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Navigation from 'lucide-svelte/icons/navigation';
	import { goto } from '$app/navigation';

	interface Props {
		action: string;
		data: {
			lat?: number;
			lon?: number;
			zoom?: number;
			label?: string;
			radius_km?: number;
			name?: string;
			signals?: any[];
			count?: number;
			west?: number;
			south?: number;
			east?: number;
			north?: number;
		};
	}

	let { action, data }: Props = $props();

	const labels: Record<string, string> = {
		navigate_to: 'Map Location',
		search_nearby: 'Nearby Search',
		create_geofence: 'Geofence Created',
		highlight_area: 'Area Highlighted',
		analyze_device: 'Movement Analysis',
		enrich_location: 'Location Enriched',
		nearby_signals: 'Nearby Signals',
	};

	const title = $derived(labels[action] || 'Map Action');

	const description = $derived.by(() => {
		if (data.label) return data.label;
		if (data.name) return data.name;
		if (data.lat && data.lon) return `${data.lat.toFixed(4)}, ${data.lon.toFixed(4)}`;
		return 'View on map';
	});

	function openMap() {
		const params = new URLSearchParams();
		if (data.lat) params.set('lat', String(data.lat));
		if (data.lon) params.set('lon', String(data.lon));
		if (data.zoom) params.set('z', String(data.zoom));
		goto(`/map?${params}`);
	}
</script>

<div class="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3">
	<div class="flex items-start gap-2">
		<MapPin size={16} class="text-blue-400 mt-0.5 shrink-0" />
		<div class="flex-1 min-w-0">
			<p class="text-xs font-medium text-blue-400">{title}</p>
			<p class="text-sm text-foreground mt-0.5 truncate">{description}</p>
			{#if data.radius_km}
				<p class="text-xs text-muted-foreground mt-1">Radius: {data.radius_km} km</p>
			{/if}
			{#if data.count !== undefined}
				<p class="text-xs text-muted-foreground mt-1">{data.count} results found</p>
			{/if}
			{#if data.signals && data.signals.length > 0}
				<p class="text-xs text-muted-foreground mt-1">{data.signals.length} signals nearby</p>
			{/if}
		</div>
		<button
			onclick={openMap}
			class="shrink-0 flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-md bg-blue-500/10 text-blue-400 hover:bg-blue-500/20 transition-colors"
		>
			<Navigation size={12} />
			Go to Map
		</button>
	</div>
</div>
