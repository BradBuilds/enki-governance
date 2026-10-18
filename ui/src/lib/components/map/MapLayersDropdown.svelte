<script lang="ts">
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Route from 'lucide-svelte/icons/route';
	import Shield from 'lucide-svelte/icons/shield';
	import Compass from 'lucide-svelte/icons/compass';
	import Pencil from 'lucide-svelte/icons/pencil';
	import Plane from 'lucide-svelte/icons/plane';
	import Ship from 'lucide-svelte/icons/ship';
	import Radio from 'lucide-svelte/icons/radio';
	import Cloud from 'lucide-svelte/icons/cloud';
	import Video from 'lucide-svelte/icons/video';
	import Anchor from 'lucide-svelte/icons/anchor';
	import PlaneTakeoff from 'lucide-svelte/icons/plane-takeoff';
	import Landmark from 'lucide-svelte/icons/landmark';
	import Droplets from 'lucide-svelte/icons/droplets';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import X from 'lucide-svelte/icons/x';
	import Satellite from 'lucide-svelte/icons/satellite';
	import Flame from 'lucide-svelte/icons/flame';
	import Sun from 'lucide-svelte/icons/sun';
	import CloudRain from 'lucide-svelte/icons/cloud-rain';
	import Thermometer from 'lucide-svelte/icons/thermometer';
	import ScanEye from 'lucide-svelte/icons/scan-eye';
	import Newspaper from 'lucide-svelte/icons/newspaper';
	import Leaf from 'lucide-svelte/icons/leaf';
	import Building2 from 'lucide-svelte/icons/building-2';

	interface LayerState {
		signals: boolean;
		tracks: boolean;
		geofences: boolean;
		knowledge: boolean;
		annotations: boolean;
		countries: boolean;
		capitals: boolean;
		rivers: boolean;
		lakes: boolean;
		entity_assets: boolean;
	}

	interface LiveSourceState {
		enabled: boolean;
		count: number;
		status?: 'loading' | 'ok' | 'error';
		error?: string;
	}

	interface WeatherOverlayState {
		goes_ir: boolean;
		radar: boolean;
		clouds: boolean;
		temperature: boolean;
		daynight: boolean;
	}

	interface Props {
		layers: LayerState;
		onLayerToggle: (key: string) => void;
		signalCount: number;
		trackCount: number;
		geofenceCount: number;
		knowledgeCount: number;
		annotationCount: number;
		liveSources: Record<string, LiveSourceState>;
		onLiveToggle: (source: string) => void;
		weatherOverlays?: WeatherOverlayState;
		onWeatherOverlayToggle?: (key: string) => void;
		sentinelOverlays?: Record<string, boolean>;
		onSentinelOverlayToggle?: (key: string) => void;
		sentinelConfigured?: boolean;
		refLayerConfigs?: any[];
		refLayerEnabled?: Record<string, boolean>;
		onRefLayerToggle?: (layerId: string) => void;
		showHeatmap?: boolean;
		onHeatmapToggle?: () => void;
		hasContextEntities?: boolean;
		showNewsHeatmap?: boolean;
		onNewsHeatmapToggle?: () => void;
		newsHeatmapWindowHours?: number;
		onNewsHeatmapWindowChange?: (h: number) => void;
		// Filters
		signalTypes: string[];
		activeSignalTypes: Set<string>;
		onTypeToggle: (type: string) => void;
		severities: string[];
		activeSeverities: Set<string>;
		onSeverityToggle: (sev: string) => void;
		devices: string[];
		activeDevice: string;
		onDeviceChange: (device: string) => void;
		getDeviceName: (id: string) => string;
		onClearFilters: () => void;
		activeFilterCount: number;
	}

	let {
		layers, onLayerToggle,
		signalCount, trackCount, geofenceCount, knowledgeCount, annotationCount,
		liveSources, onLiveToggle,
		weatherOverlays = { goes_ir: false, radar: false, clouds: false, temperature: false, daynight: false },
		onWeatherOverlayToggle = () => {},
		sentinelOverlays = { true_color: false, false_color: false, urban: false, ndvi: false, swir: false, moisture: false },
		onSentinelOverlayToggle = () => {},
		sentinelConfigured = false,
		refLayerConfigs = [],
		refLayerEnabled = {},
		onRefLayerToggle = () => {},
		showHeatmap = false,
		onHeatmapToggle = () => {},
		hasContextEntities = false,
		showNewsHeatmap = false,
		onNewsHeatmapToggle = () => {},
		newsHeatmapWindowHours = 24,
		onNewsHeatmapWindowChange = () => {},
		signalTypes, activeSignalTypes, onTypeToggle,
		severities, activeSeverities, onSeverityToggle,
		devices, activeDevice, onDeviceChange, getDeviceName,
		onClearFilters, activeFilterCount,
	}: Props = $props();

	let open = $state(false);
	let section = $state<'layers' | 'filters'>('layers');
	// Start most categories collapsed so the dropdown isn't overwhelming
	let collapsed = $state<Record<string, boolean>>({
		tracking: false, infra: false, gov: true, emergency: true, transport: true, env: true, space: true,
	});
	function toggleSection(key: string) { collapsed = { ...collapsed, [key]: !collapsed[key] }; }

	// Group reference layers by category
	const refLayersByCategory = $derived.by(() => {
		const groups: Record<string, any[]> = {};
		for (const l of refLayerConfigs) {
			const cat = l.category || 'other';
			if (!groups[cat]) groups[cat] = [];
			groups[cat].push(l);
		}
		return groups;
	});

	const categoryLabels: Record<string, string> = {
		infrastructure: 'Infrastructure',
		government: 'Military & Government',
		emergency: 'Emergency Services',
		transportation: 'Transportation',
	};
	const categoryIcons: Record<string, any> = {
		infrastructure: Radio,
		government: Shield,
		emergency: Flame,
		transportation: PlaneTakeoff,
	};

	const domainColors: Record<string, string> = {
		geospatial: '#3b82f6', security: '#ef4444', communications: '#a855f7',
		personal: '#22c55e', financial: '#f59e0b', environmental: '#06b6d4',
		infrastructure: '#f97316', vehicle: '#6366f1', intelligence: '#ec4899',
	};

	const sevColors: Record<string, string> = {
		critical: 'var(--severity-critical)', high: 'var(--severity-high)',
		medium: 'var(--severity-medium)', low: 'var(--severity-low)',
		informational: 'var(--severity-info)',
	};


	const liveIcons: Record<string, any> = {
		adsb: Plane, ais: Ship, ports: Anchor, airports: PlaneTakeoff,
		cameras: Video, osm_cameras: Video, seismic: Radio, weather: Cloud,
		satellites: Satellite, fireballs: Flame, space_weather: Sun,
		gdelt: Newspaper,
	};
	const liveLabels: Record<string, string> = {
		adsb: 'Aircraft (ADS-B)', ais: 'Vessels (AIS)',
		ports: 'Ports (NGA)', airports: 'Airports (Global)',
		cameras: 'Traffic Cameras', osm_cameras: 'OSM Cameras (Global)',
		seismic: 'Earthquakes', weather: 'Weather Stations',
		satellites: 'Satellites (CelesTrak)', fireballs: 'Fireballs (NASA)',
		space_weather: 'Space Weather (DONKI)',
		gdelt: 'News mentions (GDELT)',
	};
