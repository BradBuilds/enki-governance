<script lang="ts">
	import Plug from 'lucide-svelte/icons/plug';

	interface Props {
		connectors: any[];
	}

	let { connectors }: Props = $props();
	const shown = $derived(connectors.slice(0, 8));
</script>

<div class="rounded-lg border bg-card p-3">
	<div class="flex items-center gap-2 mb-2">
		<Plug size={14} class="text-muted-foreground" />
		<span class="text-xs font-medium text-muted-foreground">{connectors.length} connectors</span>
	</div>
	<div class="space-y-1">
		{#each shown as c}
			<div class="flex items-center justify-between text-xs py-1.5 px-2 rounded bg-muted/30">
				<span class="text-foreground">{c.display_name || c.device_id || c.name || 'Connector'}</span>
				<span class="text-muted-foreground">{c.connector_type || c.source_type || c.type || ''}</span>
			</div>
		{/each}
	</div>
</div>
