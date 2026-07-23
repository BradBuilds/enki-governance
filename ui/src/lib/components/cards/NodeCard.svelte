<script lang="ts">
	/**
	 * NodeCard — structured intelligence card for a knowledge node.
	 *
	 * Shows the full enriched card: entity, location, temporal, sources,
	 * contradictions, corroborations, gaps, confidence meter.
	 */

	import { goto } from '$app/navigation';
	import NodeChip from './NodeChip.svelte';
	import {
		getEntityChips,
		getTopicChips,
		getPlaceChips,
		getMediaChips,
		type EnvelopeSummary
	} from '$lib/cards/envelope';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Clock from 'lucide-svelte/icons/clock';
	import User from 'lucide-svelte/icons/user';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import CheckCircle from 'lucide-svelte/icons/check-circle';
	import HelpCircle from 'lucide-svelte/icons/help-circle';
	import Shield from 'lucide-svelte/icons/shield';
	import Database from 'lucide-svelte/icons/database';
	import Loader from 'lucide-svelte/icons/loader';
	import Zap from 'lucide-svelte/icons/zap';
	import Network from 'lucide-svelte/icons/network';

	interface NodeData {
		node_id: string;
		title: string;
		description?: string;
		epistemic_type: string;
		source_authority: string;
		// Phase 3.4 — envelope-first contract. When present, chip navigation
		// reads from here (entity/place/topic node_ids for the whole card).
		envelope_summary?: EnvelopeSummary | null;
		// Legacy flat fields — kept for back-compat; populated by feed
		// endpoints as aliases of envelope_summary's primary entity/place.
		entity_type?: string;
		entity_name?: string;
		entity_identifiers?: Record<string, string>;
		event_time?: string;
		event_end_time?: string;
		temporal_precision?: string;
		place_name?: string;
		place_country?: string;
		latitude?: number;
		longitude?: number;
		source_count?: number;
		source_types?: string[];
		confidence?: number;
		confidence_factors?: Record<string, any>;
		gaps?: string[];
		gap_count?: number;
		enrichment_status?: string;
		promotion_eligible?: boolean;
		concepts?: string[];
		auto_generated?: boolean;
		// Contextual scoring (from thread_nodes/case_nodes)
		evidence_quality?: number;
		context_confidence?: number;
		context_status?: string;
		context_evidence?: Record<string, any>;
		context_contradictions?: number;
		context_corroborations?: number;
		context_gaps?: string[];
	}

	interface Props {
		node: NodeData;
		compact?: boolean;
		/** Make the title link to /nodes/[node_id]. Default true. Set false
		 * when this card is rendered inside an outer clickable surface. */
		navigable?: boolean;
	}

	let { node, compact = false, navigable = true }: Props = $props();

	function openDetail(e: MouseEvent) {
		if (!navigable || !node.node_id) return;
		e.stopPropagation();
		goto(`/nodes/${encodeURIComponent(node.node_id)}`);
	}

	const epistemicColors: Record<string, { bg: string; text: string; border: string }> = {
		fact: { bg: 'bg-blue-500/10', text: 'text-blue-400', border: 'border-blue-500/30' },
		observation: { bg: 'bg-emerald-500/10', text: 'text-emerald-400', border: 'border-emerald-500/30' },
		claim: { bg: 'bg-amber-500/10', text: 'text-amber-400', border: 'border-amber-500/30' },
		hypothesis: { bg: 'bg-purple-500/10', text: 'text-purple-400', border: 'border-purple-500/30' },
		question: { bg: 'bg-red-500/10', text: 'text-red-400', border: 'border-red-500/30' },
		evidence: { bg: 'bg-cyan-500/10', text: 'text-cyan-400', border: 'border-cyan-500/30' },
	};

	const statusStyles: Record<string, { color: string; label: string }> = {
		confirmed: { color: 'text-emerald-400', label: 'CONFIRMED' },
		supported: { color: 'text-blue-400', label: 'SUPPORTED' },
		contested: { color: 'text-red-400', label: 'CONTESTED' },
		unverified: { color: 'text-amber-400', label: 'UNVERIFIED' },
		stale: { color: 'text-muted-foreground', label: 'STALE' },
		unscored: { color: 'text-muted-foreground/50', label: 'UNSCORED' },
	};

	const colors = $derived(epistemicColors[node.epistemic_type] || epistemicColors.observation);

	// Use contextual confidence if available, fall back to baseline evidence quality
	const displayConfidence = $derived(node.context_confidence ?? node.evidence_quality ?? node.confidence ?? 0.5);
	const ctxStatus = $derived(node.context_status || 'unscored');
	const statusStyle = $derived(statusStyles[ctxStatus] || statusStyles.unscored);

	const confidenceBarColor = $derived(
		ctxStatus === 'confirmed' ? 'bg-emerald-400' :
		ctxStatus === 'supported' ? 'bg-blue-400' :
		ctxStatus === 'contested' ? 'bg-red-400' :
		ctxStatus === 'stale' ? 'bg-muted-foreground/50' :
		displayConfidence >= 0.7 ? 'bg-emerald-400' :
		displayConfidence >= 0.4 ? 'bg-amber-400' : 'bg-red-400'
	);

	const isEnriching = $derived(node.enrichment_status === 'enriching' || node.enrichment_status === 'pending');

	// Gaps: use contextual if available, fall back to intrinsic
	const displayGaps = $derived(node.context_gaps?.length ? node.context_gaps : node.gaps);
	const displayContradictions = $derived(node.context_contradictions ?? 0);
	const displayCorroborations = $derived(node.context_corroborations ?? 0);

	// Phase 3.4 — envelope-first chip rows. Each chip is a navigable
	// NodeChip linking to /nodes/[node_id]. Empty arrays when envelope
	// summary is absent; the legacy entity_name / place_name fallback
	// renders instead.
	const entityChips = $derived(getEntityChips(node.envelope_summary));
	const topicChips  = $derived(getTopicChips(node.envelope_summary));
	const placeChips  = $derived(getPlaceChips(node.envelope_summary));
	const mediaChips  = $derived(getMediaChips(node.envelope_summary));
