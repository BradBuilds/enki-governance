<script lang="ts">
	import { screen } from '$lib/stores/screen.svelte';
	import SlidersHorizontal from 'lucide-svelte/icons/sliders-horizontal';
	import X from 'lucide-svelte/icons/x';

	export interface FilterDef {
		key: string;
		label: string;
		type: 'select' | 'multiselect' | 'search' | 'daterange';
		options?: { value: string; label: string }[];
		placeholder?: string;
	}

	interface Props {
		filters: FilterDef[];
		values: Record<string, any>;
		onChange: (key: string, value: any) => void;
	}

	let { filters, values, onChange }: Props = $props();
	let mobileOpen = $state(false);

	function clearAll() {
		for (const f of filters) {
			onChange(f.key, f.type === 'multiselect' ? [] : '');
		}
	}

	const hasActiveFilters = $derived(
		filters.some(f => {
			const v = values[f.key];
			if (Array.isArray(v)) return v.length > 0;
			return v !== '' && v !== undefined && v !== null;
		})
	);
</script>

{#if screen.isMobile}
	<!-- Mobile: collapsed filter button -->
	<div class="flex items-center gap-2 mb-3">
		<button
			onclick={() => mobileOpen = true}
			class="flex items-center gap-2 px-3 py-2 rounded-lg border border-border bg-card text-sm text-foreground hover:bg-muted transition-colors"
		>
			<SlidersHorizontal size={16} />
			Filters
			{#if hasActiveFilters}
				<span class="w-2 h-2 rounded-full bg-primary"></span>
			{/if}
		</button>
		{#if hasActiveFilters}
			<button onclick={clearAll} class="text-xs text-muted-foreground hover:text-foreground">Clear all</button>
		{/if}
	</div>

	{#if mobileOpen}
		<!-- Mobile filter overlay -->
		<div class="fixed inset-0 z-50 bg-background flex flex-col">
			<div class="flex items-center justify-between px-4 py-3 border-b border-border">
				<h3 class="font-medium">Filters</h3>
				<button onclick={() => mobileOpen = false}>
					<X size={20} />
				</button>
			</div>
			<div class="flex-1 overflow-y-auto p-4 space-y-4">
				{#each filters as filter}
					<div>
						<label class="block text-sm font-medium text-muted-foreground mb-1">{filter.label}</label>
						{#if filter.type === 'select'}
							<select
								value={values[filter.key] ?? ''}
								onchange={(e) => onChange(filter.key, e.currentTarget.value)}
								class="w-full px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm"
							>
								<option value="">All</option>
								{#each filter.options ?? [] as opt}
									<option value={opt.value}>{opt.label}</option>
								{/each}
							</select>
						{:else if filter.type === 'search'}
							<input
								type="text"
								value={values[filter.key] ?? ''}
								oninput={(e) => onChange(filter.key, e.currentTarget.value)}
								placeholder={filter.placeholder ?? 'Search...'}
								class="w-full px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm"
							/>
						{:else if filter.type === 'multiselect'}
							<div class="flex flex-wrap gap-1.5">
								{#each filter.options ?? [] as opt}
									{@const selected = (values[filter.key] ?? []).includes(opt.value)}
									<button
										onclick={() => {
											const current: string[] = values[filter.key] ?? [];
											const next = selected
												? current.filter((v: string) => v !== opt.value)
												: [...current, opt.value];
											onChange(filter.key, next);
										}}
										class="px-2.5 py-1 rounded-full text-xs border transition-colors
											{selected ? 'bg-primary text-primary-foreground border-primary' : 'border-border text-muted-foreground hover:bg-muted'}"
									>
										{opt.label}
									</button>
								{/each}
							</div>
						{/if}
					</div>
				{/each}
			</div>
			<div class="px-4 py-3 border-t border-border flex gap-2">
				<button onclick={clearAll} class="flex-1 px-4 py-2 rounded-lg border border-border text-sm hover:bg-muted">Clear</button>
				<button onclick={() => mobileOpen = false} class="flex-1 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm">Apply</button>
			</div>
		</div>
	{/if}
{:else}
	<!-- Desktop/Tablet: horizontal filter bar -->
	<div class="flex items-center gap-3 flex-wrap mb-4">
		{#each filters as filter}
			{#if filter.type === 'select'}
				<select
					value={values[filter.key] ?? ''}
					onchange={(e) => onChange(filter.key, e.currentTarget.value)}
					class="px-3 py-1.5 rounded-lg border border-border bg-card text-foreground text-sm min-w-[140px]"
				>
					<option value="">{filter.label}: All</option>
					{#each filter.options ?? [] as opt}
						<option value={opt.value}>{opt.label}</option>
					{/each}
				</select>
			{:else if filter.type === 'search'}
				<input
					type="text"
					value={values[filter.key] ?? ''}
					oninput={(e) => onChange(filter.key, e.currentTarget.value)}
					placeholder={filter.placeholder ?? `${filter.label}...`}
					class="px-3 py-1.5 rounded-lg border border-border bg-card text-foreground text-sm min-w-[180px]"
				/>
			{:else if filter.type === 'multiselect'}
				<div class="flex items-center gap-1.5">
					<span class="text-xs text-muted-foreground">{filter.label}:</span>
					{#each (filter.options ?? []).slice(0, 6) as opt}
						{@const selected = (values[filter.key] ?? []).includes(opt.value)}
						<button
							onclick={() => {
								const current: string[] = values[filter.key] ?? [];
								const next = selected
									? current.filter((v: string) => v !== opt.value)
									: [...current, opt.value];
								onChange(filter.key, next);
							}}
							class="px-2 py-0.5 rounded-full text-xs border transition-colors
								{selected ? 'bg-primary text-primary-foreground border-primary' : 'border-border text-muted-foreground hover:bg-muted'}"
						>
							{opt.label}
						</button>
					{/each}
				</div>
			{/if}
		{/each}
		{#if hasActiveFilters}
			<button onclick={clearAll} class="text-xs text-muted-foreground hover:text-foreground underline">Clear all</button>
		{/if}
	</div>
{/if}
