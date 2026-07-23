<script lang="ts">
	import { LIVE_SOURCES, getNestedValue } from './live/sources';
	import { screen } from '$lib/stores/screen.svelte';
	import { swipeDismiss } from '$lib/actions/swipeDismiss';
	import X from 'lucide-svelte/icons/x';
	import Navigation from 'lucide-svelte/icons/navigation';
	import FolderPlus from 'lucide-svelte/icons/folder-plus';
	import Eye from 'lucide-svelte/icons/eye';
	import Route from 'lucide-svelte/icons/route';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import Plane from 'lucide-svelte/icons/plane';
	import Ship from 'lucide-svelte/icons/ship';
	import MapPin from 'lucide-svelte/icons/map-pin';

	import Building from 'lucide-svelte/icons/building';
	import Globe from 'lucide-svelte/icons/globe';
	import Package from 'lucide-svelte/icons/package';
	import User from 'lucide-svelte/icons/user';
	import Search from 'lucide-svelte/icons/search';
	import Badge from '$lib/components/shared/Badge.svelte';

	interface Props {
		item: any;
		isLive?: boolean;
		isNode?: boolean;
		liveSource?: string;
		onClose: () => void;
		onAddToCase?: () => void;
		onShowTrail?: () => void;
		onWatch?: () => void;
		onFlyTo?: (lat: number, lon: number) => void;
	}

	let { item, isLive = false, isNode = false, liveSource = '', onClose, onAddToCase, onShowTrail, onWatch, onFlyTo }: Props = $props();

	// For entity/node items, properties is an array — don't treat it as the props object
	const props = $derived(isNode ? (item || {}) : (item?.properties || item || {}));
	const lat = $derived(item?.latitude ?? props.latitude ?? item?.geometry?.coordinates?.[1]);
	const lon = $derived(item?.longitude ?? props.longitude ?? item?.geometry?.coordinates?.[0]);
	const signalType = $derived(props.signal_type || (isLive ? `live.${liveSource}` : 'unknown'));
	const domain = $derived(signalType.split('.')[1] || '');

	const title = $derived.by(() => {
		if (isNode) return item?.label || item?.title || item?.name || 'Entity';
		if (isLive) return (props.label || props.key || '').trim() || 'Unknown';
		return props.title || signalType.split('.').pop()?.replace(/_/g, ' ') || 'Signal';
	});

	const subtitle = $derived.by(() => {
		if (isNode) {
			const et = item?.entity_type || '';
			const mc = item?.mention_count ? `${item.mention_count} mentions` : '';
			return [et, mc].filter(Boolean).join(' · ');
		}
		if (isLive && liveSource === 'adsb') {
			const r = props.payload?.r || props.payload?.registration || '';
			const t = props.payload?.t || '';
			return [r, t].filter(Boolean).join(' · ') || 'Aircraft';
		}
		if (isLive && liveSource === 'ais') {
			return props.payload?.shipname || props.payload?.mmsi || 'Vessel';
		}
		return props.device_id || props.source_domain || domain;
	});

	const liveConfig = $derived(isLive ? LIVE_SOURCES[liveSource] : null);

	const entityType = $derived(item?.entity_type || '');
	const entityTypeColors: Record<string, string> = {
		person: 'text-purple-400', organization: 'text-blue-400', country: 'text-emerald-400',
		region: 'text-teal-400', location: 'text-cyan-400', asset: 'text-orange-400',
	};

	const typeIcon = $derived.by(() => {
		if (isLive && liveSource === 'adsb') return Plane;
		if (isLive && liveSource === 'ais') return Ship;
		if (isNode && entityType === 'country') return Globe;
		if (isNode && entityType === 'organization') return Building;
		if (isNode && entityType === 'person') return User;
		if (isNode && entityType === 'asset') return Package;
		if (domain === 'geospatial') return MapPin;
		return MapPin;
	});

	const domainColors: Record<string, string> = {
		geospatial: 'text-blue-400', security: 'text-red-400', communications: 'text-purple-400',
		personal: 'text-emerald-400', financial: 'text-yellow-400', environmental: 'text-cyan-400',
	};
	const iconColor = $derived(isLive ? 'text-blue-400' : isNode ? (entityTypeColors[entityType] || 'text-cyan-400') : (domainColors[domain] || 'text-muted-foreground'));

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const secs = Math.floor(diff / 1000);
		if (secs < 60) return `${secs}s ago`;
		const mins = Math.floor(secs / 60);
		if (mins < 60) return `${mins}m ago`;
		return `${Math.floor(mins / 60)}h ago`;
	}
</script>

