<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import Activity from 'lucide-svelte/icons/activity';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';

	let depth = $state<any>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let pollTimer: any = null;
	let lastFetchedAt = $state<Date | null>(null);

	async function load() {
		try {
			const res = await fetch('/api/system/depth');
			if (res.ok) {
				depth = await res.json();
				lastFetchedAt = new Date();
			} else error = 'depth endpoint not reachable';
		} catch (e: any) {
			error = e?.message || 'fetch failed';
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		load();
		// Poll every 30s; the continuous loop on node-A moves data through
		// the pipeline in real time.
		pollTimer = setInterval(load, 30_000);
	});

	onDestroy(() => { if (pollTimer) clearInterval(pollTimer); });

	// Stage definitions for the visualizer.
	const stages = $derived.by(() => {
		if (!depth) return [];
		const r = depth.references, d = depth.documents,
		      k = depth.knowledge, q = depth.live_queue;
		return [
			{ key: 'refs',     label: 'References',  done: r.ingested + r.acquired, queue: r.pending,        kind: 'catalogue' },
			{ key: 'ingest',   label: 'Documents',   done: d.with_mentions,         queue: q.collapse_eligible, kind: 'ingest' },
			{ key: 'resolve',  label: 'Entity link', done: k.mentions_resolved,     queue: q.resolution_eligible, kind: 'resolve' },
			{ key: 'promote',  label: 'Statements',  done: k.statements_promoted,   queue: q.promotion_eligible, kind: 'promote' },
			{ key: 'collapse', label: 'Assessments', done: k.assessments_total,     queue: 0,                  kind: 'collapse' },
		];
	});
</script>

<div class="rounded-xl border border-border bg-card p-5">
	<div class="flex items-center justify-between mb-4">
		<div class="flex items-center gap-2">
			<Activity size={16} class="text-primary" />
			<h2 class="text-sm font-semibold text-foreground">Pipeline</h2>
			{#if lastFetchedAt}
				<span class="text-[10px] text-muted-foreground">refreshed {lastFetchedAt.toLocaleTimeString()}</span>
			{/if}
		</div>
		<button onclick={load} class="p-1 rounded-md text-muted-foreground hover:text-primary hover:bg-muted" aria-label="Refresh">
			<RefreshCw size={12} />
		</button>
	</div>

	{#if loading && !depth}
		<p class="text-sm text-muted-foreground">Loading…</p>
	{:else if error}
		<p class="text-sm text-destructive">⚠ {error}</p>
	{:else if depth}
		<!-- Stage strip -->
		<div class="grid grid-cols-5 gap-2 mb-5">
			{#each stages as stage}
				<div class="text-center">
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">{stage.label}</div>
					<div class="text-lg font-bold text-foreground">{stage.done.toLocaleString()}</div>
					{#if stage.queue > 0}
						<div class="text-[10px] text-amber-500 mt-0.5">+{stage.queue.toLocaleString()} queued</div>
					{:else}
						<div class="text-[10px] text-muted-foreground/50 mt-0.5">·</div>
					{/if}
				</div>
			{/each}
		</div>

		<!-- Knowledge layer summary -->
		<div class="grid grid-cols-4 gap-3 pt-4 border-t border-border/30 text-xs">
			<div>
				<div class="text-muted-foreground">Facts</div>
				<div class="text-emerald-500 font-semibold">★ {depth.knowledge.facts.toLocaleString()}</div>
			</div>
			<div>
				<div class="text-muted-foreground">Observations</div>
				<div class="text-foreground font-semibold">{depth.knowledge.observations.toLocaleString()}</div>
			</div>
			<div>
				<div class="text-muted-foreground">Events</div>
				<div class="text-foreground font-semibold">{depth.knowledge.events.toLocaleString()}</div>
			</div>
			<div>
				<div class="text-muted-foreground">Entities linked</div>
				<div class="text-foreground font-semibold">{depth.entities.linked_to_concept.toLocaleString()}/{depth.entities.instances.toLocaleString()}</div>
			</div>
		</div>

		<!-- Discovery signal -->
		<div class="pt-3 mt-3 border-t border-border/30 text-[10px] text-muted-foreground">
			Discovery signal: {depth.discovery_signal.candidate_edge_types} candidate edges ·
			{depth.discovery_signal.candidate_predicates} candidate verbs awaiting promotion ·
			{depth.live_queue.acquisition_eligible} refs due for acquisition retry
		</div>
	{/if}
</div>
