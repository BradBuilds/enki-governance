<script lang="ts">
	/**
	 * Map search — geocode place names via Nominatim (free OSM geocoder).
	 * Type a place name → results dropdown → click → fly to location.
	 * Also accepts raw coordinates: "37.2235, 35.2345"
	 */
	import type { MapAdapter } from './engine/types';
	import Search from 'lucide-svelte/icons/search';
	import MapPin from 'lucide-svelte/icons/map-pin';

	interface Props {
		adapter: MapAdapter;
	}

	let { adapter }: Props = $props();

	let query = $state('');
	let results = $state<any[]>([]);
	let searching = $state(false);
	let showResults = $state(false);
	let debounceTimer: any = null;

	function onInput() {
		clearTimeout(debounceTimer);
		if (query.trim().length < 2) {
			results = [];
			showResults = false;
			return;
		}

		// Check if it's raw coordinates
		const coordMatch = query.match(/^\s*(-?\d+\.?\d*)\s*[,\s]+\s*(-?\d+\.?\d*)\s*$/);
		if (coordMatch) {
			const lat = parseFloat(coordMatch[1]);
			const lon = parseFloat(coordMatch[2]);
			if (lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) {
				results = [{ display_name: `${lat.toFixed(4)}, ${lon.toFixed(4)}`, lat: String(lat), lon: String(lon), type: 'coordinates' }];
				showResults = true;
				return;
			}
		}

		debounceTimer = setTimeout(() => geocode(), 400);
	}

	async function geocode() {
		if (query.trim().length < 2) return;
		searching = true;

		try {
			const encoded = encodeURIComponent(query.trim());
			const res = await fetch(
				`https://nominatim.openstreetmap.org/search?format=json&q=${encoded}&limit=5&addressdetails=1`,
				{ headers: { 'User-Agent': 'Enki Governance (https://example.com)' } }
			);
			const data = await res.json();
			results = data || [];
			showResults = results.length > 0;
		} catch {
			results = [];
		}

		searching = false;
	}

	function selectResult(result: any) {
		const lat = parseFloat(result.lat);
		const lon = parseFloat(result.lon);

		if (adapter && !isNaN(lat) && !isNaN(lon)) {
			// Choose zoom based on result type
			let zoom = 14;
			const type = result.type || result.class;
			if (type === 'country' || type === 'continent') zoom = 5;
			else if (type === 'state' || type === 'region') zoom = 7;
			else if (type === 'city' || type === 'town') zoom = 12;
			else if (type === 'coordinates') zoom = 14;

			adapter.flyTo(lat, lon, zoom);
		}

		query = result.display_name?.split(',')[0] || query;
		showResults = false;
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			showResults = false;
			query = '';
		}
		if (e.key === 'Enter' && results.length > 0) {
			selectResult(results[0]);
		}
	}
</script>

<div class="relative">
	<div class="flex items-center bg-card/90 backdrop-blur-sm border border-border rounded-lg shadow-lg overflow-hidden">
		<Search class="w-3.5 h-3.5 text-muted-foreground ml-3 shrink-0" />
		<input
			bind:value={query}
			oninput={onInput}
			onkeydown={onKeydown}
			onfocus={() => { if (results.length > 0) showResults = true; }}
			onblur={() => setTimeout(() => showResults = false, 200)}
			placeholder="Search places or paste coordinates..."
			class="w-56 text-xs bg-transparent px-2 py-2 text-foreground placeholder:text-muted-foreground outline-none"
		/>
		{#if searching}
			<div class="w-3 h-3 border-2 border-primary/30 border-t-primary rounded-full animate-spin mr-3"></div>
		{/if}
	</div>

	{#if showResults && results.length > 0}
		<div class="absolute top-full mt-1 left-0 right-0 bg-card/95 backdrop-blur-sm border border-border rounded-lg shadow-xl overflow-hidden z-[1001]">
			{#each results as result}
				<button
					onmousedown={() => selectResult(result)}
					class="w-full flex items-start gap-2 px-3 py-2 text-left hover:bg-muted/50 transition-colors border-b border-border last:border-0"
				>
					<MapPin class="w-3 h-3 text-muted-foreground mt-0.5 shrink-0" />
					<div class="min-w-0">
						<p class="text-xs text-foreground truncate">{result.display_name?.split(',')[0]}</p>
						<p class="text-[10px] text-muted-foreground truncate">
							{result.type === 'coordinates'
								? 'Go to coordinates'
								: result.display_name?.split(',').slice(1).join(',').trim() || result.type}
						</p>
					</div>
				</button>
			{/each}
		</div>
	{/if}
</div>
