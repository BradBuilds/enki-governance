<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import Cloud from 'lucide-svelte/icons/cloud';
	import FileText from 'lucide-svelte/icons/file-text';
	import ExternalLink from 'lucide-svelte/icons/external-link';

	const run_id = $derived(parseInt($page.params.id, 10));

	let run = $state<any>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;

	const TERMINAL = ['done', 'failed', 'cancelled'];

	async function load() {
		try {
			const res = await fetch(`/api/research/${run_id}`);
			if (!res.ok) {
				error = `Run not found (${res.status})`;
				loading = false;
				stopPolling();
				return;
			}
			run = await res.json();
			error = null;
			if (TERMINAL.includes(run?.status)) stopPolling();
		} catch (e) {
			error = String(e);
		} finally {
			loading = false;
		}
	}

	function stopPolling() {
		if (timer) { clearInterval(timer); timer = null; }
	}

	onMount(() => {
		load();
		timer = setInterval(() => {
			if (run && TERMINAL.includes(run.status)) { stopPolling(); return; }
			load();
		}, 4000);
	});
	onDestroy(stopPolling);

	function pill(status: string): string {
		if (status === 'done') return 'bg-emerald-500/15 text-emerald-400';
		if (status === 'failed' || status === 'cancelled') return 'bg-red-500/15 text-red-400';
		return 'bg-amber-500/15 text-amber-400';
	}

	const citations = $derived((run?.report_citations || []) as any[]);
</script>

<div class="max-w-3xl mx-auto px-4 py-6 space-y-5">
	{#if loading && !run}
		<div class="text-sm text-muted-foreground">Loading research run…</div>
	{:else if error}
		<div class="text-sm text-red-400">{error}</div>
	{:else if run}
		<!-- Header -->
		<div class="space-y-2">
			<div class="flex items-start justify-between gap-3">
				<h1 class="text-lg font-semibold text-foreground">{run.goal}</h1>
				<span class="text-[11px] px-2 py-0.5 rounded-full shrink-0 {pill(run.status)}">{run.status}</span>
			</div>
			<div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
				{#if run.director_model}
					<span class="inline-flex items-center gap-1 font-medium px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30">
						<Cloud size={11} /> External · {run.director_model}
					</span>
				{/if}
				{#if !TERMINAL.includes(run.status)}
					<span>iteration {run.iteration}/{run.max_iterations}</span>
				{/if}
				{#if run.phase_detail}<span class="italic">· {run.phase_detail}</span>{/if}
			</div>
		</div>

		<!-- Counters -->
		<div class="grid grid-cols-4 gap-2 text-center">
			{#each [['Sources', run.sources_grabbed], ['Entities', run.entities_count], ['Facts', run.facts_count], ['Tokens', run.tokens_used]] as [label, val]}
				<div class="rounded-lg border border-border bg-card p-2">
					<div class="text-base font-semibold text-foreground">{val ?? 0}</div>
					<div class="text-[10px] text-muted-foreground uppercase tracking-wide">{label}</div>
				</div>
			{/each}
		</div>

		{#if run.library_id}
			<a href="/library/{run.library_id}" class="inline-flex items-center gap-1 text-xs text-primary hover:underline">
				<FileText size={12} /> Browse the collapsed library →
			</a>
		{/if}

		{#if run.error}
			<div class="rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-300">{run.error}</div>
		{/if}

		<!-- Progress timeline -->
		{#if (run.steps || []).length}
			<div class="space-y-1">
				<div class="text-xs font-medium text-muted-foreground uppercase tracking-wide">Progress</div>
				<ol class="space-y-1">
					{#each run.steps as step}
						<li class="flex items-start gap-2 text-xs">
							<span class="mt-0.5 text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground shrink-0 w-24 text-center">{step.phase}</span>
							<span class="text-foreground/90">{step.title}</span>
						</li>
					{/each}
				</ol>
			</div>
		{/if}

		<!-- Report -->
		{#if run.report_markdown}
			<div class="space-y-2">
				<div class="text-xs font-medium text-muted-foreground uppercase tracking-wide">Report</div>
				<div class="rounded-lg border border-border bg-card p-4 text-sm leading-relaxed text-foreground/90 whitespace-pre-wrap break-words">{run.report_markdown}</div>
			</div>
		{/if}

		<!-- Sources -->
		{#if citations.length}
			<div class="space-y-1">
				<div class="text-xs font-medium text-muted-foreground uppercase tracking-wide">Sources</div>
				<ol class="space-y-1">
					{#each citations as c}
						<li class="text-xs flex items-start gap-1.5">
							<span class="text-muted-foreground shrink-0">[{c.marker}]</span>
							{#if c.document_id}
								<a href="/document/{c.document_id}" class="text-primary hover:underline break-words">{c.title || c.url || `document ${c.document_id}`}</a>
							{:else if c.url}
								<a href={c.url} target="_blank" rel="noopener" class="text-primary hover:underline break-words inline-flex items-center gap-0.5">{c.title || c.url} <ExternalLink size={10} /></a>
							{:else}
								<span class="text-foreground/80">{c.title || 'source'}</span>
							{/if}
						</li>
					{/each}
				</ol>
			</div>
		{/if}
	{/if}
</div>
