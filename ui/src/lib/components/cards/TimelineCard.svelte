<script lang="ts">
	import Clock from 'lucide-svelte/icons/clock';

	interface Props {
		events: any[];
	}

	let { events }: Props = $props();
	const shown = $derived(events.slice(0, 10));

	const typeColors: Record<string, string> = {
		signal: 'bg-blue-400',
		alert: 'bg-amber-400',
		case: 'bg-purple-400',
		audit: 'bg-muted-foreground',
	};
</script>

<div class="rounded-lg border bg-card p-3">
	<div class="flex items-center gap-2 mb-3">
		<Clock size={14} class="text-muted-foreground" />
		<span class="text-xs font-medium text-muted-foreground">{events.length} events</span>
	</div>
	<div class="space-y-0">
		{#each shown as event, i}
			<div class="flex items-start gap-3 pb-2 {i < shown.length - 1 ? 'border-l border-border ml-1.5 pl-4' : 'ml-1.5 pl-4'} relative">
				<span class="absolute left-0 top-1 w-3 h-3 rounded-full border-2 border-card {typeColors[event.type] || 'bg-muted-foreground'}"></span>
				<div class="flex-1 min-w-0">
					<p class="text-xs text-foreground truncate">{event.title || event.detail || 'Event'}</p>
					{#if event.time}
						<p class="text-[10px] text-muted-foreground">{new Date(event.time).toLocaleString()}</p>
					{/if}
				</div>
			</div>
		{/each}
	</div>
	{#if events.length > 10}
		<a href="/timeline" class="block text-center text-xs text-primary mt-2 hover:underline">View full timeline</a>
	{/if}
</div>
