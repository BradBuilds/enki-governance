<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import Newspaper from 'lucide-svelte/icons/newspaper';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import ChevronUp from 'lucide-svelte/icons/chevron-up';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Clock from 'lucide-svelte/icons/clock';

	type ClusterEntity = {
		entity_id: number;
		name: string;
		type: string;
		signal_count: number;
	};
	type Cluster = {
		cluster_id: number;
		headline: string;
		summary: string;
		canonical_source: string | null;
		canonical_link: string | null;
		thumbnail_url: string | null;
		first_seen: string | null;
		last_seen: string | null;
		article_count: number;
		source_count: number;
		entities: ClusterEntity[];
		entity_ids: number[];
		keywords: string[];
	};

	type ArticleInCluster = {
		signal_rid: number;
		timestamp: string | null;
		title: string;
		source: string | null;
		link: string | null;
		summary: string;
	};

	let clusters = $state<Cluster[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let hours = $state(24);
	let minSources = $state(1);
	let expandedClusterId = $state<number | null>(null);
	let articlesByCluster = $state<Record<number, ArticleInCluster[]>>({});
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	async function load() {
		loading = true;
		try {
			const r = await fetch(
				`/api/feed/clusters?hours=${hours}&limit=80&min_sources=${minSources}`,
			);
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const d = await r.json();
			clusters = d.clusters || [];
			error = null;
		} catch (e: any) {
			error = e.message;
		} finally {
			loading = false;
		}
	}

	async function expand(c: Cluster) {
		if (expandedClusterId === c.cluster_id) {
			expandedClusterId = null;
			return;
		}
		expandedClusterId = c.cluster_id;
		if (!articlesByCluster[c.cluster_id]) {
			try {
				const r = await fetch(`/api/feed/clusters/${c.cluster_id}`);
				if (r.ok) {
					const d = await r.json();
					articlesByCluster[c.cluster_id] = d.articles || [];
				}
			} catch {}
		}
	}

	function relTime(iso: string | null): string {
		if (!iso) return '';
		const ms = Date.now() - new Date(iso).getTime();
		const m = Math.floor(ms / 60000);
		if (m < 1) return 'just now';
		if (m < 60) return `${m}m ago`;
		const h = Math.floor(m / 60);
		if (h < 24) return `${h}h ago`;
		return `${Math.floor(h / 24)}d ago`;
	}

	onMount(() => {
		load();
		pollTimer = setInterval(load, 60000);
	});
	onDestroy(() => { if (pollTimer) clearInterval(pollTimer); });

	$effect(() => {
		void hours; void minSources;
		load();
	});

	const multiSource = $derived(clusters.filter(c => c.source_count >= 2));
	const singleSource = $derived(clusters.filter(c => c.source_count < 2));
</script>

<div class="max-w-5xl mx-auto space-y-4">
	<header class="flex items-baseline justify-between gap-4">
		<div>
			<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
				<Newspaper size={20} class="text-primary" /> Stories
			</h1>
			<p class="text-xs text-muted-foreground mt-1">
				News articles collapsed by topic. Each row = one story; numbers show how many sources cover it.
			</p>
		</div>
		<div class="flex items-center gap-3 text-xs">
			<label class="flex items-center gap-1">
				<span class="text-muted-foreground">window</span>
				<select bind:value={hours}
					class="bg-card border border-border rounded px-2 py-1 text-foreground">
					<option value={6}>6h</option>
					<option value={12}>12h</option>
					<option value={24}>24h</option>
					<option value={48}>48h</option>
					<option value={168}>7d</option>
				</select>
			</label>
			<label class="flex items-center gap-1">
				<span class="text-muted-foreground">min sources</span>
				<select bind:value={minSources}
					class="bg-card border border-border rounded px-2 py-1 text-foreground">
					<option value={1}>1+</option>
					<option value={2}>2+</option>
					<option value={3}>3+</option>
					<option value={5}>5+</option>
				</select>
			</label>
			<button onclick={load}
				class="text-muted-foreground hover:text-primary"
				disabled={loading}>
				<RefreshCw size={14} class={loading ? 'animate-spin' : ''} />
			</button>
		</div>
	</header>

	{#if loading && clusters.length === 0}
		<p class="text-sm text-muted-foreground">Loading clusters…</p>
	{:else if error}
		<p class="text-sm text-destructive">⚠ {error}</p>
	{:else if clusters.length === 0}
		<p class="text-sm text-muted-foreground italic">No stories in this window.</p>
	{:else}
		<!-- Multi-source stories first (the ones that pop) -->
		{#if multiSource.length > 0}
			<section class="space-y-1">
				<h2 class="text-xs uppercase tracking-wider text-muted-foreground">
					Multi-source stories
					<span class="ml-1 text-muted-foreground/60 normal-case">({multiSource.length})</span>
				</h2>
				<div class="rounded-xl border border-border bg-card divide-y divide-border/40">
					{#each multiSource as c (c.cluster_id)}
						<div class="group">
							<button onclick={() => expand(c)}
								class="w-full text-left px-4 py-3 hover:bg-card/60 transition-colors flex items-start gap-3"
								aria-expanded={expandedClusterId === c.cluster_id}>
								<!-- Frequency badge: visual weight by source count -->
								<div class="shrink-0 flex flex-col items-center w-12">
									<div class={`text-2xl font-bold tabular-nums leading-none
										${c.source_count >= 5 ? 'text-amber-400'
										: c.source_count >= 3 ? 'text-emerald-400'
										:                        'text-primary'}`}>
										{c.source_count}
									</div>
									<div class="text-[9px] uppercase tracking-wider text-muted-foreground mt-0.5">
										sources
									</div>
								</div>

								<!-- Headline + meta -->
								<div class="flex-1 min-w-0">
									<div class="text-sm font-medium text-foreground leading-snug">
										{c.headline}
									</div>
									{#if c.summary}
										<div class="text-xs text-muted-foreground line-clamp-2 mt-1">
											{c.summary}
										</div>
									{/if}
									<!-- Entity chips: clickable into the entity dossier -->
									{#if c.entities && c.entities.length > 0}
										<div class="flex flex-wrap items-center gap-1 mt-1.5">
											{#each c.entities.slice(0, 6) as e}
												<a href={`/entity/${e.entity_id}`}
													onclick={(ev) => ev.stopPropagation()}
													class="text-[10px] px-1.5 py-0.5 rounded
														bg-primary/10 text-primary/90 hover:bg-primary/20
														border border-primary/15 hover:border-primary/30
														inline-flex items-center gap-1">
													<span class="opacity-60">{e.type === 'person' ? '·' : e.type === 'organization' ? '◆' : '◯'}</span>
													{e.name}
												</a>
											{/each}
										</div>
									{/if}
									<div class="flex items-center gap-3 text-[11px] text-muted-foreground mt-1.5">
										<span class="flex items-center gap-1">
											<Clock size={10} /> {relTime(c.last_seen)}
										</span>
										<span>{c.article_count} article{c.article_count === 1 ? '' : 's'}</span>
										{#if c.canonical_source}
											<span class="text-foreground/70">{c.canonical_source}</span>
										{/if}
									</div>
								</div>

								<!-- Thumbnail -->
								{#if c.thumbnail_url}
									<img src={c.thumbnail_url} alt=""
										class="w-16 h-16 rounded object-cover border border-border bg-muted shrink-0"
										onerror={(e) => (e.target as HTMLImageElement).style.display = 'none'} />
								{/if}

								<svelte:component this={expandedClusterId === c.cluster_id ? ChevronUp : ChevronDown}
									size={14} class="text-muted-foreground/60 mt-1 shrink-0" />
							</button>

							<!-- Expanded: all sources covering this story -->
							{#if expandedClusterId === c.cluster_id}
								<div class="px-4 pb-3 pt-1 border-t border-border/40 bg-card/30">
									{#if articlesByCluster[c.cluster_id]}
										<ul class="space-y-1.5 text-xs">
											{#each articlesByCluster[c.cluster_id] as a}
												<li class="flex items-baseline gap-2 py-0.5 pl-12">
													<span class="text-muted-foreground/60 text-[10px] w-24 shrink-0 truncate">
														{a.source || '?'}
													</span>
													{#if a.link}
														<a href={a.link} target="_blank" rel="noopener"
															class="text-foreground/85 hover:text-primary flex-1 truncate">
															{a.title}
															<ExternalLink size={10} class="inline opacity-60 ml-0.5" />
														</a>
													{:else}
														<span class="text-foreground/85 flex-1 truncate">{a.title}</span>
													{/if}
													<span class="text-[10px] text-muted-foreground/60 shrink-0">
														{relTime(a.timestamp)}
													</span>
												</li>
											{/each}
										</ul>
									{:else}
										<p class="text-xs text-muted-foreground/60 italic pl-12">Loading sources…</p>
									{/if}
								</div>
							{/if}
						</div>
					{/each}
				</div>
			</section>
		{/if}

		<!-- Single-source stories (just one outlet reported, less notable) -->
		{#if singleSource.length > 0}
			<section class="space-y-1">
				<h2 class="text-xs uppercase tracking-wider text-muted-foreground">
					Single-source stories
					<span class="ml-1 text-muted-foreground/60 normal-case">({singleSource.length})</span>
				</h2>
				<div class="rounded-xl border border-border/60 bg-card/40 divide-y divide-border/30">
					{#each singleSource.slice(0, 30) as c (c.cluster_id)}
						<div class="px-4 py-2 flex items-baseline gap-3 text-xs hover:bg-card/60">
							<span class="text-muted-foreground/60 w-12 text-right tabular-nums shrink-0">
								{c.canonical_source ? c.canonical_source.split(' — ')[0].slice(0, 12) : '?'}
							</span>
							{#if c.canonical_link}
								<a href={c.canonical_link} target="_blank" rel="noopener"
									class="text-foreground/85 hover:text-primary flex-1 truncate">
									{c.headline}
								</a>
							{:else}
								<span class="text-foreground/85 flex-1 truncate">{c.headline}</span>
							{/if}
							<span class="text-[10px] text-muted-foreground/60 shrink-0">
								{relTime(c.last_seen)}
							</span>
						</div>
					{/each}
				</div>
			</section>
		{/if}
	{/if}
</div>
