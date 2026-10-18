<script lang="ts">
	import Star from 'lucide-svelte/icons/star';
	import Circle from 'lucide-svelte/icons/circle';
	import CalendarDays from 'lucide-svelte/icons/calendar-days';
	import Search from 'lucide-svelte/icons/search';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import FileSearch from 'lucide-svelte/icons/file-search';
	import MapPin from 'lucide-svelte/icons/map-pin';

	interface ChronoItem {
		kind: 'fact' | 'event' | 'finding' | 'mention';
		when: string | null;
		title: string;
		body: string | null;
		source: string | null;
		source_url: string | null;
		image_url: string | null;
		tier?: string;
		assessment_id?: number;
		event_id?: string;
		finding_id?: number;
		entity_ids?: number[];
		confidence?: number;
		relevance_score?: number;
		// Location + inferred-date marker (mention rows carry a heuristic date
		// pulled from the paragraph text; events carry lat/lon).
		location?: string | null;
		date_inferred?: boolean;
		latitude?: number | null;
		longitude?: number | null;
		// Traceability handles populated by the story endpoint when a
		// fact/event row was derived from a statement we can deep-link
		// to in the document viewer.
		document_id?: number | null;
		statement_id?: number | null;
		paragraph_id?: number | null;
		page_number?: number | null;
	}

	interface Props {
		row: ChronoItem;
		lens?: string | null;
	}

	let { row, lens = null }: Props = $props();

	const lensQuery = $derived(lens ? `?lens=${encodeURIComponent(lens)}` : '');

	const kindColor = $derived(
		row.kind === 'fact'      ? 'text-emerald-500'
		: row.kind === 'event'   ? 'text-blue-400'
		: row.kind === 'mention' ? 'text-slate-400'
		:                          'text-amber-400',
	);
	const KindIcon = $derived(
		row.kind === 'fact'      ? Star
		: row.kind === 'event'   ? CalendarDays
		: row.kind === 'mention' ? FileSearch
		:                          Search,
	);

	function formatDate(iso: string | null): string {
		if (!iso) return '';
		if (/^\d{4}$/.test(iso)) return iso;   // year-only — don't fake a Jan 1
		try {
			const d = new Date(iso);
			if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
			return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
		} catch {
			return iso.slice(0, 10);
		}
	}

	let imgFailed = $state(false);
	const detailHref = $derived.by(() => {
		if (row.kind === 'fact'    && row.assessment_id) return `/assessment/${row.assessment_id}`;
		if (row.kind === 'event'   && row.event_id)      return `/event/${row.event_id}`;
		if (row.kind === 'finding' && row.source_url)    return row.source_url;
		if (row.kind === 'mention' && row.document_id)
			return `/document/${row.document_id}${row.page_number ? `?page=${row.page_number}` : ''}`;
		return null;
	});
	const detailExternal = $derived(row.kind === 'finding' && !!row.source_url);

	// Stable DOM id so the timeline strip can scrollIntoView to this row.
	const rowDomId = $derived.by(() => {
		if (row.kind === 'fact'    && row.assessment_id) return `story-fact-${row.assessment_id}`;
		if (row.kind === 'event'   && row.event_id)      return `story-event-${row.event_id}`;
		if (row.kind === 'finding' && row.finding_id)    return `story-finding-${row.finding_id}`;
		if (row.kind === 'mention' && row.paragraph_id)  return `story-mention-${row.paragraph_id}`;
		return undefined;
	});
</script>

<div id={rowDomId}
	class="group flex items-start gap-3 py-2.5 px-1 border-b border-border/30 hover:bg-card/30 transition-colors scroll-mt-24 target:bg-primary/10">
	<!-- Date column ('~' marks a date inferred from the text, not a collapsed field) -->
	<div class="shrink-0 w-[72px] pt-0.5 text-[10px] tabular-nums text-muted-foreground/80"
		title={row.date_inferred ? 'Date read from the source text (approximate)' : ''}>
		{#if row.when}{row.date_inferred ? '~' : ''}{formatDate(row.when)}{:else}—{/if}
	</div>

	<!-- Kind icon -->
	<svelte:component this={KindIcon} size={12} class={`shrink-0 mt-1 ${kindColor}`} aria-label={row.kind} />

	<!-- Body column -->
	<div class="flex-1 min-w-0">
		<div class="flex items-baseline gap-2 flex-wrap">
			{#if detailHref}
				<a href={detailHref}
					target={detailExternal ? '_blank' : null}
					rel={detailExternal ? 'noopener' : null}
					class="text-sm font-medium text-foreground hover:text-primary leading-snug">
					{row.title}
					{#if detailExternal}<ExternalLink size={10} class="inline opacity-60" />{/if}
				</a>
			{:else}
				<span class="text-sm font-medium text-foreground leading-snug">{row.title}</span>
			{/if}
			<span class="text-[9px] uppercase tracking-wider {kindColor} opacity-70">{row.kind}</span>
			{#if row.tier && row.tier !== 'observation'}
				<span class="text-[9px] uppercase tracking-wider text-muted-foreground/60">{row.tier}</span>
			{/if}
		</div>

		{#if row.body}
			<p class="text-[11px] leading-relaxed text-muted-foreground/90 italic mt-0.5 line-clamp-3">
				"{row.body.slice(0, 500)}"
			</p>
		{/if}

		{#if row.location}
			{@const geo = row.latitude != null && row.longitude != null}
			<div class="text-[10px] text-muted-foreground/70 mt-0.5 flex items-center gap-1">
				<MapPin size={10} class="shrink-0 text-rose-400/70" />
				{#if geo}
					<a href={`/map?lat=${row.latitude}&lng=${row.longitude}`} class="hover:text-primary hover:underline">{row.location}</a>
				{:else}
					<span>{row.location}</span>
				{/if}
			</div>
		{/if}

		{#if row.source}
			<div class="text-[10px] text-muted-foreground/60 mt-0.5 flex items-center gap-2 flex-wrap">
				<span>— {row.source}</span>
				{#if row.confidence}
					<span class="font-mono">{(row.confidence * 100).toFixed(0)}%</span>
				{/if}
				{#if row.document_id && row.statement_id}
					<a href={`/document/${row.document_id}?statement=${row.statement_id}`}
						class="inline-flex items-center gap-0.5 text-primary hover:underline">
						<FileSearch size={10} />
						View source
					</a>
				{/if}
			</div>
		{/if}
	</div>

	<!-- Image thumb when available -->
	{#if row.image_url && !imgFailed}
		<a href={row.source_url || row.image_url} target="_blank" rel="noopener" class="shrink-0">
			<img src={row.image_url}
				onerror={() => (imgFailed = true)}
				alt={row.title}
				class="w-16 h-16 rounded object-cover border border-border bg-muted" />
		</a>
	{/if}
</div>
