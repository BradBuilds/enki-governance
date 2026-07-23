<script lang="ts">
	/**
	 * Worker Activity Panel — shows what the system is discovering.
	 * Polls /api/workers/activity every 15 seconds when visible.
	 */
	import { onMount, onDestroy } from 'svelte';
	import Activity from 'lucide-svelte/icons/activity';
	import Zap from 'lucide-svelte/icons/zap';
	import Search from 'lucide-svelte/icons/search';
	import GitMerge from 'lucide-svelte/icons/git-merge';
	import Shield from 'lucide-svelte/icons/shield';
	import { chat } from '$lib/stores/chat.svelte';

	interface Props {
		limit?: number;
		compact?: boolean;
	}

	let { limit = 10, compact = false }: Props = $props();

	let events = $state<any[]>([]);
	let loading = $state(true);
	let interval: ReturnType<typeof setInterval> | null = null;

	const workerColors: Record<string, string> = {
		gdu_gateway: 'text-emerald-400',
		investigation: 'text-purple-400',
		enrichment: 'text-blue-400',
		reconciliation: 'text-amber-400',
		personal: 'text-cyan-400',
	};

	const actionLabels: Record<string, string> = {
		admit: 'Admitted',
		collapse: 'Collapsed',
		reject: 'Rejected',
		finding: 'Found',
		route_ldup: 'Staged',
	};

	async function loadActivity() {
		try {
			const res = await fetch(`/api/workers/activity?limit=${limit}`);
			if (res.ok) {
				const data = await res.json();
				events = data.events || [];
			}
		} catch {}
		loading = false;
	}

	function askAbout(event: any) {
		const name = event.entity_name || event.detail || event.node_id || 'this';
		chat.open();
		chat.sendMessage(`Tell me about ${name}`);
	}

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'now';
		if (mins < 60) return `${mins}m`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h`;
		return `${Math.floor(hrs / 24)}d`;
	}

	onMount(() => {
		loadActivity();
		interval = setInterval(loadActivity, 15000);
	});

	onDestroy(() => {
		if (interval) clearInterval(interval);
	});
</script>

{#if loading}
	<div class="text-xs text-muted-foreground py-2">Loading activity...</div>
{:else if events.length === 0}
	<div class="text-xs text-muted-foreground py-2">No recent worker activity</div>
{:else}
	<div class="space-y-0.5">
		{#each events as event}
			<button
				onclick={() => askAbout(event)}
				class="flex items-center gap-2 w-full text-left px-2 py-1.5 rounded hover:bg-muted/30 transition-colors group"
			>
				<span class="shrink-0 {workerColors[event.worker] || 'text-muted-foreground'}">
					{#if event.worker === 'investigation'}
						<Search class="w-3 h-3" />
					{:else if event.action === 'collapse'}
						<GitMerge class="w-3 h-3" />
					{:else if event.action === 'reject'}
						<Shield class="w-3 h-3" />
					{:else}
						<Zap class="w-3 h-3" />
					{/if}
				</span>
				<span class="flex-1 min-w-0 text-[11px] truncate text-foreground">
					<span class="font-medium {workerColors[event.worker] || ''}">{actionLabels[event.action] || event.action}</span>
					{#if event.entity_name}
						{event.entity_name}
					{:else if event.detail}
						{event.detail}
					{:else if event.category && event.subtype}
						{event.category}.{event.subtype}
					{/if}
				</span>
				{#if !compact && event.base60_address}
					<span class="text-[9px] font-mono text-muted-foreground/50 shrink-0">{event.base60_address}</span>
				{/if}
				<span class="text-[9px] text-muted-foreground/50 shrink-0">{timeAgo(event.timestamp)}</span>
			</button>
		{/each}
	</div>
{/if}
