<script lang="ts">
	import { onMount } from 'svelte';
	import Badge from '$lib/components/shared/Badge.svelte';
	import LoadingSkeleton from '$lib/components/shared/LoadingSkeleton.svelte';
	import Check from 'lucide-svelte/icons/check';
	import X from 'lucide-svelte/icons/x';
	import Key from 'lucide-svelte/icons/key';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Globe from 'lucide-svelte/icons/globe';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Eye from 'lucide-svelte/icons/eye';
	import EyeOff from 'lucide-svelte/icons/eye-off';
	import Save from 'lucide-svelte/icons/save';

	let loading = $state(true);
	let sources = $state<any[]>([]);
	let editingKey = $state<string>('');
	let keyInput = $state('');
	let saving = $state('');
	let categoryFilter = $state('all');

	const categories = [
		{ value: 'all', label: 'All' },
		{ value: 'live_tracking', label: 'Live Tracking' },
		{ value: 'terrain', label: 'Terrain' },
		{ value: 'location', label: 'Location' },
		{ value: 'demographics', label: 'Demographics' },
		{ value: 'infrastructure', label: 'Infrastructure' },
		{ value: 'environmental', label: 'Environmental' },
		{ value: 'maritime', label: 'Maritime' },
		{ value: 'transportation', label: 'Transportation' },
		{ value: 'aviation', label: 'Aviation' },
		{ value: 'rf_comms', label: 'RF/Comms' },
		{ value: 'business', label: 'Business' },
		{ value: 'imagery', label: 'Imagery' },
	];

	const coverageVariant: Record<string, 'info' | 'success'> = {
		'Global': 'success',
		'US': 'info',
	};

	const filteredSources = $derived(
		categoryFilter === 'all'
			? sources
			: sources.filter(s => s.category === categoryFilter)
	);

	const stats = $derived({
		total: sources.length,
		enabled: sources.filter(s => s.enabled).length,
		needsKey: sources.filter(s => s.requires_key && !s.has_key).length,
	});

	onMount(async () => {
		try {
			const res = await fetch('/api/data-sources', { headers: { 'Content-Type': 'application/json' } });
			if (res.ok) {
				const data = await res.json();
				sources = data.sources || [];
			}
		} catch {}
		loading = false;
	});

	async function toggleSource(sourceId: string, enabled: boolean) {
		saving = sourceId;
		try {
			await fetch(`/api/data-sources/${sourceId}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ enabled }),
			});
			sources = sources.map(s => s.source_id === sourceId ? { ...s, enabled } : s);
		} catch {}
		saving = '';
	}

	async function saveKey(sourceId: string) {
		if (!keyInput.trim()) return;
		saving = sourceId;
		try {
			await fetch(`/api/data-sources/${sourceId}`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ api_key: keyInput.trim(), enabled: true }),
			});
			sources = sources.map(s => s.source_id === sourceId ? { ...s, has_key: true, enabled: true } : s);
		} catch {}
		editingKey = '';
		keyInput = '';
		saving = '';
	}
</script>

<div class="max-w-4xl mx-auto">
	<div class="flex items-center justify-between mb-6">
		<div>
			<h1 class="text-xl font-semibold text-foreground">Data Sources</h1>
			<p class="text-sm text-muted-foreground mt-0.5">
				{stats.enabled} of {stats.total} sources enabled
				{#if stats.needsKey > 0}
					· <span class="text-yellow-400">{stats.needsKey} need API keys</span>
				{/if}
			</p>
		</div>
	</div>

	<!-- Category filter -->
	<div class="flex gap-1.5 overflow-x-auto pb-2 mb-4">
		{#each categories as cat}
			<button
				onclick={() => categoryFilter = cat.value}
				class="px-3 py-1 rounded-full text-xs whitespace-nowrap transition-colors
					{categoryFilter === cat.value
						? 'bg-primary text-primary-foreground'
						: 'bg-muted text-muted-foreground hover:text-foreground'}"
			>
				{cat.label}
			</button>
		{/each}
	</div>

	{#if loading}
		<LoadingSkeleton type="card" lines={5} />
	{:else}
		<div class="space-y-3">
			{#each filteredSources as source}
				<div class="rounded-lg border border-border bg-card p-4 {source.requires_key && !source.has_key ? 'border-l-4 border-l-yellow-500/50' : source.enabled ? 'border-l-4 border-l-emerald-500/50' : ''}">
					<div class="flex items-start gap-3">
						<!-- Toggle -->
						<button
							onclick={() => {
								if (source.requires_key && !source.has_key && !source.enabled) {
									editingKey = source.source_id;
									keyInput = '';
								} else {
									toggleSource(source.source_id, !source.enabled);
								}
							}}
							class="mt-0.5 shrink-0"
							disabled={saving === source.source_id}
						>
							{#if source.enabled}
								<Eye size={18} class="text-emerald-400" />
							{:else}
								<EyeOff size={18} class="text-muted-foreground" />
							{/if}
						</button>

						<!-- Info -->
						<div class="flex-1 min-w-0">
							<div class="flex items-center gap-2 flex-wrap">
								<span class="text-sm font-medium text-foreground">{source.name}</span>
								<Badge text={source.coverage} variant={coverageVariant[source.coverage] || 'neutral'} size="sm" />
								{#if source.requires_key && source.has_key}
									<Badge text="Key set" variant="success" size="sm" />
								{:else if source.requires_key}
									<Badge text="Needs key" variant="medium" size="sm" />
								{/if}
							</div>
							<p class="text-xs text-muted-foreground mt-0.5">{source.description}</p>

							<!-- Key instructions + registration -->
							{#if source.requires_key && !source.has_key}
								<div class="mt-2 p-2 rounded-lg bg-yellow-500/5 border border-yellow-500/20">
									{#if source.key_instructions}
										<p class="text-xs text-foreground">{source.key_instructions}</p>
									{/if}
									{#if source.registration_url}
										<a
											href={source.registration_url}
											target="_blank"
											rel="noopener"
											class="inline-flex items-center gap-1 mt-1 text-xs text-primary hover:underline"
										>
											<ExternalLink size={11} /> Register for free API key
										</a>
									{/if}
								</div>
							{/if}

							<!-- Key input (when editing) -->
							{#if editingKey === source.source_id}
								<div class="flex items-center gap-2 mt-2">
									<Key size={14} class="text-muted-foreground shrink-0" />
									<input
										type="text"
										bind:value={keyInput}
										placeholder="Enter API key..."
										class="flex-1 px-2 py-1.5 rounded border border-border bg-card text-foreground text-xs font-mono"
									/>
									<button
										onclick={() => saveKey(source.source_id)}
										disabled={!keyInput.trim() || saving === source.source_id}
										class="px-3 py-1.5 rounded bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 disabled:opacity-40"
									>
										<Save size={12} class="inline mr-1" />Save
									</button>
									<button
										onclick={() => { editingKey = ''; keyInput = ''; }}
										class="px-2 py-1.5 rounded border border-border text-xs text-muted-foreground hover:bg-muted"
									>
										Cancel
									</button>
								</div>
							{:else if source.requires_key && source.has_key}
								<button
									onclick={() => { editingKey = source.source_id; keyInput = ''; }}
									class="mt-1 text-[10px] text-muted-foreground hover:text-foreground"
								>
									Update key
								</button>
							{/if}
						</div>
					</div>
				</div>
			{/each}
		</div>

		<!-- Summary -->
		<div class="mt-6 p-4 rounded-lg border border-border bg-muted/30">
			<h3 class="text-sm font-medium text-foreground mb-2">Registration Summary</h3>
			<div class="space-y-1.5">
				{#each sources.filter(s => s.requires_key && !s.has_key) as s}
					<div class="flex items-center gap-2 text-xs">
						<span class="w-2 h-2 rounded-full bg-yellow-500"></span>
						<span class="text-foreground flex-1">{s.name}</span>
						{#if s.registration_url}
							<a href={s.registration_url} target="_blank" rel="noopener" class="text-primary hover:underline flex items-center gap-1">
								Register <ExternalLink size={10} />
							</a>
						{:else}
							<span class="text-muted-foreground">No free API</span>
						{/if}
					</div>
				{/each}
				{#if sources.filter(s => s.requires_key && !s.has_key).length === 0}
					<p class="text-xs text-emerald-400">All sources configured!</p>
				{/if}
			</div>
		</div>
	{/if}
</div>
