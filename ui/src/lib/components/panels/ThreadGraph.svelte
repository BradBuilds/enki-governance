<script lang="ts">
	/**
	 * ThreadGraph — visual node manager for a chat thread.
	 *
	 * Features:
	 *  - Force-directed layout (connected nodes cluster together)
	 *  - Drag to rearrange, click to inspect
	 *  - Pin/dim/unlink controls per node
	 *  - Manual edge creation (shift+drag from node to node)
	 *  - Cross-thread node search and import
	 */

	import { onMount, onDestroy } from 'svelte';

	import Pin from 'lucide-svelte/icons/pin';
	import PinOff from 'lucide-svelte/icons/pin-off';
	import EyeOff from 'lucide-svelte/icons/eye-off';
	import Eye from 'lucide-svelte/icons/eye';
	import Unlink from 'lucide-svelte/icons/unlink';
	import Link from 'lucide-svelte/icons/link';
	import Network from 'lucide-svelte/icons/network';
	import X from 'lucide-svelte/icons/x';
	import Plus from 'lucide-svelte/icons/plus';
	import Search from 'lucide-svelte/icons/search';
	import ArrowRight from 'lucide-svelte/icons/arrow-right';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import NodeCard from '$lib/components/cards/NodeCard.svelte';
	import Workflow from 'lucide-svelte/icons/workflow';

	interface ThreadNode {
		node_id: string;
		title: string;
		epistemic_type: string;
		description: string;
		source_authority: string;
		latitude?: number;
		longitude?: number;
		concepts: string[];
		auto_generated: boolean;
		relevance: 'pinned' | 'active' | 'dimmed' | 'unlinked';
		user_note?: string;
		position_x?: number;
		position_y?: number;
		// Structured card fields
		entity_type?: string;
		entity_name?: string;
		entity_identifiers?: Record<string, string>;
		event_time?: string;
		event_end_time?: string;
		temporal_precision?: string;
		place_name?: string;
		place_country?: string;
		source_count?: number;
		source_types?: string[];
		confidence?: number;
		confidence_factors?: Record<string, any>;
		enrichment_status?: string;
		promotion_eligible?: boolean;
		// Contextual scoring (from thread_nodes)
		evidence_quality?: number;
		context_confidence?: number;
		context_status?: string;
		context_evidence?: Record<string, any>;
		context_contradictions?: number;
		context_corroborations?: number;
		context_gaps?: string[];
		// Force sim state
		vx?: number;
		vy?: number;
		fx?: number;
		fy?: number;
	}

	interface ThreadEdge {
		edge_id: string;
		source: string;
		target: string;
		relationship: string;
		strength: number;
	}

	interface Props {
		threadId: string;
		onClose: () => void;
	}

	let { threadId, onClose }: Props = $props();

	let nodes = $state<ThreadNode[]>([]);
	let edges = $state<ThreadEdge[]>([]);
	let loading = $state(true);
	let selectedNode = $state<string | null>(null);
	let showUnlinked = $state(false);

	// Modes
	let edgeDrawMode = $state(false);
	let fullNodeData = $state<ThreadNode | null>(null);
	let enriching = $state(false);
	let firstLoad = true;
	let searchMode = $state(false);
	let searchQuery = $state('');
	let searchResults = $state<ThreadNode[]>([]);
	let searchLoading = $state(false);

	// Edge drawing state
	let edgeSource = $state<string | null>(null);
	let edgeMousePos = $state({ x: 0, y: 0 });
	let edgeRelationship = $state('supports');

	// SVG viewport
	let svgEl: SVGSVGElement;
	let containerEl: HTMLDivElement;

	// Drag state
	let dragging = $state<string | null>(null);
	let dragOffset = { x: 0, y: 0 };
	let dragStartTime = 0;

	// Force simulation
	let simRunning = false;
	let simFrame: number;

	const epistemicColors: Record<string, string> = {
		fact: '#3b82f6',
		observation: '#10b981',
		claim: '#f59e0b',
		hypothesis: '#a855f7',
		question: '#ef4444',
		evidence: '#06b6d4',
		recommendation: '#ec4899',
		opinion: '#6b7280',
		definition: '#8b5cf6',
	};

	const relevanceStyles: Record<string, { opacity: number; stroke: string; dash: string }> = {
		pinned: { opacity: 1, stroke: '#f59e0b', dash: '' },
		active: { opacity: 1, stroke: 'none', dash: '' },
		dimmed: { opacity: 0.35, stroke: 'none', dash: '4,4' },
		unlinked: { opacity: 0.2, stroke: '#ef4444', dash: '2,6' },
	};

	const relationshipColors: Record<string, string> = {
		supports: '#10b981',
		contrasts: '#ef4444',
		defines: '#3b82f6',
		extends: '#8b5cf6',
		references: '#6b7280',
		supersedes: '#f59e0b',
		led_to: '#06b6d4',
		same_concept: '#a855f7',
		questions: '#ec4899',
	};

	const relationships = ['supports', 'contrasts', 'extends', 'references', 'led_to', 'questions', 'supersedes', 'same_concept'];

	onMount(() => {
		loadNodes();
	});

	onDestroy(() => {
		stopSim();
	});

	async function loadNodes() {
		loading = true;
		try {
			// Rescore on first load so contextual scores are fresh
			const params = new URLSearchParams();
			if (showUnlinked) params.set('include_unlinked', 'true');
			if (firstLoad) { params.set('rescore', 'true'); firstLoad = false; }
			const url = `/api/threads/${threadId}/nodes?${params}`;
			const res = await fetch(url);
			if (res.ok) {
				const data = await res.json();
				nodes = (data.nodes || []).map((n: ThreadNode) => ({ ...n, vx: 0, vy: 0 }));
				edges = data.edges || [];
				initLayout();
				startSim();
			}
		} catch { /* */ }
		loading = false;
	}

	async function rescoreAll() {
		try {
			await fetch(`/api/threads/${threadId}/rescore`, { method: 'POST' });
			await loadNodes();
		} catch { /* */ }
	}

	// ─── Force-Directed Layout ──────────────────────────────────────────

	function initLayout() {
		const cx = 200;
		const cy = 150;
		const radius = 100;
		nodes.forEach((node, i) => {
			if (node.position_x == null) {
				const angle = (2 * Math.PI * i) / Math.max(nodes.length, 1) + Math.random() * 0.3;
				node.position_x = cx + radius * Math.cos(angle) + (Math.random() - 0.5) * 20;
				node.position_y = cy + radius * Math.sin(angle) + (Math.random() - 0.5) * 20;
			}
			node.vx = 0;
			node.vy = 0;
		});
	}

	function startSim() {
		if (simRunning) return;
		simRunning = true;
		let ticks = 0;
		const maxTicks = 150;

		function step() {
			if (!simRunning || ticks > maxTicks) {
				simRunning = false;
				return;
			}
			ticks++;
			const alpha = 1 - ticks / maxTicks;
			simulateForces(alpha);
			nodes = [...nodes]; // trigger reactivity
			simFrame = requestAnimationFrame(step);
		}
		simFrame = requestAnimationFrame(step);
	}

	function stopSim() {
		simRunning = false;
		if (simFrame) cancelAnimationFrame(simFrame);
	}

	function simulateForces(alpha: number) {
		const strength = alpha * 0.3;
		const repulsion = 2500;
		const linkDistance = 90;
		const linkStrength = 0.15;
		const centerX = 200;
		const centerY = 150;
		const centerPull = 0.01;

		// Repulsion between all nodes
		for (let i = 0; i < nodes.length; i++) {
			for (let j = i + 1; j < nodes.length; j++) {
				const a = nodes[i];
				const b = nodes[j];
				let dx = (b.position_x || 0) - (a.position_x || 0);
				let dy = (b.position_y || 0) - (a.position_y || 0);
				let dist = Math.sqrt(dx * dx + dy * dy) || 1;
				let force = repulsion / (dist * dist);
				let fx = dx / dist * force * strength;
				let fy = dy / dist * force * strength;
				a.vx = (a.vx || 0) - fx;
				a.vy = (a.vy || 0) - fy;
				b.vx = (b.vx || 0) + fx;
				b.vy = (b.vy || 0) + fy;
			}
		}

		// Attraction along edges
		for (const edge of edges) {
			const src = nodes.find(n => n.node_id === edge.source);
			const tgt = nodes.find(n => n.node_id === edge.target);
			if (!src || !tgt) continue;
			let dx = (tgt.position_x || 0) - (src.position_x || 0);
			let dy = (tgt.position_y || 0) - (src.position_y || 0);
			let dist = Math.sqrt(dx * dx + dy * dy) || 1;
			let force = (dist - linkDistance) * linkStrength * strength;
			let fx = dx / dist * force;
			let fy = dy / dist * force;
			src.vx = (src.vx || 0) + fx;
			src.vy = (src.vy || 0) + fy;
			tgt.vx = (tgt.vx || 0) - fx;
			tgt.vy = (tgt.vy || 0) - fy;
		}

		// Center gravity
		for (const node of nodes) {
			node.vx = (node.vx || 0) + (centerX - (node.position_x || 0)) * centerPull;
			node.vy = (node.vy || 0) + (centerY - (node.position_y || 0)) * centerPull;
		}

		// Apply velocities with damping
		for (const node of nodes) {
			if (node.fx != null) {
				node.position_x = node.fx;
				node.position_y = node.fy;
				node.vx = 0;
				node.vy = 0;
				continue;
			}
			node.vx = (node.vx || 0) * 0.6;
			node.vy = (node.vy || 0) * 0.6;
			node.position_x = (node.position_x || 0) + (node.vx || 0);
			node.position_y = (node.position_y || 0) + (node.vy || 0);
			// Clamp to viewport
			node.position_x = Math.max(30, Math.min(370, node.position_x || 0));
			node.position_y = Math.max(30, Math.min(270, node.position_y || 0));
		}
	}

	// ─── Interactions ───────────────────────────────────────────────────

	async function setRelevance(nodeId: string, relevance: string, note?: string) {
		try {
			await fetch(`/api/threads/${threadId}/nodes/${nodeId}/relevance`, {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ relevance, user_note: note }),
			});
			const node = nodes.find(n => n.node_id === nodeId);
			if (node) {
				node.relevance = relevance as any;
				nodes = [...nodes];
			}
		} catch { /* */ }
	}

	function handleMouseDown(e: MouseEvent, nodeId: string) {
		if (e.button !== 0) return;
		e.preventDefault();
		dragStartTime = Date.now();

		if (edgeDrawMode) {
			edgeSource = nodeId;
			return;
		}

		dragging = nodeId;
		const node = nodes.find(n => n.node_id === nodeId);
		if (!node) return;
		const rect = svgEl.getBoundingClientRect();
		dragOffset.x = e.clientX - rect.left - (node.position_x || 0);
		dragOffset.y = e.clientY - rect.top - (node.position_y || 0);
		// Fix position during drag
		node.fx = node.position_x;
		node.fy = node.position_y;
		stopSim();
	}

	function handleMouseMove(e: MouseEvent) {
		if (edgeSource) {
			const rect = svgEl.getBoundingClientRect();
			edgeMousePos = { x: e.clientX - rect.left, y: e.clientY - rect.top };
			return;
		}
		if (!dragging) return;
		const node = nodes.find(n => n.node_id === dragging);
		if (!node) return;
		const rect = svgEl.getBoundingClientRect();
		node.position_x = e.clientX - rect.left - dragOffset.x;
		node.position_y = e.clientY - rect.top - dragOffset.y;
		node.fx = node.position_x;
		node.fy = node.position_y;
		nodes = [...nodes];
	}

	async function handleMouseUp(e: MouseEvent) {
		if (edgeSource) {
			// Check if we're over a target node
			const rect = svgEl.getBoundingClientRect();
			const mx = e.clientX - rect.left;
			const my = e.clientY - rect.top;
			const target = visibleNodes.find(n => {
				const dx = (n.position_x || 0) - mx;
				const dy = (n.position_y || 0) - my;
				return Math.sqrt(dx * dx + dy * dy) < 25 && n.node_id !== edgeSource;
			});
			if (target) {
				await createEdge(edgeSource, target.node_id);
			}
			edgeSource = null;
			return;
		}

		if (!dragging) return;
		const node = nodes.find(n => n.node_id === dragging);
		const wasClick = Date.now() - dragStartTime < 200;
		if (node) {
			node.fx = undefined;
			node.fy = undefined;
			if (!wasClick && node.position_x != null && node.position_y != null) {
				try {
					await fetch(`/api/threads/${threadId}/nodes/${node.node_id}/position`, {
						method: 'PUT',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({ position_x: node.position_x, position_y: node.position_y }),
					});
				} catch { /* */ }
			}
		}
		if (wasClick && dragging) {
			toggleSelect(dragging);
		}
		dragging = null;
	}

	// ─── Manual Edge Creation ───────────────────────────────────────────

	async function createEdge(sourceId: string, targetId: string) {
		try {
			const res = await fetch(`/api/knowledge/edges`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					source_node_id: sourceId,
					target_node_id: targetId,
					relationship: edgeRelationship,
					strength: 0.8,
				}),
			});
			if (res.ok) {
				const data = await res.json();
				edges = [...edges, {
					edge_id: data.edge_id || `${sourceId}-${targetId}`,
					source: sourceId,
					target: targetId,
					relationship: edgeRelationship,
					strength: 0.8,
				}];
			}
		} catch { /* */ }
		edgeDrawMode = false;
	}

	// ─── Cross-Thread Node Search ───────────────────────────────────────

	async function searchNodes() {
		if (!searchQuery.trim()) return;
		searchLoading = true;
		try {
			const res = await fetch(`/api/knowledge/nodes/search?q=${encodeURIComponent(searchQuery)}&limit=10`);
			if (res.ok) {
				const data = await res.json();
				// Filter out nodes already in this thread
				const existingIds = new Set(nodes.map(n => n.node_id));
				searchResults = (data.nodes || []).filter((n: any) => !existingIds.has(n.node_id));
			}
		} catch { /* */ }
		searchLoading = false;
	}

	async function importNode(nodeId: string) {
		try {
			await fetch(`/api/threads/${threadId}/nodes/${nodeId}/link`, { method: 'POST' });
			searchResults = searchResults.filter(n => n.node_id !== nodeId);
			await loadNodes();
		} catch { /* */ }
	}

	// ─── Helpers ────────────────────────────────────────────────────────

	function getNodePos(nodeId: string): { x: number; y: number } {
		const n = nodes.find(n => n.node_id === nodeId);
		return { x: n?.position_x || 0, y: n?.position_y || 0 };
	}

	function toggleSelect(nodeId: string) {
		if (selectedNode === nodeId) {
			selectedNode = null;
			fullNodeData = null;
		} else {
			selectedNode = nodeId;
			fetchFullNode(nodeId);
		}
	}

	async function fetchFullNode(nodeId: string) {
		try {
			const res = await fetch(`/api/knowledge/nodes/${nodeId}`);
			if (res.ok) {
				const data = await res.json();
				fullNodeData = data;
			}
		} catch { fullNodeData = null; }
	}

	async function enrichNode(nodeId: string) {
		enriching = true;
		try {
			const res = await fetch(`/api/knowledge/nodes/${nodeId}/enrich`, { method: 'POST' });
			if (res.ok) {
				await fetchFullNode(nodeId);
				await loadNodes();
			}
		} catch { /* */ }
		enriching = false;
	}

	let prevShowUnlinked = showUnlinked;
	$effect(() => {
		if (showUnlinked !== prevShowUnlinked) {
			prevShowUnlinked = showUnlinked;
			loadNodes();
		}
	});

	const visibleNodes = $derived(
		showUnlinked ? nodes : nodes.filter(n => n.relevance !== 'unlinked')
	);
	const selectedNodeData = $derived(nodes.find(n => n.node_id === selectedNode));
	const pinnedCount = $derived(nodes.filter(n => n.relevance === 'pinned').length);
