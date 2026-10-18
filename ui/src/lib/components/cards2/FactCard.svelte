<script lang="ts">
	import { onMount } from 'svelte';
	import CardShell from './CardShell.svelte';
	import EntityHoverCard from './EntityHoverCard.svelte';
	import Star from 'lucide-svelte/icons/star';
	import Circle from 'lucide-svelte/icons/circle';
	import AlertCircle from 'lucide-svelte/icons/alert-circle';
	import HelpCircle from 'lucide-svelte/icons/help-circle';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import ChevronUp from 'lucide-svelte/icons/chevron-up';

	interface Props {
		assessment_id: number;
		size?: 'compact' | 'default' | 'detail';
		assessment?: any;
		/** When true (default), expanding a fact loads its source statements
		 * from /api/assessments/{id}/statements. Set false for low-data lists. */
		loadSources?: boolean;
		/** Expanded by default. Used on /assessment/[id]. */
		startOpen?: boolean;
		/** When set, entity links rendered inside this card forward the lens
		 * (e.g. "library/3") to the entity page so the destination opens in
		 * that contextual lens too. Preserves the "context first, then widen"
		 * navigation flow. */
		lens?: string | null;
	}

	let {
		assessment_id,
		size = 'default',
		assessment: prefetched,
		loadSources = true,
		startOpen = false,
		lens = null,
	}: Props = $props();

	const lensQuery = $derived(lens ? `?lens=${encodeURIComponent(lens)}` : '');

	let assessment = $state<any>(prefetched ?? null);
	let loading = $state(!prefetched);
	let error = $state<string | null>(null);

	let open = $state(startOpen);
	let statements = $state<any[]>([]);
	let statementsLoaded = $state(false);
	let statementsLoading = $state(false);

	onMount(async () => {
		if (!prefetched) {
			try {
				const res = await fetch(`/api/assessments/${assessment_id}`);
				if (res.ok) assessment = await res.json();
				else error = `assessment ${assessment_id} not found`;
			} catch (e: any) {
				error = e?.message || 'fetch failed';
			} finally {
				loading = false;
			}
		}
		if (startOpen) await ensureStatements();
	});

	async function ensureStatements() {
		if (!loadSources || statementsLoaded || statementsLoading) return;
		statementsLoading = true;
		try {
			const r = await fetch(`/api/assessments/${assessment_id}/statements`);
			if (r.ok) {
				const j = await r.json();
				statements = j.statements || [];
			}
			statementsLoaded = true;
		} catch {
			statementsLoaded = true;
		} finally {
			statementsLoading = false;
		}
	}

	function toggle() {
		open = !open;
		if (open) ensureStatements();
	}

	const tier = $derived(assessment?.tier ?? assessment?.assessment_tier ?? 'observation');
	const iconForTier = (t: string) => {
		switch (t) {
			case 'fact': return Star;
			case 'observation': return Circle;
			case 'claim': return AlertCircle;
			case 'hypothesis': case 'question': case 'recommendation': return HelpCircle;
			default: return Circle;
		}
	};
	const tierDotColor = (t: string) => {
		switch (t) {
			case 'fact': return 'text-emerald-500';
			case 'observation': return 'text-slate-400';
			case 'claim': return 'text-amber-500';
			case 'hypothesis': return 'text-blue-400';
			default: return 'text-muted-foreground';
		}
	};
	const tierLabel = (t: string) => (t || 'observation').toUpperCase();

	const prettyPredicate = $derived(
		(assessment?.predicate_edge_type ?? 'related to').replace(/_/g, ' ')
	);

	// Sometimes subj == obj (self-loop bug). Suppress duplicate object in display.
	const isSelfLoop = $derived(
		assessment && assessment.subj_entity_id && assessment.subj_entity_id === assessment.obj_entity_id
	);

	// Prefer KB-enriched display_name when the substrate has a bare token
	// like "Marlow" that maps to a fuller "Kevin Marlow" in entity_kb_cache.
	const subjLabel = $derived(
		assessment?.subj_display ?? assessment?.subj_name ?? assessment?.subj ?? '?'
	);
	const objLabel = $derived(
		isSelfLoop ? null : (assessment?.obj_display ?? assessment?.obj_name ?? assessment?.obj ?? assessment?.obj_value ?? null)
	);

	// First source's paragraph text — the surrounding context where the
	// claim came from. Far more useful than the bare trigger sentence
	// ("resides in Wellington" vs the full paragraph that says she was
	// brought to Acme's house). Falls back to raw_text if paragraph
	// isn't joined.
	const firstSourceQuote = $derived.by(() => {
		const src = (assessment?.sources || [])[0];
		if (!src) return null;
		const txt = src.paragraph_text || src.raw_text || src.summarized_text || '';
		return txt ? txt.trim().slice(0, 600) : null;
	});
	const triggerSentence = $derived.by(() => {
		// The narrow extracted sentence — used as a highlighted snippet inside
		// the wider paragraph quote when both are present and distinguishable.
		const src = (assessment?.sources || [])[0];
		return src?.raw_text?.trim() || null;
	});
	const sourceDocTitle = $derived.by(() => {
		const src = (assessment?.sources || [])[0];
		return src?.document_title || src?.signal_title || src?.doc_title || null;
	});

	// Anaphora resolutions written by the binding resolver. Each row in
	// `resolutions` has {side, from, to, entity_id, confidence, via_document_id}.
	const subjRes = $derived(
		(assessment?.resolutions || []).find((r: any) => r.side === 'subj') || null
	);
	const objRes = $derived(
		(assessment?.resolutions || []).find((r: any) => r.side === 'obj') || null
	);

	const eventDate = $derived(
		assessment?.earliest_event_time
			? new Date(assessment.earliest_event_time).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
			: null
	);

	const sourceCount = $derived(assessment?.corroborated_by_count ?? assessment?.source_statement_ids?.length ?? 0);
	const contradictCount = $derived(assessment?.contradicted_by_count ?? 0);

	// — Title for the `detail` CardShell mode —
	const title = $derived(
		assessment
			? `${assessment.subj_name ?? '?'} ${prettyPredicate}${objLabel ? ' ' + objLabel : ''}`
			: `assessment ${assessment_id}`
	);