</script>

<!-- Per-layer status: loading spinner, error flag, or the live count (incl. 0
     when a layer loaded but has nothing in view). Makes the map self-diagnosing. -->
{#snippet layerStatus(state: LiveSourceState, color: string)}
	{#if state.status === 'loading'}
		<span class="text-[10px] text-muted-foreground animate-pulse" title="loading…">•••</span>
	{:else if state.status === 'error'}
		<span class="text-[10px] text-red-400" title={state.error || 'failed to load'}>⚠</span>
	{:else if state.enabled && state.status === 'ok'}
		<span class="text-[10px] {state.count > 0 ? color : 'text-muted-foreground/50'}" title={state.count === 0 ? 'none in this view' : ''}>{state.count.toLocaleString()}</span>
	{:else if state.count > 0}
		<span class="text-[10px] {color}">{state.count.toLocaleString()}</span>
	{/if}
{/snippet}

<div class="relative">
	<button
		onclick={() => open = !open}
		class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-card/90 backdrop-blur-sm border border-border shadow-lg text-sm text-foreground hover:bg-card transition-colors"
	>
		Layers
		{#if activeFilterCount > 0}
			<span class="w-2 h-2 rounded-full bg-primary"></span>
		{/if}
		<ChevronDown size={14} class="text-muted-foreground" />
	</button>

	{#if open}
		<!-- Backdrop -->
		<button class="fixed inset-0 z-[999]" onclick={() => open = false} aria-label="Close"></button>

		<!-- Dropdown -->
		<div class="absolute right-0 top-full mt-1 z-[1000] w-72 max-h-[70vh] overflow-y-auto rounded-xl bg-card/95 backdrop-blur-sm border border-border shadow-2xl">
			<!-- Tabs -->
			<div class="flex border-b border-border sticky top-0 bg-card/95 backdrop-blur-sm z-10">
				{#each [['layers', 'Layers'], ['filters', 'Filters']] as [key, label]}
					<button
						onclick={() => section = key as typeof section}
						class="flex-1 px-2 py-2 text-xs font-medium transition-colors
							{section === key ? 'text-primary border-b-2 border-primary -mb-px' : 'text-muted-foreground hover:text-foreground'}"
					>
						{label}
						{#if key === 'filters' && activeFilterCount > 0}
							<span class="ml-1 text-[9px] bg-primary text-primary-foreground rounded-full px-1">{activeFilterCount}</span>
						{/if}
					</button>
				{/each}
			</div>

			<div class="p-3 space-y-1.5">
				{#if section === 'layers'}
					<!-- Intelligence layers -->
					<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium">Intelligence</p>
					{@const intLayers = [
						{ key: 'signals', icon: MapPin, label: 'Signals', count: signalCount, color: 'text-primary' },
						{ key: 'tracks', icon: Route, label: 'Tracks', count: trackCount, color: 'text-emerald-400' },
						{ key: 'geofences', icon: Shield, label: 'Geofences', count: geofenceCount, color: 'text-red-400' },
						{ key: 'knowledge', icon: Compass, label: 'Knowledge', count: knowledgeCount, color: 'text-emerald-400' },
						{ key: 'annotations', icon: Pencil, label: 'Annotations', count: annotationCount, color: 'text-primary' },
						{ key: 'entity_assets', icon: MapPin, label: 'Entity Assets', count: 0, color: 'text-amber-400' },
					]}
					{#each intLayers as l}
						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={layers[l.key as keyof LayerState]} onchange={() => onLayerToggle(l.key)} class="rounded border-border" />
							<svelte:component this={l.icon} size={13} class={l.color} />
							<span class="text-xs text-foreground flex-1">{l.label}</span>
							{#if l.count > 0}<span class="text-[10px] text-muted-foreground">{l.count}</span>{/if}
						</label>
					{/each}

					{#each Object.entries(liveSources).filter(([s]) => ['gdelt'].includes(s)) as [source, state]}
						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={state.enabled} onchange={() => onLiveToggle(source)} class="rounded border-border" />
							<Newspaper size={13} class="text-fuchsia-400" />
							<span class="text-xs text-foreground flex-1">{liveLabels[source] ?? source}</span>
							{@render layerStatus(state, 'text-fuchsia-400')}
						</label>
					{/each}

					<!-- Context heatmap toggle -->
					{#if hasContextEntities}
						<div class="pt-1 mt-1">
							<label class="flex items-center gap-2 cursor-pointer py-0.5">
								<input type="checkbox" checked={showHeatmap} onchange={onHeatmapToggle} class="rounded border-border" />
								<span class="w-3 h-3 rounded-sm" style="background:linear-gradient(135deg, #06b6d4 0%, #8b5cf6 40%, #f97316 70%, #ef4444 100%);"></span>
								<span class="text-xs text-foreground flex-1">Intensity Heatmap</span>
							</label>
						</div>
					{/if}

					<!-- News mention heatmap removed June 3 2026.
					     See ui/src/routes/(app)/map/+page.svelte for the
					     full reasoning — short version: it visualized
					     country-centroid mention density which looked
					     impressive but couldn't answer 'what's happening
					     at this location?' usefully. News goes on /feed. -->

					<!-- ─── Tracking ─── -->
					<div class="pt-2 mt-2 border-t border-border/50">
						<button onclick={() => toggleSection('tracking')} class="w-full flex items-center justify-between py-0.5">
							<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium">Tracking</p>
							<ChevronDown size={10} class="text-muted-foreground transition-transform {collapsed['tracking'] ? '-rotate-90' : ''}" />
						</button>
						{#if !collapsed['tracking']}
							{#each Object.entries(liveSources).filter(([s]) => ['adsb','ais'].includes(s)) as [source, state]}
								{@const Icon = liveIcons[source] ?? Radio}
								<label class="flex items-center gap-2 cursor-pointer py-0.5">
									<input type="checkbox" checked={state.enabled} onchange={() => onLiveToggle(source)} class="rounded border-border" />
									<svelte:component this={Icon} size={13} class="text-blue-400" />
									<span class="text-xs text-foreground flex-1">{liveLabels[source] ?? source}</span>
									{@render layerStatus(state, 'text-blue-400')}
								</label>
							{/each}
						{/if}
					</div>

					<!-- ─── Infrastructure ─── -->
					<div class="pt-2 mt-2 border-t border-border/50">
						<button onclick={() => toggleSection('infra')} class="w-full flex items-center justify-between py-0.5">
							<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium">Infrastructure</p>
							<ChevronDown size={10} class="text-muted-foreground transition-transform {collapsed['infra'] ? '-rotate-90' : ''}" />
						</button>
						{#if !collapsed['infra']}
							{#each Object.entries(liveSources).filter(([s]) => ['ports','airports','cameras','osm_cameras'].includes(s)) as [source, state]}
								{@const Icon = liveIcons[source] ?? Radio}
								<label class="flex items-center gap-2 cursor-pointer py-0.5">
									<input type="checkbox" checked={state.enabled} onchange={() => onLiveToggle(source)} class="rounded border-border" />
									<svelte:component this={Icon} size={13} class="text-teal-400" />
									<span class="text-xs text-foreground flex-1">{liveLabels[source] ?? source}</span>
									{@render layerStatus(state, 'text-teal-400')}
								</label>
							{/each}
							<!-- Generic reference layers in infrastructure category -->
							{#each (refLayersByCategory['infrastructure'] || []) as layer}
								<label class="flex items-center gap-2 cursor-pointer py-0.5">
									<input type="checkbox" checked={refLayerEnabled[layer.layer_id] || false} onchange={() => onRefLayerToggle(layer.layer_id)} class="rounded border-border" />
									<span class="w-2 h-2 rounded-full shrink-0" style="background:{layer.color}"></span>
									<span class="text-xs text-foreground flex-1">{layer.display_name}</span>
									{#if layer.item_count > 0}<span class="text-[10px] text-muted-foreground">{layer.item_count.toLocaleString()}</span>{/if}
								</label>
							{/each}
						{/if}
					</div>

					<!-- ─── Military & Government ─── -->
					{#if (refLayersByCategory['government'] || []).length > 0}
					<div class="pt-2 mt-2 border-t border-border/50">
						<button onclick={() => toggleSection('gov')} class="w-full flex items-center justify-between py-0.5">
							<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium">Military & Government</p>
							<ChevronDown size={10} class="text-muted-foreground transition-transform {collapsed['gov'] ? '-rotate-90' : ''}" />
						</button>
						{#if !collapsed['gov']}
							{#each (refLayersByCategory['government'] || []) as layer}
								<label class="flex items-center gap-2 cursor-pointer py-0.5">
									<input type="checkbox" checked={refLayerEnabled[layer.layer_id] || false} onchange={() => onRefLayerToggle(layer.layer_id)} class="rounded border-border" />
									<span class="w-2 h-2 rounded-full shrink-0" style="background:{layer.color}"></span>
									<span class="text-xs text-foreground flex-1">{layer.display_name}</span>
									{#if layer.item_count > 0}<span class="text-[10px] text-muted-foreground">{layer.item_count.toLocaleString()}</span>{/if}
								</label>
							{/each}
						{/if}
					</div>
					{/if}

					<!-- ─── Emergency Services ─── -->
					{#if (refLayersByCategory['emergency'] || []).length > 0}
					<div class="pt-2 mt-2 border-t border-border/50">
						<button onclick={() => toggleSection('emergency')} class="w-full flex items-center justify-between py-0.5">
							<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium">Emergency Services</p>
							<ChevronDown size={10} class="text-muted-foreground transition-transform {collapsed['emergency'] ? '-rotate-90' : ''}" />
						</button>
						{#if !collapsed['emergency']}
							{#each (refLayersByCategory['emergency'] || []) as layer}
								<label class="flex items-center gap-2 cursor-pointer py-0.5">
									<input type="checkbox" checked={refLayerEnabled[layer.layer_id] || false} onchange={() => onRefLayerToggle(layer.layer_id)} class="rounded border-border" />
									<span class="w-2 h-2 rounded-full shrink-0" style="background:{layer.color}"></span>
									<span class="text-xs text-foreground flex-1">{layer.display_name}</span>
									{#if layer.item_count > 0}<span class="text-[10px] text-muted-foreground">{layer.item_count.toLocaleString()}</span>{/if}
								</label>
							{/each}
						{/if}
					</div>
					{/if}

					<!-- ─── Transportation ─── -->
					{#if (refLayersByCategory['transportation'] || []).length > 0}
					<div class="pt-2 mt-2 border-t border-border/50">
						<button onclick={() => toggleSection('transport')} class="w-full flex items-center justify-between py-0.5">
							<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium">Transportation</p>
							<ChevronDown size={10} class="text-muted-foreground transition-transform {collapsed['transport'] ? '-rotate-90' : ''}" />
						</button>
						{#if !collapsed['transport']}
							{#each (refLayersByCategory['transportation'] || []) as layer}
								<label class="flex items-center gap-2 cursor-pointer py-0.5">
									<input type="checkbox" checked={refLayerEnabled[layer.layer_id] || false} onchange={() => onRefLayerToggle(layer.layer_id)} class="rounded border-border" />
									<span class="w-2 h-2 rounded-full shrink-0" style="background:{layer.color}"></span>
									<span class="text-xs text-foreground flex-1">{layer.display_name}</span>
									{#if layer.item_count > 0}<span class="text-[10px] text-muted-foreground">{layer.item_count.toLocaleString()}</span>{/if}
								</label>
							{/each}
						{/if}
					</div>
					{/if}

					<!-- ─── Environmental ─── -->
					<div class="pt-2 mt-2 border-t border-border/50">
						<button onclick={() => toggleSection('env')} class="w-full flex items-center justify-between py-0.5">
							<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium">Environmental</p>
							<ChevronDown size={10} class="text-muted-foreground transition-transform {collapsed['env'] ? '-rotate-90' : ''}" />
						</button>
						{#if !collapsed['env']}
							{#each Object.entries(liveSources).filter(([s]) => ['seismic','weather'].includes(s)) as [source, state]}
								{@const Icon = liveIcons[source] ?? Radio}
								<label class="flex items-center gap-2 cursor-pointer py-0.5">
									<input type="checkbox" checked={state.enabled} onchange={() => onLiveToggle(source)} class="rounded border-border" />
									<svelte:component this={Icon} size={13} class="text-cyan-400" />
									<span class="text-xs text-foreground flex-1">{liveLabels[source] ?? source}</span>
									{@render layerStatus(state, 'text-cyan-400')}
								</label>
							{/each}
						{/if}
					</div>

					<!-- ─── Space ─── -->
					<div class="pt-2 mt-2 border-t border-border/50">
						<button onclick={() => toggleSection('space')} class="w-full flex items-center justify-between py-0.5">
							<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium">Space & Astronomy</p>
							<ChevronDown size={10} class="text-muted-foreground transition-transform {collapsed['space'] ? '-rotate-90' : ''}" />
						</button>
						{#if !collapsed['space']}
							{#each Object.entries(liveSources).filter(([s]) => ['satellites','fireballs','space_weather'].includes(s)) as [source, state]}
								{@const Icon = liveIcons[source] ?? Radio}
								<label class="flex items-center gap-2 cursor-pointer py-0.5">
									<input type="checkbox" checked={state.enabled} onchange={() => onLiveToggle(source)} class="rounded border-border" />
									<svelte:component this={Icon} size={13} class="text-amber-400" />
									<span class="text-xs text-foreground flex-1">{liveLabels[source] ?? source}</span>
									{@render layerStatus(state, 'text-amber-400')}
								</label>
							{/each}
						{/if}
					</div>

					<!-- Reference -->
					<div class="pt-2 mt-2 border-t border-border/50">
						<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1">Reference</p>
						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={layers.countries} onchange={() => onLayerToggle('countries')} class="rounded border-border" />
							<Landmark size={12} class="text-muted-foreground" />
							<span class="text-xs text-foreground">Countries</span>
						</label>
						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={layers.capitals} onchange={() => onLayerToggle('capitals')} class="rounded border-border" />
							<MapPin size={12} class="text-muted-foreground" />
							<span class="text-xs text-foreground">Capitals</span>
						</label>
						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={layers.rivers} onchange={() => { onLayerToggle('rivers'); onLayerToggle('lakes'); }} class="rounded border-border" />
							<Droplets size={12} class="text-cyan-400" />
							<span class="text-xs text-foreground">Rivers & Lakes</span>
						</label>
					</div>

					<!-- Celestial & Weather Overlays -->
					<div class="pt-2 mt-2 border-t border-border/50">
						<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1">Celestial & Weather</p>

						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={weatherOverlays.daynight} onchange={() => onWeatherOverlayToggle('daynight')} class="rounded border-border" />
							<Sun size={13} class="text-yellow-400" />
							<span class="text-xs text-foreground flex-1">Day/Night + Sun & Moon</span>
						</label>
						{#if weatherOverlays.daynight}
						<div class="ml-6 mb-1 p-1.5 rounded bg-muted/50 text-[9px] text-muted-foreground space-y-0.5">
							<p>Live terminator line, sun & moon sub-points</p>
							<div class="flex items-center gap-1 flex-wrap">
								<span class="inline-block w-2 h-2 rounded-full" style="background:#fbbf24"></span><span>Sun</span>
								<span class="inline-block w-2 h-2 rounded-full" style="background:#c4c4cc"></span><span>Moon</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:rgba(0,0,17,0.35)"></span><span>Night</span>
							</div>
						</div>
						{/if}


						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={weatherOverlays.goes_ir} onchange={() => onWeatherOverlayToggle('goes_ir')} class="rounded border-border" />
							<ScanEye size={13} class="text-orange-400" />
							<span class="text-xs text-foreground flex-1">GOES Infrared (US)</span>
						</label>
						{#if weatherOverlays.goes_ir}
						<div class="ml-6 mb-1 p-1.5 rounded bg-muted/50 text-[9px] text-muted-foreground space-y-0.5">
							<p>Cloud-top temperature from GOES-East/West</p>
							<div class="flex items-center gap-1 flex-wrap">
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#222"></span><span>Clear</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#888"></span><span>Low</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#eee"></span><span>High</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#ff4444"></span><span>Storms</span>
							</div>
						</div>
						{/if}

						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={weatherOverlays.radar} onchange={() => onWeatherOverlayToggle('radar')} class="rounded border-border" />
							<CloudRain size={13} class="text-green-400" />
							<span class="text-xs text-foreground flex-1">NEXRAD Radar (US)</span>
						</label>
						{#if weatherOverlays.radar}
						<div class="ml-6 mb-1 p-1.5 rounded bg-muted/50 text-[9px] text-muted-foreground space-y-0.5">
							<p>Base reflectivity — precipitation intensity</p>
							<div class="flex items-center gap-1 flex-wrap">
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#00ee00"></span><span>Light</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#eeee00"></span><span>Mod</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#ff8800"></span><span>Heavy</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#ff0000"></span><span>Severe</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#ff00ff"></span><span>Extreme</span>
							</div>
						</div>
						{/if}

						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={weatherOverlays.clouds} onchange={() => onWeatherOverlayToggle('clouds')} class="rounded border-border" />
							<Cloud size={13} class="text-gray-400" />
							<span class="text-xs text-foreground flex-1">GOES Visible (Americas)</span>
						</label>
						{#if weatherOverlays.clouds}
						<div class="ml-6 mb-1 p-1.5 rounded bg-muted/50 text-[9px] text-muted-foreground space-y-0.5">
							<p>Visible light satellite — cloud formations, daytime only</p>
							<div class="flex items-center gap-1 flex-wrap">
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#111"></span><span>Clear</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#aaa"></span><span>Thin cloud</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#fff"></span><span>Dense cloud</span>
							</div>
						</div>
						{/if}

						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={weatherOverlays.temperature} onchange={() => onWeatherOverlayToggle('temperature')} class="rounded border-border" />
							<CloudRain size={13} class="text-blue-400" />
							<span class="text-xs text-foreground flex-1">Precipitation (Global)</span>
						</label>
						{#if weatherOverlays.temperature}
						<div class="ml-6 mb-1 p-1.5 rounded bg-muted/50 text-[9px] text-muted-foreground space-y-0.5">
							<p>Worldwide radar composite — rain, snow, mixed</p>
							<div class="flex items-center gap-1 flex-wrap">
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#0000ff"></span><span>Drizzle</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#00ccff"></span><span>Light</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#00ff00"></span><span>Mod</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#ffff00"></span><span>Heavy</span>
								<span class="inline-block w-2 h-2 rounded-sm" style="background:#ff0000"></span><span>Extreme</span>
							</div>
						</div>
						{/if}
					</div>

					<!-- Sentinel-2 Imagery (ESA / Copernicus) -->
					<div class="pt-2 mt-2 border-t border-border/50">
						<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1">Sentinel-2 Imagery (ESA)</p>

						{#if !sentinelConfigured}
						<a href="/settings/satellite" class="block ml-1 mb-1 p-1.5 rounded bg-muted/50 text-[10px] text-muted-foreground hover:text-foreground">
							Not connected — <span class="text-primary">add your free ESA/Copernicus key in Settings →</span>
						</a>
						{:else}
						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={sentinelOverlays.true_color} onchange={() => onSentinelOverlayToggle('true_color')} class="rounded border-border" />
							<Satellite size={13} class="text-sky-400" />
							<span class="text-xs text-foreground flex-1">True Color</span>
						</label>

						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={sentinelOverlays.false_color} onchange={() => onSentinelOverlayToggle('false_color')} class="rounded border-border" />
							<Leaf size={13} class="text-red-400" />
							<span class="text-xs text-foreground flex-1">False Color (vegetation)</span>
						</label>

						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={sentinelOverlays.urban} onchange={() => onSentinelOverlayToggle('urban')} class="rounded border-border" />
							<Building2 size={13} class="text-amber-400" />
							<span class="text-xs text-foreground flex-1">False Color (urban)</span>
						</label>

						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={sentinelOverlays.ndvi} onchange={() => onSentinelOverlayToggle('ndvi')} class="rounded border-border" />
							<Leaf size={13} class="text-green-400" />
							<span class="text-xs text-foreground flex-1">NDVI (vegetation health)</span>
						</label>

						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={sentinelOverlays.swir} onchange={() => onSentinelOverlayToggle('swir')} class="rounded border-border" />
							<Flame size={13} class="text-orange-400" />
							<span class="text-xs text-foreground flex-1">SWIR (fire / geology)</span>
						</label>

						<label class="flex items-center gap-2 cursor-pointer py-0.5">
							<input type="checkbox" checked={sentinelOverlays.moisture} onchange={() => onSentinelOverlayToggle('moisture')} class="rounded border-border" />
							<Droplets size={13} class="text-cyan-400" />
							<span class="text-xs text-foreground flex-1">Moisture Index</span>
						</label>

						{#if sentinelOverlays.true_color || sentinelOverlays.false_color || sentinelOverlays.urban || sentinelOverlays.ndvi || sentinelOverlays.swir || sentinelOverlays.moisture}
						<div class="ml-6 mt-1 mb-1 p-1.5 rounded bg-amber-500/10 border border-amber-500/20 text-[9px] text-amber-200/90">
							<p>🔍 <span class="font-medium">Zoom into a region.</span> High-res (~10 m) imagery only renders at regional/street zoom — the globe view stays blank by design. Last 90 days · ≤20% cloud · © Copernicus/ESA</p>
						</div>
						{/if}
						{/if}
					</div>

				{:else if section === 'filters'}
					<!-- Signal type -->
					<div class="flex items-center justify-between">
						<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium">Signal Type</p>
						{#if activeSignalTypes.size > 0}
							<button onclick={() => { for (const t of activeSignalTypes) onTypeToggle(t); }} class="text-[9px] text-primary">Clear</button>
						{/if}
					</div>
					{#if signalTypes.length > 0}
						<div class="space-y-0.5 max-h-28 overflow-y-auto">
							{#each signalTypes as type}
								{@const domain = type.split('.')[1] || ''}
								{@const label = type.split('.').pop()?.replace(/_/g, ' ') || type}
								<label class="flex items-center gap-2 cursor-pointer py-0.5">
									<input type="checkbox" checked={activeSignalTypes.has(type)} onchange={() => onTypeToggle(type)} class="rounded border-border" />
									<span class="w-2 h-2 rounded-full shrink-0" style="background: {domainColors[domain] || '#6b7280'}"></span>
									<span class="text-[10px] text-foreground truncate">{label}</span>
								</label>
							{/each}
						</div>
					{:else}
						<p class="text-[10px] text-muted-foreground">No signals loaded</p>
					{/if}

					<!-- Severity -->
					<div class="pt-2 mt-2 border-t border-border/50">
						<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1">Severity</p>
						<div class="flex flex-wrap gap-1">
							{#each severities as sev}
								<button
									onclick={() => onSeverityToggle(sev)}
									class="text-[10px] px-2 py-0.5 rounded-full border transition-colors
										{activeSeverities.has(sev) ? 'border-current opacity-100' : 'border-border text-muted-foreground opacity-60 hover:opacity-100'}"
									style={activeSeverities.has(sev) ? `color: ${sevColors[sev]}; border-color: ${sevColors[sev]}` : ''}
								>
									{sev}
								</button>
							{/each}
						</div>
					</div>

					<!-- Device / Track -->
					<div class="pt-2 mt-2 border-t border-border/50">
						<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1">Device Track</p>
						<select
							value={activeDevice}
							onchange={(e) => onDeviceChange(e.currentTarget.value)}
							class="w-full text-xs bg-muted border border-border rounded-lg px-2 py-1.5 text-foreground"
						>
							<option value="">All devices</option>
							{#each devices as dev}
								<option value={dev}>{getDeviceName(dev)}</option>
							{/each}
						</select>
					</div>

					{#if activeFilterCount > 0}
						<div class="pt-2 mt-2 border-t border-border/50 flex justify-between items-center">
							<span class="text-[10px] text-foreground">{signalCount} signals shown</span>
							<button onclick={onClearFilters} class="text-[9px] text-destructive hover:underline">Reset all</button>
						</div>
					{/if}

				{/if}
			</div>
		</div>
	{/if}
</div>
