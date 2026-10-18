<script lang="ts">
	/** Gauge embed — renders a single value with min/max range and optional thresholds. */
	let { data }: { data: any } = $props();

	const value = $derived(data?.value ?? 0);
	const min = $derived(data?.min ?? 0);
	const max = $derived(data?.max ?? 100);
	const unit = $derived(data?.unit || '');
	const thresholds = $derived(data?.thresholds || []);

	const range = $derived(max - min || 1);
	const percent = $derived(Math.max(0, Math.min(100, ((value - min) / range) * 100)));

	// Determine color based on thresholds
	const barColor = $derived(() => {
		for (const t of [...thresholds].sort((a: any, b: any) => b.value - a.value)) {
			if (value >= t.value) return t.color === 'red' ? '#ef4444' : t.color === 'orange' ? '#f97316' : '#3b82f6';
		}
		return '#22c55e'; // green = normal
	});
</script>

<div class="rounded-lg border border-border bg-muted/30 p-3">
	<div class="flex items-center justify-between mb-2">
		<span class="text-2xl font-bold text-foreground">{value}<span class="text-sm text-muted-foreground ml-1">{unit}</span></span>
	</div>
	<!-- Bar gauge -->
	<div class="relative h-3 rounded-full bg-muted overflow-hidden">
		<div
			class="absolute left-0 top-0 h-full rounded-full transition-all duration-500"
			style="width: {percent}%; background-color: {barColor()}"
		></div>
		<!-- Threshold markers -->
		{#each thresholds as t}
			{@const tPercent = ((t.value - min) / range) * 100}
			<div class="absolute top-0 h-full w-0.5 bg-foreground/20" style="left: {tPercent}%"></div>
		{/each}
	</div>
	<div class="flex justify-between mt-1 text-[10px] text-muted-foreground/50">
		<span>{min}{unit}</span>
		{#each thresholds as t}
			<span class="text-{t.color === 'red' ? 'enki-red' : t.color === 'orange' ? 'enki-orange' : 'muted-foreground'}">{t.label || t.value}</span>
		{/each}
		<span>{max}{unit}</span>
	</div>
</div>