</script>

{#if size === 'detail'}
	<CardShell
		{size}
		icon={iconForTier(tier)}
		{title}
		subtitle={`${tierLabel(tier)} · ${sourceCount} source${sourceCount === 1 ? '' : 's'}${contradictCount ? ` · ${contradictCount} contradicting` : ''}`}
		address={assessment?.base60_address}
		{loading}
		{error}
	>
		{#snippet children()}
			{#if assessment}
				{#if eventDate}
					<div class="text-xs text-muted-foreground mb-3">Asserted: {eventDate}</div>
				{/if}
				{#if assessment.canonical_summary}
					<p class="text-sm text-foreground/90 mb-3">{assessment.canonical_summary}</p>
				{/if}

				<div class="border-t border-border pt-3">
					<div class="flex items-center justify-between mb-2">
						<h4 class="text-xs uppercase tracking-wider text-muted-foreground">Sources</h4>
						{#if statementsLoading}<span class="text-[10px] text-muted-foreground">loading…</span>{/if}
					</div>
					{#if statements.length === 0 && statementsLoaded}
						<p class="text-xs text-muted-foreground italic">No source statements indexed.</p>
					{:else}
						<ul class="space-y-2.5 text-xs">
							{#each statements as s}
								<li class="border-l-2 border-border pl-3">
									<div class="text-muted-foreground text-[10px] mb-0.5">
										{s.doc_title || 'document'}
										{#if s.event_time} · {new Date(s.event_time).toLocaleDateString()}{/if}
										{#if s.predicate_verb_surface} · "{s.predicate_verb_surface}"{/if}
										<a href={`/reference/${s.document_id}`} class="text-primary/70 hover:text-primary ml-1">↗</a>
									</div>
									<div class="text-foreground/85 leading-relaxed">"{s.raw_text || s.summarized_text || ''}"</div>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{/if}
		{/snippet}
	</CardShell>
{:else}
	<!-- Lean row: the FACT is the row, sources collapse behind a chevron -->
	<div class="group border-b border-border/30 hover:bg-card/40 transition-colors">
		{#if loading}
			<div class="py-2.5 px-1 animate-pulse">
				<div class="h-3 bg-muted rounded w-2/3"></div>
			</div>
		{:else if error}
			<div class="py-2.5 px-1 text-xs text-destructive">⚠ {error}</div>
		{:else if assessment}
			<button
				type="button"
				onclick={toggle}
				class="w-full text-left py-2.5 px-1"
				aria-expanded={open}
			>
				<!-- Row 1: the fact triple -->
				<div class="flex items-baseline gap-3">
					<span class={`text-base leading-none ${tierDotColor(tier)} shrink-0`} title={tierLabel(tier)}>●</span>

					<span class="flex-1 text-sm leading-snug">
						{#if assessment.subj_entity_id}
							<EntityHoverCard entity_id={assessment.subj_entity_id} {lens}>
								<a href={`/entity/${assessment.subj_entity_id}${lensQuery}`}
									onclick={(e) => e.stopPropagation()}
									class="font-medium text-foreground hover:text-primary">{subjLabel}</a>
							</EntityHoverCard>
						{:else}
							<span class="font-medium text-foreground">{subjLabel}</span>
						{/if}
						{#if subjRes}
							<span class="text-[10px] text-amber-400/80 ml-1" title={`Resolved from "${subjRes.from}" via document #${subjRes.via_document_id} (confidence ${(subjRes.confidence * 100).toFixed(0)}%)`}>↩ {subjRes.from}</span>
						{/if}
						<span class="text-muted-foreground"> {prettyPredicate} </span>
						{#if objLabel}
							{#if assessment.obj_entity_id}
								<EntityHoverCard entity_id={assessment.obj_entity_id} {lens}>
									<a href={`/entity/${assessment.obj_entity_id}${lensQuery}`}
										onclick={(e) => e.stopPropagation()}
										class="font-medium text-foreground hover:text-primary">{objLabel}</a>
								</EntityHoverCard>
							{:else}
								<span class="font-medium text-foreground/90">{objLabel}</span>
							{/if}
							{#if objRes}
								<span class="text-[10px] text-amber-400/80 ml-1" title={`Resolved from "${objRes.from}" via document #${objRes.via_document_id} (confidence ${(objRes.confidence * 100).toFixed(0)}%)`}>↩ {objRes.from}</span>
							{/if}
						{/if}
					</span>

					{#if eventDate}
						<span class="text-[11px] text-muted-foreground font-mono shrink-0 hidden sm:inline">{eventDate}</span>
					{/if}

					<span class="inline-flex items-center gap-1 text-[11px] text-muted-foreground shrink-0">
						{#if sourceCount > 1}
							<span class="tabular-nums" title={`${sourceCount} sources`}>{sourceCount}</span>
						{/if}
						{#if contradictCount > 0}
							<span class="text-amber-500 ml-1" title={`${contradictCount} contradicting`}>⚠</span>
						{/if}
						<svelte:component this={open ? ChevronUp : ChevronDown} size={12} class="opacity-60 group-hover:opacity-100 transition-opacity" />
					</span>
				</div>

				<!-- Row 2: the actual source quote (default visible — "the WHAT") -->
				{#if firstSourceQuote}
					<div class="pl-7 mt-1 text-[11px] leading-relaxed text-muted-foreground/90 italic">
						"{firstSourceQuote}"
						{#if sourceDocTitle}
							<span class="not-italic text-muted-foreground/60 ml-1">— {sourceDocTitle}</span>
						{/if}
					</div>
				{/if}
			</button>

			{#if open}
				<div class="pb-3 pl-7 pr-2">
					{#if statementsLoading}
						<p class="text-[11px] text-muted-foreground italic">Loading sources…</p>
					{:else if statements.length === 0}
						<p class="text-[11px] text-muted-foreground italic">No source statements indexed.</p>
					{:else}
						<ul class="space-y-1.5">
							{#each statements as s}
								<li class="text-[11px] border-l-2 border-border/60 pl-2.5">
									<div class="text-muted-foreground/80 text-[10px]">
										{s.doc_title || 'document'}
										{#if s.event_time} · {new Date(s.event_time).toLocaleDateString()}{/if}
										<a href={`/reference/${s.document_id}`} class="text-primary/70 hover:text-primary ml-1">↗</a>
									</div>
									<div class="text-foreground/80 leading-snug">"{s.raw_text || s.summarized_text || ''}"</div>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{/if}
		{/if}
	</div>
{/if}
