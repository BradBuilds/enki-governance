<script lang="ts">
	/**
	 * Slide-out detail panel when a map marker is clicked.
	 * Shows signal info, key fields, and action buttons.
	 */
	import X from 'lucide-svelte/icons/x';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import FolderPlus from 'lucide-svelte/icons/folder-plus';
	import ExternalLink from 'lucide-svelte/icons/external-link';

	interface Props {
		signal: any;
		onClose: () => void;
	}

	let { signal, onClose }: Props = $props();

	const props = $derived(signal?.properties || signal || {});
	const signalType = $derived(props.signal_type || 'Unknown');
	const typeParts = $derived(signalType.split('.'));
	const typeLabel = $derived(typeParts[typeParts.length - 1]?.replace(/_/g, ' ') || 'Signal');
	const domain = $derived(typeParts[1] || '');

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
</script>

<div class="w-80 border-l border-border bg-card h-full overflow-y-auto">
	<!-- Header -->
	<div class="flex items-center justify-between px-4 py-3 border-b border-border">
		<div class="flex items-center gap-2">
			<MapPin class="w-4 h-4 text-primary" />
			<span class="text-sm font-medium text-foreground capitalize">{typeLabel}</span>
		</div>
		<button onclick={onClose} class="p-1 rounded hover:bg-muted transition-colors">
			<X class="w-4 h-4 text-muted-foreground" />
		</button>
	</div>

	<div class="px-4 py-3 space-y-4">
		<!-- Signal type badge -->
		<div>
			<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Type</span>
			<p class="text-xs font-mono text-foreground mt-0.5">{signalType}</p>
		</div>

		<!-- Location -->
		{#if props.latitude != null && props.longitude != null}
			<div>
				<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Coordinates</span>
				<p class="text-xs font-mono text-foreground mt-0.5">
					{Number(props.latitude).toFixed(6)}, {Number(props.longitude).toFixed(6)}
				</p>
				{#if props.altitude != null}
					<p class="text-[10px] text-muted-foreground mt-0.5">Alt: {props.altitude}m</p>
				{/if}
			</div>
		{/if}

		<!-- Timestamp -->
		{#if props.timestamp}
			<div>
				<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Time</span>
				<p class="text-xs text-foreground mt-0.5">{timeAgo(props.timestamp)}</p>
				<p class="text-[10px] text-muted-foreground">{new Date(props.timestamp).toLocaleString()}</p>
			</div>
		{/if}

		<!-- Device -->
		{#if props.device_id}
			<div>
				<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Device</span>
				<p class="text-xs font-mono text-foreground mt-0.5">{props.device_id}</p>
			</div>
		{/if}

		<!-- Severity -->
		{#if props.severity && props.severity !== 'informational'}
			<div>
				<span class="text-[10px] uppercase tracking-wide text-muted-foreground">Severity</span>
				<span class="inline-block mt-0.5 text-[10px] px-1.5 py-0.5 rounded bg-severity-{props.severity}/10 text-severity-{props.severity}">
					{props.severity}
				</span>
			</div>
		{/if}

		<!-- Actions -->
		<div class="flex flex-col gap-2 pt-2 border-t border-border">
			{#if props.signal_id}
				<a href="/signals" class="flex items-center gap-2 text-xs text-primary hover:underline">
					<ExternalLink class="w-3 h-3" /> View full signal
				</a>
			{/if}
			<button class="flex items-center gap-2 text-xs text-muted-foreground hover:text-primary transition-colors">
				<FolderPlus class="w-3 h-3" /> Add to Case
			</button>
		</div>
	</div>
</div>
