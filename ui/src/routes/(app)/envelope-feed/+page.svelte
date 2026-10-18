<script lang="ts">
	/**
	 * Envelope Feed — all structured intelligence nodes produced by the
	 * envelope extraction pipeline (Phases 0-9). One row per node_id,
	 * rendered by EnvelopeCard, filterable by node_type / tier / score.
	 *
	 * Backend: /api/envelope-cards (list + stats + /{node_id} detail)
	 */

	import { onMount } from 'svelte';
	import EnvelopeCard from '$lib/components/feed/EnvelopeCard.svelte';
	import Boxes from 'lucide-svelte/icons/boxes';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import Filter from 'lucide-svelte/icons/filter';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import X from 'lucide-svelte/icons/x';
	import { loadBookmarks } from '$lib/stores/bookmarks.svelte';

	type Card = {
		node_id: string;
		node_type?: string;
		subtype?: string;
		title?: string;
		headline?: string;
		narrative?: string;
		outlet?: string;
		url?: string | null;
		published?: string | null;
		domain?: string;
		topics?: string[];
		entity_count?: number;
		completeness?: { score: number; tier: string; filled_slots: number; total_slots: number; gaps: string[] };
		updated_at?: string;
	};

	type Stats = {
		by_type: Array<{ node_type: string; subtype: string | null; count: number; avg_score: number }>;
		by_tier: Array<{ tier: string; count: number }>;
		total: number;
	};

	let cards = $state<Card[]>([]);
	let stats = $state<Stats | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let offset = $state(0);
	let limit = 50;
	let hasMore = $state(true);

	// Filters
	let filterNodeType = $state<string>('');
	let filterSubtype = $state<string>('');
	let filterTier = $state<string>('');
	let filterMinScore = $state<number>(0);

	const activeFilters = $derived(
		(filterNodeType ? 1 : 0) +
		(filterSubtype ? 1 : 0) +
		(filterTier ? 1 : 0) +
		(filterMinScore > 0 ? 1 : 0)
	);

	function buildQuery(nextOffset: number): string {
		const params = new URLSearchParams();
		params.set('limit', String(limit));
		params.set('offset', String(nextOffset));
		if (filterNodeType) params.set('node_type', filterNodeType);
		if (filterSubtype) params.set('subtype', filterSubtype);
		if (filterTier) params.set('tier', filterTier);
		if (filterMinScore > 0) params.set('min_score', String(filterMinScore));
		return params.toString();
	}

	async function loadStats() {
		try {
			const res = await fetch('/api/envelope-cards/stats');
			if (res.ok) stats = await res.json();
		} catch {}
	}

	async function loadCards(reset = false) {
		if (reset) {
			offset = 0;
			cards = [];
			hasMore = true;
		}
		loading = true;
		error = null;
		try {
			const res = await fetch(`/api/envelope-cards?${buildQuery(offset)}`);
			if (!res.ok) {
				const body = await res.text().catch(() => '');
				error = `API returned ${res.status}. ${body.slice(0, 200)}`;
				hasMore = false;
				return;
			}
			const data = await res.json();
			const batch: Card[] = data.cards || [];
			cards = reset ? batch : [...cards, ...batch];
			hasMore = batch.length >= limit;
			offset = cards.length;
		} catch (e: any) {
			error = e?.message || String(e);
			hasMore = false;
		} finally {
			loading = false;
		}
	}

	function clearFilters() {
		filterNodeType = '';
		filterSubtype = '';
		filterTier = '';
		filterMinScore = 0;
		loadCards(true);
	}

	// Unique node_types from stats for the filter dropdown
	const nodeTypeOptions = $derived.by(() => {
		const set = new Set<string>();
		for (const row of stats?.by_type || []) if (row.node_type) set.add(row.node_type);
		return Array.from(set).sort();
	});

	const subtypeOptions = $derived.by(() => {
		const set = new Set<string>();
		for (const row of stats?.by_type || []) {
			if (!filterNodeType || row.node_type === filterNodeType) {
				if (row.subtype) set.add(row.subtype);
			}
		}
		return Array.from(set).sort();
	});

	const tierOptions = ['stub', 'partial', 'solid', 'verified'];

	const tierColor: Record<string, string> = {
		stub: 'text-muted-foreground bg-muted/40 border-border',
		partial: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
		solid: 'text-blue-400 bg-blue-500/10 border-blue-500/20',
		verified: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
	};

	onMount(() => {
		loadStats();
		loadCards(true);
		loadBookmarks();
	});
</script>

