<script lang="ts">
	import { onMount } from 'svelte';
	import Shield from 'lucide-svelte/icons/shield';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import ArrowLeft from 'lucide-svelte/icons/arrow-left';
	import Check from 'lucide-svelte/icons/check';
	import X from 'lucide-svelte/icons/x';
	import Route from 'lucide-svelte/icons/route';
	import Tag from 'lucide-svelte/icons/tag';
	import { api, apiSafe } from '$lib/api/client';

	type Group = {
		suspected_type: string;
		count: number;
		first_seen: string | null;
		last_seen: string | null;
		examples: string[];
	};

	type Vocab = {
		subtypes: string[];
		drops: string[];
		targets_by_category: Record<string, string[]>;
	};

	type Triage = {
		groups: Group[];
		total_pending: number;
		vocab: Vocab;
	};

	let loading = $state(true);
	let toast = $state<string | null>(null);
	let data = $state<Triage | null>(null);
	let selected = $state<string | null>(null);

	// Decision form state — keyed off the currently-selected group
	let decision = $state<'route' | 'drop' | 'accept' | 'alias'>('route');
	let targetCat = $state<string>('place');
	let targetSub = $state<string>('named_area');
	let canonical = $state<string>('organization');
	let persistVocab = $state(true);
	let submitting = $state(false);

	function showToast(msg: string) {
		toast = msg;
		setTimeout(() => { toast = null; }, 4000);
	}

	async function load() {
		loading = true;
		const res = await apiSafe<Triage>('/governance/ldup-triage');
		data = res;
		loading = false;
		if (res && res.groups.length > 0 && !selected) {
			selected = res.groups[0].suspected_type;
		}
	}

	onMount(load);

	let selectedGroup = $derived.by(() =>
		data?.groups.find(g => g.suspected_type === selected) || null
	);

	let availableTargetSubtypes = $derived.by(() =>
		data?.vocab.targets_by_category[targetCat] || []
	);

	$effect(() => {
		// When user changes target category, reset subtype to first valid
		if (availableTargetSubtypes.length > 0 && !availableTargetSubtypes.includes(targetSub)) {
			targetSub = availableTargetSubtypes[0];
		}
	});

	async function submitDecision() {
		if (!selectedGroup) return;
		submitting = true;
		try {
			const body: Record<string, unknown> = {
				suspected_type: selectedGroup.suspected_type,
				decision,
				persist_to_vocab: persistVocab,
			};
			if (decision === 'route') {
				body.target_category = targetCat;
				body.target_subtype = targetSub;
			} else if (decision === 'accept' || decision === 'alias') {
				body.canonical_subtype = canonical;
			}
			const res = await api<{ affected_rows: number; vocab: { persisted: boolean; change?: string; error?: string } }>(
				'/governance/ldup-decide',
				{ method: 'POST', body }
			);
			const vocabMsg = res.vocab.persisted
				? ` · vocab: ${res.vocab.change}`
				: (res.vocab.error ? ` · vocab error: ${res.vocab.error}` : '');
			showToast(`${decision} applied to ${res.affected_rows} rows${vocabMsg}`);
			await load();
		} catch (e) {
			showToast(`Decision failed: ${(e as Error).message}`);
		} finally {
			submitting = false;
		}
	}
</script>

<svelte:head>
	<title>LDU-P Triage — Enki</title>
</svelte:head>