</script>

<div class="flex flex-col h-full bg-card" bind:this={containerEl}>
	<!-- Header -->
	<div class="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
		<div class="flex items-center gap-2">
			<Network size={14} class="text-primary" />
			<span class="text-xs font-medium text-foreground">Thread Graph</span>
			<span class="text-[10px] text-muted-foreground">{visibleNodes.length} nodes</span>
			{#if pinnedCount > 0}
				<span class="text-[10px] text-amber-400">{pinnedCount} pinned</span>
			{/if}
		</div>
		<div class="flex items-center gap-1">
			<!-- Import nodes from other threads -->
			<button
				onclick={() => { searchMode = !searchMode; edgeDrawMode = false; }}
				class="p-1 rounded hover:bg-muted transition-colors {searchMode ? 'text-primary bg-primary/10' : 'text-muted-foreground'}"
				title="Search & import nodes from other threads"
			>
				<Plus size={14} />
			</button>
			<!-- Draw edges -->
			<button
				onclick={() => { edgeDrawMode = !edgeDrawMode; searchMode = false; edgeSource = null; }}
				class="p-1 rounded hover:bg-muted transition-colors {edgeDrawMode ? 'text-primary bg-primary/10' : 'text-muted-foreground'}"
				title={edgeDrawMode ? 'Cancel edge drawing' : 'Draw edge between nodes'}
			>
				<Workflow size={14} />
			</button>
			<button
				onclick={() => showUnlinked = !showUnlinked}
				class="p-1 rounded hover:bg-muted text-muted-foreground"
				title={showUnlinked ? 'Hide unlinked' : 'Show unlinked'}
			>
				{#if showUnlinked}
					<Eye size={14} />
				{:else}
					<EyeOff size={14} />
				{/if}
			</button>
			<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground">
				<X size={14} />
			</button>
		</div>
	</div>

	<!-- Mode bars -->
	{#if edgeDrawMode}
		<div class="flex items-center gap-2 px-3 py-1.5 bg-primary/5 border-b border-primary/20 shrink-0">
			<Workflow size={12} class="text-primary" />
			<span class="text-[10px] text-primary">Drag from one node to another to create an edge</span>
			<select
				bind:value={edgeRelationship}
				class="ml-auto text-[10px] bg-transparent border border-primary/30 rounded px-1 py-0.5 text-primary"
			>
				{#each relationships as rel}
					<option value={rel}>{rel}</option>
				{/each}
			</select>
		</div>
	{/if}

	{#if searchMode}
		<div class="px-3 py-2 bg-muted/30 border-b border-border shrink-0 space-y-2">
			<div class="flex items-center gap-2">
				<Search size={12} class="text-muted-foreground shrink-0" />
				<input
					bind:value={searchQuery}
					onkeydown={(e) => { if (e.key === 'Enter') searchNodes(); }}
					placeholder="Search nodes across all threads..."
					class="flex-1 bg-transparent text-xs text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
				/>
				<button
					onclick={searchNodes}
					disabled={searchLoading}
					class="text-[10px] px-2 py-0.5 rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
				>
					{searchLoading ? '...' : 'Search'}
				</button>
			</div>
			{#if searchResults.length > 0}
				<div class="space-y-1 max-h-[120px] overflow-y-auto">
					{#each searchResults as result}
						<div class="flex items-center justify-between gap-2 px-2 py-1 rounded bg-card border border-border/50">
							<div class="min-w-0">
								<div class="text-[11px] text-foreground truncate">{result.title}</div>
								<div class="text-[9px] text-muted-foreground">{result.epistemic_type}</div>
							</div>
							<button
								onclick={() => importNode(result.node_id)}
								class="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary hover:bg-primary/20"
							>
								Import
							</button>
						</div>
					{/each}
				</div>
			{:else if searchQuery && !searchLoading}
				<p class="text-[10px] text-muted-foreground">No results found</p>
			{/if}
		</div>
	{/if}

	{#if loading}
		<div class="flex-1 flex items-center justify-center">
			<span class="text-sm text-muted-foreground">Loading graph...</span>
		</div>
	{:else if nodes.length === 0}
		<div class="flex-1 flex flex-col items-center justify-center p-4 text-center">
			<Network size={32} class="text-muted-foreground/20 mb-2" />
			<p class="text-sm text-muted-foreground">No knowledge nodes yet</p>
			<p class="text-[11px] text-muted-foreground/60 mt-1">Nodes are created as the AI researches in this thread</p>
		</div>
	{:else}
		<!-- Graph area -->
		<div class="flex-1 overflow-hidden relative">
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<svg
				bind:this={svgEl}
				class="w-full h-full {edgeDrawMode ? 'cursor-crosshair' : ''}"
				onmousemove={handleMouseMove}
				onmouseup={handleMouseUp}
				onmouseleave={() => { if (edgeSource) edgeSource = null; if (dragging) { const n = nodes.find(n => n.node_id === dragging); if (n) { n.fx = undefined; n.fy = undefined; } dragging = null; } }}
			>
				<!-- Edges -->
				{#each edges as edge}
					{@const src = getNodePos(edge.source)}
					{@const tgt = getNodePos(edge.target)}
					{@const color = relationshipColors[edge.relationship] || '#6b7280'}
					<!-- Arrow line -->
					<line
						x1={src.x} y1={src.y}
						x2={tgt.x} y2={tgt.y}
						stroke={color} stroke-width="1.5"
						stroke-opacity="0.4"
						stroke-dasharray={edge.relationship === 'contrasts' ? '6,3' : ''}
						marker-end="url(#arrow-{edge.relationship})"
					/>
					<!-- Edge label -->
					<text
						x={(src.x + tgt.x) / 2} y={(src.y + tgt.y) / 2 - 5}
						text-anchor="middle" class="text-[7px] select-none pointer-events-none" fill={color} fill-opacity="0.5"
					>
						{edge.relationship}
					</text>
				{/each}

				<!-- Edge drawing preview line -->
				{#if edgeSource}
					{@const srcPos = getNodePos(edgeSource)}
					<line
						x1={srcPos.x} y1={srcPos.y}
						x2={edgeMousePos.x} y2={edgeMousePos.y}
						stroke="hsl(var(--primary))" stroke-width="2"
						stroke-dasharray="4,4" stroke-opacity="0.6"
					/>
				{/if}

				<!-- Arrow marker defs -->
				<defs>
					{#each Object.entries(relationshipColors) as [rel, color]}
						<marker id="arrow-{rel}" viewBox="0 0 10 10" refX="22" refY="5" markerWidth="6" markerHeight="6" orient="auto">
							<path d="M 0 0 L 10 5 L 0 10 z" fill={color} fill-opacity="0.4" />
						</marker>
					{/each}
				</defs>

				<!-- Nodes -->
				{#each visibleNodes as node}
					{@const x = node.position_x || 0}
					{@const y = node.position_y || 0}
					{@const color = epistemicColors[node.epistemic_type] || '#6b7280'}
					{@const style = relevanceStyles[node.relevance] || relevanceStyles.active}
					{@const isSelected = selectedNode === node.node_id}
					{@const r = isSelected ? 26 : 20}
					{@const ctxConf = node.context_confidence ?? node.evidence_quality ?? 0.5}
					{@const ctxStatus = node.context_status || 'unscored'}
					{@const statusColor = ctxStatus === 'confirmed' ? '#10b981' : ctxStatus === 'supported' ? '#3b82f6' : ctxStatus === 'contested' ? '#ef4444' : ctxStatus === 'stale' ? '#6b7280' : '#f59e0b'}

					<!-- svelte-ignore a11y_no_static_element_interactions -->
					<g
						opacity={style.opacity}
						class="{edgeDrawMode ? 'cursor-crosshair' : 'cursor-grab active:cursor-grabbing'}"
						onmousedown={(e) => handleMouseDown(e, node.node_id)}
					>
						<!-- Confidence arc (shows contextual confidence as partial ring) -->
						{#if ctxConf > 0}
							{@const arcAngle = ctxConf * 2 * Math.PI}
							{@const arcR = r + 2}
							{@const ax = x + arcR * Math.sin(arcAngle)}
							{@const ay = y - arcR * Math.cos(arcAngle)}
							<path
								d="M {x} {y - arcR} A {arcR} {arcR} 0 {arcAngle > Math.PI ? 1 : 0} 1 {ax} {ay}"
								fill="none" stroke={statusColor} stroke-width="2" stroke-opacity="0.5"
								stroke-linecap="round"
							/>
						{/if}

						<!-- Pinned glow -->
						{#if node.relevance === 'pinned'}
							<circle cx={x} cy={y} r={r + 5} fill="none" stroke="#f59e0b" stroke-width="1.5" stroke-opacity="0.25">
								<animate attributeName="r" values="{r + 4};{r + 7};{r + 4}" dur="3s" repeatCount="indefinite" />
							</circle>
						{/if}

						<!-- Selection ring -->
						{#if isSelected}
							<circle cx={x} cy={y} r={r + 3} fill="none" stroke="hsl(var(--primary))" stroke-width="2" stroke-opacity="0.5" />
						{/if}

						<!-- Contested warning ring -->
						{#if ctxStatus === 'contested'}
							<circle cx={x} cy={y} r={r + 1} fill="none" stroke="#ef4444" stroke-width="1" stroke-dasharray="3,3" stroke-opacity="0.6" />
						{/if}

						<!-- Node circle -->
						<circle
							cx={x} cy={y} r={r}
							fill={color} fill-opacity="0.12"
							stroke={style.stroke !== 'none' ? style.stroke : color}
							stroke-width="1.5"
							stroke-dasharray={style.dash}
						/>

						<!-- Epistemic type letter -->
						<text
							x={x} y={y + 1}
							text-anchor="middle" dominant-baseline="middle"
							class="text-[10px] font-bold select-none pointer-events-none"
							fill={color}
						>
							{node.epistemic_type[0].toUpperCase()}
						</text>

						<!-- Status indicator dot (bottom-right) -->
						<circle cx={x + r * 0.6} cy={y + r * 0.6} r="3" fill={statusColor} stroke="hsl(var(--card))" stroke-width="1" />

						<!-- Title label -->
						<text
							x={x} y={y + r + 11}
							text-anchor="middle"
							class="text-[8px] select-none pointer-events-none"
							fill="currentColor" fill-opacity="0.6"
						>
							{node.title.length > 28 ? node.title.slice(0, 25) + '...' : node.title}
						</text>
					</g>
				{/each}
			</svg>
		</div>

		<!-- Selected node detail panel -->
		{#if selectedNodeData}
			<div class="border-t border-border shrink-0 max-h-[250px] overflow-y-auto">
				<!-- Close + enrich bar -->
				<div class="flex items-center justify-between px-3 py-1.5 bg-muted/30 border-b border-border/30 sticky top-0">
					<div class="flex items-center gap-1">
						<!-- Relevance controls -->
						<button
							onclick={() => setRelevance(selectedNodeData!.node_id, selectedNodeData!.relevance === 'pinned' ? 'active' : 'pinned')}
							class="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] transition-colors
								{selectedNodeData.relevance === 'pinned' ? 'bg-amber-500/20 text-amber-400' : 'hover:bg-muted text-muted-foreground'}"
						>
							{#if selectedNodeData.relevance === 'pinned'}<PinOff size={10} /> Unpin{:else}<Pin size={10} /> Pin{/if}
						</button>
						<button
							onclick={() => setRelevance(selectedNodeData!.node_id, selectedNodeData!.relevance === 'dimmed' ? 'active' : 'dimmed')}
							class="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] transition-colors hover:bg-muted text-muted-foreground"
						>
							{#if selectedNodeData.relevance === 'dimmed'}<Eye size={10} /> Restore{:else}<EyeOff size={10} /> Dim{/if}
						</button>
						<button
							onclick={() => setRelevance(selectedNodeData!.node_id, selectedNodeData!.relevance === 'unlinked' ? 'active' : 'unlinked')}
							class="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] transition-colors
								{selectedNodeData.relevance === 'unlinked' ? 'text-red-400' : 'hover:bg-muted text-muted-foreground'}"
						>
							{#if selectedNodeData.relevance === 'unlinked'}<Link size={10} /> Relink{:else}<Unlink size={10} /> Unlink{/if}
						</button>
					</div>
					<div class="flex items-center gap-1">
						<button
							onclick={() => enrichNode(selectedNodeData!.node_id)}
							disabled={enriching}
							class="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-primary/10 text-primary hover:bg-primary/20 transition-colors disabled:opacity-40"
							title="Run L→D→U enrichment pipeline"
						>
							<RefreshCw size={10} class={enriching ? 'animate-spin' : ''} />
							{enriching ? 'Enriching...' : 'Enrich'}
						</button>
						<button onclick={() => { selectedNode = null; fullNodeData = null; }} class="p-0.5 rounded hover:bg-muted text-muted-foreground">
							<X size={12} />
						</button>
					</div>
				</div>
				<!-- Rich node card -->
				<div class="p-2">
					<NodeCard node={fullNodeData || selectedNodeData} />
				</div>
			</div>
		{/if}

		<!-- Legend -->
		<div class="border-t border-border/50 px-3 py-1 shrink-0 flex items-center gap-3 flex-wrap">
			{#each Object.entries(epistemicColors).slice(0, 6) as [type, color]}
				<div class="flex items-center gap-1">
					<div class="w-1.5 h-1.5 rounded-full" style="background: {color}"></div>
					<span class="text-[7px] text-muted-foreground">{type}</span>
				</div>
			{/each}
		</div>
	{/if}
</div>
