<script lang="ts">
	/**
	 * Chronicle — daily intelligence brief.
	 *
	 * Rebuilt June 3 2026 around named items per feedback_no_vanity_numbers.
	 * The old page rendered chronicle_worker's aggregate-count envelope
	 * (NODES/TOPICS/PLACES/OUTLETS boxes + bar charts of country mentions)
	 * which read as a stats dashboard, not an intelligence brief. The new
	 * page pulls /api/chronicle/{day}/rich — a synthesizer that returns:
	 *   - top story (signal with the most distinct entity mentions on the day)
	 *   - first-seen entities (entities whose earliest mention is this day)
	 *   - confirmed today (assessments first reaching fact tier)
	 *   - contested today (assessments freshly contradicted)
	 *   - small contextual activity numbers
	 */

	import { onMount } from 'svelte';
	import CalendarDays from 'lucide-svelte/icons/calendar-days';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import Newspaper from 'lucide-svelte/icons/newspaper';
	import UserPlus from 'lucide-svelte/icons/user-plus';
	import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';

	type ChronicleHeader = {
		node_id: string;
		date: string;
		title: string;
	};

	type RichDay = {
		day: string;
		top_story: null | {
			signal_rid: number;
			signal_id: string;
			title: string;
			source: string | null;
			link: string | null;
			entity_count: number;
			mention_count: number;
		};
		first_seen_entities: Array<{
			entity_id: number;
			name: string;
			type: string | null;
			first_seen: string | null;
			mentions_today: number;
		}>;
		confirmed_today: Array<{
			assessment_id: number;
			subj_entity_id: number | null;
			subject: string | null;
			predicate: string | null;
			obj_entity_id: number | null;
			object: string | null;
			sources: number;
		}>;
		contested_today: Array<{
			assessment_id: number;
			subj_entity_id: number | null;
			subject: string | null;
			predicate: string | null;
			obj_entity_id: number | null;
			object: string | null;
			corroborated: number;
			contradicted: number;
		}>;
		activity: {
			documents_ingested: number;
			statements_promoted: number;
			entities_first_seen: number;
		};
	};

	let pastDays = $state<ChronicleHeader[]>([]);
	let selectedDay = $state<string>(todayISODate());
	let rich = $state<RichDay | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let backfilling = $state(false);
	let backfillToast = $state<string | null>(null);

	function todayISODate(): string {
		const d = new Date();
		return d.toISOString().slice(0, 10);
	}

	function predicateLabel(p: string | null | undefined): string {
		if (!p) return '—';
		return p.replace(/_/g, ' ');
	}

	async function loadSidebar() {
		try {
			const r = await fetch('/api/chronicle?limit=14');
			if (r.ok) {
				const d = await r.json();
				pastDays = (d.chronicles || []).map((c: any) => ({
					node_id: c.node_id, date: c.date, title: c.title,
				}));
				// If worker hasn't generated today yet, prepend it so the
				// user can still see the live synthesis.
				if (!pastDays.find((p) => p.date === selectedDay)) {
					pastDays.unshift({ node_id: `chronicle:${selectedDay}`, date: selectedDay, title: `Today — ${selectedDay}` });
				}
			}
		} catch (_) {}
	}

	async function loadDay(day: string) {
		loading = true;
		error = null;
		try {
			const r = await fetch(`/api/chronicle/${day}/rich`);
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			rich = await r.json();
		} catch (e: any) {
			error = e?.message || 'fetch failed';
			rich = null;
		} finally {
			loading = false;
		}
	}

	async function triggerBackfill() {
		backfilling = true;
		backfillToast = null;
		try {
			const r = await fetch('/api/chronicle/run', { method: 'POST' });
			const d = await r.json().catch(() => ({}));
			backfillToast = r.ok
				? `Backfill started (pid ${d.pid ?? '?'}, since ${d.since ?? '?'})`
				: `Backfill failed: HTTP ${r.status}`;
			setTimeout(() => { backfillToast = null; }, 6000);
		} catch (e: any) {
			backfillToast = `Backfill error: ${e?.message || 'unknown'}`;
		} finally {
			backfilling = false;
		}
	}

	function selectDay(d: string) {
		selectedDay = d;
		loadDay(d);
	}

	onMount(async () => {
		await loadSidebar();
		await loadDay(selectedDay);
	});
