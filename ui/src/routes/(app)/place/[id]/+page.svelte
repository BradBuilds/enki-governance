<script lang="ts">
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Building2 from 'lucide-svelte/icons/building-2';
	import Calendar from 'lucide-svelte/icons/calendar';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
	const place = $derived(data.place);
	const reclassified = $derived(data.reclassified);

	function fmtCoord(v: number | null | undefined): string {
		if (v === null || v === undefined) return '—';
		return v.toFixed(4);
	}
</script>

<svelte:head>
	<title>{place?.canonical_name ?? 'Place'} — Enki</title>
</svelte:head>

<div class="max-w-4xl mx-auto p-4 space-y-3">
	{#if reclassified}
		<div class="rounded-xl border border-amber-500/40 bg-amber-500/5 p-5 space-y-2">
			<div class="flex items-center gap-2">
				<AlertTriangle class="w-4 h-4 text-amber-400" />
				<h2 class="text-sm font-semibold">This place was reclassified</h2>
			</div>
			{#if reclassified.pending && reclassified.target_category}
				<p class="text-xs text-muted-foreground">
					Queued for migration to <code>{reclassified.target_category}.{reclassified.target_subtype ?? '?'}</code>.
				</p>
			{:else if reclassified.sweep_action === 'dropped_garbage'}
				<p class="text-xs text-muted-foreground">
					Flagged as noise during decontamination ({reclassified.sweep_reason ?? 'no reason'}).
				</p>
			{:else}
				<p class="text-xs text-muted-foreground">
					This place was reclassified but the redirect target is missing.
				</p>
			{/if}
		</div>
	{:else if place}
		<header class="rounded-xl border border-border bg-card px-5 py-4"
			data-researchable
			data-node-id={`entity:${place.entity_id || place.place_id}`}>
			<div class="flex items-baseline gap-2.5 mb-1 flex-wrap">
				<MapPin size={18} class="text-primary shrink-0" />
				<h1 class="text-lg font-semibold text-foreground flex-1 min-w-0 break-words">
					{place.canonical_name}
				</h1>
				{#if place.base60_address}
					<span class="text-[10px] uppercase tracking-wider text-primary/70 font-mono px-1.5 py-0.5 rounded bg-primary/10">
						{place.base60_address}
					</span>
				{/if}
				{#if place.canonical_type}
					<span class="text-[10px] uppercase tracking-wider text-muted-foreground/80 bg-muted px-1.5 py-0.5 rounded">
						{place.canonical_type}
					</span>
				{/if}
				{#if place.iso_country_code}
					<span class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-primary/10 text-primary/90 border border-primary/20">
						{place.iso_country_code}{place.iso_region_code ? `/${place.iso_region_code}` : ''}
					</span>
				{/if}
			</div>
			<div class="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground/80 mt-2">
				<span>{place.mention_count ?? 0} mentions</span>
				{#if place.parent_place_id}
					<span>· parent <a href="/place/{place.parent_place_id}" class="text-primary hover:underline">{place.parent_name ?? place.parent_place_id}</a></span>
				{/if}
				{#if place.last_seen}
					<span>· last seen {place.last_seen.slice(0, 10)}</span>
				{/if}
			</div>

			<!-- Facts grid — mirror the entity Profile shape -->
			<div class="rounded-md border border-border/60 bg-background/30 p-3 mt-3">
				<div class="text-[10px] uppercase tracking-wider text-muted-foreground mb-2">Facts</div>
				<dl class="grid grid-cols-[120px_1fr] gap-x-3 gap-y-1.5 text-xs">
					{#if place.iso_country_code}
						<dt class="text-muted-foreground">country code</dt>
						<dd class="text-foreground font-mono">{place.iso_country_code}</dd>
					{/if}
					{#if place.iso_region_code}
						<dt class="text-muted-foreground">region code</dt>
						<dd class="text-foreground font-mono">{place.iso_region_code}</dd>
					{/if}
					{#if place.latitude !== null && place.longitude !== null}
						<dt class="text-muted-foreground">coordinates</dt>
						<dd class="text-foreground font-mono">
							{fmtCoord(place.latitude)}, {fmtCoord(place.longitude)}
							{#if place.geocode_source}
								<span class="text-[10px] text-muted-foreground ml-1">[{place.geocode_source}]</span>
							{/if}
						</dd>
					{/if}
					{#if place.elevation_m}
						<dt class="text-muted-foreground">elevation</dt>
						<dd class="text-foreground">{Math.round(place.elevation_m)} m</dd>
					{/if}
					{#if place.geonames_id}
						<dt class="text-muted-foreground">geonames</dt>
						<dd>
							<a href={`https://www.geonames.org/${place.geonames_id}`} target="_blank" rel="noopener" class="text-primary hover:underline font-mono">
								{place.geonames_id} ↗
							</a>
						</dd>
					{/if}
					{#if place.parent_name}
						<dt class="text-muted-foreground">located in</dt>
						<dd>
							<a href={`/place/${place.parent_place_id}`} class="text-primary hover:underline">
								{place.parent_name}
							</a>
						</dd>
					{/if}
				</dl>
			</div>

			{#if place.aliases?.length}
				<div class="flex flex-wrap gap-1 mt-3">
					{#each place.aliases as a}
						<span class="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{a}</span>
					{/each}
				</div>
			{/if}
		</header>

		{#if place.entities_here?.length}
			<section class="rounded-xl border border-border bg-card px-5 py-4">
				<h2 class="text-sm font-semibold text-foreground mb-2 flex items-center gap-2">
					<Building2 size={14} class="text-muted-foreground" />
					Entities located here <span class="text-muted-foreground/60 font-normal">({place.entities_here.length})</span>
				</h2>
				<ul class="grid grid-cols-1 md:grid-cols-2 gap-1">
					{#each place.entities_here as e}
						<li class="flex items-center justify-between gap-2 py-1 px-2 rounded hover:bg-muted/40">
							<a href="/entity/{e.entity_id}" class="text-xs text-foreground hover:text-primary truncate flex-1 min-w-0">
								{e.canonical_name}
							</a>
							<span class="text-[10px] text-muted-foreground/70 shrink-0">{e.canonical_type}</span>
							<span class="text-[10px] tabular-nums text-muted-foreground/60 shrink-0">{e.signal_count}</span>
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if place.events_here?.length}
			<section class="rounded-xl border border-border bg-card px-5 py-4">
				<h2 class="text-sm font-semibold text-foreground mb-2 flex items-center gap-2">
					<Calendar size={14} class="text-muted-foreground" />
					Events at this place <span class="text-muted-foreground/60 font-normal">({place.events_here.length})</span>
				</h2>
				<ul class="space-y-1">
					{#each place.events_here as ev}
						<li class="flex items-baseline gap-2 py-1 px-2 rounded hover:bg-muted/40">
							<a href="/event/{ev.event_id}" class="text-xs text-foreground hover:text-primary truncate flex-1 min-w-0">
								{ev.title}
							</a>
							<span class="text-[10px] text-muted-foreground/70 shrink-0">{ev.event_type}</span>
							{#if ev.event_time}
								<span class="text-[10px] text-muted-foreground/60 shrink-0">{ev.event_time.slice(0, 10)}</span>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		{#if (!place.entities_here || !place.entities_here.length) && (!place.events_here || !place.events_here.length)}
			<div class="rounded-xl border border-border bg-card px-5 py-6 text-center text-xs text-muted-foreground">
				No entities or events linked to this place yet.
			</div>
		{/if}

		<div class="text-xs text-muted-foreground/70 pt-2">
			Place ID <span class="font-mono">{place.place_id}</span>
		</div>
	{/if}
</div>