{#if screen.isMobile}
	<!-- Mobile: bottom sheet -->
	<div
		class="fixed bottom-0 left-0 right-0 z-50 bg-card border-t border-border rounded-t-2xl shadow-2xl safe-bottom max-h-[60vh] overflow-y-auto"
		use:swipeDismiss={{ onDismiss: onClose }}
	>
		<div class="flex justify-center pt-2 pb-1">
			<div class="w-8 h-1 rounded-full bg-muted-foreground/30"></div>
		</div>
		<div class="px-4 pb-4">
			{@render content()}
		</div>
	</div>
{:else}
	<!-- Desktop/Tablet: side panel -->
	<div class="w-[320px] bg-card/95 backdrop-blur-sm border-l border-border shadow-xl overflow-y-auto shrink-0 flex flex-col">
		<div class="flex items-center justify-between px-4 py-3 border-b border-border sticky top-0 bg-card/95 z-10">
			<span class="text-sm font-medium text-foreground truncate">{title}</span>
			<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground"><X size={16} /></button>
		</div>
		<div class="flex-1 overflow-y-auto px-4 py-3">
			{@render content()}
		</div>
	</div>
{/if}

{#snippet content()}
	<!-- Header -->
	<div class="flex items-start gap-3 mb-4">
		<div class="w-10 h-10 rounded-lg {isLive ? 'bg-blue-500/10' : 'bg-primary/10'} flex items-center justify-center shrink-0">
			<svelte:component this={typeIcon} size={20} class={iconColor} />
		</div>
		<div class="flex-1 min-w-0">
			<h3 class="text-sm font-medium text-foreground">{title}</h3>
			<p class="text-xs text-muted-foreground">{subtitle}</p>
			{#if isLive}
				<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-blue-500/10 text-blue-400 mt-1 inline-block">LIVE</span>
			{/if}
		</div>
	</div>

	<!-- Entity card (for context pins) -->
	{#if isNode && entityType}
		<div class="mb-4">
			<!-- Type + aliases -->
			<div class="flex items-center gap-2 mb-3">
				<Badge text={entityType} variant={entityType === 'country' ? 'success' : entityType === 'person' ? 'info' : entityType === 'organization' ? 'info' : 'neutral'} size="sm" />
				{#if item.mention_count}
					<span class="text-[10px] text-muted-foreground">{item.mention_count} mentions</span>
				{/if}
			</div>
			{#if item.aliases?.length}
				<p class="text-[10px] text-muted-foreground mb-3">Also known as: {item.aliases.join(', ')}</p>
			{/if}

			<!-- Key Facts -->
			{#if item.properties?.length}
				<div class="mb-3">
					<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-2">Key Facts</p>
					<div class="space-y-1.5">
						{#each item.properties as prop}
							<div class="flex items-start gap-2 text-xs">
								<span class="w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 bg-emerald-400"></span>
								<div class="flex-1">
									<span class="text-foreground font-medium capitalize">{prop.key.replace(/_/g, ' ')}:</span>
									<span class="text-muted-foreground ml-1">{prop.value}</span>
								</div>
							</div>
						{/each}
					</div>
				</div>
			{/if}

			<!-- Linked Articles -->
			{#if item.article_ids?.length}
				<div class="mb-3">
					<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-2">Referenced Articles ({item.article_ids.length})</p>
					<div class="space-y-1.5">
						{#each item.article_ids as artId}
							<div class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-muted/30 hover:bg-muted/50 transition-colors text-xs">
								<span class="text-muted-foreground">📰</span>
								<span class="text-foreground font-mono text-[10px] truncate">{artId}</span>
							</div>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	{/if}

	<!-- Fields -->
	<div class="space-y-2 mb-4">
		{#if isLive && liveConfig}
			{#each liveConfig.detailFields as field}
				{@const val = getNestedValue(item, field.key) ?? getNestedValue(props, field.key)}
				{#if val != null && val !== ''}
					<div class="flex justify-between text-xs">
						<span class="text-muted-foreground">{field.label}</span>
						<span class="text-foreground font-mono">
							{typeof val === 'number' ? Math.round(val * 10) / 10 : String(val).trim()}
							{#if field.unit}{field.unit}{/if}
						</span>
					</div>
				{/if}
			{/each}
		{:else}
			<!-- Permanent signal fields -->
			{#if props.severity && props.severity !== 'informational'}
				<div class="flex justify-between text-xs">
					<span class="text-muted-foreground">Severity</span>
					<span class="text-foreground capitalize">{props.severity}</span>
				</div>
			{/if}
			{#if props.device_id}
				<div class="flex justify-between text-xs">
					<span class="text-muted-foreground">Device</span>
					<span class="text-foreground font-mono text-[11px]">{props.device_id}</span>
				</div>
			{/if}
			{#if props.timestamp}
				<div class="flex justify-between text-xs">
					<span class="text-muted-foreground">Time</span>
					<span class="text-foreground">{timeAgo(props.timestamp)}</span>
				</div>
			{/if}
		{/if}

		<!-- Coordinates -->
		{#if lat != null && lon != null}
			<div class="flex justify-between text-xs">
				<span class="text-muted-foreground">Position</span>
				<button
					onclick={() => onFlyTo?.(lat, lon)}
					class="text-primary hover:underline font-mono text-[11px] flex items-center gap-1"
				>
					<Navigation size={10} />
					{Number(lat).toFixed(4)}, {Number(lon).toFixed(4)}
				</button>
			</div>
		{/if}

		<!-- Cached time for live -->
		{#if isLive && props.cached_at}
			<div class="flex justify-between text-xs">
				<span class="text-muted-foreground">Updated</span>
				<span class="text-foreground">{timeAgo(props.cached_at)}</span>
			</div>
		{/if}
	</div>

	<!-- Observables (permanent signals) -->
	{#if !isLive && (item?.observables?.length || props.observables?.length)}
		<div class="mb-4">
			<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1.5">Observables</p>
			<div class="flex flex-wrap gap-1">
				{#each (item?.observables || props.observables || []).slice(0, 8) as obs}
					<span class="text-[10px] px-2 py-0.5 rounded-full bg-muted text-foreground font-mono">
						{obs.observable_type || obs.type}: {(obs.observable_value || obs.value || '').slice(0, 20)}
					</span>
				{/each}
			</div>
		</div>
	{/if}

	<!-- Camera image (if this is a camera marker) -->
	{#if isLive && liveSource === 'cameras' && (item?.image_url || props.image_url)}
		<div class="mb-4">
			<p class="text-[9px] uppercase tracking-wider text-muted-foreground/50 font-medium mb-1.5">Live Feed</p>
			<div class="rounded-lg overflow-hidden border border-border bg-black">
				<img
					src={item?.image_url || props.image_url}
					alt={title}
					class="w-full h-auto"
					onerror={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
				/>
			</div>
			<p class="text-[9px] text-muted-foreground mt-1">Image refreshes with camera feed · {props.source_provider || 'Unknown provider'}</p>
		</div>
	{/if}

	<!-- Actions -->
	<div class="flex flex-col gap-2">
		{#if onShowTrail}
			<button onclick={onShowTrail} class="flex items-center justify-center gap-2 w-full px-3 py-2 rounded-lg border border-border text-xs text-foreground hover:bg-muted transition-colors">
				<Route size={14} /> Show Trail
			</button>
		{/if}
		{#if onAddToCase}
			<button onclick={onAddToCase} class="flex items-center justify-center gap-2 w-full px-3 py-2 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 transition-colors">
				<FolderPlus size={14} /> Add to Case
			</button>
		{/if}
		{#if onWatch && isLive}
			<button onclick={onWatch} class="flex items-center justify-center gap-2 w-full px-3 py-2 rounded-lg border border-primary text-primary text-xs hover:bg-primary/10 transition-colors">
				<Eye size={14} /> Watch This
			</button>
		{/if}
		{#if isNode && item.promoted_to}
			<a href="/contacts/{item.promoted_to}" class="flex items-center justify-center gap-2 w-full px-3 py-2 rounded-lg bg-emerald-500/10 text-emerald-400 text-xs font-medium hover:bg-emerald-500/20 transition-colors">
				<User size={14} /> View Contact Profile
			</a>
		{:else if isNode && (entityType === 'person' || entityType === 'organization' || entityType === 'country')}
			<a href="/watch/topic_ukraine?tab=entities" class="flex items-center justify-center gap-2 w-full px-3 py-2 rounded-lg bg-primary/10 text-primary text-xs font-medium hover:bg-primary/20 transition-colors">
				<Search size={14} /> Research Entity
			</a>
		{/if}
		<a href="/chat?q=tell me about {isLive ? title : (props.signal_id || title)}" class="flex items-center justify-center gap-2 w-full px-3 py-2 rounded-lg border border-border text-xs text-muted-foreground hover:text-foreground hover:bg-muted transition-colors">
			<MessageCircle size={14} /> Ask Enki
		</a>
	</div>
{/snippet}

<style>
	.safe-bottom { padding-bottom: env(safe-area-inset-bottom, 0px); }
</style>
