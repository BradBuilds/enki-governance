<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { browser } from '$app/environment';
	import Badge from '$lib/components/shared/Badge.svelte';

	// Dynamic import d3-force only in browser (not SSR)
	let d3: any = null;

	interface GraphNode {
		id: string;
		title: string;
		epistemic_type?: string;
		entity_type?: string;
		source_authority?: string;
		mention_count?: number;
		traverse_depth?: number;
		properties?: any[];
		canonical_entity_id?: number;
		resolution_status?: string;
		completeness_tier?: string;
		completeness_score?: number;
		aliases?: string[];
		dossier?: {
			current_role?: string;
			nationality?: string;
			organization?: string;
			date_of_birth?: string;
			population?: string;
			capital?: string;
			head_of_state?: string;
			head_of_government?: string;
			currency?: string;
			cik?: string;
			twitter?: string;
		};
		// d3 simulation fields
		x?: number;
		y?: number;
		fx?: number | null;
		fy?: number | null;
	}

	interface GraphEdge {
		source: string | GraphNode;
		target: string | GraphNode;
		relationship: string;
		strength: number;
		evidence?: string;
	}

	interface Props {
		nodes: GraphNode[];
		edges: GraphEdge[];
		focalNodeId?: string;
		maxDepth?: number;
		width?: number;
		height?: number;
		onNodeClick?: (node: GraphNode) => void;
		onNodeExpand?: (node: GraphNode) => void;
	}

	let {
		nodes, edges, focalNodeId, maxDepth = 4,
		width = 900, height = 550,
		onNodeClick, onNodeExpand,
	}: Props = $props();

	let svgEl: SVGSVGElement;
	let simulation: any = null;
	let simNodes = $state<GraphNode[]>([]);
	let simEdges = $state<any[]>([]);
	let selectedNode = $state<GraphNode | null>(null);
	let hoveredNode = $state<GraphNode | null>(null);
	let hoverPos = $state({ x: 0, y: 0 });
	let hoveredEdge = $state<any>(null);
	let depthFilter = $state(maxDepth);
	let strengthFilter = $state(0.0);

	// Pan/zoom state
	let viewBox = $state({ x: -width / 2, y: -height / 2, w: width, h: height });
	let isPanning = $state(false);
	let panStart = { x: 0, y: 0, vx: 0, vy: 0 };
	let draggedNode = $state<GraphNode | null>(null);

	// Colors
	const epistemicColors: Record<string, string> = {
		fact: '#22c55e', observation: '#3b82f6', claim: '#eab308',
		hypothesis: '#a855f7', question: '#f97316', evidence: '#06b6d4',
	};
	const entityColors: Record<string, string> = {
		person: '#8b5cf6', organization: '#3b82f6', location: '#22c55e',
		asset: '#f97316', country: '#10b981', region: '#14b8a6',
		// Phase 5A — richer type palette
		aircraft: '#06b6d4', helicopter: '#0891b2', vessel: '#0284c7',
		vehicle: '#6366f1', weapon: '#ef4444',
		military_unit: '#b91c1c', government_agency: '#1e40af',
		city: '#22c55e', event: '#fbbf24', topic: '#64748b',
		media: '#ec4899', document: '#94a3b8', source: '#94a3b8',
		university: '#7c3aed', mass_media: '#f472b6', research_institute: '#c084fc',
		fictional_person: '#6b7280', content_hash: '#6b7280',
	};
	const edgeColors: Record<string, string> = {
		supports: '#22c55e', contrasts: '#ef4444', defines: '#3b82f6',
		extends: '#3b82f6', references: '#6b7280', supersedes: '#f97316',
		led_to: '#a855f7', same_concept: '#6b7280', questions: '#f97316',
		holds_position: '#3b82f6', led: '#a855f7', associated_with: '#6b7280',
		located_in: '#22c55e', capital_of: '#10b981', member_of: '#3b82f6',
		condemned: '#ef4444', inspected: '#06b6d4', negotiated: '#a855f7',
		met_with: '#6b7280',
	};

	function nodeColor(n: GraphNode): string {
		if (n.entity_type) return entityColors[n.entity_type] || '#6b7280';
		return epistemicColors[n.epistemic_type || 'observation'] || '#6b7280';
	}

	function nodeRadius(n: GraphNode): number {
		if (n.id === focalNodeId) return 28;
		const mentions = n.mention_count || 1;
		const base = Math.min(22, 10 + Math.sqrt(mentions) * 2);
		const depthScale = 1 - (n.traverse_depth || 0) * 0.12;
		return Math.max(8, base * depthScale);
	}

	function nodeOpacity(n: GraphNode): number {
		const d = n.traverse_depth || 0;
		return Math.max(0.4, 1 - d * 0.15);
	}

	function edgeColor(e: any): string {
		return edgeColors[e.relationship] || '#6b7280';
	}

	function edgeWidth(e: any): number {
		return Math.max(1, (e.strength || 0.5) * 3);
	}

	function edgeOpacity(e: any): number {
		return 0.3 + (e.strength || 0.5) * 0.4;
	}

	function edgeDash(e: any): string {
		if (e.relationship === 'contrasts' || e.relationship === 'questions') return '4,3';
		if (e.relationship === 'extends' || e.relationship === 'references') return '2,2';
		return 'none';
	}

	function nodeLabel(n: GraphNode): string {
		const t = n.title || '';
		return t.length > 20 ? t.slice(0, 18) + '...' : t;
	}

	function nodeIcon(n: GraphNode): string {
		const et = n.entity_type;
		if (et === 'person') return '👤';
		if (et === 'organization') return '🏢';
		if (et === 'location') return '📍';
		if (et === 'country') return '🌍';
		if (et === 'region') return '🗺️';
		if (et === 'asset') return '📦';
		const ep = n.epistemic_type;
		if (ep === 'fact') return '✓';
		if (ep === 'claim') return '?';
		if (ep === 'evidence') return '📄';
		return '●';
	}

	// Type filter toggles — start with all on
	let showPeople = $state(true);
	let showOrgs = $state(true);
	let showLocations = $state(true);
	let showEquipment = $state(true);
	let showOther = $state(true);
	let topNLimit = $state<number>(0);  // 0 = all, else top N by mention_count

	function entityCategory(t: string | undefined): string {
		if (!t) return 'other';
		const tl = t.toLowerCase();
		if (tl === 'person') return 'people';
		// All org subtypes from Phase 5 validation
		if (['organization','org','military_unit','government_agency',
			 'university','mass_media','research_institute'].includes(tl)) return 'orgs';
		// All place types from Phase 5 (natural features + infrastructure)
		if (['location','country','region','city','facility','strait','bay',
			 'gulf','canal','peninsula','island','island_group','archipelago',
			 'mountain','mountain_range','desert','river','lake','sea','ocean',
			 'airport','seaport','railway_station','pipeline','bridge','dam',
			 'power_plant','refinery','continent','reef'].includes(tl)) return 'locations';
		// Equipment types
		if (['aircraft','helicopter','airliner','vessel','vehicle','weapon',
			 'spacecraft','unmanned_aerial_vehicle'].includes(tl)) return 'equipment';
		// Events and topics
		if (['event','topic','document','source','chronicle','assessment','pattern'].includes(tl)) return 'other';
		return 'other';
	}

	// Filter nodes by depth, type, and top N
	let filteredNodes = $derived.by(() => {
		let list = nodes.filter(n => (n.traverse_depth ?? 0) <= depthFilter);
		// Type filter
		list = list.filter(n => {
			const cat = entityCategory(n.entity_type);
			if (cat === 'people' && !showPeople) return false;
			if (cat === 'orgs' && !showOrgs) return false;
			if (cat === 'locations' && !showLocations) return false;
			if (cat === 'equipment' && !showEquipment) return false;
			if (cat === 'other' && !showOther) return false;
			return true;
		});
		// Top N by mention count
		if (topNLimit > 0) {
			list = [...list].sort((a, b) => (b.mention_count || 0) - (a.mention_count || 0)).slice(0, topNLimit);
		}
		return list;
	});

	let filteredEdges = $derived.by(() => {
		const nodeIds = new Set(filteredNodes.map(n => n.id));
		return edges.filter(e => {
			const s = typeof e.source === 'string' ? e.source : e.source.id;
			const t = typeof e.target === 'string' ? e.target : e.target.id;
			return nodeIds.has(s) && nodeIds.has(t) && (e.strength || 1) >= strengthFilter;
		});
	});

	let mounted = $state(false);
	let tickCounter = 0;

	function runSimulation() {
		if (simulation) simulation.stop();
		if (filteredNodes.length === 0 || !d3) return;

		const newNodes = filteredNodes.map(n => ({ ...n }));
		const newEdges = filteredEdges.map(e => ({
			...e,
			source: typeof e.source === 'string' ? e.source : e.source.id,
			target: typeof e.target === 'string' ? e.target : e.target.id,
		}));

		// Pin focal node at center
		const focal = newNodes.find(n => n.id === focalNodeId);
		if (focal) { focal.fx = 0; focal.fy = 0; }

		simNodes = newNodes;
		simEdges = newEdges;
		tickCounter = 0;

		// Scale forces by node count — more nodes need much more spread
		const nodeCount = newNodes.length;
		const repulsion = -Math.max(800, 1000 + nodeCount * 25);
		const linkDistance = Math.min(280, 140 + nodeCount * 2.5);

		// Pre-position nodes in a ring by type to give the simulation a better starting point
		const byCategory: Record<string, any[]> = { people: [], orgs: [], locations: [], other: [] };
		for (const n of newNodes) {
			byCategory[entityCategory(n.entity_type)].push(n);
		}
		const ringRadius = Math.max(250, nodeCount * 8);
		const sectors = ['people', 'orgs', 'locations', 'other'];
		const sectorAngle = (Math.PI * 2) / sectors.length;
		for (let s = 0; s < sectors.length; s++) {
			const list = byCategory[sectors[s]];
			const baseAngle = s * sectorAngle - Math.PI / 2;
			const subAngle = sectorAngle / Math.max(list.length, 1);
			for (let i = 0; i < list.length; i++) {
				const a = baseAngle + subAngle * (i + 0.5);
				if (list[i].x == null) {
					list[i].x = Math.cos(a) * ringRadius * (0.7 + Math.random() * 0.3);
					list[i].y = Math.sin(a) * ringRadius * (0.7 + Math.random() * 0.3);
				}
			}
		}

		const linkForce = d3.forceLink(newEdges as any)
			.id((dd: any) => dd.id)
			.distance(linkDistance)
			.strength(0.15);  // weaker links so spread wins

		// Type-based radial anchors — push each type to its sector
		simulation = d3.forceSimulation(newNodes as any)
			.force('link', linkForce)
			.force('charge', d3.forceManyBody().strength(repulsion).distanceMax(1200))
			.force('center', d3.forceCenter(0, 0).strength(0.01))
			.force('collide', d3.forceCollide().radius((d: any) => nodeRadius(d) + 18))
			.force('x', d3.forceX((d: any) => {
				const cat = entityCategory(d.entity_type);
				const idx = sectors.indexOf(cat);
				return Math.cos(idx * sectorAngle - Math.PI / 2) * (ringRadius * 0.6);
			}).strength(0.08))
			.force('y', d3.forceY((d: any) => {
				const cat = entityCategory(d.entity_type);
				const idx = sectors.indexOf(cat);
				return Math.sin(idx * sectorAngle - Math.PI / 2) * (ringRadius * 0.6);
			}).strength(0.08))
			.alpha(1.0)
			.alphaDecay(0.012)
			.on('tick', () => {
				tickCounter++;
				// Only update Svelte state every 3rd tick to avoid perf issues
				if (tickCounter % 3 === 0 || simulation.alpha() < 0.05) {
					simNodes = [...newNodes];
					simEdges = [...newEdges];
				}
			});
	}

	onMount(async () => {
		d3 = await import('d3-force');
		mounted = true;
		runSimulation();
	});

	function onDepthChange(d: number) {
		depthFilter = d;
		if (mounted) setTimeout(() => runSimulation(), 0);
	}

	// Re-run simulation when filters change (filteredNodes is a derived value)
	let prevNodeCount = $state(0);
	$effect(() => {
		const currentCount = filteredNodes.length;
		if (mounted && currentCount !== prevNodeCount) {
			prevNodeCount = currentCount;
			setTimeout(() => runSimulation(), 0);
		}
	});

	function onStrengthChange(s: number) {
		strengthFilter = s;
		if (mounted) setTimeout(() => runSimulation(), 0);
	}

	onDestroy(() => { if (simulation) simulation.stop(); });

	// Mouse handlers
	function onMouseDownNode(e: MouseEvent, node: GraphNode) {
		e.stopPropagation();
		draggedNode = node;
		simulation?.alphaTarget(0.3).restart();
	}

	function onMouseMove(e: MouseEvent) {
		if (draggedNode) {
			const svg = svgEl.getBoundingClientRect();
			const scaleX = viewBox.w / svg.width;
			const scaleY = viewBox.h / svg.height;
			draggedNode.fx = viewBox.x + (e.clientX - svg.left) * scaleX;
			draggedNode.fy = viewBox.y + (e.clientY - svg.top) * scaleY;
		} else if (isPanning) {
			const svg = svgEl.getBoundingClientRect();
			const scaleX = viewBox.w / svg.width;
			const scaleY = viewBox.h / svg.height;
			viewBox = {
				...viewBox,
				x: panStart.vx - (e.clientX - panStart.x) * scaleX,
				y: panStart.vy - (e.clientY - panStart.y) * scaleY,
			};
		}
	}

	function onMouseUp() {
		if (draggedNode) {
			draggedNode.fx = null;
			draggedNode.fy = null;
			draggedNode = null;
			simulation?.alphaTarget(0);
		}
		isPanning = false;
	}

	function onMouseDownSvg(e: MouseEvent) {
		if (e.target === svgEl || (e.target as Element)?.tagName === 'rect') {
			isPanning = true;
			panStart = { x: e.clientX, y: e.clientY, vx: viewBox.x, vy: viewBox.y };
			selectedNode = null;
		}
	}

	function onWheel(e: WheelEvent) {
		e.preventDefault();
		const factor = e.deltaY > 0 ? 1.1 : 0.9;
		const svg = svgEl.getBoundingClientRect();
		const mx = viewBox.x + ((e.clientX - svg.left) / svg.width) * viewBox.w;
		const my = viewBox.y + ((e.clientY - svg.top) / svg.height) * viewBox.h;
		const nw = viewBox.w * factor;
		const nh = viewBox.h * factor;
		viewBox = { x: mx - (mx - viewBox.x) * factor, y: my - (my - viewBox.y) * factor, w: nw, h: nh };
	}

	function fitView() {
		viewBox = { x: -width / 2, y: -height / 2, w: width, h: height };
	}

	function clickNode(e: MouseEvent, node: GraphNode) {
		e.stopPropagation();
		selectedNode = selectedNode?.id === node.id ? null : node;
		onNodeClick?.(node);
	}
