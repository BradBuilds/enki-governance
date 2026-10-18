<script lang="ts">
	import { api } from '$lib/api/mock';
	import { watchTopicModal } from '$lib/stores/watchTopicModal.svelte';
	import { getHeaders } from '$lib/api/mock';
	import { onMount } from 'svelte';
	import X from 'lucide-svelte/icons/x';
	import Eye from 'lucide-svelte/icons/eye';

	type EnabledSource = {
		catalog_id: string;
		name: string;
		category: string;
		region: string;
	};

	let title = $state('');
	let keywords = $state('');
	let backfill = $state(0);
	let creating = $state(false);
	let error = $state<string | null>(null);
	let enabledSources = $state<EnabledSource[]>([]);
	let selected = $state<Set<string>>(new Set());
	let loadingSources = $state(false);

	async function loadEnabledSources() {
		loadingSources = true;
		try {
			const r = await fetch('/api/sources/catalog?enabled_only=true', {
				headers: getHeaders()
			});
			if (r.ok) {
				const data = await r.json();
				enabledSources = data.items || [];
				// Default-select all enabled sources for new topic
				if (selected.size === 0) {
					selected = new Set(enabledSources.map((s) => s.catalog_id));
				}
			}
		} finally {
			loadingSources = false;
		}
	}

	$effect(() => {
		if (watchTopicModal.isOpen && watchTopicModal.prefill && !title) {
			const p = watchTopicModal.prefill;
			if (p.title) title = p.title;
			if (p.keywords?.length) keywords = p.keywords.join(', ');
		}
	});

	$effect(() => {
		if (watchTopicModal.isOpen && enabledSources.length === 0 && !loadingSources) {
			loadEnabledSources();
		}
	});

	function toggleSource(id: string) {
		const next = new Set(selected);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selected = next;
	}

	function reset() {
		title = '';
		keywords = '';
		backfill = 0;
		selected = new Set();
		error = null;
	}

	function cancel() {
		reset();
		watchTopicModal.close();
	}

	async function submit() {
		if (!title.trim()) return;
		if (selected.size === 0) {
			error = 'Pick at least one source';
			return;
		}
		creating = true;
		error = null;
		try {
			const sources: Record<string, true> = {};
			for (const cid of selected) sources[cid] = true;

			const payload = {
				title,
				keywords: keywords
					? keywords.split(',').map((k) => k.trim()).filter(Boolean)
					: [title.toLowerCase()],
				sources,
				backfill_months: backfill,
			};
			const res: any = await api.createWatchTopic(payload);
			if (res?.topic_id) {
				watchTopicModal.onCreated?.(res.topic_id);
				reset();
				watchTopicModal.close();
			} else {
				error = res?.error || 'Failed to create topic';
			}
		} catch (e) {
			error = 'Failed to create topic';
		}
		creating = false;
	}
</script>

{#if watchTopicModal.isOpen}
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm"
		onclick={cancel}
		role="dialog"
		aria-modal="true"
	>
		<div
			class="w-full max-w-lg mx-4 rounded-xl border border-border bg-card shadow-2xl"
			onclick={(e) => e.stopPropagation()}
			role="document"
		>
			<div class="flex items-center justify-between px-5 py-3 border-b border-border">
				<div class="flex items-center gap-2">
					<Eye class="w-4 h-4 text-primary" />
					<h3 class="text-sm font-semibold text-foreground">Watch a new topic</h3>
				</div>
				<button
					onclick={cancel}
					class="p-1 rounded-md hover:bg-muted transition-colors"
					aria-label="Close"
				>
					<X class="w-4 h-4 text-muted-foreground" />
				</button>
			</div>

			<div class="px-5 py-4 space-y-4">
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Topic</label>
					<input
						type="text"
						bind:value={title}
						placeholder='e.g. "Ukraine conflict", "quantum computing"...'
						class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring"
					/>
				</div>
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Keywords</label>
					<input
						type="text"
						bind:value={keywords}
						placeholder="Comma-separated, or leave empty to use the topic title"
						class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring"
					/>
				</div>
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wide mb-1.5 block">
						Sources
						<span class="text-muted-foreground/60 normal-case ml-1">
							({selected.size} of {enabledSources.length} enabled)
						</span>
					</label>
					{#if loadingSources}
						<p class="text-xs text-muted-foreground">Loading sources…</p>
					{:else if enabledSources.length === 0}
						<p class="text-xs text-muted-foreground">
							No sources enabled. Open
							<a href="/intel/sources" class="text-primary underline">/intel/sources</a>
							to compose your intel diet.
						</p>
					{:else}
						<div class="max-h-40 overflow-y-auto border border-border rounded-md p-2 space-y-1 bg-muted/30">
							{#each enabledSources as src (src.catalog_id)}
								<label class="flex items-center gap-2 text-xs cursor-pointer hover:bg-muted/50 px-1 py-0.5 rounded">
									<input
										type="checkbox"
										checked={selected.has(src.catalog_id)}
										onchange={() => toggleSource(src.catalog_id)}
										class="rounded"
									/>
									<span class="flex-1">{src.name}</span>
									<span class="text-[10px] text-muted-foreground">
										{src.category} · {src.region}
									</span>
								</label>
							{/each}
						</div>
					{/if}
				</div>
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wide">Backfill</label>
					<select
						bind:value={backfill}
						class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring"
					>
						<option value={0}>No — start from now</option>
						<option value={1}>Last 1 month</option>
						<option value={3}>Last 3 months</option>
						<option value={6}>Last 6 months</option>
						<option value={12}>Last 12 months</option>
					</select>
				</div>

				{#if error}
					<div class="text-xs text-red-400">{error}</div>
				{/if}
			</div>

			<div class="flex items-center justify-end gap-2 px-5 py-3 border-t border-border">
				<button
					onclick={cancel}
					class="px-3 py-1.5 text-xs rounded-md bg-muted text-muted-foreground hover:text-foreground transition-colors"
				>
					Cancel
				</button>
				<button
					onclick={submit}
					disabled={!title.trim() || creating}
					class="px-3 py-1.5 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
				>
					{creating ? 'Creating…' : 'Start Watching'}
				</button>
			</div>
		</div>
	</div>
{/if}
