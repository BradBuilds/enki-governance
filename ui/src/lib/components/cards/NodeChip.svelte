<script lang="ts">
	/**
	 * NodeChip — the atomic "link to another node" primitive.
	 *
	 * Use anywhere one card references another node: the Connected section
	 * of a card, a chip row in a list, an inline mention inside prose.
	 *
	 * Clicking a chip navigates to the target's detail page (/nodes/[id]).
	 * The chip shows a single-character type marker so entities, places,
	 * events, topics, and documents are visually distinguishable at a glance.
	 *
	 * Keep this component tiny — it will render hundreds of times on a
	 * dense card.
	 */
	import { goto } from '$app/navigation';

	interface Props {
		node_id: string;
		/** Display label. Falls back to node_id. */
		label?: string | null;
		/** entity | place | event | topic | media | document | chronicle | ... */
		node_type?: string | null;
		/** Optional tooltip (edge relationship + strength, etc.) */
		title?: string;
		/** Visual scale. "sm" = dense rows, "md" = default. */
		size?: 'sm' | 'md';
		/** For media chips — render a thumbnail preview instead of a marker. */
		thumbnail_url?: string | null;
	}

	let {
		node_id, label = null, node_type = null, title,
		size = 'md', thumbnail_url = null
	}: Props = $props();

	const isMedia = $derived(node_type === 'media');

	// One-character type marker — fast visual scan without needing color.
	const MARKER: Record<string, string> = {
		entity:     '◆',
		place:      '▲',
		event:      '●',
		topic:      '▪',
		document:   '□',
		source:     '□',   // legacy alias
		chronicle:  '◇',
		assessment: '◎',
		pattern:    '✦'
	};
	const marker = $derived(MARKER[node_type ?? ''] ?? '·');

	const display = $derived(label || node_id);

	// Tailwind size variants — keep inline so the component is self-contained
	const sizeClass = $derived(
		size === 'sm'
			? 'px-1.5 py-0.5 text-[10px] max-w-[180px]'
			: 'px-2 py-1 text-xs max-w-[260px]'
	);

	function navigate(e: MouseEvent) {
		e.stopPropagation();
		goto(`/nodes/${encodeURIComponent(node_id)}`);
	}
</script>

{#if isMedia && thumbnail_url}
	<!-- Thumbnail variant — small square preview, tooltip carries the label -->
	<button
		type="button"
		title={title ?? display}
		onclick={navigate}
		class="inline-block rounded border border-border overflow-hidden
		       hover:border-foreground/50 transition-colors shrink-0"
		style="width: {size === 'sm' ? 40 : 64}px; height: {size === 'sm' ? 40 : 64}px;"
	>
		<img
			src={thumbnail_url}
			alt={display}
			loading="lazy"
			class="w-full h-full object-cover"
		/>
	</button>
{:else}
	<button
		type="button"
		{title}
		onclick={navigate}
		class="inline-flex items-center gap-1 rounded border border-border
		       hover:bg-muted/50 hover:border-foreground/30 transition-colors
		       truncate {sizeClass}"
	>
		<span class="text-muted-foreground shrink-0" aria-hidden="true">{marker}</span>
		<span class="truncate">{display}</span>
	</button>
{/if}