</script>

<div class="rounded-xl border border-border bg-card overflow-hidden">
	<!-- Controls -->
	<div class="flex items-center gap-4 px-4 py-2 border-b border-border bg-muted/20 flex-wrap">
		<div class="flex items-center gap-2 text-xs">
			<span class="text-muted-foreground">Depth:</span>
			{#each [1, 2, 3, 4] as d}
				<button
					onclick={() => onDepthChange(d)}
					class="w-6 h-6 rounded text-[10px] font-medium transition-colors
						{depthFilter === d ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}"
				>{d}</button>
			{/each}
		</div>
		<div class="flex items-center gap-2 text-xs">
			<span class="text-muted-foreground">Strength:</span>
			<input type="range" min="0" max="100" value={strengthFilter * 100}
				oninput={(e) => onStrengthChange(Number((e.target as HTMLInputElement).value) / 100)}
				class="w-20 h-1 accent-primary" />
			<span class="text-muted-foreground w-8">{Math.round(strengthFilter * 100)}%</span>
		</div>

		<!-- Type filter toggles -->
		<div class="flex items-center gap-1 text-xs">
			<button onclick={() => { showPeople = !showPeople; if (mounted) setTimeout(() => runSimulation(), 0); }}
				class="text-[10px] px-2 py-0.5 rounded border transition-colors {showPeople ? 'bg-purple-500/20 text-purple-300 border-purple-500/40' : 'border-border text-muted-foreground'}"
				title="Toggle people">👤 People</button>
			<button onclick={() => { showOrgs = !showOrgs; if (mounted) setTimeout(() => runSimulation(), 0); }}
				class="text-[10px] px-2 py-0.5 rounded border transition-colors {showOrgs ? 'bg-amber-500/20 text-amber-300 border-amber-500/40' : 'border-border text-muted-foreground'}"
				title="Toggle organizations">🏢 Orgs</button>
			<button onclick={() => { showLocations = !showLocations; if (mounted) setTimeout(() => runSimulation(), 0); }}
				class="text-[10px] px-2 py-0.5 rounded border transition-colors {showLocations ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/40' : 'border-border text-muted-foreground'}"
				title="Toggle locations">🌍 Places</button>
			<button onclick={() => { showOther = !showOther; if (mounted) setTimeout(() => runSimulation(), 0); }}
				class="text-[10px] px-2 py-0.5 rounded border transition-colors {showOther ? 'bg-muted text-foreground border-border' : 'border-border text-muted-foreground/50'}"
				title="Toggle other">● Other</button>
		</div>

		<!-- Top N selector -->
		<div class="flex items-center gap-1 text-xs">
			<span class="text-muted-foreground">Top:</span>
			{#each [10, 25, 50, 0] as n}
				<button onclick={() => { topNLimit = n; if (mounted) setTimeout(() => runSimulation(), 0); }}
					class="text-[10px] px-1.5 py-0.5 rounded transition-colors {topNLimit === n ? 'bg-primary/20 text-primary' : 'text-muted-foreground hover:text-foreground'}">
					{n === 0 ? 'All' : n}
				</button>
			{/each}
		</div>

		<span class="text-[10px] text-muted-foreground">{simNodes.length} nodes · {simEdges.length} edges</span>
		<span class="flex-1"></span>
		<button onclick={fitView} class="text-[10px] text-primary hover:underline">Fit View</button>
	</div>

	<!-- SVG Graph -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<svg
		bind:this={svgEl}
		viewBox="{viewBox.x} {viewBox.y} {viewBox.w} {viewBox.h}"
		class="w-full bg-background cursor-grab"
		style:height="{height}px"
		onmousedown={onMouseDownSvg}
		onmousemove={onMouseMove}
		onmouseup={onMouseUp}
		onmouseleave={onMouseUp}
		onwheel={onWheel}
	>
		<!-- Background -->
		<rect x={viewBox.x} y={viewBox.y} width={viewBox.w} height={viewBox.h} fill="transparent" />

		<!-- Edges -->
		{#each simEdges as edge}
			{@const sx = (edge.source as any).x ?? 0}
			{@const sy = (edge.source as any).y ?? 0}
			{@const tx = (edge.target as any).x ?? 0}
			{@const ty = (edge.target as any).y ?? 0}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<g onmouseenter={() => hoveredEdge = edge} onmouseleave={() => hoveredEdge = null}>
				<line
					x1={sx} y1={sy} x2={tx} y2={ty}
					stroke={edgeColor(edge)}
					stroke-width={edgeWidth(edge)}
					stroke-opacity={edgeOpacity(edge)}
					stroke-dasharray={edgeDash(edge)}
				/>
				{#if hoveredEdge === edge}
					<text x={(sx + tx) / 2} y={(sy + ty) / 2 - 6}
						text-anchor="middle" fill="hsl(var(--foreground))" font-size="9" font-weight="500">
						{edge.relationship} ({Math.round((edge.strength || 0) * 100)}%)
					</text>
				{/if}
			</g>
		{/each}

		<!-- Nodes -->
		{#each simNodes as node}
			{@const r = nodeRadius(node)}
			{@const color = nodeColor(node)}
			{@const opacity = nodeOpacity(node)}
			{@const isFocal = node.id === focalNodeId}
			{@const isSelected = selectedNode?.id === node.id}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<g
				transform="translate({node.x ?? 0},{node.y ?? 0})"
				style:opacity={opacity}
				style:cursor="pointer"
				onmousedown={(e) => onMouseDownNode(e, node)}
				onclick={(e) => clickNode(e, node)}
				onmouseenter={(e) => { hoveredNode = node; hoverPos = { x: e.clientX, y: e.clientY }; }}
				onmouseleave={() => { hoveredNode = null; }}
			>
				<!-- Outer ring for focal/selected -->
				{#if isFocal || isSelected}
					<circle r={r + 4} fill="none" stroke={color} stroke-width="2" stroke-opacity="0.4" stroke-dasharray={isSelected ? '4,2' : 'none'} />
				{/if}

				<!-- Node circle — border style reflects dossier completeness -->
				<circle r={r} fill={color} fill-opacity="0.15" stroke={color} stroke-width={isFocal ? 2.5 : 1.5}
					stroke-dasharray="{node.completeness_tier === 'stub' ? '2,3' : node.completeness_tier === 'partial' ? '4,2' : node.source_authority === 'disputed' ? '3,2' : 'none'}" />

				<!-- Icon -->
				<text text-anchor="middle" dominant-baseline="central" font-size={r > 16 ? '12' : '9'}>
					{nodeIcon(node)}
				</text>

				<!-- Conflict indicator (amber dot) -->
				{#if (node as any).conflict_count}
					<circle cx={r * 0.7} cy={-r * 0.7} r="4" fill="#f59e0b" stroke="#1e1b2e" stroke-width="1" />
					<text x={r * 0.7} y={-r * 0.7 + 1} text-anchor="middle" dominant-baseline="central" font-size="5" fill="#1e1b2e" font-weight="bold">!</text>
				{/if}

				<!-- Label below node -->
				<text y={r + 12} text-anchor="middle" fill="hsl(var(--muted-foreground))" font-size="9" font-weight="400">
					{nodeLabel(node)}
				</text>
			</g>
		{/each}
	</svg>

	<!-- Selected node detail popover -->
	<!-- Hover Card (tooltip on mouseover) -->
	{#if hoveredNode && !draggedNode && hoveredNode.id !== selectedNode?.id}
		<div
			class="fixed z-50 pointer-events-none"
			style="left: {Math.min(hoverPos.x + 12, (typeof window !== 'undefined' ? window.innerWidth - 320 : 600))}px; top: {Math.max(hoverPos.y - 10, 10)}px;"
		>
			<div class="w-72 rounded-xl border border-border bg-card shadow-xl overflow-hidden">
				<!-- Header -->
				<div class="px-3 py-2 border-b border-border/50 bg-muted/30">
					<div class="flex items-center gap-2">
						<span class="text-sm">{nodeIcon(hoveredNode)}</span>
						<div class="flex-1 min-w-0">
							<h4 class="text-xs font-semibold text-foreground truncate">{hoveredNode.title}</h4>
							<div class="flex items-center gap-1.5 mt-0.5">
								{#if hoveredNode.entity_type}
									<span class="text-[9px] px-1 py-0.5 rounded bg-primary/10 text-primary">{hoveredNode.entity_type}</span>
								{/if}
								{#if hoveredNode.completeness_tier}
									<span class="text-[9px] px-1 py-0.5 rounded font-medium
										{hoveredNode.completeness_tier === 'verified' ? 'bg-emerald-500/20 text-emerald-300' :
										 hoveredNode.completeness_tier === 'solid' ? 'bg-blue-500/20 text-blue-300' :
										 hoveredNode.completeness_tier === 'partial' ? 'bg-amber-500/20 text-amber-300' :
										 'bg-red-500/20 text-red-300'}">
										{hoveredNode.completeness_tier}
									</span>
								{/if}
								{#if hoveredNode.mention_count}
									<span class="text-[9px] text-muted-foreground">{hoveredNode.mention_count} mentions</span>
								{/if}
							</div>
						</div>
					</div>
				</div>

				<!-- Dossier Body -->
				<div class="px-3 py-2 space-y-1.5">
					<!-- Person dossier -->
					{#if hoveredNode.dossier?.current_role}
						<div class="text-[10px]"><span class="text-muted-foreground">Role:</span> <span class="text-foreground">{hoveredNode.dossier.current_role}</span></div>
					{/if}
					{#if hoveredNode.dossier?.organization}
						<div class="text-[10px]"><span class="text-muted-foreground">Org:</span> <span class="text-foreground">{hoveredNode.dossier.organization}</span></div>
					{/if}
					{#if hoveredNode.dossier?.nationality}
						<div class="text-[10px]"><span class="text-muted-foreground">Nationality:</span> <span class="text-foreground">{hoveredNode.dossier.nationality}</span></div>
					{/if}
					{#if hoveredNode.dossier?.date_of_birth}
						<div class="text-[10px]"><span class="text-muted-foreground">Born:</span> <span class="text-foreground">{hoveredNode.dossier.date_of_birth}</span></div>
					{/if}
					{#if hoveredNode.dossier?.twitter}
						<div class="text-[10px]"><span class="text-muted-foreground">Twitter:</span> <span class="text-foreground font-mono">{hoveredNode.dossier.twitter}</span></div>
					{/if}

					<!-- Location/Country dossier -->
					{#if hoveredNode.dossier?.population}
						<div class="text-[10px]"><span class="text-muted-foreground">Population:</span> <span class="text-foreground">{Number(hoveredNode.dossier.population).toLocaleString()}</span></div>
					{/if}
					{#if hoveredNode.dossier?.capital}
						<div class="text-[10px]"><span class="text-muted-foreground">Capital:</span> <span class="text-foreground">{hoveredNode.dossier.capital}</span></div>
					{/if}
					{#if hoveredNode.dossier?.head_of_state}
						<div class="text-[10px]"><span class="text-muted-foreground">Head of State:</span> <span class="text-foreground">{hoveredNode.dossier.head_of_state}</span></div>
					{/if}
					{#if hoveredNode.dossier?.head_of_government}
						<div class="text-[10px]"><span class="text-muted-foreground">Head of Gov:</span> <span class="text-foreground">{hoveredNode.dossier.head_of_government}</span></div>
					{/if}
					{#if hoveredNode.dossier?.currency}
						<div class="text-[10px]"><span class="text-muted-foreground">Currency:</span> <span class="text-foreground">{hoveredNode.dossier.currency}</span></div>
					{/if}

					<!-- Organization dossier -->
					{#if hoveredNode.dossier?.cik}
						<div class="text-[10px]"><span class="text-muted-foreground">SEC CIK:</span> <span class="text-foreground font-mono">{hoveredNode.dossier.cik}</span></div>
					{/if}

					<!-- Aliases -->
					{#if hoveredNode.aliases?.length}
						<div class="text-[10px] text-muted-foreground">AKA: {hoveredNode.aliases.slice(0, 3).join(', ')}</div>
					{/if}

					<!-- Completeness bar -->
					{#if hoveredNode.completeness_score !== undefined && hoveredNode.completeness_score > 0}
						<div class="flex items-center gap-1.5 pt-1">
							<div class="flex-1 h-1 bg-muted rounded-full overflow-hidden">
								<div class="h-full rounded-full
									{hoveredNode.completeness_score >= 0.75 ? 'bg-emerald-400' :
									 hoveredNode.completeness_score >= 0.5 ? 'bg-blue-400' :
									 hoveredNode.completeness_score >= 0.25 ? 'bg-amber-400' : 'bg-red-400'}"
									style="width: {Math.round(hoveredNode.completeness_score * 100)}%"></div>
							</div>
							<span class="text-[8px] text-muted-foreground">{Math.round(hoveredNode.completeness_score * 100)}%</span>
						</div>
					{/if}

					<!-- Conflicts badge -->
					{#if (hoveredNode as any).conflict_count}
						<div class="flex items-center gap-1 text-[10px] text-amber-400 bg-amber-500/10 rounded px-1.5 py-0.5 mt-1">
							<span class="font-semibold">{(hoveredNode as any).conflict_count}</span>
							<span>property conflict{(hoveredNode as any).conflict_count !== 1 ? 's' : ''} detected</span>
						</div>
					{/if}

					<!-- Empty dossier -->
					{#if !hoveredNode.dossier || Object.keys(hoveredNode.dossier).length === 0}
						<p class="text-[10px] text-muted-foreground italic">No research data yet — click to research</p>
					{/if}
				</div>

				<!-- Footer hint -->
				<div class="px-3 py-1.5 border-t border-border/30 bg-muted/10">
					<p class="text-[8px] text-muted-foreground">Click for full profile · Double-click to re-center</p>
				</div>
			</div>
		</div>
	{/if}

	{#if selectedNode}
		<div class="border-t border-border px-4 py-3 bg-muted/20">
			<div class="flex items-start justify-between">
				<div class="flex items-center gap-2">
					<span class="text-lg">{nodeIcon(selectedNode)}</span>
					<div>
						<h4 class="text-sm font-medium text-foreground">{selectedNode.title}</h4>
						<div class="flex items-center gap-2 mt-0.5">
							{#if selectedNode.epistemic_type}
								<Badge text={selectedNode.epistemic_type} variant={
									selectedNode.epistemic_type === 'fact' ? 'success' :
									selectedNode.epistemic_type === 'observation' ? 'info' :
									selectedNode.epistemic_type === 'claim' ? 'medium' : 'neutral'
								} size="sm" />
							{/if}
							{#if selectedNode.entity_type}
								<Badge text={selectedNode.entity_type} variant="info" size="sm" />
							{/if}
							{#if selectedNode.source_authority}
								<Badge text={selectedNode.source_authority} variant={
									selectedNode.source_authority === 'verified' ? 'success' :
									selectedNode.source_authority === 'disputed' ? 'high' : 'neutral'
								} size="sm" />
							{/if}
							{#if selectedNode.mention_count}
								<span class="text-[10px] text-muted-foreground">{selectedNode.mention_count} mentions</span>
							{/if}
							{#if selectedNode.completeness_tier}
								<span class="text-[9px] px-1.5 py-0.5 rounded-full font-medium
									{selectedNode.completeness_tier === 'verified' ? 'bg-emerald-500/20 text-emerald-300' :
									 selectedNode.completeness_tier === 'solid' ? 'bg-blue-500/20 text-blue-300' :
									 selectedNode.completeness_tier === 'partial' ? 'bg-amber-500/20 text-amber-300' :
									 'bg-red-500/20 text-red-300'}">
									{selectedNode.completeness_tier.toUpperCase()} dossier
								</span>
							{/if}
						</div>
					</div>
				</div>
				<button onclick={() => selectedNode = null} class="text-muted-foreground hover:text-foreground text-xs">Close</button>
			</div>
			<!-- Dossier preview -->
			{#if selectedNode.aliases?.length}
				<div class="mt-1.5">
					<span class="text-[10px] text-muted-foreground">AKA: {selectedNode.aliases.slice(0, 4).join(', ')}</span>
				</div>
			{/if}
			{#if selectedNode.completeness_score !== undefined}
				<div class="flex items-center gap-2 mt-1.5">
					<div class="flex-1 h-1 bg-muted rounded-full overflow-hidden">
						<div class="h-full rounded-full transition-all
							{selectedNode.completeness_score >= 0.75 ? 'bg-emerald-400' :
							 selectedNode.completeness_score >= 0.5 ? 'bg-blue-400' :
							 selectedNode.completeness_score >= 0.25 ? 'bg-amber-400' : 'bg-red-400'}"
							style="width: {Math.round((selectedNode.completeness_score || 0) * 100)}%"></div>
					</div>
					<span class="text-[9px] text-muted-foreground">{Math.round((selectedNode.completeness_score || 0) * 100)}% complete</span>
				</div>
			{/if}
			<!-- Properties preview -->
			{#if selectedNode.properties?.length}
				<div class="mt-2 space-y-0.5">
					{#each selectedNode.properties.slice(0, 3) as prop}
						<div class="text-[10px] text-muted-foreground">
							<span class="text-foreground">{prop.key}:</span> {prop.value}
						</div>
					{/each}
				</div>
			{/if}
			<div class="flex items-center gap-2 mt-2">
				{#if onNodeExpand}
					<button onclick={() => onNodeExpand?.(selectedNode!)}
						class="text-[10px] px-2 py-1 rounded-md bg-primary/10 text-primary hover:bg-primary/20 transition-colors">Re-center here</button>
				{/if}
				{#if onNodeClick}
					<button onclick={() => onNodeClick?.(selectedNode!)}
						class="text-[10px] px-2 py-1 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">Open profile</button>
				{/if}
			</div>
		</div>
	{/if}
</div>
