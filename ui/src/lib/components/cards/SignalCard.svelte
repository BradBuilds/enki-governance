<script lang="ts">
	/**
	 * Universal Signal Card — renders ANY signal using its card recipe.
	 *
	 * The card recipe (from the card_templates system) tells us:
	 * - What media blocks to render (map, image, graph, gauge, table, email, video)
	 * - What fields to show (with path-based resolution + format hints)
	 * - What trackable fields exist (for sparklines from history)
	 * - What summary text to display
	 * - Whether there's an alert/watchlist match
	 *
	 * Fields use `path` to resolve values from the payload at render time.
	 * If history is provided, trackable fields show inline sparklines.
	 */
	import MapEmbed from './embeds/MapEmbed.svelte';
	import ImageEmbed from './embeds/ImageEmbed.svelte';
	import GraphEmbed from './embeds/GraphEmbed.svelte';
	import GaugeEmbed from './embeds/GaugeEmbed.svelte';
	import TableEmbed from './embeds/TableEmbed.svelte';
	import EmailEmbed from './embeds/EmailEmbed.svelte';
	import VideoEmbed from './embeds/VideoEmbed.svelte';
	import SparklineEmbed from './embeds/SparklineEmbed.svelte';
	import CardFields from './CardFields.svelte';
	import CardObservables from './CardObservables.svelte';
	import {
		resolvePath,
		extractTrackableSeries,
		extractTrackCoordinates,
		resolveTable,
	} from '$lib/utils/cardResolver';
	import type { TrackableSeries } from '$lib/utils/cardResolver';

	import Hash from 'lucide-svelte/icons/hash';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import ChevronUp from 'lucide-svelte/icons/chevron-up';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import FolderPlus from 'lucide-svelte/icons/folder-plus';
	import ShieldAlert from 'lucide-svelte/icons/shield-alert';

	// Dynamic icon mapping
	import Mail from 'lucide-svelte/icons/mail';
	import ImageIcon from 'lucide-svelte/icons/image';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Lock from 'lucide-svelte/icons/lock';
	import Thermometer from 'lucide-svelte/icons/thermometer';
	import DollarSign from 'lucide-svelte/icons/dollar-sign';
	import Globe from 'lucide-svelte/icons/globe';
	import Car from 'lucide-svelte/icons/car';
	import FileText from 'lucide-svelte/icons/file-text';
	import Video from 'lucide-svelte/icons/video';
	import Radar from 'lucide-svelte/icons/radar';
	import Activity from 'lucide-svelte/icons/activity';
	import Zap from 'lucide-svelte/icons/zap';
	import Smartphone from 'lucide-svelte/icons/smartphone';
	import Wifi from 'lucide-svelte/icons/wifi';
	import Cpu from 'lucide-svelte/icons/cpu';
	import HardDrive from 'lucide-svelte/icons/hard-drive';
	import Monitor from 'lucide-svelte/icons/monitor';
	import CheckCircle from 'lucide-svelte/icons/check-circle';

	const iconMap: Record<string, any> = {
		'mail': Mail, 'image': ImageIcon, 'map-pin': MapPin,
		'lock': Lock, 'shield-alert': ShieldAlert, 'thermometer': Thermometer,
		'dollar-sign': DollarSign, 'globe': Globe, 'car': Car,
		'file-text': FileText, 'video': Video, 'radar': Radar,
		'activity': Activity, 'zap': Zap, 'smartphone': Smartphone,
		'wifi': Wifi, 'cpu': Cpu, 'hard-drive': HardDrive,
		'monitor': Monitor, 'check-circle': CheckCircle,
	};

	const mediaComponents: Record<string, any> = {
		'map': MapEmbed,
		'image': ImageEmbed,
		'graph': GraphEmbed,
		'gauge': GaugeEmbed,
		'table': TableEmbed,
		'email': EmailEmbed,
		'video': VideoEmbed,
	};

	interface Props {
		signal: any;
		history?: any[];
		compact?: boolean;
		variant?: 'compact' | 'standard' | 'expanded';
	}

	let { signal, history = [], compact = false, variant }: Props = $props();

	// Map legacy compact prop to variant system
	const effectiveVariant = $derived(variant ?? (compact ? 'compact' : 'standard'));

	let expanded = $state(false);
	let viewMode = $state<'card' | 'raw'>('card');
	let showCaseModal = $state(false);
	let caseList = $state<any[]>([]);
	let selectedCaseId = $state('');
	let addingToCase = $state(false);

	async function openCaseModal() {
		showCaseModal = true;
		try {
			const res = await fetch('/api/cases', { headers: { 'Content-Type': 'application/json' } });
			if (res.ok) {
				const data = await res.json();
				caseList = (data.cases || []).filter((c: any) => c.status === 'open' || c.status === 'investigating');
			}
		} catch { caseList = []; }
	}

	async function addToCase() {
		if (!selectedCaseId || !signal?.observables?.length) return;
		addingToCase = true;
		try {
			for (const obs of signal.observables.slice(0, 5)) {
				await fetch(`/api/cases/${selectedCaseId}/observables`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						observable_type: obs.observable_type || obs.type,
						observable_value: obs.observable_value || obs.value,
						notes: `From signal ${signal.signal_id || ''}`,
					}),
				});
			}
		} catch {}
		addingToCase = false;
		showCaseModal = false;
	}

	// Auto-expand when variant is 'expanded'
	$effect(() => {
		if (effectiveVariant === 'expanded') expanded = true;
	});

	const recipe = $derived(signal?.metadata?.card_recipe || signal?.card_recipe || null);
	const payload = $derived(signal?.payload || {});
	const cardType = $derived(recipe?.card_type || 'generic');
	const title = $derived(recipe?.title || signal?.signal_type?.split('.').pop()?.replace(/_/g, ' ') || 'Signal');
	const icon = $derived(recipe?.icon || 'zap');
	const IconComponent = $derived(iconMap[icon] || Zap);
	const fields = $derived(recipe?.fields || []);
	const summary = $derived(recipe?.summary || '');
	const alert = $derived(recipe?.alert || null);
	const observables = $derived(signal?.observables || []);
	const cid = $derived(signal?.cid?.slice(0, 12) || '');
	const trackableConfig = $derived(recipe?.trackable || []);

	// Resolve media blocks — some need payload resolution (tables with source_field)
	const media = $derived.by(() => {
		const raw = recipe?.media || [];
		return raw.map((block: any) => {
			// For map media with lat_field/lon_field and history, build track coordinates
			if (block.type === 'map' && block.lat_field && block.lon_field && history?.length >= 2) {
				const track = extractTrackCoordinates(block.lat_field, block.lon_field, history);
				if (track.length >= 2) {
					return { ...block, track };
				}
			}
			// For table media with source_field, resolve from payload
			if (block.type === 'table' && block.source_field && !block.rows) {
				const resolved = resolveTable(payload, block.source_field, block.column_keys || []);
				return { ...block, headers: resolved.headers, rows: resolved.rows };
			}
			// For gauge media with source_field, resolve current value
			if (block.type === 'gauge' && block.source_field) {
				const val = resolvePath(payload, block.source_field);
				if (typeof val === 'number') return { ...block, value: val };
			}
			// For graph media with source_field, resolve data array
			if (block.type === 'graph' && block.source_field && !block.data) {
				const data = resolvePath(payload, block.source_field);
				if (Array.isArray(data)) return { ...block, data: data.slice(0, 50) };
			}
			return block;
		});
	});

	// Extract trackable sparkline series from history
	const trackableSeries = $derived.by((): TrackableSeries[] => {
		if (!trackableConfig.length || !history?.length) return [];
		return extractTrackableSeries(trackableConfig, history);
	});

	const severityColors: Record<string, string> = {
		critical: 'bg-severity-critical/10 border-severity-critical/30 text-severity-critical',
		high: 'bg-severity-high/10 border-severity-high/30 text-severity-high',
		medium: 'bg-severity-medium/10 border-severity-medium/30 text-severity-medium',
		low: 'bg-severity-low/10 border-severity-low/30 text-severity-low',
		informational: 'bg-muted border-border text-muted-foreground',
	};

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}
</script>

