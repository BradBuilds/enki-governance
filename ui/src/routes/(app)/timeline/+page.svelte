<script lang="ts">
	import { onMount } from 'svelte';
	import CalendarDays from 'lucide-svelte/icons/calendar-days';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import Library from 'lucide-svelte/icons/library';
	import FileText from 'lucide-svelte/icons/file-text';
	import GitBranch from 'lucide-svelte/icons/git-branch';
	import List from 'lucide-svelte/icons/list';

	// Master timeline event (GET /timeline/master) — cross-library, quality-gated,
	// topic-tagged. The topic timeline is a filtered lens on this same spine.
	interface TopicTag { library_id: number; title?: string | null; }
	interface MEvent {
		statement_id: number;
		date: string | null;
		predicate: string;
		text: string;
		document_id: number | null;
		document_title: string | null;
		subj_entity_id: number | null;
		subj_name: string | null;
		obj_entity_id: number | null;
		obj_name: string | null;
		topics: TopicTag[];
	}

	let events = $state<MEvent[]>([]);
	let libraries = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let expandedId = $state<number | null>(null);

	// Filters + view
	let libraryFilter = $state<number | null>(null);
	let daysFilter = $state<number>(36500); // default: all history
	let order = $state<'desc' | 'asc'>('desc');
	let view = $state<'spine' | 'tree'>('spine');

	// A stable colour per topic (library_id) — the "branches" of the master pole.
	const PALETTE = [
		'#60a5fa', '#f472b6', '#34d399', '#fbbf24', '#a78bfa',
		'#fb7185', '#22d3ee', '#c084fc', '#4ade80', '#f59e0b',
	];
	function topicColor(libId: number): string {
		return PALETTE[Math.abs(libId) % PALETTE.length];
	}
	function eventColor(ev: MEvent): string {
		return ev.topics[0] ? topicColor(ev.topics[0].library_id) : '#64748b';
	}

	function windowFrom(days: number): string | null {
		if (days >= 36500) return null;
		const d = new Date();
		d.setDate(d.getDate() - days);
		return d.toISOString().slice(0, 10);
	}

	async function load() {
		loading = true;
		error = null;
		try {
			const params = new URLSearchParams();
			if (libraryFilter !== null) params.set('library_id', String(libraryFilter));
			const from = windowFrom(daysFilter);
			if (from) params.set('from_date', from);
			params.set('order', order);
			params.set('limit', '400');
			const [evRes, libRes] = await Promise.all([
				fetch(`/api/timeline/master?${params.toString()}`),
				libraries.length === 0 ? fetch('/api/libraries') : Promise.resolve(null),
			]);
			if (evRes.ok) {
				events = (await evRes.json()).events || [];
			} else {
				error = `HTTP ${evRes.status}`;
			}
			if (libRes && libRes.ok) {
				libraries = (await libRes.json()).libraries || [];
			}
		} catch (e: any) {
			error = e?.message || 'fetch failed';
		} finally {
			loading = false;
		}
	}

	onMount(load);
	$effect(() => {
		void libraryFilter; void daysFilter; void order;
		load();
	});

	function fmtDate(iso: string | null): string {
		if (!iso) return '';
		try {
			return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
		} catch { return iso; }
	}
	function yearOf(iso: string | null): string {
		if (!iso) return 'undated';
		try { return String(new Date(iso).getFullYear()); } catch { return 'undated'; }
	}

	// Group into year sections, respecting sort order.
	const byYear = $derived.by(() => {
		const g = new Map<string, MEvent[]>();
		for (const e of events) {
			const y = yearOf(e.date);
			if (!g.has(y)) g.set(y, []);
			g.get(y)!.push(e);
		}
		const entries = Array.from(g.entries());
		entries.sort((a, b) => {
			if (a[0] === 'undated') return 1;
			if (b[0] === 'undated') return -1;
			return order === 'desc' ? Number(b[0]) - Number(a[0]) : Number(a[0]) - Number(b[0]);
		});
		return entries;
	});
</script>

