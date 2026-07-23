<script lang="ts">
	/**
	 * Mobile bottom sheet for map detail panels.
	 * Replaces the fixed-width sidebar on small screens.
	 * Supports signal details and knowledge node details.
	 */
	import X from 'lucide-svelte/icons/x';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Compass from 'lucide-svelte/icons/compass';
	import FolderPlus from 'lucide-svelte/icons/folder-plus';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import ChevronUp from 'lucide-svelte/icons/chevron-up';

	interface Props {
		signal?: any;
		node?: any;
		onClose: () => void;
		onNodeNavigate?: (node: any) => void;
	}

	let { signal = null, node = null, onClose, onNodeNavigate }: Props = $props();

	let expanded = $state(false);

	// Signal derived
	const sigProps = $derived(signal?.properties || signal || {});
	const signalType = $derived(sigProps.signal_type || '');
	const typeParts = $derived(signalType.split('.'));
	const typeLabel = $derived(typeParts[typeParts.length - 1]?.replace(/_/g, ' ') || 'Signal');

	// Node derived
	const nodeProps = $derived(node?.properties || node || {});

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	// Touch handling for swipe-to-dismiss
	let startY = 0;
	let currentY = 0;
	let dragging = $state(false);
	let translateY = $state(0);

	function onTouchStart(e: TouchEvent) {
		startY = e.touches[0].clientY;
		dragging = true;
	}

	function onTouchMove(e: TouchEvent) {
		if (!dragging) return;
		currentY = e.touches[0].clientY;
		const diff = currentY - startY;
		// Only allow dragging down
		translateY = Math.max(0, diff);
	}

	function onTouchEnd() {
		dragging = false;
		if (translateY > 100) {
			onClose();
		}
		translateY = 0;
	}
</script>

<!-- Backdrop -->
<button
	onclick={onClose}
	class="fixed inset-0 z-40"
	aria-label="Close details"
></button>

<!-- Bottom sheet -->
<div
	class="fixed bottom-0 left-0 right-0 z-50 bg-card border-t border-border rounded-t-2xl shadow-2xl transition-transform safe-bottom
		   {expanded ? 'max-h-[80vh]' : 'max-h-[45vh]'}"
	style="transform: translateY({translateY}px)"
	ontouchstart={onTouchStart}
	ontouchmove={onTouchMove}
	ontouchend={onTouchEnd}
