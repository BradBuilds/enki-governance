<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import * as d3 from 'd3-force';

	interface Props {
		topics: any[];
		onSelect?: (topic: any) => void;
	}
	let { topics, onSelect }: Props = $props();

	let svgEl: SVGSVGElement | null = $state(null);
	let width = $state(800);
	let height = $state(560);
	let simulation: any = null;

	type GNode = { id: string; topic: any; x: number; y: number; r: number };
	type GEdge = { source: string; target: string; shared: number };

	let nodes: GNode[] = $state([]);
	let edges: GEdge[] = $state([]);

	function rebuild() {
		nodes = (topics || []).map((t) => ({
			id: t.topic_id,
			topic: t,
			x: width / 2 + (Math.random() - 0.5) * 200,
			y: height / 2 + (Math.random() - 0.5) * 200,
			r: Math.max(18, Math.min(48, 14 + Math.sqrt((t.article_count || 0) + 1) * 2.5)),
		}));

		// Edges: topics that share at least one keyword
		const edgesOut: GEdge[] = [];
		for (let i = 0; i < topics.length; i++) {
			const a = new Set<string>((topics[i].keywords || []).map((k: string) => k.toLowerCase()));
			for (let j = i + 1; j < topics.length; j++) {
				const b: string[] = (topics[j].keywords || []).map((k: string) => k.toLowerCase());
				const shared = b.filter((k) => a.has(k)).length;
				if (shared > 0) {
					edgesOut.push({ source: topics[i].topic_id, target: topics[j].topic_id, shared });
				}
			}
		}
		edges = edgesOut;

		if (simulation) simulation.stop();
		simulation = d3
			.forceSimulation(nodes as any)
			.force(
				'link',
				d3
					.forceLink(edges as any)
					.id((d: any) => d.id)
					.distance(110)
					.strength(0.3),
			)
			.force('charge', d3.forceManyBody().strength(-260))
			.force('center', d3.forceCenter(width / 2, height / 2))
			.force('collide', d3.forceCollide((d: any) => d.r + 6))
			.on('tick', () => {
				nodes = [...nodes];
				edges = [...edges];
			});
	}

	onMount(() => {
		if (svgEl) {
			const rect = svgEl.getBoundingClientRect();
			width = rect.width || 800;
			height = rect.height || 560;
		}
		rebuild();
	});

	$effect(() => {
		// rebuild when topic count changes
		if (topics) rebuild();
	});

	onDestroy(() => {
		if (simulation) simulation.stop();
	});
</script>

<div class="w-full h-full relative rounded-xl border border-border bg-card overflow-hidden">
	{#if !topics || topics.length === 0}
		<div class="absolute inset-0 flex items-center justify-center text-xs text-muted-foreground">
			No topics yet — create a watch topic to see the graph.
		</div>
	{:else}
		<svg
			bind:this={svgEl}
			{width}
			{height}
			class="w-full h-full"
			viewBox="0 0 {width} {height}"
		>
			<!-- edges -->
			{#each edges as e}
				{@const s = nodes.find((n) => n.id === (e.source as any).id || n.id === e.source)}
				{@const t = nodes.find((n) => n.id === (e.target as any).id || n.id === e.target)}
				{#if s && t}
					<line
						x1={s.x}
						y1={s.y}
						x2={t.x}
						y2={t.y}
						stroke="currentColor"
						class="text-border"
						stroke-width={Math.min(4, 1 + e.shared)}
						stroke-opacity="0.5"
					/>
				{/if}
			{/each}
			<!-- nodes -->
			{#each nodes as n}
				<g
					class="cursor-pointer"
					onclick={() => onSelect?.(n.topic)}
					role="button"
					tabindex="0"
				>
					<circle
						cx={n.x}
						cy={n.y}
						r={n.r}
						class="fill-primary/20 stroke-primary transition-colors hover:fill-primary/40"
						stroke-width="2"
					/>
					<text
						x={n.x}
						y={n.y + 4}
						text-anchor="middle"
						class="fill-foreground text-[11px] font-semibold pointer-events-none"
					>
						{n.topic.title?.slice(0, 14)}{n.topic.title?.length > 14 ? '…' : ''}
					</text>
					<text
						x={n.x}
						y={n.y + n.r + 12}
						text-anchor="middle"
						class="fill-muted-foreground text-[9px] pointer-events-none"
					>
						{n.topic.article_count ?? 0} articles
					</text>
				</g>
			{/each}
		</svg>
	{/if}
</div>
