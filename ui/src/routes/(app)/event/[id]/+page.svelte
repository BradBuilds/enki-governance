<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import Calendar from 'lucide-svelte/icons/calendar';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Users from 'lucide-svelte/icons/users';
	import FileText from 'lucide-svelte/icons/file-text';
	import LinkIcon from 'lucide-svelte/icons/link';

	const event_id = $derived(parseInt($page.params.id, 10));

	type Participant = {
		entity_id: number;
		canonical_name: string | null;
		canonical_type: string | null;
		base60_address: string | null;
		roles: string[];
	};
	type Source = {
		statement_id: number;
		raw_text: string;
		document_id: number | null;
		document_title: string | null;
		source_type: string | null;
		source_url: string | null;
		publisher: string | null;
		published_at: string | null;
	};
	type RelatedEvent = {
		event_id: number;
		title: string;
		event_type: string;
		event_time: string | null;
		latitude: number | null;
		longitude: number | null;
	};

	let event = $state<any>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	onMount(async () => {
		try {
			const res = await fetch(`/api/events/${event_id}`);
			if (res.ok) event = await res.json();
			else error = `event ${event_id} not found`;
		} catch (e: any) {
			error = e.message;
		} finally {
			loading = false;
		}
	});

	function fmtDate(s: string | null): string {
		if (!s) return '—';
		const d = new Date(s);
		return d.toLocaleString(undefined, {
			year: 'numeric', month: 'short', day: 'numeric',
			hour: '2-digit', minute: '2-digit'
		});
	}

	function fmtDateOnly(s: string | null): string {
		if (!s) return '—';
		return new Date(s).toLocaleDateString(undefined, {
			year: 'numeric', month: 'short', day: 'numeric'
		});
	}
</script>

