<script lang="ts">
	/**
	 * SparklineEmbed — renders a trackable series as a full-width sparkline graph.
	 * Shows the field label, current value, min/max, and an SVG line graph.
	 * Used inside SignalCard when a trackable field is expanded.
	 */
	import { sparklinePath } from '$lib/utils/cardResolver';
	import type { TrackableSeries } from '$lib/utils/cardResolver';

	let { series }: { series: TrackableSeries } = $props();

	const path = $derived(sparklinePath(series.values, 300, 60));
	const min = $derived(series.values.length > 0 ? Math.min(...series.values) : 0);
	const max = $derived(series.values.length > 0 ? Math.max(...series.values) : 100);
	const current = $derived(series.current);

	function gaugeColor(value: number, max: number): string {
		const pct = max > 0 ? (value / max) * 100 : 0;
		if (pct >= 90) return 'text-severity-critical';
		if (pct >= 75) return 'text-severity-high';
		if (pct >= 50) return 'text-severity-medium';
		return 'text-enki-green';
	}

	function timeLabel(ts: string): string {
		if (!ts) return '';
		const d = new Date(ts);
		const diff = Date.now() - d.getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}
</script>

<div class="rounded-lg border border-border bg-muted/30 p-3">
	<div class="flex items-center justify-between mb-2">
		<span class="text-[10px] text-muted-foreground uppercase tracking-wide">
			{series.label}
		</span>
		<span class="text-sm font-bold {gaugeColor(current, max)}">
			{current}{series.unit}
		</span>
	</div>

	{#if path}
		<svg viewBox="0 0 300 60" class="w-full h-14" preserveAspectRatio="none">
			<!-- Fill under line -->
			<path
				d="{path} L300,60 L0,60 Z"
				fill="currentColor"
				class="text-primary/5"
			/>
			<!-- Line -->
			<path
				d={path}
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linejoin="round"
				class="text-primary"
			/>
			<!-- Latest value dot -->
			{#if series.values.length > 0}
				{@const lastY = 60 - ((series.values[series.values.length - 1] - min) / (max - min || 1)) * 60}
				<circle cx="300" cy={lastY} r="3" fill="currentColor" class="text-primary" />
			{/if}
		</svg>
	{/if}

	<div class="flex justify-between mt-1 text-[9px] text-muted-foreground/50">
		<span>{series.timestamps.length > 0 ? timeLabel(series.timestamps[0]) : ''}</span>
		<span>min: {min.toFixed(1)}{series.unit} · max: {max.toFixed(1)}{series.unit}</span>
		<span>now</span>
	</div>
</div>