<div class="flex flex-col h-full bg-background">
	<!-- Top bar -->
	<div class="flex items-center gap-2 px-4 py-2 border-b border-border bg-card/50 shrink-0 flex-wrap">
		<Boxes size={16} class="text-primary" />
		<h2 class="text-sm font-semibold text-foreground">Envelope Feed</h2>
		<span class="text-[10px] text-muted-foreground">
			Structured intelligence nodes from the extraction pipeline
		</span>

		{#if activeFilters > 0}
			<button onclick={clearFilters}
				class="text-[10px] px-2 py-0.5 rounded-full bg-primary/20 text-primary flex items-center gap-1 ml-2">
				<Filter class="w-2.5 h-2.5" /> {activeFilters} <X class="w-2.5 h-2.5" />
			</button>
		{/if}

		<div class="ml-auto flex items-center gap-2">
			{#if stats}
				<span class="text-[10px] text-muted-foreground">
					{stats.total.toLocaleString()} total nodes
				</span>
			{/if}
			<button onclick={() => { loadStats(); loadCards(true); }}
				class="p-1 text-muted-foreground hover:text-foreground"
				title="Refresh">
				<RefreshCw class="w-3.5 h-3.5 {loading ? 'animate-spin' : ''}" />
			</button>
		</div>
	</div>

	<!-- Filter bar -->
	<div class="flex items-center gap-2 px-4 py-2 border-b border-border/50 bg-card/30 shrink-0 flex-wrap">
		<!-- Node type -->
		<div class="relative">
			<select bind:value={filterNodeType} onchange={() => { filterSubtype = ''; loadCards(true); }}
				class="text-[11px] bg-card border border-border rounded px-2 py-1 pr-6 appearance-none cursor-pointer focus:outline-none focus:ring-1 focus:ring-primary">
				<option value="">All types</option>
				{#each nodeTypeOptions as nt}
					<option value={nt}>{nt}</option>
				{/each}
			</select>
			<ChevronDown class="w-3 h-3 absolute right-1.5 top-1.5 pointer-events-none text-muted-foreground" />
		</div>

		<!-- Subtype -->
		{#if subtypeOptions.length > 0}
			<div class="relative">
				<select bind:value={filterSubtype} onchange={() => loadCards(true)}
					class="text-[11px] bg-card border border-border rounded px-2 py-1 pr-6 appearance-none cursor-pointer focus:outline-none focus:ring-1 focus:ring-primary">
					<option value="">All subtypes</option>
					{#each subtypeOptions as st}
						<option value={st}>{st}</option>
					{/each}
				</select>
				<ChevronDown class="w-3 h-3 absolute right-1.5 top-1.5 pointer-events-none text-muted-foreground" />
			</div>
		{/if}

		<!-- Tier chips -->
		<div class="flex items-center gap-1">
			{#each tierOptions as tier}
				{@const tierStats = stats?.by_tier?.find((t) => t.tier === tier)}
				<button onclick={() => { filterTier = filterTier === tier ? '' : tier; loadCards(true); }}
					class="text-[10px] px-2 py-0.5 rounded border transition-colors
						{filterTier === tier ? 'ring-1 ring-primary ' + tierColor[tier] : tierColor[tier] + ' opacity-70 hover:opacity-100'}">
					{tier}{tierStats ? ` · ${tierStats.count}` : ''}
				</button>
			{/each}
		</div>

		<!-- Min score slider -->
		<div class="flex items-center gap-1.5 ml-2">
			<span class="text-[10px] text-muted-foreground">min score</span>
			<input type="range" min="0" max="1" step="0.1" bind:value={filterMinScore}
				onchange={() => loadCards(true)}
				class="w-20 accent-primary h-1" />
			<span class="text-[10px] text-muted-foreground w-8 font-mono">
				{Math.round(filterMinScore * 100)}%
			</span>
		</div>
	</div>

	<!-- Content -->
	<div class="flex-1 overflow-y-auto p-4">
		{#if error}
			<div class="max-w-3xl mx-auto rounded-lg border border-amber-500/30 bg-amber-500/5 p-4">
				<p class="text-sm text-amber-400 font-medium mb-1">Envelope pipeline not reachable</p>
				<p class="text-xs text-muted-foreground font-mono">{error}</p>
				<p class="text-xs text-muted-foreground mt-2">
					This is expected on a fresh install until the extraction workers have
					populated <code class="text-[11px]">signal_extractions</code>. It's live on production.
				</p>
			</div>
		{:else if cards.length === 0 && !loading}
			<div class="max-w-2xl mx-auto text-center text-muted-foreground py-12">
				<Boxes class="w-10 h-10 mx-auto mb-3 opacity-30" />
				<p class="text-sm">No envelope nodes match current filters</p>
				{#if activeFilters > 0}
					<button onclick={clearFilters} class="text-xs text-primary hover:underline mt-2">
						Clear filters
					</button>
				{/if}
			</div>
		{:else}
			<div class="max-w-3xl mx-auto space-y-0">
				{#each cards as card (card.node_id)}
					<EnvelopeCard {card} />
				{/each}

				{#if hasMore}
					<div class="text-center py-4">
						<button onclick={() => loadCards(false)}
							disabled={loading}
							class="text-xs px-4 py-2 rounded-md border border-border bg-card hover:bg-muted text-foreground disabled:opacity-60">
							{loading ? 'Loading…' : 'Load more'}
						</button>
					</div>
				{:else if cards.length > 0}
					<div class="text-center py-4 text-[10px] text-muted-foreground/50">
						End of feed · {cards.length} nodes loaded
					</div>
				{/if}
			</div>
		{/if}
	</div>
</div>