</script>

<div class="grid grid-cols-[200px_1fr] gap-6 max-w-6xl mx-auto">
	<aside class="border-r border-border pr-4">
		<div class="flex items-center justify-between mb-3">
			<h2 class="text-sm font-semibold text-foreground flex items-center gap-1.5">
				<CalendarDays size={14} class="text-primary" />
				Chronicle
			</h2>
			<button
				onclick={triggerBackfill}
				disabled={backfilling}
				title="Run chronicle worker to fill missing days"
				class="text-xs text-muted-foreground hover:text-primary disabled:opacity-50"
			>
				<RefreshCw size={12} class={backfilling ? 'animate-spin' : ''} />
			</button>
		</div>

		{#if backfillToast}
			<div class="text-[10px] text-muted-foreground italic mb-2">{backfillToast}</div>
		{/if}

		<ul class="space-y-1">
			{#each pastDays as p (p.node_id)}
				<li>
					<button
						onclick={() => selectDay(p.date)}
						class="w-full text-left px-2 py-1.5 rounded text-xs transition-colors
							{selectedDay === p.date
								? 'bg-primary/10 text-primary font-medium'
								: 'text-foreground hover:bg-muted'}"
					>
						{p.date === todayISODate() ? `Today (${p.date})` : p.date}
					</button>
				</li>
			{/each}
		</ul>
	</aside>

	<main class="space-y-5">
		<header>
			<h1 class="text-2xl font-bold text-foreground">
				{selectedDay === todayISODate() ? 'Today — ' : ''}{selectedDay}
			</h1>
			<p class="text-sm text-muted-foreground mt-1">
				What happened. What's new. What's confirmed. What's contested.
			</p>
		</header>

		{#if loading}
			<div class="text-sm text-muted-foreground italic">Loading…</div>
		{:else if error}
			<div class="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
				⚠ {error}
			</div>
		{:else if rich}
			<section class="rounded-xl border border-border bg-card p-5">
				<div class="flex items-center gap-2 mb-3">
					<Newspaper size={16} class="text-primary" />
					<h2 class="text-sm font-semibold text-foreground">Top story</h2>
				</div>
				{#if rich.top_story}
					<div class="space-y-1">
						{#if rich.top_story.link}
							<a
								href={rich.top_story.link}
								target="_blank"
								rel="noopener"
								class="text-base font-medium text-foreground hover:text-primary hover:underline"
							>
								{rich.top_story.title}
							</a>
						{:else}
							<div class="text-base font-medium text-foreground">{rich.top_story.title}</div>
						{/if}
						<div class="text-xs text-muted-foreground">
							{#if rich.top_story.source}{rich.top_story.source} · {/if}
							{rich.top_story.entity_count} {rich.top_story.entity_count === 1 ? 'entity' : 'entities'} mentioned
						</div>
					</div>
				{:else}
					<p class="text-sm text-muted-foreground italic">No stories yet today.</p>
				{/if}
			</section>

			<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
				<section class="rounded-xl border border-border bg-card p-5">
					<div class="flex items-center gap-2 mb-3">
						<UserPlus size={16} class="text-primary" />
						<h2 class="text-sm font-semibold text-foreground">First seen today</h2>
					</div>
					{#if rich.first_seen_entities.length === 0}
						<p class="text-sm text-muted-foreground italic">No new entities today.</p>
					{:else}
						<ul class="space-y-1.5">
							{#each rich.first_seen_entities as e}
								<li class="text-sm">
									<a href="/entity/{e.entity_id}" class="text-foreground hover:text-primary hover:underline">
										{e.name}
									</a>
									{#if e.type}<span class="text-xs text-muted-foreground"> · {e.type}</span>{/if}
									<span class="text-xs text-muted-foreground"> · {e.mentions_today} mention{e.mentions_today === 1 ? '' : 's'}</span>
								</li>
							{/each}
						</ul>
					{/if}
				</section>

				<section class="rounded-xl border border-border bg-card p-5">
					<div class="flex items-center gap-2 mb-3">
						<CheckCircle2 size={16} class="text-emerald-500" />
						<h2 class="text-sm font-semibold text-foreground">Confirmed today</h2>
					</div>
					{#if rich.confirmed_today.length === 0}
						<p class="text-sm text-muted-foreground italic">Nothing newly confirmed today.</p>
					{:else}
						<ul class="space-y-1.5">
							{#each rich.confirmed_today as c}
								<li class="text-sm">
									{#if c.subj_entity_id}
										<a href="/entity/{c.subj_entity_id}" class="text-foreground font-medium hover:text-primary hover:underline">{c.subject || '—'}</a>
									{:else}
										<span class="text-foreground font-medium">{c.subject || '—'}</span>
									{/if}
									<span class="text-muted-foreground"> {predicateLabel(c.predicate)} </span>
									{#if c.obj_entity_id}
										<a href="/entity/{c.obj_entity_id}" class="text-foreground font-medium hover:text-primary hover:underline">{c.object || '—'}</a>
									{:else}
										<span class="text-foreground font-medium">{c.object || '—'}</span>
									{/if}
									<span class="text-xs text-muted-foreground"> · {c.sources} source{c.sources === 1 ? '' : 's'}</span>
								</li>
							{/each}
						</ul>
					{/if}
				</section>

				<section class="rounded-xl border border-amber-500/30 bg-amber-500/5 p-5">
					<div class="flex items-center gap-2 mb-3">
						<AlertTriangle size={16} class="text-amber-500" />
						<h2 class="text-sm font-semibold text-foreground">Contested today</h2>
					</div>
					{#if rich.contested_today.length === 0}
						<p class="text-sm text-muted-foreground italic">No contradictions today.</p>
					{:else}
						<ul class="space-y-1.5">
							{#each rich.contested_today as c}
								<li class="text-sm">
									{#if c.subj_entity_id}
										<a href="/entity/{c.subj_entity_id}" class="text-foreground font-medium hover:text-primary hover:underline">{c.subject || '—'}</a>
									{:else}
										<span class="text-foreground font-medium">{c.subject || '—'}</span>
									{/if}
									<span class="text-muted-foreground"> {predicateLabel(c.predicate)} </span>
									{#if c.obj_entity_id}
										<a href="/entity/{c.obj_entity_id}" class="text-foreground font-medium hover:text-primary hover:underline">{c.object || '—'}</a>
									{:else}
										<span class="text-foreground font-medium">{c.object || '—'}</span>
									{/if}
									<span class="text-xs text-muted-foreground"> · {c.corroborated} for / {c.contradicted} against</span>
								</li>
							{/each}
						</ul>
					{/if}
				</section>

				<section class="rounded-xl border border-border bg-card p-5">
					<div class="flex items-center gap-2 mb-3">
						<CalendarDays size={16} class="text-primary" />
						<h2 class="text-sm font-semibold text-foreground">Activity</h2>
					</div>
					<ul class="space-y-1 text-sm text-foreground">
						<li>{rich.activity.documents_ingested} new document{rich.activity.documents_ingested === 1 ? '' : 's'}</li>
						<li>{rich.activity.statements_promoted} statement{rich.activity.statements_promoted === 1 ? '' : 's'} extracted</li>
						<li>{rich.activity.entities_first_seen} entity first-mentions</li>
					</ul>
				</section>
			</div>
		{/if}
	</main>
</div>