<div class="max-w-5xl mx-auto p-4 space-y-3">
	<a href="javascript:history.back()" class="text-xs text-muted-foreground hover:text-foreground inline-block">◀ Back</a>

	{#if loading}
		<p class="text-sm text-muted-foreground">Loading…</p>
	{:else if error}
		<p class="text-sm text-destructive">⚠ {error}</p>
	{:else if event}

		<!-- HEADER -->
		<header class="rounded-xl border border-border bg-card px-5 py-4">
			<div class="flex items-baseline gap-2.5 mb-1 flex-wrap">
				<Calendar size={18} class="text-primary shrink-0" />
				<h1 class="text-lg font-semibold text-foreground flex-1 min-w-0 break-words">
					{event.title || event.event_type}
				</h1>
				{#if event.base60_address}
					<span class="text-[10px] uppercase tracking-wider text-primary/70 font-mono px-1.5 py-0.5 rounded bg-primary/10">
						{event.base60_address}
					</span>
				{/if}
				<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
					{event.event_type}
				</span>
			</div>

			{#if event.narrative && event.narrative !== event.title}
				<p class="text-sm text-foreground/80 leading-snug mb-2">
					{event.narrative}
				</p>
			{/if}

			<div class="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
				<span>
					<Calendar size={11} class="inline" />
					{fmtDate(event.event_time)}
					{#if event.event_end_time}
						→ {fmtDate(event.event_end_time)}
					{/if}
				</span>
				{#if event.location_name}
					<span>
						<MapPin size={11} class="inline" />
						<a href={`/entity/${event.location_entity_id}`} class="text-primary hover:underline">
							{event.location_name}
						</a>
					</span>
				{:else if event.latitude != null}
					<span class="font-mono">
						<MapPin size={11} class="inline" />
						{event.latitude.toFixed(4)}, {event.longitude.toFixed(4)}
					</span>
				{/if}
				{#if event.participants?.length}
					<span>{event.participants.length} participant{event.participants.length === 1 ? '' : 's'}</span>
				{/if}
				{#if event.sources?.length}
					<span>{event.sources.length} source{event.sources.length === 1 ? '' : 's'}</span>
				{/if}
				<span class="text-muted-foreground/60">
					confidence {Math.round((event.confidence || 0) * 100)}%
				</span>
			</div>
		</header>

		<!-- PARTICIPANTS — each gets canonical name + role + click-through -->
		{#if event.participants?.length}
			<section class="rounded-xl border border-border bg-card p-5">
				<div class="flex items-center justify-between mb-3">
					<h2 class="text-sm font-semibold text-foreground">
						<Users size={14} class="inline -mt-0.5" /> Participants
					</h2>
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
						{event.participants.length}
					</span>
				</div>
				<ul class="space-y-1.5">
					{#each event.participants as p (p.entity_id)}
						<li class="flex items-baseline gap-3 text-sm">
							<span class="text-[10px] uppercase tracking-wider w-12 shrink-0
								{p.roles.includes('actor') ? 'text-amber-300' : 'text-muted-foreground'}">
								{p.roles.join(' · ') || 'related'}
							</span>
							<a href={`/entity/${p.entity_id}`}
								class="text-foreground hover:text-primary hover:underline flex-1">
								{p.canonical_name || `entity ${p.entity_id}`}
							</a>
							{#if p.canonical_type}
								<span class="text-[10px] text-muted-foreground/70">
									{p.canonical_type}
								</span>
							{/if}
							{#if p.base60_address}
								<span class="text-[10px] text-muted-foreground/50 font-mono">
									{p.base60_address}
								</span>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		<!-- SOURCES — articles/statements that reported this event -->
		{#if event.sources?.length}
			<section class="rounded-xl border border-border bg-card p-5">
				<div class="flex items-center justify-between mb-3">
					<h2 class="text-sm font-semibold text-foreground">
						<FileText size={14} class="inline -mt-0.5" /> Sources
					</h2>
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
						{event.sources.length}
					</span>
				</div>
				<ul class="space-y-3">
					{#each event.sources as s (s.statement_id)}
						<li class="pl-3 border-l-2 border-border">
							<div class="flex items-baseline gap-2 mb-1 flex-wrap">
								{#if s.document_id}
									<a href={`/document/${s.document_id}`}
										class="text-xs font-semibold text-foreground hover:text-primary hover:underline">
										{s.document_title || `document ${s.document_id}`}
									</a>
								{:else}
									<span class="text-xs font-semibold text-foreground">
										{s.document_title || 'unsourced'}
									</span>
								{/if}
								{#if s.publisher}
									<span class="text-[10px] text-muted-foreground">
										{s.publisher}
									</span>
								{/if}
								{#if s.source_type}
									<span class="text-[10px] uppercase tracking-wider text-muted-foreground/70">
										· {s.source_type}
									</span>
								{/if}
								{#if s.published_at}
									<span class="text-[10px] text-muted-foreground">
										· {fmtDateOnly(s.published_at)}
									</span>
								{/if}
								{#if s.source_url}
									<a href={s.source_url} target="_blank" rel="noopener"
										class="text-[10px] text-primary hover:underline ml-auto">
										↗ original
									</a>
								{/if}
							</div>
							{#if s.raw_text}
								<p class="text-xs text-foreground/80 leading-snug italic">
									"{s.raw_text.length > 280 ? s.raw_text.slice(0, 280) + '…' : s.raw_text}"
								</p>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		<!-- RELATED EVENTS — same actors, ±7 days. Helps disambiguate
		     when several events share a generic title. -->
		{#if event.related_events?.length}
			<section class="rounded-xl border border-amber-500/30 bg-amber-500/5 p-5">
				<div class="flex items-baseline justify-between mb-2">
					<h2 class="text-sm font-semibold text-foreground">
						<LinkIcon size={14} class="inline -mt-0.5" /> Related events
					</h2>
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground">
						{event.related_events.length} · same participants · ±7 days
					</span>
				</div>
				<p class="text-xs text-muted-foreground mb-3">
					Events sharing at least one participant with this one, within a week. If they look the same, the title is probably generic
					(<span class="font-mono">"Article published"</span> etc.) — check the participants + times to tell them apart.
				</p>
				<ul class="space-y-1.5">
					{#each event.related_events as r (r.event_id)}
						<li class="flex items-baseline gap-3 text-xs">
							<span class="text-[10px] text-muted-foreground/70 w-28 shrink-0 tabular-nums">
								{fmtDate(r.event_time)}
							</span>
							<a href={`/event/${r.event_id}`} class="text-foreground hover:text-primary hover:underline flex-1 truncate">
								{r.title}
							</a>
							<span class="text-[10px] text-muted-foreground/60 shrink-0">
								{r.event_type}
							</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

	{/if}
</div>