<div class="p-4 space-y-4 max-w-7xl mx-auto">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div>
			<a href="/validation" class="text-xs text-muted-foreground hover:text-foreground inline-flex items-center gap-1 mb-1">
				<ArrowLeft class="w-3 h-3" /> Validation
			</a>
			<h1 class="text-lg font-semibold flex items-center gap-2">
				<Shield class="w-5 h-5 text-primary" />
				LDU-P Triage
			</h1>
			<p class="text-xs text-muted-foreground mt-0.5">
				Novel <code class="text-[10px]">suspected_type</code> values stuck in purgatory. Decide once → drains backlog + closes future ingest path.
			</p>
		</div>
		<button onclick={load} disabled={loading}
			class="text-xs px-2 py-1.5 rounded bg-muted text-muted-foreground hover:text-foreground inline-flex items-center gap-1.5 disabled:opacity-30">
			<RefreshCw class="w-3.5 h-3.5" /> Refresh
		</button>
	</div>

	{#if toast}
		<div class="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs">
			{toast}
		</div>
	{/if}

	{#if loading}
		<div class="text-center text-muted-foreground text-sm py-12">Loading…</div>
	{:else if !data || data.groups.length === 0}
		<div class="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-6 text-center">
			<Check class="w-8 h-8 text-emerald-400 mx-auto mb-2" />
			<div class="text-sm font-medium">No pending decisions</div>
			<div class="text-xs text-muted-foreground mt-1">
				Every suspected_type currently in LDU-P is covered by the vocab.
				{#if data}({data.total_pending} rows pending, all waiting on the promoter tick){/if}
			</div>
		</div>
	{:else}
		<div class="text-xs text-muted-foreground">
			{data.total_pending} rows pending across {data.groups.length} novel types
		</div>

		<div class="grid grid-cols-1 md:grid-cols-[280px_1fr] gap-4">
			<!-- Left: type list -->
			<div class="rounded-lg border border-border/50 max-h-[70vh] overflow-y-auto">
				{#each data.groups as g}
					<button
						onclick={() => { selected = g.suspected_type; }}
						class="w-full text-left px-3 py-2 text-xs border-b border-border/30 hover:bg-muted/50 transition flex items-center justify-between {selected === g.suspected_type ? 'bg-primary/10 border-l-2 border-l-primary' : ''}">
						<span class="font-mono truncate">{g.suspected_type || '∅'}</span>
						<span class="text-muted-foreground tabular-nums">{g.count}</span>
					</button>
				{/each}
			</div>

			<!-- Right: decision panel -->
			<div class="rounded-lg border border-border/50 p-4 space-y-4">
				{#if selectedGroup}
					<div>
						<div class="flex items-center gap-2 text-sm font-medium">
							<Tag class="w-4 h-4 text-muted-foreground" />
							<code class="font-mono">{selectedGroup.suspected_type}</code>
							<span class="text-xs text-muted-foreground">· {selectedGroup.count} pending</span>
						</div>
						<div class="text-[10px] text-muted-foreground mt-1">
							First seen: {selectedGroup.first_seen?.slice(0,16)} ·
							Last seen: {selectedGroup.last_seen?.slice(0,16)}
						</div>
					</div>

					<!-- Examples -->
					<div>
						<div class="text-xs font-medium text-muted-foreground mb-1">Example surface forms</div>
						<div class="space-y-1">
							{#each selectedGroup.examples as ex}
								<div class="text-xs bg-muted/50 rounded px-2 py-1 font-mono">{ex}</div>
							{/each}
						</div>
					</div>

					<!-- Decision -->
					<div>
						<div class="text-xs font-medium text-muted-foreground mb-2">Decision</div>
						<div class="grid grid-cols-4 gap-2">
							<button
								onclick={() => { decision = 'route'; }}
								class="text-xs px-2 py-1.5 rounded border {decision === 'route' ? 'bg-primary text-primary-foreground border-primary' : 'border-border/50 hover:bg-muted/50'}">
								<Route class="w-3 h-3 inline mr-1" /> Route
							</button>
							<button
								onclick={() => { decision = 'drop'; }}
								class="text-xs px-2 py-1.5 rounded border {decision === 'drop' ? 'bg-red-500 text-white border-red-500' : 'border-border/50 hover:bg-muted/50'}">
								<X class="w-3 h-3 inline mr-1" /> Drop
							</button>
							<button
								onclick={() => { decision = 'accept'; }}
								class="text-xs px-2 py-1.5 rounded border {decision === 'accept' ? 'bg-emerald-500 text-white border-emerald-500' : 'border-border/50 hover:bg-muted/50'}">
								<Check class="w-3 h-3 inline mr-1" /> Accept
							</button>
							<button
								onclick={() => { decision = 'alias'; }}
								class="text-xs px-2 py-1.5 rounded border {decision === 'alias' ? 'bg-blue-500 text-white border-blue-500' : 'border-border/50 hover:bg-muted/50'}">
								<Tag class="w-3 h-3 inline mr-1" /> Alias
							</button>
						</div>
						<div class="text-[10px] text-muted-foreground mt-2">
							{#if decision === 'route'}
								Forward all <code>{selectedGroup.suspected_type}</code> rows to a different substrate table.
							{:else if decision === 'drop'}
								Mark all rows as garbage. Add <code>{selectedGroup.suspected_type}</code> to vocab drop_types so future ingests skip it.
							{:else if decision === 'accept'}
								Treat as a real entity. Caller must choose which canonical entity subtype it maps to.
							{:else if decision === 'alias'}
								Same as accept, but ALSO add this string as an alias of the canonical subtype so future writes normalize automatically.
							{/if}
						</div>
					</div>

					<!-- Decision params -->
					{#if decision === 'route'}
						<div class="grid grid-cols-2 gap-3">
							<div>
								<label class="text-[10px] uppercase text-muted-foreground tracking-wide" for="cat">Target category</label>
								<select id="cat" bind:value={targetCat}
									class="w-full mt-1 text-xs bg-background border border-border/50 rounded px-2 py-1.5">
									{#each Object.keys(data.vocab.targets_by_category) as c}
										<option value={c}>{c}</option>
									{/each}
								</select>
							</div>
							<div>
								<label class="text-[10px] uppercase text-muted-foreground tracking-wide" for="sub">Target subtype</label>
								<select id="sub" bind:value={targetSub}
									class="w-full mt-1 text-xs bg-background border border-border/50 rounded px-2 py-1.5">
									{#each availableTargetSubtypes as s}
										<option value={s}>{s}</option>
									{/each}
								</select>
							</div>
						</div>
					{/if}

					{#if decision === 'accept' || decision === 'alias'}
						<div>
							<label class="text-[10px] uppercase text-muted-foreground tracking-wide" for="canonical">Canonical entity subtype</label>
							<select id="canonical" bind:value={canonical}
								class="w-full mt-1 text-xs bg-background border border-border/50 rounded px-2 py-1.5">
								{#each data.vocab.subtypes as s}
									<option value={s}>{s}</option>
								{/each}
							</select>
						</div>
					{/if}

					<!-- Persist toggle -->
					<label class="flex items-center gap-2 text-xs text-muted-foreground">
						<input type="checkbox" bind:checked={persistVocab} class="rounded border-border/50" />
						Also persist to entityvocab JSON (closes future ingest path)
					</label>

					<!-- Submit -->
					<div class="flex justify-end">
						<button
							onclick={submitDecision}
							disabled={submitting}
							class="text-xs px-4 py-1.5 rounded bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-30">
							{submitting ? 'Applying…' : `Apply to ${selectedGroup.count} rows`}
						</button>
					</div>
				{:else}
					<div class="text-xs text-muted-foreground py-8 text-center">
						Select a type from the list to make a decision.
					</div>
				{/if}
			</div>
		</div>
	{/if}
</div>
