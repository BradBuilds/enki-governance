<script lang="ts">
	/**
	 * Basemap tile layer switcher.
	 * Shows thumbnail buttons for dark, satellite, terrain, street tiles.
	 * Stores preference in localStorage.
	 */
	import Map from 'lucide-svelte/icons/map';

	interface Props {
		map: any;
	}

	let { map }: Props = $props();

	let currentBasemap = $state(localStorage.getItem('enki-basemap') || 'satellite');
	let currentLayer: any = null;
	let open = $state(false);
	let L: any = null;

	const basemaps: Record<string, { label: string; url: string; maxZoom: number; attr: string }> = {
		dark: {
			label: 'Dark',
			url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
			maxZoom: 19,
			attr: 'CARTO',
		},
		satellite: {
			label: 'Satellite',
			url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
			maxZoom: 18,
			attr: 'Esri',
		},
		terrain: {
			label: 'Terrain',
			url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
			maxZoom: 17,
			attr: 'OpenTopoMap',
		},
		street: {
			label: 'Street',
			url: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
			maxZoom: 19,
			attr: 'CARTO',
		},
	};

	async function switchBasemap(key: string) {
		if (!map || key === currentBasemap) { open = false; return; }

		if (!L) L = await import('leaflet');

		if (currentLayer) {
			map.removeLayer(currentLayer);
		}

		const bm = basemaps[key];
		const opts: any = { maxZoom: bm.maxZoom };
		if (bm.url.includes('{s}')) opts.subdomains = 'abcd';
		currentLayer = L.default.tileLayer(bm.url, opts).addTo(map);

		currentBasemap = key;
		localStorage.setItem('enki-basemap', key);
		open = false;
	}

	// Apply saved basemap on mount (MapShell already loads the saved basemap,
	// but we need to track the layer reference for future switches)
	$effect(() => {
		if (map && currentBasemap !== 'satellite' && currentBasemap !== 'dark') {
			switchBasemap(currentBasemap);
		}
	});
</script>

<div class="relative">
	<button
		onclick={() => open = !open}
		class="flex items-center gap-1.5 bg-card/90 backdrop-blur-sm border border-border rounded-lg px-3 py-2 shadow-lg text-[10px] text-muted-foreground hover:text-foreground transition-colors"
	>
		<Map class="w-3 h-3" />
		<span>{basemaps[currentBasemap]?.label || 'Dark'}</span>
	</button>

	{#if open}
		<div class="absolute bottom-full mb-2 right-0 bg-card/90 backdrop-blur-sm border border-border rounded-lg shadow-lg p-2 flex gap-1">
			{#each Object.entries(basemaps) as [key, bm]}
				<button
					onclick={() => switchBasemap(key)}
					class="flex flex-col items-center gap-1 px-2 py-1.5 rounded transition-colors
						{currentBasemap === key ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
				>
					<div class="w-10 h-10 rounded bg-muted border border-border flex items-center justify-center text-[8px] font-mono">
						{bm.label.slice(0, 3)}
					</div>
					<span class="text-[9px]">{bm.label}</span>
				</button>
			{/each}
		</div>
	{/if}
</div>