</script>

<div class="rounded-lg border {colors.border} {colors.bg} overflow-hidden">
	<!-- Header -->
	<div class="px-3 py-2 flex items-start justify-between gap-2">
		<div class="min-w-0 flex-1">
			<div class="flex items-center gap-1.5 mb-0.5">
				<span class="text-[10px] px-1.5 py-0.5 rounded font-medium {colors.text} {colors.bg} border {colors.border}">
					{node.epistemic_type}
				</span>
				{#if node.promotion_eligible}
					<span class="text-[9px] px-1 py-0.5 rounded bg-emerald-500/20 text-emerald-400" title="Eligible for promotion">
						↑ promote
					</span>
				{/if}
				{#if isEnriching}
					<Loader size={10} class="text-muted-foreground animate-spin" />
				{/if}
			</div>
			{#if navigable && node.node_id}
				<button
					type="button"
					onclick={openDetail}
					class="text-sm font-medium text-foreground leading-tight text-left hover:underline"
				>{node.title}</button>
			{:else}
				<div class="text-sm font-medium text-foreground leading-tight">{node.title}</div>
			{/if}
		</div>
		<!-- Status + Confidence -->
		<div class="shrink-0 text-right">
			<div class="text-[9px] font-semibold {statusStyle.color}">{statusStyle.label}</div>
			<div class="text-[10px] text-muted-foreground">{Math.round(displayConfidence * 100)}%</div>
			<div class="w-10 h-1 rounded-full bg-muted mt-0.5 overflow-hidden">
				<div class="{confidenceBarColor} h-full rounded-full transition-all" style="width: {displayConfidence * 100}%"></div>
			</div>
		</div>
	</div>

	{#if !compact}
		<div class="px-3 pb-2 space-y-1.5">
			<!-- Description -->
			{#if node.description}
				<p class="text-[11px] text-muted-foreground leading-relaxed">{node.description}</p>
			{/if}

			<!-- Entities -->
			{#if entityChips.length > 0}
				<div class="flex items-start gap-1.5 text-[11px]">
					<User size={11} class="text-muted-foreground shrink-0 mt-0.5" />
					<div class="flex flex-wrap gap-1 min-w-0">
						{#each entityChips.slice(0, 8) as chip (chip.node_id)}
							<NodeChip
								node_id={chip.node_id}
								label={chip.label}
								node_type="entity"
								size="sm"
								title={chip.role ? `role: ${chip.role}` : undefined}
							/>
						{/each}
						{#if entityChips.length > 8}
							<span class="text-[10px] text-muted-foreground self-center">+{entityChips.length - 8}</span>
						{/if}
					</div>
				</div>
			{:else if node.entity_name}
				<!-- Legacy fallback when envelope_summary is absent -->
				<div class="flex items-center gap-1.5 text-[11px]">
					<User size={11} class="text-muted-foreground shrink-0" />
					<span class="text-foreground font-medium">{node.entity_name}</span>
					{#if node.entity_type && node.entity_type !== 'unknown'}
						<span class="text-muted-foreground">({node.entity_type})</span>
					{/if}
					{#if node.entity_identifiers}
						{#each Object.entries(node.entity_identifiers).slice(0, 3) as [key, val]}
							<span class="text-[9px] px-1 py-0.5 rounded bg-muted text-muted-foreground font-mono">{key}: {val}</span>
						{/each}
					{/if}
				</div>
			{/if}

			<!-- Places -->
			{#if placeChips.length > 0}
				<div class="flex items-start gap-1.5 text-[11px]">
					<MapPin size={11} class="text-muted-foreground shrink-0 mt-0.5" />
					<div class="flex flex-wrap gap-1 min-w-0">
						{#each placeChips as chip (chip.node_id)}
							<NodeChip
								node_id={chip.node_id}
								label={chip.label}
								node_type="place"
								size="sm"
							/>
						{/each}
					</div>
				</div>
			{:else if node.place_name || node.latitude}
				<!-- Legacy fallback when envelope_summary has no resolved_places -->
				<div class="flex items-center gap-1.5 text-[11px]">
					<MapPin size={11} class="text-muted-foreground shrink-0" />
					<span class="text-foreground">{node.place_name || `${node.latitude?.toFixed(3)}, ${node.longitude?.toFixed(3)}`}</span>
					{#if node.place_country}
						<span class="text-muted-foreground">{node.place_country}</span>
					{/if}
				</div>
			{/if}

			<!-- Temporal -->
			{#if node.event_time}
				<div class="flex items-center gap-1.5 text-[11px]">
					<Clock size={11} class="text-muted-foreground shrink-0" />
					<span class="text-foreground">
						{new Date(node.event_time).toLocaleDateString()} {new Date(node.event_time).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}
					</span>
					{#if node.event_end_time}
						<span class="text-muted-foreground">→ {new Date(node.event_end_time).toLocaleDateString()}</span>
					{/if}
					{#if node.temporal_precision && node.temporal_precision !== 'unknown'}
						<span class="text-[9px] text-muted-foreground/60">±{node.temporal_precision}</span>
					{/if}
				</div>
			{/if}

			<!-- Sources -->
			<div class="flex items-center gap-1.5 text-[11px]">
				<Database size={11} class="text-muted-foreground shrink-0" />
				<span class="text-foreground">{node.source_count || 0} source{(node.source_count || 0) !== 1 ? 's' : ''}</span>
				{#if node.source_types?.length}
					{#each node.source_types.slice(0, 3) as st}
						<span class="text-[9px] px-1 py-0.5 rounded bg-muted text-muted-foreground">{st.split('.').pop()}</span>
					{/each}
				{/if}
				<span class="text-[9px] text-muted-foreground">
					· {node.source_authority}
				</span>
			</div>

			<!-- Corroborations (contextual) -->
			{#if displayCorroborations > 0}
				<div class="flex items-center gap-1.5 text-[11px]">
					<CheckCircle size={11} class="text-emerald-400 shrink-0" />
					<span class="text-emerald-400">{displayCorroborations} corroboration{displayCorroborations !== 1 ? 's' : ''} in context</span>
				</div>
			{/if}

			<!-- Contradictions (contextual) -->
			{#if displayContradictions > 0}
				<div class="flex items-center gap-1.5 text-[11px]">
					<AlertTriangle size={11} class="text-red-400 shrink-0" />
					<span class="text-red-400">{displayContradictions} contradiction{displayContradictions !== 1 ? 's' : ''} in context</span>
				</div>
			{/if}

			<!-- Gaps (contextual if available, else intrinsic) -->
			{#if displayGaps?.length}
				<div class="mt-1 space-y-0.5">
					{#each displayGaps.slice(0, 4) as gap}
						<div class="flex items-start gap-1.5 text-[10px] text-muted-foreground/70">
							<HelpCircle size={10} class="shrink-0 mt-0.5" />
							<span>{gap}</span>
						</div>
					{/each}
				</div>
			{/if}

			<!-- Media thumbnails (envelope chips) -->
			{#if mediaChips.length > 0}
				<div class="flex flex-wrap gap-1 pt-0.5">
					{#each mediaChips.slice(0, 6) as chip (chip.node_id)}
						<NodeChip
							node_id={chip.node_id}
							label={chip.label}
							node_type="media"
							size="sm"
							thumbnail_url={chip.thumbnail_url}
							title={chip.role ? `role: ${chip.role}` : undefined}
						/>
					{/each}
					{#if mediaChips.length > 6}
						<span class="text-[10px] text-muted-foreground self-center">+{mediaChips.length - 6}</span>
					{/if}
				</div>
			{/if}

			<!-- Topics (envelope chips when available; legacy strings otherwise) -->
			{#if topicChips.length > 0}
				<div class="flex flex-wrap gap-1 pt-0.5">
					{#each topicChips.slice(0, 6) as chip (chip.node_id)}
						<NodeChip
							node_id={chip.node_id}
							label={chip.label}
							node_type="topic"
							size="sm"
						/>
					{/each}
					{#if topicChips.length > 6}
						<span class="text-[10px] text-muted-foreground self-center">+{topicChips.length - 6}</span>
					{/if}
				</div>
			{:else if node.concepts?.length}
				<div class="flex flex-wrap gap-1 pt-0.5">
					{#each node.concepts.slice(0, 6) as concept}
						<span class="text-[9px] px-1.5 py-0.5 rounded bg-muted/80 text-muted-foreground">{concept}</span>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</div>