<!-- Card body, shared by both the spine and the tree views. -->
{#snippet eventBody(ev: MEvent)}
	<div class="flex items-center gap-2 flex-wrap text-[10px] mb-1.5">
		{#if ev.date}<span class="font-mono text-muted-foreground">{fmtDate(ev.date)}</span>{/if}
		{#each ev.topics as t}
			<a href={`/library/${t.library_id}`}
				class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border hover:opacity-80"
				style={`color:${topicColor(t.library_id)};border-color:${topicColor(t.library_id)}33;background:${topicColor(t.library_id)}14`}>
				<Library size={9} /> {t.title || `Topic ${t.library_id}`}
			</a>
		{/each}
	</div>
	<div class="text-base text-foreground leading-snug">
		{#if ev.subj_entity_id}
			<a href={`/entity/${ev.subj_entity_id}`} class="font-semibold hover:text-primary">{ev.subj_name || '?'}</a>
		{:else}
			<span class="font-semibold">{ev.subj_name || '?'}</span>
		{/if}
		{#if ev.predicate}<span class="text-muted-foreground mx-1.5 text-sm">{ev.predicate}</span>{/if}
		{#if ev.obj_entity_id}
			<a href={`/entity/${ev.obj_entity_id}`} class="font-semibold hover:text-primary">{ev.obj_name || '?'}</a>
		{:else if ev.obj_name}
			<span class="font-semibold">{ev.obj_name}</span>
		{/if}
	</div>
	<div class="mt-2 pt-2 border-t border-border/40">
		<button onclick={() => expandedId = expandedId === ev.statement_id ? null : ev.statement_id}
			class="text-[10px] text-muted-foreground hover:text-foreground inline-flex items-center gap-1">
			{expandedId === ev.statement_id ? '▾' : '▸'} source
		</button>
		{#if expandedId === ev.statement_id}
			<div class="mt-1.5 text-[11px]">
				<p class="text-foreground/85 italic">"{(ev.text || '').slice(0, 300)}"</p>
				{#if ev.document_title}
					<a href={`/document/${ev.document_id}`} class="text-[10px] text-muted-foreground/80 hover:text-primary mt-0.5 inline-flex items-center gap-1">
						<FileText class="inline w-3 h-3" /> {ev.document_title}
					</a>
				{/if}
			</div>
		{/if}
	</div>
{/snippet}

<div class="max-w-5xl mx-auto space-y-4">
	<header class="flex items-baseline justify-between">
		<div>
			<h1 class="text-2xl font-bold text-foreground flex items-center gap-2">
				<CalendarDays size={22} class="text-primary" /> Master Timeline
			</h1>
			<p class="text-sm text-muted-foreground mt-1">
				Every dated, sourced event across all topics on one chronological spine.
				Filter to a topic for a focused lens.
			</p>
		</div>
		<div class="flex items-center gap-2">
			<!-- view toggle: spine (list) vs tree (branches off the pole) -->
			<div class="flex rounded-md border border-border overflow-hidden text-xs">
				<button onclick={() => view = 'spine'}
					class="px-2 py-1 inline-flex items-center gap-1 {view === 'spine' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}">
					<List size={12} /> Spine
				</button>
				<button onclick={() => view = 'tree'}
					class="px-2 py-1 inline-flex items-center gap-1 {view === 'tree' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'}">
					<GitBranch size={12} /> Tree
				</button>
			</div>
			<button onclick={load} class="p-1.5 rounded text-muted-foreground hover:text-primary hover:bg-muted" title="Refresh">
				<RefreshCw size={14} />
			</button>
		</div>
	</header>

	<!-- Topic filter (the branches) -->
	<div class="flex flex-wrap items-center gap-2 pb-3 border-b border-border">
		<span class="text-[10px] uppercase tracking-wider text-muted-foreground w-14 shrink-0">Topic</span>
		<button onclick={() => libraryFilter = null}
			class="px-2.5 py-0.5 text-xs rounded-full border {libraryFilter === null ? 'bg-primary text-primary-foreground border-primary' : 'bg-card text-foreground border-border hover:border-primary/40'}">
			All topics
		</button>
		{#each libraries as lib}
			{@const active = libraryFilter === lib.library_id}
			<button onclick={() => libraryFilter = active ? null : lib.library_id}
				class="px-2.5 py-0.5 text-xs rounded-full border flex items-center gap-1.5
					{active ? 'text-primary-foreground border-transparent' : 'bg-card text-foreground border-border hover:border-primary/40'}"
				style={active ? `background:${topicColor(lib.library_id)}` : ''}>
				<span class="w-2 h-2 rounded-full shrink-0" style={`background:${topicColor(lib.library_id)}`}></span>
				{lib.focal_term || lib.canonical_name}
			</button>
		{/each}
	</div>

	<!-- Window + order -->
	<div class="flex flex-wrap items-center gap-2">
		<span class="text-[10px] uppercase tracking-wider text-muted-foreground w-14 shrink-0">Window</span>
		{#each [{ v: 365, label: '1y' }, { v: 1825, label: '5y' }, { v: 7300, label: '20y' }, { v: 36500, label: 'All' }] as opt}
			<button onclick={() => daysFilter = opt.v}
				class="px-2.5 py-0.5 text-xs rounded-full border {daysFilter === opt.v ? 'bg-primary text-primary-foreground border-primary' : 'bg-card text-foreground border-border hover:border-primary/40'}">
				{opt.label}
			</button>
		{/each}
		<span class="ml-4 text-[10px] uppercase tracking-wider text-muted-foreground shrink-0">Order</span>
		<button onclick={() => order = order === 'desc' ? 'asc' : 'desc'}
			class="px-2.5 py-0.5 text-xs rounded-full border bg-card text-foreground border-border hover:border-primary/40">
			{order === 'desc' ? '↓ Newest first' : '↑ Oldest first'}
		</button>
	</div>

	<div class="text-xs text-muted-foreground">
		{#if loading}Loading…{:else}<strong class="text-foreground">{events.length}</strong> events{/if}
	</div>

	{#if error}
		<div class="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">⚠ {error}</div>
	{:else if !loading && events.length === 0}
		<div class="rounded-xl border border-dashed border-border p-8 text-center text-muted-foreground">
			<p class="text-sm">No events match these filters.</p>
		</div>

	{:else if view === 'spine'}
		<!-- SPINE: year-grouped left rail + cards -->
		<div class="relative">
			<div class="absolute left-[72px] top-0 bottom-0 w-px bg-border"></div>
			{#each byYear as [year, evts] (year)}
				<section class="mb-6">
					<div class="flex items-center gap-3 mb-2">
						<div class="w-[64px] text-right">
							<span class="text-2xl font-bold {year === 'undated' ? 'text-muted-foreground/60' : 'text-foreground'}">{year}</span>
						</div>
						<div class="w-3 h-3 rounded-full bg-primary shrink-0"></div>
						<span class="text-[10px] uppercase tracking-wider text-muted-foreground">{evts.length} event{evts.length === 1 ? '' : 's'}</span>
					</div>
					<div class="ml-[80px] space-y-3">
						{#each evts as ev (ev.statement_id)}
							<article class="rounded-lg border border-border bg-card p-3 hover:border-border/80 transition-colors"
								style={`border-left:3px solid ${eventColor(ev)}`}>
								{@render eventBody(ev)}
							</article>
						{/each}
					</div>
				</section>
			{/each}
		</div>

	{:else}
		<!-- TREE: branches alternating off a central time pole -->
		<div class="tree">
			<div class="tree-pole"></div>
			{#each byYear as [year, evts] (year)}
				<div class="year-marker">
					<span class="year-badge">{year}</span>
				</div>
				{#each evts as ev, i (ev.statement_id)}
					<div class="branch {i % 2 === 0 ? 'left' : 'right'}">
						<span class="node-dot" style={`background:${eventColor(ev)};box-shadow:0 0 0 3px ${eventColor(ev)}22`}></span>
						<span class="connector" style={`background:${eventColor(ev)}55`}></span>
						<article class="branch-card rounded-lg border border-border bg-card p-3"
							style={`border-left:3px solid ${eventColor(ev)}`}>
							{@render eventBody(ev)}
						</article>
					</div>
				{/each}
			{/each}
		</div>
	{/if}
</div>

<style>
	/* TREE view — a central "pole" (time) with event branches forking left/right. */
	.tree { position: relative; padding: 0.5rem 0; }
	.tree-pole {
		position: absolute; left: 50%; top: 0; bottom: 0; width: 2px;
		background: hsl(var(--border)); transform: translateX(-50%);
	}
	.year-marker { display: flex; justify-content: center; margin: 1.25rem 0 0.75rem; position: relative; z-index: 2; }
	.year-badge {
		background: hsl(var(--background)); border: 1px solid hsl(var(--border));
		color: hsl(var(--foreground)); font-weight: 700; font-size: 0.95rem;
		padding: 0.15rem 0.7rem; border-radius: 999px;
	}
	.branch { position: relative; display: flex; margin: 0.5rem 0; min-height: 2.5rem; }
	.branch.left { justify-content: flex-start; }
	.branch.right { justify-content: flex-end; }
	.branch-card { width: 46%; position: relative; z-index: 1; }
	/* the dot sits on the pole */
	.node-dot {
		position: absolute; left: 50%; top: 1rem; width: 11px; height: 11px;
		border-radius: 999px; transform: translateX(-50%); z-index: 2;
	}
	/* horizontal connector from the pole to the card */
	.connector { position: absolute; top: calc(1rem + 4px); height: 2px; width: 4%; z-index: 0; }
	.branch.left .connector { left: 46%; }
	.branch.right .connector { right: 46%; }
	@media (max-width: 640px) {
		.tree-pole, .node-dot, .connector { display: none; }
		.branch, .branch.left, .branch.right { justify-content: stretch; }
		.branch-card { width: 100%; }
		.year-marker { justify-content: flex-start; }
	}
</style>
