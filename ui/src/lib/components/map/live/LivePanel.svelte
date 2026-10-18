<script lang="ts">
	import { LIVE_SOURCES, getNestedValue, type LiveSourceConfig } from './sources';
	import Badge from '$lib/components/shared/Badge.svelte';
	import Plane from 'lucide-svelte/icons/plane';
	import Ship from 'lucide-svelte/icons/ship';
	import Radio from 'lucide-svelte/icons/radio';
	import Eye from 'lucide-svelte/icons/eye';
	import EyeOff from 'lucide-svelte/icons/eye-off';
	import Plus from 'lucide-svelte/icons/plus';
	import X from 'lucide-svelte/icons/x';
	import Navigation from 'lucide-svelte/icons/navigation';

	interface Props {
		enabledSources: Record<string, boolean>;
		sourceCounts: Record<string, number>;
		selectedItem: any | null;
		selectedSource: string;
		onToggleSource: (source: string) => void;
		onWatchItem: (source: string, item: any) => void;
		onAddToCase: (source: string, item: any) => void;
		onFlyTo: (lat: number, lon: number) => void;
		onClose: () => void;
		filterValues: Record<string, Record<string, any>>;
		onFilterChange: (source: string, key: string, value: any) => void;
	}

	let {
		enabledSources, sourceCounts, selectedItem, selectedSource,
		onToggleSource, onWatchItem, onAddToCase, onFlyTo, onClose,
		filterValues, onFilterChange,
	}: Props = $props();

	const sourceIcons: Record<string, any> = {
		adsb: Plane,
		ais: Ship,
		seismic: Radio,
		weather: Radio,
	};

	function getConfig(source: string): LiveSourceConfig {
		return LIVE_SOURCES[source];
	}
</script>

<div class="flex flex-col h-full">
	<!-- Header -->
	<div class="flex items-center justify-between px-3 py-2 border-b border-border">
		<h3 class="text-sm font-medium text-foreground">Live Feeds</h3>
		<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground">
			<X size={14} />
		</button>
	</div>

	<!-- Source toggles -->
	<div class="p-3 space-y-2 border-b border-border">
		{#each Object.entries(LIVE_SOURCES) as [key, config]}
			{@const enabled = enabledSources[key] ?? false}
			{@const count = sourceCounts[key] ?? 0}
			{@const Icon = sourceIcons[key] ?? Radio}
			<div>
				<button
					onclick={() => onToggleSource(key)}
					class="flex items-center gap-2 w-full px-2 py-1.5 rounded-lg text-left transition-colors
						{enabled ? 'bg-primary/10 text-foreground' : 'text-muted-foreground hover:bg-muted'}"
				>
					{#if enabled}
						<Eye size={14} class="text-primary shrink-0" />
					{:else}
						<EyeOff size={14} class="shrink-0" />
					{/if}
					<svelte:component this={Icon} size={14} class="shrink-0" />
					<span class="text-xs flex-1">{config.name}</span>
					{#if count > 0}
						<span class="text-[10px] text-muted-foreground bg-muted rounded-full px-1.5">{count}</span>
					{/if}
				</button>

				<!-- Source filters (when enabled) -->
				{#if enabled && config.filters.length > 0}
					<div class="ml-8 mt-1.5 space-y-1.5">
						{#each config.filters as filter}
							{#if filter.type === 'search'}
								<input
									type="text"
									placeholder={filter.label}
									value={filterValues[key]?.[filter.key] ?? ''}
									oninput={(e) => onFilterChange(key, filter.key, e.currentTarget.value)}
									class="w-full px-2 py-1 text-xs rounded border border-border bg-card text-foreground placeholder:text-muted-foreground/50"
								/>
							{:else if filter.type === 'range'}
								<div class="flex items-center gap-1.5 text-[10px] text-muted-foreground">
									<span>{filter.label}:</span>
									<input
										type="number"
										value={filterValues[key]?.[filter.key + '_min'] ?? filter.min ?? 0}
										oninput={(e) => onFilterChange(key, filter.key + '_min', e.currentTarget.valueAsNumber)}
										class="w-14 px-1 py-0.5 rounded border border-border bg-card text-foreground text-center"
									/>
									<span>—</span>
									<input
										type="number"
										value={filterValues[key]?.[filter.key + '_max'] ?? filter.max ?? 99999}
										oninput={(e) => onFilterChange(key, filter.key + '_max', e.currentTarget.valueAsNumber)}
										class="w-14 px-1 py-0.5 rounded border border-border bg-card text-foreground text-center"
									/>
									{#if filter.unit}<span>{filter.unit}</span>{/if}
								</div>
							{/if}
						{/each}
					</div>
				{/if}
			</div>
		{/each}
	</div>

	<!-- Selected item detail -->
	{#if selectedItem}
		{@const config = getConfig(selectedSource)}
		<div class="flex-1 overflow-y-auto p-3">
			<div class="rounded-lg border border-primary/30 bg-primary/5 p-3">
				<div class="flex items-center justify-between mb-2">
					<span class="text-sm font-medium text-foreground">
						{(selectedItem.label || selectedItem.key || '').trim() || 'Unknown'}
					</span>
					<span class="text-[10px] text-muted-foreground">{selectedSource.toUpperCase()}</span>
				</div>

				<!-- Detail fields -->
				{#if config?.detailFields}
					<div class="space-y-1 mb-3">
						{#each config.detailFields as field}
							{@const val = getNestedValue(selectedItem, field.key)}
							{#if val != null}
								<div class="flex justify-between text-xs">
									<span class="text-muted-foreground">{field.label}</span>
									<span class="text-foreground font-mono">
										{typeof val === 'number' ? Math.round(val * 100) / 100 : val}
										{#if field.unit}{field.unit}{/if}
									</span>
								</div>
							{/if}
						{/each}
					</div>
				{/if}

				<!-- Coordinates -->
				{#if selectedItem.latitude && selectedItem.longitude}
					<div class="flex justify-between text-xs mb-3">
						<span class="text-muted-foreground">Position</span>
						<button
							onclick={() => onFlyTo(selectedItem.latitude, selectedItem.longitude)}
							class="text-primary hover:underline font-mono flex items-center gap-1"
						>
							<Navigation size={10} />
							{selectedItem.latitude.toFixed(4)}, {selectedItem.longitude.toFixed(4)}
						</button>
					</div>
				{/if}

				<!-- Actions -->
				<div class="flex gap-2">
					{#if config?.observableTypes?.length}
						<button
							onclick={() => onAddToCase(selectedSource, selectedItem)}
							class="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 transition-colors"
						>
							<Plus size={12} /> Add to Case
						</button>
					{/if}
					<button
						onclick={() => onWatchItem(selectedSource, selectedItem)}
						class="flex-1 flex items-center justify-center gap-1 px-2 py-1.5 rounded-lg border border-border text-xs text-foreground hover:bg-muted transition-colors"
					>
						<Eye size={12} /> Watch This
					</button>
				</div>
			</div>
		</div>
	{:else}
		<div class="flex-1 flex items-center justify-center p-4">
			<p class="text-xs text-muted-foreground text-center">Click a live marker on the map to see details</p>
		</div>
	{/if}
</div>
