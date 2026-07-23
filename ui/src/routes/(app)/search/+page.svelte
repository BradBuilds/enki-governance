<script lang="ts">
	import SearchIcon from 'lucide-svelte/icons/search';
	import User from 'lucide-svelte/icons/user';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Hash from 'lucide-svelte/icons/hash';
	import FileText from 'lucide-svelte/icons/file-text';
	import Calendar from 'lucide-svelte/icons/calendar';

	type Hit = { id: number; name: string; type: string; signal_count?: number; mention_count?: number; source_count?: number; page_count?: number };
	type Results = {
		entities: Hit[];
		places: Hit[];
		topics: Hit[];
		documents: Hit[];
		events: Hit[];
		query: string;
	};

	let q = $state('');
	let results = $state<Results | null>(null);
	let loading = $state(false);
	let errorMsg = $state<string | null>(null);
	let searchTimer: any = null;

	function debouncedSearch() {
		if (searchTimer) clearTimeout(searchTimer);
		searchTimer = setTimeout(runSearch, 250);
	}

	async function runSearch() {
		const term = q.trim();
		if (term.length < 2) {
			results = null;
			return;
		}
		loading = true;
		errorMsg = null;
		try {
			const res = await fetch(`/api/search?q=${encodeURIComponent(term)}&limit=15`);
			if (!res.ok) {
				errorMsg = `Search failed: ${res.status}`;
				return;
			}
			results = await res.json();
		} catch (e: any) {
			errorMsg = e?.message || String(e);
		} finally {
			loading = false;
		}
	}

	const totalHits = $derived.by(() => {
		if (!results) return 0;
		return results.entities.length + results.places.length + results.topics.length +
			results.documents.length + results.events.length;
	});
</script>

<svelte:head><title>Search — Enki</title></svelte:head>

<div class="max-w-4xl mx-auto p-4 space-y-4">
	<header class="space-y-2">
		<h1 class="text-lg font-semibold flex items-center gap-2">
			<SearchIcon class="w-5 h-5 text-primary" />
			Search
		</h1>
		<div class="relative">
			<SearchIcon class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
			<input
				type="text"
				bind:value={q}
				oninput={debouncedSearch}
				placeholder="Entities, places, topics, documents, events…"
				class="w-full pl-10 pr-3 py-2 bg-background border border-border rounded text-sm"
				autofocus
			/>
		</div>
		{#if loading}
			<p class="text-xs text-muted-foreground">Searching…</p>
		{:else if errorMsg}
			<p class="text-xs text-destructive">{errorMsg}</p>
		{:else if results && totalHits === 0}
			<p class="text-xs text-muted-foreground">No results for <code>{results.query}</code>.</p>
		{:else if results}
			<p class="text-xs text-muted-foreground">{totalHits} results for <code>{results.query}</code>.</p>
		{:else if q.length === 1}
			<p class="text-xs text-muted-foreground">Keep typing…</p>
		{/if}
	</header>

	{#if results && totalHits > 0}
		{#if results.entities.length}
			<section class="rounded-xl border border-border bg-card p-4">
				<h2 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2 flex items-center gap-1.5">
					<User class="w-3 h-3" /> Entities ({results.entities.length})
				</h2>
				<ul class="space-y-1">
					{#each results.entities as h}
						<li class="flex items-center justify-between gap-2 py-1 px-2 rounded hover:bg-muted/40">
							<a href="/entity/{h.id}" class="text-sm text-foreground hover:text-primary truncate flex-1 min-w-0">{h.name}</a>
							<span class="text-[10px] text-muted-foreground/70 shrink-0">{h.type}</span>
							<span class="text-[10px] tabular-nums text-muted-foreground/60 shrink-0">{h.signal_count}</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if results.places.length}
			<section class="rounded-xl border border-border bg-card p-4">
				<h2 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2 flex items-center gap-1.5">
					<MapPin class="w-3 h-3" /> Places ({results.places.length})
				</h2>
				<ul class="space-y-1">
					{#each results.places as h}
						<li class="flex items-center justify-between gap-2 py-1 px-2 rounded hover:bg-muted/40">
							<a href="/place/{h.id}" class="text-sm text-foreground hover:text-primary truncate flex-1 min-w-0">{h.name}</a>
							<span class="text-[10px] text-muted-foreground/70 shrink-0">{h.type}</span>
							<span class="text-[10px] tabular-nums text-muted-foreground/60 shrink-0">{h.mention_count}</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if results.topics.length}
			<section class="rounded-xl border border-border bg-card p-4">
				<h2 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2 flex items-center gap-1.5">
					<Hash class="w-3 h-3" /> Topics ({results.topics.length})
				</h2>
				<ul class="space-y-1">
					{#each results.topics as h}
						<li class="flex items-center justify-between gap-2 py-1 px-2 rounded hover:bg-muted/40">
							<a href="/topic/{h.id}" class="text-sm text-foreground hover:text-primary truncate flex-1 min-w-0">{h.name}</a>
							<span class="text-[10px] text-muted-foreground/70 shrink-0">{h.type}</span>
							<span class="text-[10px] tabular-nums text-muted-foreground/60 shrink-0">{h.mention_count}</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if results.events.length}
			<section class="rounded-xl border border-border bg-card p-4">
				<h2 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2 flex items-center gap-1.5">
					<Calendar class="w-3 h-3" /> Events ({results.events.length})
				</h2>
				<ul class="space-y-1">
					{#each results.events as h}
						<li class="flex items-center justify-between gap-2 py-1 px-2 rounded hover:bg-muted/40">
							<a href="/event/{h.id}" class="text-sm text-foreground hover:text-primary truncate flex-1 min-w-0">{h.name}</a>
							<span class="text-[10px] text-muted-foreground/70 shrink-0">{h.type}</span>
							<span class="text-[10px] tabular-nums text-muted-foreground/60 shrink-0">{h.source_count}</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if results.documents.length}
			<section class="rounded-xl border border-border bg-card p-4">
				<h2 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2 flex items-center gap-1.5">
					<FileText class="w-3 h-3" /> Documents ({results.documents.length})
				</h2>
				<ul class="space-y-1">
					{#each results.documents as h}
						<li class="flex items-center justify-between gap-2 py-1 px-2 rounded hover:bg-muted/40">
							<a href="/document/{h.id}" class="text-sm text-foreground hover:text-primary truncate flex-1 min-w-0">{h.name}</a>
							<span class="text-[10px] text-muted-foreground/70 shrink-0">{h.type ?? '—'}</span>
							{#if h.page_count}
								<span class="text-[10px] tabular-nums text-muted-foreground/60 shrink-0">{h.page_count}p</span>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/if}
	{/if}
</div>
