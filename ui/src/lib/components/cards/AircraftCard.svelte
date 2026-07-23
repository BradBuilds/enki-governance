<script lang="ts">
	import Plane from 'lucide-svelte/icons/plane';

	interface Props {
		aircraft: any[];
		count: number;
	}

	let { aircraft, count }: Props = $props();
	const shown = $derived(aircraft.slice(0, 8));
</script>

<div class="rounded-lg border bg-card p-3">
	<div class="flex items-center gap-2 mb-2">
		<Plane size={14} class="text-cyan-400" />
		<span class="text-xs font-medium text-muted-foreground">{count} aircraft tracked</span>
	</div>
	<div class="space-y-1">
		{#each shown as ac}
			<div class="flex items-center justify-between text-xs py-1 px-2 rounded bg-muted/30">
				<span class="font-mono text-foreground">{ac.callsign || ac.flight || ac.hex || '—'}</span>
				<span class="text-muted-foreground">
					{#if ac.altitude}{Math.round(ac.altitude).toLocaleString()} ft{/if}
					{#if ac.speed} · {Math.round(ac.speed)} kt{/if}
				</span>
			</div>
		{/each}
		{#if count > 8}
			<p class="text-[10px] text-muted-foreground text-center pt-1">+{count - 8} more</p>
		{/if}
	</div>
</div>