<div class="rounded-xl border border-border bg-card overflow-hidden hover:border-primary/20 transition-colors">
	<!-- Header -->
	<button
		onclick={() => expanded = !expanded}
		class="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-muted/30 transition-colors"
	>
		<div class="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
			<svelte:component this={IconComponent} class="w-4 h-4 text-primary" />
		</div>
		<div class="flex-1 min-w-0">
			<div class="flex items-center gap-2">
				<span class="text-sm font-medium text-foreground truncate">{title}</span>
				{#if signal?.severity && signal.severity !== 'informational'}
					<span class="text-[10px] px-1.5 py-0.5 rounded {severityColors[signal.severity] || ''}">{signal.severity}</span>
				{/if}
			</div>
			{#if effectiveVariant !== 'compact' && summary}
				<p class="text-xs text-muted-foreground truncate mt-0.5">{summary}</p>
			{/if}
		</div>
		<span class="text-[10px] text-muted-foreground shrink-0">{timeAgo(signal?.timestamp)}</span>
		{#if expanded}
			<ChevronUp class="w-4 h-4 text-muted-foreground shrink-0" />
		{:else}
			<ChevronDown class="w-4 h-4 text-muted-foreground shrink-0" />
		{/if}
	</button>

	<!-- Alert banner -->
	{#if alert}
		<div class="mx-4 mb-2 flex items-center gap-2 px-3 py-2 rounded-lg {severityColors[alert.severity] || 'bg-severity-high/10 border-severity-high/30 text-severity-high'} border text-xs">
			<ShieldAlert class="w-3.5 h-3.5 shrink-0" />
			<span>{alert.message}</span>
		</div>
	{/if}

	<!-- Expanded content -->
	{#if expanded}
		<div class="px-4 pb-4 space-y-3">
			<!-- View toggle: Card / Raw -->
			<div class="flex items-center gap-1 border-b border-border pb-2">
				<button
					onclick={() => viewMode = 'card'}
					class="px-3 py-1 rounded text-xs transition-colors {viewMode === 'card' ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground'}"
				>Card</button>
				<button
					onclick={() => viewMode = 'raw'}
					class="px-3 py-1 rounded text-xs transition-colors {viewMode === 'raw' ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground'}"
				>Raw JSON</button>
			</div>

			{#if viewMode === 'card'}
				<!-- Media blocks -->
				{#if media.length > 0}
					<div class="space-y-2">
						{#each media as block}
							{#if mediaComponents[block.type]}
								<svelte:component this={mediaComponents[block.type]} data={block} />
							{/if}
						{/each}
					</div>
				{/if}

				<!-- Fields with path resolution and sparklines -->
				{#if fields.length > 0}
					<CardFields {fields} {payload} {trackableSeries} signalType={signal?.signal_type || ''} />
				{/if}

				<!-- Trackable sparkline graphs (from history) -->
				{#if trackableSeries.length > 0}
					<div class="space-y-2">
						{#each trackableSeries as series}
							{#if series.values.length >= 2}
								<SparklineEmbed {series} />
							{/if}
						{/each}
					</div>
				{/if}

				<!-- Observables -->
				{#if observables.length > 0}
					<CardObservables {observables} />
				{/if}
			{:else}
				<!-- Raw JSON view -->
				{#if payload && Object.keys(payload).length > 0}
					<pre class="p-3 rounded-lg bg-muted text-muted-foreground overflow-x-auto text-[10px] font-mono leading-relaxed max-h-96">{JSON.stringify(payload, null, 2)}</pre>
				{/if}
				{#if signal?.metadata}
					<details class="text-xs">
						<summary class="text-muted-foreground cursor-pointer hover:text-foreground">Metadata</summary>
						<pre class="mt-2 p-3 rounded-lg bg-muted text-muted-foreground overflow-x-auto text-[10px] font-mono leading-relaxed max-h-48">{JSON.stringify(signal.metadata, null, 2)}</pre>
					</details>
				{/if}
			{/if}

			<!-- Action bar (always visible) -->
			<div class="flex items-center justify-between pt-2 border-t border-border">
				<div class="flex items-center gap-1 text-[10px] text-muted-foreground/50 font-mono">
					<Hash class="w-3 h-3" />
					{cid || 'no-cid'}
				</div>
				<div class="flex items-center gap-2">
					<button onclick={openCaseModal} class="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-primary transition-colors">
						<FolderPlus class="w-3 h-3" /> Add to Case
					</button>
					<a href="/chat?q=tell me about signal {signal?.signal_id || ''}" class="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-primary transition-colors">
						<MessageCircle class="w-3 h-3" /> Ask Enki
					</a>
				</div>
			</div>
		</div>
	{/if}
</div>

{#if showCaseModal}
	<div class="fixed inset-0 z-50 flex items-center justify-center p-4" onclick={() => showCaseModal = false}>
		<div class="absolute inset-0 bg-black/50"></div>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="relative bg-card border border-border rounded-xl p-4 w-full max-w-sm shadow-xl" onclick={(e) => e.stopPropagation()}>
			<h3 class="text-sm font-medium text-foreground mb-3">Add Observables to Case</h3>
			{#if signal?.observables?.length}
				<div class="mb-3 space-y-1">
					<p class="text-xs text-muted-foreground">Observables from this signal:</p>
					{#each signal.observables.slice(0, 5) as obs}
						<div class="text-xs font-mono text-foreground bg-muted px-2 py-1 rounded">
							{obs.observable_type || obs.type}: {obs.observable_value || obs.value}
						</div>
					{/each}
				</div>
			{:else}
				<p class="text-xs text-muted-foreground mb-3">No observables extracted from this signal.</p>
			{/if}
			<select bind:value={selectedCaseId} class="w-full px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm mb-3">
				<option value="">Select a case...</option>
				{#each caseList as c}
					<option value={c.case_id}>{c.title} ({c.status})</option>
				{/each}
			</select>
			<div class="flex gap-2">
				<button onclick={() => showCaseModal = false} class="flex-1 px-3 py-1.5 rounded-lg border border-border text-xs hover:bg-muted">Cancel</button>
				<button
					onclick={addToCase}
					disabled={!selectedCaseId || !signal?.observables?.length || addingToCase}
					class="flex-1 px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 disabled:opacity-40"
				>
					{addingToCase ? 'Adding...' : 'Add to Watchlist'}
				</button>
			</div>
		</div>
	</div>
{/if}
