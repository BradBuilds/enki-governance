<script lang="ts">
	import Eye from 'lucide-svelte/icons/eye';
	import EyeOff from 'lucide-svelte/icons/eye-off';
	import Shield from 'lucide-svelte/icons/shield';

	interface Props {
		entries: any[];
	}

	let { entries }: Props = $props();

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}
</script>

<div class="rounded-lg border bg-card p-3">
	<div class="flex items-center gap-2 mb-2">
		<Shield size={14} class="text-amber-400" />
		<span class="text-xs font-medium text-muted-foreground">Access Log</span>
	</div>
	{#if entries.length === 0}
		<p class="text-xs text-muted-foreground">No access events recorded.</p>
	{:else}
		<div class="space-y-1">
			{#each entries.slice(0, 10) as entry}
				<div class="flex items-center justify-between text-[11px] py-1 px-2 rounded
					{entry.allowed ? 'bg-muted/20' : 'bg-red-500/5 border border-red-500/20'}">
					<div class="flex items-center gap-1.5">
						{#if entry.allowed}
							<Eye class="w-3 h-3 text-muted-foreground" />
						{:else}
							<EyeOff class="w-3 h-3 text-red-400" />
						{/if}
						<span class="text-foreground font-medium">{entry.querier_id}</span>
						<span class="text-muted-foreground">{entry.allowed ? 'viewed' : 'denied'}</span>
						<span class="text-foreground">{entry.data_type}</span>
					</div>
					<span class="text-muted-foreground/50 text-[9px]">{timeAgo(entry.timestamp)}</span>
				</div>
			{/each}
		</div>
	{/if}
</div>
