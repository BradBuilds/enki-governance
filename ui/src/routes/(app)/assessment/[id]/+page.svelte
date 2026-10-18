<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { FactCard } from '$lib/components/cards2';
	import RotateCcw from 'lucide-svelte/icons/rotate-ccw';
	import ArrowDown from 'lucide-svelte/icons/arrow-down';

	const assessment_id = $derived(parseInt($page.params.id, 10));

	let assessment = $state<any>(null);
	let statements = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	onMount(async () => {
		try {
			const [a, s] = await Promise.all([
				fetch(`/api/assessments/${assessment_id}`).then(r => r.ok ? r.json() : null),
				fetch(`/api/assessments/${assessment_id}/statements`).then(r => r.ok ? r.json() : { statements: [] }),
			]);
			assessment = a;
			statements = s.statements || [];
			if (!assessment) error = `assessment ${assessment_id} not found`;
		} catch (e: any) {
			error = e.message;
		} finally {
			loading = false;
		}
	});
</script>

<div class="max-w-4xl mx-auto space-y-5">
	<a href="javascript:history.back()" class="text-xs text-muted-foreground hover:text-foreground">◀ Back</a>

	{#if loading}
		<p class="text-sm text-muted-foreground">Loading…</p>
	{:else if error}
		<p class="text-sm text-destructive">⚠ {error}</p>
	{:else if assessment}
		<FactCard assessment_id={assessment.assessment_id} assessment={assessment} size="detail" />

		<!-- Verdict + actions -->
		<section class="rounded-xl border border-border bg-card p-5">
			<h3 class="text-xs uppercase tracking-wider text-muted-foreground mb-3">Verdict</h3>
			<dl class="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
				<div>
					<dt class="text-muted-foreground">Tier</dt>
					<dd class="font-semibold text-foreground uppercase">{assessment.tier || assessment.assessment_tier}</dd>
				</div>
				<div>
					<dt class="text-muted-foreground">Corroboration</dt>
					<dd class="text-emerald-500 font-semibold">{assessment.corroborated_by_count ?? 1}</dd>
				</div>
				<div>
					<dt class="text-muted-foreground">Contradiction</dt>
					<dd class="text-amber-500 font-semibold">{assessment.contradicted_by_count ?? 0}</dd>
				</div>
				<div>
					<dt class="text-muted-foreground">Confidence</dt>
					<dd class="text-foreground font-semibold">{assessment.confidence !== null && assessment.confidence !== undefined ? Number(assessment.confidence).toFixed(2) : '—'}</dd>
				</div>
			</dl>

			<div class="flex flex-wrap gap-2 mt-5 pt-4 border-t border-border/50">
				<button class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-muted text-xs text-foreground hover:bg-muted/80">
					<ArrowDown size={12} /> Demote to claim
				</button>
				<button class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-muted text-xs text-foreground hover:bg-muted/80">
					<RotateCcw size={12} /> Reverse
				</button>
				<button class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-muted text-xs text-foreground hover:bg-muted/80">
					Add contradicting evidence
				</button>
				<span class="text-[10px] text-muted-foreground italic ml-2 self-center">
					Edit actions wire to backend in F4 follow-on.
				</span>
			</div>
		</section>

		<!-- Subject / Predicate / Object -->
		<section class="grid grid-cols-1 md:grid-cols-3 gap-3">
			<div class="rounded-xl border border-border bg-card p-4">
				<h4 class="text-xs uppercase tracking-wider text-muted-foreground mb-2">Subject</h4>
				{#if assessment.subj_entity_id}
					<a href={`/entity/${assessment.subj_entity_id}`} class="text-sm text-primary hover:underline">
						{assessment.subj_name || `entity ${assessment.subj_entity_id}`}
					</a>
				{:else}
					<span class="text-sm text-muted-foreground">—</span>
				{/if}
			</div>
			<div class="rounded-xl border border-border bg-card p-4">
				<h4 class="text-xs uppercase tracking-wider text-muted-foreground mb-2">Predicate</h4>
				<span class="text-sm text-foreground font-mono">{assessment.predicate_edge_type}</span>
			</div>
			<div class="rounded-xl border border-border bg-card p-4">
				<h4 class="text-xs uppercase tracking-wider text-muted-foreground mb-2">Object</h4>
				{#if assessment.obj_entity_id}
					<a href={`/entity/${assessment.obj_entity_id}`} class="text-sm text-primary hover:underline">
						{assessment.obj_name || `entity ${assessment.obj_entity_id}`}
					</a>
				{:else if assessment.obj_value}
					<span class="text-sm text-foreground">{assessment.obj_value}</span>
				{:else}
					<span class="text-sm text-muted-foreground">—</span>
				{/if}
			</div>
		</section>

		<!-- Supporting statements (verbatim quotes from source documents) -->
		<section class="rounded-xl border border-border bg-card p-5">
			<h3 class="text-xs uppercase tracking-wider text-muted-foreground mb-3">
				Supporting statements ({statements.length})
			</h3>
			{#if statements.length === 0}
				<p class="text-sm text-muted-foreground italic">No source statements linked.</p>
			{:else}
				<ul class="space-y-3">
					{#each statements as s}
						<li class="border-l-2 border-primary/30 pl-3">
							<blockquote class="text-sm text-foreground leading-relaxed">
								"{s.raw_text || s.summarized_text || '(no text)'}"
							</blockquote>
							<div class="mt-1 flex items-center gap-2 flex-wrap text-[10px] text-muted-foreground">
								<a href={`/document/${s.document_id}`} class="text-primary hover:underline">
									{s.doc_title || `doc ${s.document_id}`}
								</a>
								{#if s.page_id}<span>· page {s.page_id}</span>{/if}
								{#if s.paragraph_id}<span>· paragraph {s.paragraph_id}</span>{/if}
								<span>· {s.modality}/{s.polarity}</span>
								<span>· verb: <span class="font-mono">{s.predicate_lemma}</span></span>
								{#if s.confidence}<span>· conf {Number(s.confidence).toFixed(2)}</span>{/if}
							</div>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	{/if}
</div>
