<script lang="ts">
	/**
	 * WatchDerivativesCards — four dashboard tiles backed by /watch-dashboard.
	 *
	 * Phase 4.8 of the collapse rebuild plan. Renders Trending / Hotspots /
	 * Hype / Narrative-push lists from node_metrics. Polls every 60s.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { getHeaders } from '$lib/api/mock';
	import TrendingUp from 'lucide-svelte/icons/trending-up';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Megaphone from 'lucide-svelte/icons/megaphone';
	import Radio from 'lucide-svelte/icons/radio';

	type Row = {
		node_id: string;
		title: string | null;
		node_type: string | null;
		subtype: string | null;
		base60_address: string | null;
		mention_count: number;
		mentions_per_hour: number;
		spike_factor: number;
		source_entropy: number;
		dominant_source: string | null;
		dominant_source_share: number;
		hype_score: number;
		hotspot_intensity: number | null;
	};

	let trending = $state<Row[]>([]);
	let hotspots = $state<Row[]>([]);
	let hype = $state<Row[]>([]);
	let narrative = $state<Row[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let pollT: ReturnType<typeof setInterval> | null = null;
	let lastFetched = $state<Date | null>(null);

	async function fetchOne(path: string): Promise<Row[]> {
		const r = await fetch(path, { headers: getHeaders() });
		if (!r.ok) return [];
		const data = await r.json();
		return Array.isArray(data) ? data : data.items || [];
	}

	async function loadAll() {
		try {
			const [t, h, hp, n] = await Promise.all([
				fetchOne('/api/watch-dashboard/trending?limit=8'),
				fetchOne('/api/watch-dashboard/hotspots?limit=8'),
				fetchOne('/api/watch-dashboard/hype?limit=8'),
				fetchOne('/api/watch-dashboard/narrative?limit=8')
			]);
			trending = t;
			hotspots = h;
			hype = hp;
			narrative = n;
			lastFetched = new Date();
			error = null;
		} catch (e: any) {
			error = e?.message || 'failed to load watch derivatives';
		}
		loading = false;
	}

	onMount(() => {
		loadAll();
		pollT = setInterval(loadAll, 60_000);
	});

	onDestroy(() => {
		if (pollT) clearInterval(pollT);
	});

	function fmt(n: number, digits = 2): string {
		return Number.isFinite(n) ? n.toFixed(digits) : '—';
	}

	const hasAny = $derived(
		trending.length || hotspots.length || hype.length || narrative.length
	);
</script>

{#if !loading && hasAny}
	<div>
		<div class="flex items-center justify-between mb-2">
			<h3 class="text-sm font-semibold text-foreground flex items-center gap-2">
				<TrendingUp class="w-4 h-4 text-amber-400" /> Watch
				<span class="text-[10px] text-muted-foreground">derivatives</span>
			</h3>
			{#if lastFetched}
				<span class="text-[10px] text-muted-foreground">
					updated {lastFetched.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
				</span>
			{/if}
		</div>

		<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
			<!-- Trending -->
			<div class="rounded-xl border border-border bg-card p-3">
				<div class="flex items-center gap-2 mb-2">
					<TrendingUp class="w-3.5 h-3.5 text-amber-400" />
					<h4 class="text-xs font-semibold text-foreground uppercase tracking-wider">
						Trending now
					</h4>
				</div>
				{#if trending.length === 0}
					<p class="text-xs text-muted-foreground py-2">No surges detected.</p>
				{:else}
					<ul class="space-y-1.5">
						{#each trending as row (row.node_id)}
							<li class="flex items-center gap-2 text-xs">
								<span class="text-amber-400 font-mono w-10 shrink-0">
									{fmt(row.spike_factor, 1)}×
								</span>
								<a href="/nodes/{row.node_id}"
									class="text-foreground hover:text-primary truncate flex-1">
									{row.title || row.node_id}
								</a>
								<span class="text-muted-foreground text-[10px]">
									{row.mention_count}
								</span>
							</li>
						{/each}
					</ul>
				{/if}
			</div>

			<!-- Hotspots -->
			<div class="rounded-xl border border-border bg-card p-3">
				<div class="flex items-center gap-2 mb-2">
					<MapPin class="w-3.5 h-3.5 text-rose-400" />
					<h4 class="text-xs font-semibold text-foreground uppercase tracking-wider">
						Hotspots
					</h4>
				</div>
				{#if hotspots.length === 0}
					<p class="text-xs text-muted-foreground py-2">No place concentrations.</p>
				{:else}
					<ul class="space-y-1.5">
						{#each hotspots as row (row.node_id)}
							<li class="flex items-center gap-2 text-xs">
								<span class="text-rose-400 font-mono w-10 shrink-0">
									{fmt(row.hotspot_intensity ?? 0, 1)}
								</span>
								<a href="/nodes/{row.node_id}"
									class="text-foreground hover:text-primary truncate flex-1">
									{row.title || row.node_id}
								</a>
								<span class="text-muted-foreground text-[10px]">
									{row.mention_count}
								</span>
							</li>
						{/each}
					</ul>
				{/if}
			</div>

			<!-- Hype -->
			<div class="rounded-xl border border-border bg-card p-3">
				<div class="flex items-center gap-2 mb-2">
					<Megaphone class="w-3.5 h-3.5 text-fuchsia-400" />
					<h4 class="text-xs font-semibold text-foreground uppercase tracking-wider">
						Hype watch
					</h4>
				</div>
				{#if hype.length === 0}
					<p class="text-xs text-muted-foreground py-2">No hype signals.</p>
				{:else}
					<ul class="space-y-1.5">
						{#each hype as row (row.node_id)}
							<li class="flex items-center gap-2 text-xs">
								<span class="text-fuchsia-400 font-mono w-10 shrink-0">
									{fmt(row.hype_score)}
								</span>
								<a href="/nodes/{row.node_id}"
									class="text-foreground hover:text-primary truncate flex-1">
									{row.title || row.node_id}
								</a>
								<span class="text-muted-foreground text-[10px]">
									{Math.round(row.dominant_source_share * 100)}%
								</span>
							</li>
						{/each}
					</ul>
				{/if}
			</div>

			<!-- Narrative push -->
			<div class="rounded-xl border border-border bg-card p-3">
				<div class="flex items-center gap-2 mb-2">
					<Radio class="w-3.5 h-3.5 text-orange-400" />
					<h4 class="text-xs font-semibold text-foreground uppercase tracking-wider">
						Narrative push
					</h4>
				</div>
				{#if narrative.length === 0}
					<p class="text-xs text-muted-foreground py-2">No single-source dominance.</p>
				{:else}
					<ul class="space-y-1.5">
						{#each narrative as row (row.node_id)}
							<li class="flex items-center gap-2 text-xs">
								<span class="text-orange-400 font-mono w-10 shrink-0">
									{Math.round(row.dominant_source_share * 100)}%
								</span>
								<a href="/nodes/{row.node_id}"
									class="text-foreground hover:text-primary truncate flex-1">
									{row.title || row.node_id}
								</a>
								<span class="text-muted-foreground text-[10px] truncate max-w-[80px]">
									{row.dominant_source || '—'}
								</span>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		</div>

		{#if error}
			<p class="text-[10px] text-muted-foreground mt-1">{error}</p>
		{/if}
	</div>
{/if}
