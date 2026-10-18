<script lang="ts">
	import { resolvePath, formatValue, sparklinePath } from '$lib/utils/cardResolver';
	import { cardPreferences, getTypePrefs, togglePin, toggleHide, applyPreferences } from '$lib/stores/cardPreferences';
	import type { TrackableSeries } from '$lib/utils/cardResolver';
	import Pin from 'lucide-svelte/icons/pin';
	import EyeOff from 'lucide-svelte/icons/eye-off';

	interface Field {
		label: string;
		value?: string;
		path?: string;
		format?: string;
		highlight?: boolean;
		badge_color?: string;
	}

	let {
		fields = [],
		payload = {},
		trackableSeries = [],
		signalType = '',
	}: {
		fields: Field[];
		payload?: Record<string, any>;
		trackableSeries?: TrackableSeries[];
		signalType?: string;
	} = $props();

	const badgeColors: Record<string, string> = {
		green: 'bg-enki-green/10 text-enki-green',
		red: 'bg-enki-red/10 text-enki-red',
		orange: 'bg-enki-orange/10 text-enki-orange',
		blue: 'bg-primary/10 text-primary',
		yellow: 'bg-severity-medium/10 text-severity-medium',
		gray: 'bg-muted text-muted-foreground',
	};

	// Apply user preferences (pin/hide) to the field list
	const visibleFields = $derived.by(() => {
		let prefs = { pinned: [] as string[], hidden: [] as string[] };
		if (signalType) {
			// Read current prefs from store synchronously via get
			const unsub = cardPreferences.subscribe((p) => { prefs = getTypePrefs(p, signalType); });
			unsub();
		}
		return applyPreferences(fields, prefs);
	});

	function getDisplayValue(field: Field): string {
		if (field.path && payload) {
			const raw = resolvePath(payload, field.path);
			if (raw == null) return field.value || '';
			return formatValue(raw, field.format || 'text');
		}
		return field.value || '';
	}

	function getSparkline(field: Field): TrackableSeries | null {
		if (!field.path || !trackableSeries?.length) return null;
		return trackableSeries.find((s) => s.path === field.path) || null;
	}

	function isPinned(field: Field): boolean {
		if (!field.path || !signalType) return false;
		let result = false;
		const unsub = cardPreferences.subscribe((p) => {
			result = getTypePrefs(p, signalType).pinned.includes(field.path!);
		});
		unsub();
		return result;
	}
</script>

<div class="grid grid-cols-2 gap-x-4 gap-y-1.5">
	{#each visibleFields as field}
		{@const displayValue = getDisplayValue(field)}
		{@const spark = getSparkline(field)}
		{#if displayValue}
			<div class="flex flex-col group relative {field.format === 'large' ? 'col-span-2' : ''}">
				<div class="flex items-center gap-1">
					<span class="text-[10px] text-muted-foreground/60 uppercase tracking-wide">{field.label}</span>
					{#if isPinned(field)}
						<Pin class="w-2.5 h-2.5 text-primary/40" />
					{/if}

					<!-- Hover controls for pin/hide -->
					{#if field.path && signalType}
						<div class="hidden group-hover:flex items-center gap-0.5 ml-auto">
							<button
								onclick={(e) => { e.stopPropagation(); togglePin(signalType, field.path!); }}
								class="p-0.5 rounded hover:bg-primary/10 text-muted-foreground/40 hover:text-primary transition-colors"
								title="Pin to top"
							>
								<Pin class="w-2.5 h-2.5" />
							</button>
							<button
								onclick={(e) => { e.stopPropagation(); toggleHide(signalType, field.path!); }}
								class="p-0.5 rounded hover:bg-destructive/10 text-muted-foreground/40 hover:text-destructive transition-colors"
								title="Hide field"
							>
								<EyeOff class="w-2.5 h-2.5" />
							</button>
						</div>
					{/if}
				</div>
				<div class="flex items-center gap-2">
					{#if field.format === 'badge'}
						<span class="inline-flex w-fit px-2 py-0.5 rounded text-xs font-medium {badgeColors[field.badge_color || 'blue'] || badgeColors.blue}">
							{displayValue}
						</span>
					{:else if field.format === 'large'}
						<span class="text-lg font-bold text-foreground">{displayValue}</span>
					{:else if field.format === 'ip' || field.format === 'mac'}
						<span class="text-xs font-mono text-enki-cyan">{displayValue}</span>
					{:else if field.format === 'email'}
						<span class="text-xs text-primary">{displayValue}</span>
					{:else if field.format === 'url'}
						<a href={displayValue} target="_blank" rel="noopener" class="text-xs text-primary hover:underline truncate">{displayValue}</a>
					{:else if field.format === 'numeric' && field.highlight}
						<span class="text-sm font-bold text-foreground">{displayValue}</span>
					{:else}
						<span class="text-xs text-foreground">{displayValue}</span>
					{/if}

					<!-- Inline sparkline for trackable numeric fields -->
					{#if spark && spark.values.length >= 2}
						<svg viewBox="0 0 60 16" class="w-14 h-4 shrink-0 ml-auto">
							<path
								d={sparklinePath(spark.values, 60, 16)}
								fill="none"
								stroke="currentColor"
								stroke-width="1.5"
								class="text-primary/60"
							/>
						</svg>
					{/if}
				</div>
			</div>
		{/if}
	{/each}
</div>
