<script lang="ts">
	import Activity from 'lucide-svelte/icons/activity';
	import WorkerActivityPanel from '$lib/components/panels/WorkerActivityPanel.svelte';

	interface Props {
		events?: any[];
		limit?: number;
	}

	let { events = [], limit = 10 }: Props = $props();
</script>

<div class="rounded-lg border bg-card p-3">
	<div class="flex items-center gap-2 mb-2">
		<Activity size={14} class="text-emerald-400" />
		<span class="text-xs font-medium text-muted-foreground">System Activity</span>
	</div>
	<WorkerActivityPanel {limit} compact={true} />
</div>