>
	<!-- Handle bar -->
	<div class="flex justify-center pt-2 pb-1">
		<div class="w-10 h-1 rounded-full bg-muted-foreground/20"></div>
	</div>

	<!-- Header -->
	<div class="flex items-center justify-between px-4 pb-2">
		{#if signal}
			<div class="flex items-center gap-2">
				<MapPin class="w-4 h-4 text-primary" />
				<span class="text-sm font-medium text-foreground capitalize">{typeLabel}</span>
			</div>
		{:else if node}
			<div class="flex items-center gap-2">
				<Compass class="w-4 h-4 text-enki-green" />
				<span class="text-sm font-medium text-foreground">{nodeProps.title || 'Location'}</span>
			</div>
		{/if}
		<div class="flex items-center gap-1">
			<button onclick={() => expanded = !expanded} class="p-1.5 rounded hover:bg-muted transition-colors">
				<ChevronUp class="w-4 h-4 text-muted-foreground transition-transform {expanded ? '' : 'rotate-180'}" />
			</button>
			<button onclick={onClose} class="p-1.5 rounded hover:bg-muted transition-colors">
				<X class="w-4 h-4 text-muted-foreground" />
			</button>
		</div>
	</div>

	<!-- Content -->
	<div class="px-4 pb-4 overflow-y-auto {expanded ? 'max-h-[70vh]' : 'max-h-[35vh]'}">
		{#if signal}
			<div class="space-y-3">
				<div>
					<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Type</span>
					<p class="text-xs font-mono text-foreground mt-0.5">{signalType}</p>
				</div>

				{#if sigProps.latitude != null && sigProps.longitude != null}
					<div>
						<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Coordinates</span>
						<p class="text-xs font-mono text-foreground mt-0.5">
							{Number(sigProps.latitude).toFixed(6)}, {Number(sigProps.longitude).toFixed(6)}
						</p>
					</div>
				{/if}

				{#if sigProps.timestamp}
					<div>
						<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Time</span>
						<p class="text-xs text-foreground mt-0.5">{timeAgo(sigProps.timestamp)}</p>
						<p class="text-[10px] text-muted-foreground">{new Date(sigProps.timestamp).toLocaleString()}</p>
					</div>
				{/if}

				{#if sigProps.device_id}
					<div>
						<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Device</span>
						<p class="text-xs font-mono text-foreground mt-0.5">{sigProps.device_id}</p>
					</div>
				{/if}

				{#if sigProps.severity && sigProps.severity !== 'informational'}
					<div>
						<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Severity</span>
						<span class="inline-block mt-0.5 text-[10px] px-1.5 py-0.5 rounded bg-severity-{sigProps.severity}/10 text-severity-{sigProps.severity}">
							{sigProps.severity}
						</span>
					</div>
				{/if}

				<!-- Actions -->
				<div class="flex gap-3 pt-2 border-t border-border">
					{#if sigProps.signal_id}
						<a href="/signals" class="flex items-center gap-1.5 text-xs text-primary hover:underline">
							<ExternalLink class="w-3 h-3" /> View signal
						</a>
					{/if}
					<button class="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-primary transition-colors">
						<FolderPlus class="w-3 h-3" /> Add to Case
					</button>
				</div>
			</div>
		{:else if node}
			<div class="space-y-3">
				{#if nodeProps.epistemic_type}
					<div>
						<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Type</span>
						<span class="inline-block mt-0.5 text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary capitalize">
							{nodeProps.epistemic_type}
						</span>
					</div>
				{/if}

				{#if nodeProps.summary || nodeProps.description}
					<div>
						<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Summary</span>
						<p class="text-xs text-foreground mt-0.5 leading-relaxed">{nodeProps.summary || nodeProps.description}</p>
					</div>
				{/if}

				{#if nodeProps.latitude != null && nodeProps.longitude != null}
					<div>
						<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Coordinates</span>
						<p class="text-xs font-mono text-foreground mt-0.5">
							{Number(nodeProps.latitude).toFixed(6)}, {Number(nodeProps.longitude).toFixed(6)}
						</p>
					</div>
				{/if}

				{#if nodeProps.confidence}
					<div>
						<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Confidence</span>
						<div class="flex items-center gap-2 mt-0.5">
							<div class="flex-1 bg-muted rounded-full h-1.5 max-w-[120px]">
								<div class="bg-enki-green rounded-full h-1.5" style="width: {nodeProps.confidence * 100}%"></div>
							</div>
							<span class="text-[10px] text-foreground">{Math.round(nodeProps.confidence * 100)}%</span>
						</div>
					</div>
				{/if}

				{#if nodeProps.connections && nodeProps.connections.length > 0}
					<div>
						<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Connected ({nodeProps.connections.length})</span>
						<div class="mt-1 space-y-1">
							{#each nodeProps.connections.slice(0, 5) as conn}
								<button
									onclick={() => onNodeNavigate?.(conn)}
									class="block w-full text-left text-xs text-primary hover:underline truncate"
								>
									{conn.properties?.title || conn.title || 'Node'}
								</button>
							{/each}
						</div>
					</div>
				{/if}

				<div class="flex gap-3 pt-2 border-t border-border">
					<button class="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-primary transition-colors">
						<FolderPlus class="w-3 h-3" /> Add to Case
					</button>
				</div>
			</div>
		{/if}
	</div>
</div>

<style>
	.safe-bottom {
		padding-bottom: env(safe-area-inset-bottom, 0px);
	}
</style>
