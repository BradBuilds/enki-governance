<script lang="ts">
	/**
	 * NodeCardCompact — the unified compact card for list views.
	 *
	 * Any node_type renders through this component with the same shape:
	 *   [type badge]  Title                     [source count] [conflict warn]
	 *                 optional meta row (event time, place)
	 *                 optional one-line narrative
	 *
	 * Click anywhere → navigate to /nodes/[id] detail page.
	 *
	 * Consumes the shape returned by `GET /nodes` (Phase 3.1):
	 *   { node_id, node_type, subtype, title, narrative,
	 *     source_article_count, source_outlet_count,
	 *     open_conflicts_count, completeness_tier,
	 *     envelope: {...} } — only node_id + node_type + title required.
	 *
	 * For richer compact views (contextual confidence, corroborations),
	 * use NodeCard.svelte instead — that's the enriched research view.
	 */
	import { goto } from '$app/navigation';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import Database from 'lucide-svelte/icons/database';

	interface NodeSummary {
		node_id: string;
		node_type: string | null;
		subtype?: string | null;
		title: string | null;
		narrative?: string | null;
		source_article_count?: number;
		source_outlet_count?: number;
		open_conflicts_count?: number;
		completeness_tier?: string | null;
		// Optional meta; if present we render a meta row
		event_time?: string | null;
		place_text?: string[] | null;
		envelope?: any;
	}

	interface Props {
		node: NodeSummary;
		/** Omit the narrative line for the tightest density. */
		dense?: boolean;
	}

	let { node, dense = false }: Props = $props();

	// Pull meta from the envelope if not provided as top-level fields.
	const eventTime = $derived(node.event_time ?? node.envelope?.temporal?.event_time ?? null);
	const places = $derived(node.place_text ?? node.envelope?.spatial?.place_text ?? []);

	const TYPE_LABEL: Record<string, string> = {
		entity: 'entity', place: 'place', event: 'event',
		topic: 'topic', document: 'article', source: 'article',
		chronicle: 'chronicle', assessment: 'assessment', pattern: 'pattern'
	};
	const typeLabel = $derived(
		(node.subtype && node.subtype !== node.node_type ? node.subtype
		  : TYPE_LABEL[node.node_type ?? ''] ?? node.node_type) ?? '—'
	);

	const hasConflicts = $derived((node.open_conflicts_count ?? 0) > 0);
	const hasSources = $derived((node.source_article_count ?? 0) > 0);

	function open() {
		goto(`/nodes/${encodeURIComponent(node.node_id)}`);
	}
</script>

<button
	type="button"
	onclick={open}
	class="w-full text-left rounded-md border bg-card hover:bg-muted/30
	       transition-colors px-3 py-2"
>
	<div class="flex items-start gap-2">
		<span
			class="shrink-0 px-1.5 py-0.5 rounded bg-muted text-[10px] font-mono
			       text-muted-foreground uppercase tracking-wider"
		>
			{typeLabel}
		</span>
		<div class="min-w-0 flex-1">
			<div class="text-sm font-medium leading-tight truncate">
				{node.title || node.node_id}
			</div>
			{#if eventTime || (places && places.length > 0)}
				<div class="text-xs text-muted-foreground mt-0.5 flex gap-2 truncate">
					{#if eventTime}
						<span>{new Date(eventTime).toLocaleString()}</span>
					{/if}
					{#if places && places.length > 0}
						<span class="truncate">{places.slice(0, 2).join(' · ')}</span>
					{/if}
				</div>
			{/if}
			{#if !dense && node.narrative}
				<div class="text-xs text-muted-foreground mt-1 line-clamp-2">
					{node.narrative}
				</div>
			{/if}
		</div>

		<!-- Badges: source stack + conflict warning -->
		<div class="shrink-0 flex items-center gap-1.5 text-xs text-muted-foreground">
			{#if hasSources}
				<span
					class="inline-flex items-center gap-0.5"
					title="{node.source_article_count} article{node.source_article_count === 1 ? '' : 's'} across {node.source_outlet_count} outlet{node.source_outlet_count === 1 ? '' : 's'}"
				>
					<Database class="h-3 w-3" />
					{node.source_article_count}
				</span>
			{/if}
			{#if hasConflicts}
				<span
					class="inline-flex items-center gap-0.5 text-amber-500"
					title="{node.open_conflicts_count} unresolved conflict{node.open_conflicts_count === 1 ? '' : 's'}"
				>
					<AlertTriangle class="h-3 w-3" />
					{node.open_conflicts_count}
				</span>
			{/if}
			{#if node.completeness_tier && node.completeness_tier !== 'empty'}
				<span class="px-1 py-0.5 rounded bg-muted/50 text-[10px]">
					{node.completeness_tier}
				</span>
			{/if}
		</div>
	</div>
</button>
