<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import Activity from 'lucide-svelte/icons/activity';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import ChevronUp from 'lucide-svelte/icons/chevron-up';

	type LiveStatus = {
		workers_running: number;
		workers_total: number;
		throughput_5m: { statements: number; findings: number; investigations: number };
		queue_pending: { investigations: number; enrichments: number };
		last_activity: string | null;
		geocoder: { running: boolean; geocoded: number; phase?: string };
	};

	let status = $state<LiveStatus | null>(null);
	let pipe = $state<any>(null);
	let open = $state(false);
	let timer: ReturnType<typeof setInterval> | null = null;

	async function tick() {
		try {
			const r = await fetch('/api/workers/live');
			if (r.ok) status = await r.json();
		} catch {}
		try {
			const p = await fetch('/api/system/pipeline-health');
			if (p.ok) pipe = await p.json();
		} catch {}
	}

	// Background daemon workers are off; the cron pipeline does the mining.
	// Show real pipeline activity instead of a "0/0 workers" that reads as idle.
	const minTick = $derived(pipe?.topics?.length
		? Math.min(...pipe.topics.map((t: any) => t.minutes_since_tick)) : null);

	onMount(() => {
		tick();
		timer = setInterval(tick, 8000);
	});
	onDestroy(() => { if (timer) clearInterval(timer); });

	function relativeTime(iso: string | null): string {
		if (!iso) return '—';
		const ms = Date.now() - new Date(iso).getTime();
		const s = Math.floor(ms / 1000);
		if (s < 60) return `${s}s ago`;
		const m = Math.floor(s / 60);
		if (m < 60) return `${m}m ago`;
		const h = Math.floor(m / 60);
		if (h < 24) return `${h}h ago`;
		return `${Math.floor(h / 24)}d ago`;
	}

	const isAlive = $derived((status && status.workers_running > 0) || (pipe && pipe.any_active));
	const totalThroughput = $derived(
		status
			? status.throughput_5m.statements + status.throughput_5m.findings + status.throughput_5m.investigations
			: 0,
	);
</script>

{#if status}
	<div class="border-b border-border/40 bg-card/50 text-[11px]">
		<button
			type="button"
			onclick={() => (open = !open)}
			class="w-full flex items-center gap-3 px-4 py-1 hover:bg-card/80 transition-colors text-left"
			title="Click to expand processing details"
		>
			<span class="flex items-center gap-1.5 shrink-0">
				<span class={`inline-block w-1.5 h-1.5 rounded-full ${isAlive ? 'bg-emerald-500 animate-pulse' : 'bg-slate-500'}`}></span>
				<Activity size={11} class="text-muted-foreground" />
				{#if pipe && pipe.topics && pipe.topics.length}
					<span class="text-foreground tabular-nums">
						{pipe.active_count} mining{#if pipe.stalled_count} · <span class="text-amber-400">{pipe.stalled_count} stalled</span>{/if}
						{#if minTick != null} · tick {minTick < 1 ? '<1' : Math.round(minTick)}m ago{/if}
					</span>
				{:else}
					<span class="text-foreground tabular-nums">{status.workers_running}/{status.workers_total} workers</span>
				{/if}
			</span>

			{#if totalThroughput > 0}
				<span class="text-muted-foreground">
					last 5m:
					{#if status.throughput_5m.findings > 0}
						<span class="text-foreground tabular-nums">{status.throughput_5m.findings}</span> findings
					{/if}
					{#if status.throughput_5m.statements > 0}
						<span class="text-foreground tabular-nums ml-2">{status.throughput_5m.statements}</span> statements
					{/if}
					{#if status.throughput_5m.investigations > 0}
						<span class="text-foreground tabular-nums ml-2">{status.throughput_5m.investigations}</span> inv
					{/if}
				</span>
			{/if}

			{#if status.queue_pending.investigations > 0}
				<span class="text-muted-foreground">
					queue: <span class="text-amber-400 tabular-nums">{status.queue_pending.investigations}</span> pending
				</span>
			{/if}

			{#if status.geocoder.running}
				<span class="text-muted-foreground">
					geocoder: <span class="text-foreground tabular-nums">{status.geocoder.geocoded}</span> done
				</span>
			{/if}

			<span class="text-muted-foreground ml-auto">
				last activity {relativeTime(status.last_activity)}
			</span>

			<svelte:component this={open ? ChevronUp : ChevronDown} size={12} class="text-muted-foreground/60" />
		</button>

		{#if open}
			<div class="px-4 py-2 border-t border-border/30 bg-card/70 grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-1.5">
				<div>
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Workers</div>
					<div class="text-foreground">{status.workers_running} / {status.workers_total} running</div>
				</div>
				<div>
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Throughput (5m)</div>
					<div class="text-foreground tabular-nums">
						{status.throughput_5m.findings}f · {status.throughput_5m.statements}s · {status.throughput_5m.investigations}i
					</div>
				</div>
				<div>
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Investigation queue</div>
					<div class="text-foreground tabular-nums">{status.queue_pending.investigations} pending</div>
				</div>
				<div>
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Geocoder</div>
					<div class="text-foreground">
						{status.geocoder.running ? 'running' : 'stopped'} · {status.geocoder.geocoded} done
					</div>
				</div>
				<div class="md:col-span-4 mt-1 text-[10px]">
					<a href="/admin/workers" class="text-primary hover:underline">Open admin workers page →</a>
				</div>
			</div>
		{/if}
	</div>
{/if}
