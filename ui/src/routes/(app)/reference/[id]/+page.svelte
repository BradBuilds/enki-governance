<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { RefCard } from '$lib/components/cards2';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Upload from 'lucide-svelte/icons/upload';
	import XCircle from 'lucide-svelte/icons/x-circle';

	const reference_id = $derived(parseInt($page.params.id, 10));

	let reference = $state<any>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	onMount(async () => {
		try {
			const res = await fetch(`/api/references/${reference_id}`);
			if (res.ok) reference = await res.json();
			else error = `reference ${reference_id} not found`;
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
	{:else if reference}
		<RefCard reference_id={reference.reference_id} reference={reference} size="detail" />

		<!-- Identifiers + status -->
		<section class="grid grid-cols-1 md:grid-cols-2 gap-3">
			<div class="rounded-xl border border-border bg-card p-4">
				<h3 class="text-xs uppercase tracking-wider text-muted-foreground mb-3">Identifiers</h3>
				{#if reference.identifiers && Object.keys(reference.identifiers).length > 0}
					<dl class="space-y-1 text-xs">
						{#each Object.entries(reference.identifiers) as [k, v]}
							<div class="flex justify-between gap-2">
								<dt class="text-muted-foreground">{k}</dt>
								<dd class="text-foreground font-mono truncate">{String(v)}</dd>
							</div>
						{/each}
					</dl>
				{:else}
					<p class="text-xs text-muted-foreground italic">No identifiers.</p>
				{/if}
			</div>

			<div class="rounded-xl border border-border bg-card p-4">
				<h3 class="text-xs uppercase tracking-wider text-muted-foreground mb-3">Acquisition</h3>
				<dl class="space-y-1 text-sm">
					<div class="flex justify-between gap-2">
						<dt class="text-muted-foreground">status</dt>
						<dd class="text-foreground">{reference.acquisition_status}</dd>
					</div>
					<div class="flex justify-between gap-2">
						<dt class="text-muted-foreground">attempts</dt>
						<dd class="text-foreground">{reference.acquisition_attempts ?? 0}</dd>
					</div>
					{#if reference.last_attempted_at}
						<div class="flex justify-between gap-2">
							<dt class="text-muted-foreground">last try</dt>
							<dd class="text-foreground text-xs">{new Date(reference.last_attempted_at).toLocaleString()}</dd>
						</div>
					{/if}
					{#if reference.next_retry_at}
						<div class="flex justify-between gap-2">
							<dt class="text-muted-foreground">next retry</dt>
							<dd class="text-foreground text-xs">{new Date(reference.next_retry_at).toLocaleString()}</dd>
						</div>
					{/if}
					{#if reference.acquired_url}
						<div class="flex justify-between gap-2">
							<dt class="text-muted-foreground">acquired from</dt>
							<dd><a href={reference.acquired_url} target="_blank" rel="noopener noreferrer" class="text-primary hover:underline text-xs truncate">link →</a></dd>
						</div>
					{/if}
				</dl>
			</div>
		</section>

		<!-- Acquisition paths (purchase links) -->
		<section class="rounded-xl border border-border bg-card p-5">
			<h3 class="text-xs uppercase tracking-wider text-muted-foreground mb-3">
				Acquisition paths ({(reference.purchase_links || []).length})
			</h3>
			{#if (reference.purchase_links || []).length === 0}
				<p class="text-sm text-muted-foreground italic">No purchase paths generated yet.</p>
			{:else}
				<div class="space-y-1.5">
					{#each reference.purchase_links as link}
						<a
							href={link.url}
							target="_blank"
							rel="noopener noreferrer"
							class="flex items-center gap-2 p-2 -mx-1 rounded-lg hover:bg-muted transition-colors group"
						>
							<span class="inline-flex items-center justify-center w-5 h-5 rounded-full bg-primary/10 text-primary text-[10px] font-mono shrink-0">
								{link.priority}
							</span>
							<span class="text-sm text-foreground flex-1">{link.vendor}</span>
							<span class="text-[10px] text-muted-foreground">{link.cost_hint}</span>
							<ExternalLink size={12} class="text-muted-foreground group-hover:text-primary shrink-0" />
						</a>
					{/each}
				</div>
			{/if}
		</section>

		<!-- Analyst actions -->
		<section class="rounded-xl border border-border bg-card p-5">
			<h3 class="text-xs uppercase tracking-wider text-muted-foreground mb-3">Actions</h3>
			<div class="flex flex-wrap gap-2">
				<button class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-muted text-xs text-foreground hover:bg-muted/80">
					<Upload size={12} /> Upload PDF (I have this)
				</button>
				<button class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-muted text-xs text-foreground hover:bg-muted/80">
					<XCircle size={12} /> Mark unavailable
				</button>
				<button class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-muted text-xs text-foreground hover:bg-muted/80">
					Retry now
				</button>
			</div>
			<p class="text-[10px] text-muted-foreground italic mt-3">
				Mutation endpoints land in F4 follow-on.
			</p>
		</section>
	{/if}
</div>
