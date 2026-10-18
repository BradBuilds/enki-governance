<script lang="ts">
	/** Graph/sparkline embed — renders numeric arrays as a line chart using Canvas. */
	import { onMount } from 'svelte';

	let { data }: { data: any } = $props();
	let canvas: HTMLCanvasElement;

	const values = $derived(data?.data || []);
	const labels = $derived(data?.labels || []);
	const label = $derived(data?.label || 'Value');

	onMount(() => {
		if (!canvas || values.length === 0) return;
		drawChart();
	});

	function drawChart() {
		const ctx = canvas.getContext('2d');
		if (!ctx) return;

		const w = canvas.width = canvas.offsetWidth * 2;
		const h = canvas.height = canvas.offsetHeight * 2;
		ctx.scale(2, 2);

		const cw = canvas.offsetWidth;
		const ch = canvas.offsetHeight;
		const padding = 8;
		const plotW = cw - padding * 2;
		const plotH = ch - padding * 2;

		const min = Math.min(...values);
		const max = Math.max(...values);
		const range = max - min || 1;

		// Background
		ctx.fillStyle = 'rgba(0,0,0,0)';
		ctx.fillRect(0, 0, cw, ch);

		// Line
		ctx.beginPath();
		ctx.strokeStyle = '#3b82f6';
		ctx.lineWidth = 2;
		ctx.lineJoin = 'round';

		values.forEach((v: number, i: number) => {
			const x = padding + (i / (values.length - 1)) * plotW;
			const y = padding + plotH - ((v - min) / range) * plotH;
			if (i === 0) ctx.moveTo(x, y);
			else ctx.lineTo(x, y);
		});
		ctx.stroke();

		// Fill under line
		const lastX = padding + plotW;
		const lastY = padding + plotH - ((values[values.length - 1] - min) / range) * plotH;
		ctx.lineTo(lastX, ch - padding);
		ctx.lineTo(padding, ch - padding);
		ctx.closePath();
		ctx.fillStyle = 'rgba(59, 130, 246, 0.1)';
		ctx.fill();

		// Latest value dot
		ctx.beginPath();
		ctx.fillStyle = '#3b82f6';
		ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
		ctx.fill();
	}
</script>

<div class="rounded-lg border border-border bg-muted/30 p-3">
	<div class="flex items-center justify-between mb-2">
		<span class="text-[10px] text-muted-foreground uppercase tracking-wide">{label}</span>
		{#if values.length > 0}
			<span class="text-xs font-mono text-foreground">{values[values.length - 1]}</span>
		{/if}
	</div>
	<canvas bind:this={canvas} class="w-full h-20"></canvas>
</div>
