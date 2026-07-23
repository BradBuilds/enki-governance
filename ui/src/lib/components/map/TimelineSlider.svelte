<script lang="ts">
	/**
	 * Time range filter for the map.
	 * Dual-handle slider for selecting a time window.
	 */

	interface Props {
		onRangeChange?: (range: { after: string; before: string }) => void;
	}

	let { onRangeChange = () => {} }: Props = $props();

	// Default: last 24 hours
	const now = Date.now();
	const dayMs = 24 * 60 * 60 * 1000;

	let rangeHours = $state(24);

	const ranges = [
		{ label: '1h', hours: 1 },
		{ label: '6h', hours: 6 },
		{ label: '24h', hours: 24 },
		{ label: '7d', hours: 168 },
		{ label: '30d', hours: 720 },
		{ label: 'All', hours: 0 },
	];

	function selectRange(hours: number) {
		rangeHours = hours;
		if (hours === 0) {
			onRangeChange({ after: '', before: '' });
		} else {
			const after = new Date(now - hours * 60 * 60 * 1000).toISOString();
			const before = new Date(now).toISOString();
			onRangeChange({ after, before });
		}
	}
</script>

<div class="flex items-center gap-1 bg-card/90 backdrop-blur-sm border border-border rounded-lg px-3 py-2 shadow-lg">
	<span class="text-[10px] text-muted-foreground mr-2 shrink-0">Time:</span>
	{#each ranges as r}
		<button
			onclick={() => selectRange(r.hours)}
			class="px-2 py-1 rounded text-[10px] font-medium transition-colors {rangeHours === r.hours ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
		>
			{r.label}
		</button>
	{/each}
</div>
