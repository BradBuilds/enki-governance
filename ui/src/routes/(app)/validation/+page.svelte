<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import Shield from 'lucide-svelte/icons/shield';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import Check from 'lucide-svelte/icons/check';
	import XCircle from 'lucide-svelte/icons/x-circle';
	import Info from 'lucide-svelte/icons/info';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import Play from 'lucide-svelte/icons/play';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import { api, apiSafe } from '$lib/api/client';

	type Finding = {
		name: string;
		severity: 'info' | 'warn' | 'error' | 'critical';
		count: number;
		sample: any[];
		description: string;
		fix_hint: string;
		query_ms?: number;
	};

	type LatestRun = {
		run_id: number;
		run_started_at: string;
		run_finished_at: string;
		triggered_by: string;
		summary: {
			total_checks: number;
			checks_with_issues: number;
			by_severity: Record<string, number>;
		};
		findings: Finding[];
	};

	type Regression = {
		check: string;
		prev_count: number;
		cur_count: number;
		delta: number;
	};

	type WorkerStatus = {
		running: boolean;
		phase: string;
		last_run_at: number | null;
		last_run_id: number | null;
		checks_run: number;
		issues_seen: number;
		errors: number;
	};

	let loading = $state(true);
	let running = $state(false);
	let toast = $state<string | null>(null);
	let latest = $state<LatestRun | null>(null);
	let regressions = $state<Regression[]>([]);
	let workerStatus = $state<WorkerStatus | null>(null);
	let expanded = $state<Record<string, boolean>>({});
	let severityFilter = $state<'all' | 'issues_only'>('issues_only');
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const COLLAPSE_CORRECTNESS_CHECKS = new Set([
		'entities_with_ungated_type',
		'stale_pending_ldup',
		'stuck_signal_extractions',
		'ungoverned_canonical_nodes',
		'orphan_entity_redirects',
	]);

	async function loadAll() {
		const [l, r, w] = await Promise.all([
			apiSafe<LatestRun>('/validation/history/latest'),
			apiSafe<{ regressions: Regression[] }>('/validation/history/regressions'),
			apiSafe<WorkerStatus>('/validation/worker/status'),
		]);
		latest = l;
		regressions = r?.regressions || [];
		workerStatus = w;
		loading = false;
	}

	async function runNow() {
		running = true;
		try {
			await api('/validation/run-now', { method: 'POST' });
			toast = 'Validation run started — refreshing in 5s';
			setTimeout(() => { loadAll(); toast = null; }, 5000);
		} catch (e) {
			toast = `Failed: ${e instanceof Error ? e.message : String(e)}`;
		} finally {
			running = false;
		}
	}

	async function startWorker() {
		await apiSafe('/validation/worker/start', { method: 'POST' });
		toast = 'Worker started';
		setTimeout(() => { loadAll(); toast = null; }, 2000);
	}

	function sevColor(sev: string): string {
		switch (sev) {
			case 'critical': return 'border-red-600 bg-red-600/10 text-red-400';
			case 'error':    return 'border-red-500/40 bg-red-500/10 text-red-400';
			case 'warn':     return 'border-amber-500/40 bg-amber-500/10 text-amber-400';
			case 'info':     return 'border-emerald-500/30 bg-emerald-500/5 text-emerald-400';
			default:         return 'border-border bg-card text-muted-foreground';
		}
	}

	function sevIcon(sev: string) {
		switch (sev) {
			case 'critical':
			case 'error':    return XCircle;
			case 'warn':     return AlertTriangle;
			case 'info':     return Check;
			default:         return Info;
		}
	}

	function formatAge(ts: string | null): string {
		if (!ts) return '—';
		const d = new Date(ts);
		const secs = (Date.now() - d.getTime()) / 1000;
		if (secs < 60) return `${Math.round(secs)}s ago`;
		if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
		if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
		return `${Math.round(secs / 86400)}d ago`;
	}

	function toggle(name: string) {
		expanded[name] = !expanded[name];
	}

	onMount(() => {
		loadAll();
		pollTimer = setInterval(loadAll, 30_000);
	});

	onDestroy(() => {
		if (pollTimer) clearInterval(pollTimer);
	});

	let visibleFindings = $derived.by(() => {
		if (!latest) return [];
		const f = latest.findings.slice().sort((a, b) => {
			const order: Record<string, number> = { critical: 0, error: 1, warn: 2, info: 3 };
			const so = (order[a.severity] ?? 9) - (order[b.severity] ?? 9);
			if (so !== 0) return so;
			return (b.count || 0) - (a.count || 0);
		});
		return severityFilter === 'issues_only' ? f.filter(x => x.count > 0 && x.severity !== 'info') : f;
	});

	let collapseChecks = $derived.by(() =>
		(latest?.findings || []).filter(f => COLLAPSE_CORRECTNESS_CHECKS.has(f.name))
	);
</script>

<svelte:head>
	<title>Validation — Enki</title>
</svelte:head>

<div class="p-4 space-y-4 max-w-7xl mx-auto">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-lg font-semibold flex items-center gap-2">
				<Shield class="w-5 h-5 text-primary" />
				Data Integrity
			</h1>
			<p class="text-xs text-muted-foreground mt-0.5">
				Automated checks across the collapse pipeline. Runs every 4 hours.
			</p>
		</div>
		<div class="flex items-center gap-2">
			<button onclick={loadAll} disabled={loading}
				class="text-xs px-2 py-1.5 rounded bg-muted text-muted-foreground hover:text-foreground inline-flex items-center gap-1.5 disabled:opacity-30">
				<RefreshCw class="w-3.5 h-3.5" /> Refresh
			</button>
			<a href="/validation/ldup-triage"
				class="text-xs px-2 py-1.5 rounded bg-muted text-muted-foreground hover:text-foreground inline-flex items-center gap-1.5">
				LDU-P Triage
			</a>
			<button onclick={runNow} disabled={running}
				class="text-xs px-3 py-1.5 rounded bg-primary text-primary-foreground hover:opacity-90 inline-flex items-center gap-1.5 disabled:opacity-30">
				<Play class="w-3.5 h-3.5" /> {running ? 'Running…' : 'Run Now'}
			</button>
		</div>
	</div>

	{#if toast}
		<div class="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs text-foreground">
			{toast}
		</div>
	{/if}

	<!-- Worker status -->
	{#if workerStatus !== null}
		<div class="rounded-lg border {workerStatus.running ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-amber-500/40 bg-amber-500/10'} px-3 py-2 flex items-center justify-between">
			<div class="flex items-center gap-2 text-xs">
				<span class="w-2 h-2 rounded-full {workerStatus.running ? 'bg-emerald-400' : 'bg-amber-400'}"></span>
				<span class="font-medium {workerStatus.running ? 'text-emerald-400' : 'text-amber-400'}">
					{workerStatus.running ? 'Worker running' : 'Worker stopped'}
				</span>
				<span class="text-muted-foreground">— phase: {workerStatus.phase}</span>
				{#if workerStatus.last_run_id}
					<span class="text-muted-foreground">· last run: #{workerStatus.last_run_id}</span>
				{/if}
				{#if workerStatus.errors > 0}
					<span class="text-red-400">· {workerStatus.errors} errors</span>
				{/if}
			</div>
			{#if !workerStatus.running}
				<button onclick={startWorker}
					class="text-[10px] px-2 py-0.5 rounded bg-muted text-muted-foreground hover:text-foreground">Start</button>
			{/if}
		</div>
	{/if}

	<!-- Latest-run summary -->
	{#if latest}
		<div class="grid grid-cols-5 gap-3">
			<div class="rounded-lg border border-border bg-card p-3">
				<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Run</div>
				<div class="text-2xl font-bold">#{latest.run_id}</div>
				<div class="text-[10px] text-muted-foreground mt-0.5">
					{formatAge(latest.run_finished_at)} · {latest.triggered_by}
				</div>
			</div>
			<div class="rounded-lg border border-border bg-card p-3">
				<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Checks</div>
				<div class="text-2xl font-bold">{latest.summary.total_checks}</div>
			</div>
			<div class="rounded-lg border border-red-500/30 bg-red-500/5 p-3">
				<div class="text-[10px] uppercase tracking-wider text-red-400">Errors</div>
				<div class="text-2xl font-bold text-red-400">{latest.summary.by_severity?.error || 0}</div>
			</div>
			<div class="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
				<div class="text-[10px] uppercase tracking-wider text-amber-400">Warnings</div>
				<div class="text-2xl font-bold text-amber-400">{latest.summary.by_severity?.warn || 0}</div>
			</div>
			<div class="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3">
				<div class="text-[10px] uppercase tracking-wider text-emerald-400">Clean</div>
				<div class="text-2xl font-bold text-emerald-400">
					{(latest.summary.total_checks || 0) - (latest.summary.checks_with_issues || 0)}
				</div>
			</div>
		</div>
	{/if}

	<!-- Collapse-correctness highlight (today's commit family) -->
	{#if collapseChecks.length > 0}
		<div class="rounded-lg border border-primary/30 bg-primary/5 p-3">
			<div class="text-xs font-semibold mb-2 flex items-center gap-1.5">
				<Shield class="w-3.5 h-3.5 text-primary" />
				Collapse-pipeline invariants
			</div>
			<div class="grid grid-cols-5 gap-2 text-xs">
				{#each collapseChecks as f (f.name)}
					<div class="rounded border {sevColor(f.severity)} px-2 py-1.5">
						<div class="font-mono text-[10px] opacity-80 truncate">{f.name}</div>
						<div class="text-lg font-bold mt-0.5">{f.count}</div>
					</div>
				{/each}
			</div>
		</div>
	{/if}

	<!-- Regressions (since last run) -->
	{#if regressions.length > 0}
		<div class="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
			<div class="text-xs font-semibold text-amber-400 mb-2 flex items-center gap-1.5">
				<AlertTriangle class="w-3.5 h-3.5" />
				Regressions since previous run ({regressions.length})
			</div>
			<div class="space-y-1">
				{#each regressions as r (r.check)}
					<div class="text-xs flex items-center justify-between font-mono">
						<span>{r.check}</span>
						<span class="text-amber-400">{r.prev_count} → {r.cur_count} (+{r.delta})</span>
					</div>
				{/each}
			</div>
		</div>
	{/if}

	<!-- Filter -->
	<div class="flex items-center gap-2">
		<span class="text-xs text-muted-foreground">Show:</span>
		<button onclick={() => severityFilter = 'issues_only'}
			class="text-xs px-2 py-1 rounded {severityFilter === 'issues_only' ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}">
			Issues only
		</button>
		<button onclick={() => severityFilter = 'all'}
			class="text-xs px-2 py-1 rounded {severityFilter === 'all' ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}">
			All checks
		</button>
	</div>

	<!-- Findings table -->
	{#if loading}
		<div class="text-center py-8 text-muted-foreground text-sm">Loading…</div>
	{:else if !latest}
		<div class="text-center py-8 text-muted-foreground text-sm">
			No validation run on record. Click <strong>Run Now</strong> above to baseline.
		</div>
	{:else if visibleFindings.length === 0}
		<div class="text-center py-8 text-emerald-400 text-sm">
			<Check class="w-5 h-5 mx-auto mb-2" />
			All checks clean.
		</div>
	{:else}
		<div class="rounded-lg border border-border overflow-hidden">
			{#each visibleFindings as f (f.name)}
				{@const Icon = sevIcon(f.severity)}
				<div class="border-b border-border last:border-0">
					<button onclick={() => toggle(f.name)}
						class="w-full text-left px-3 py-2 hover:bg-muted/30 flex items-center gap-3 transition-colors">
						<span class="text-muted-foreground">
							{#if expanded[f.name]}
								<ChevronDown class="w-3.5 h-3.5" />
							{:else}
								<ChevronRight class="w-3.5 h-3.5" />
							{/if}
						</span>
						<span class="text-xs px-1.5 py-0.5 rounded border {sevColor(f.severity)} inline-flex items-center gap-1">
							<Icon class="w-3 h-3" /> {f.severity}
						</span>
						<span class="font-mono text-xs flex-1 truncate">{f.name}</span>
						<span class="text-sm tabular-nums {f.count > 0 ? 'font-semibold' : 'text-muted-foreground'}">{f.count}</span>
						<span class="text-[10px] text-muted-foreground tabular-nums w-12 text-right">
							{f.query_ms ? f.query_ms + 'ms' : ''}
						</span>
					</button>
					{#if expanded[f.name]}
						<div class="px-3 pb-3 pl-12 space-y-2 text-xs">
							<div class="text-muted-foreground">{f.description}</div>
							{#if f.fix_hint}
								<div class="text-muted-foreground italic">
									<span class="font-semibold not-italic">Fix:</span> {f.fix_hint}
								</div>
							{/if}
							{#if f.sample && f.sample.length > 0}
								<div>
									<div class="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">Sample ({f.sample.length}):</div>
									<pre class="rounded bg-muted/30 p-2 overflow-x-auto text-[11px] leading-snug">{JSON.stringify(f.sample, null, 2)}</pre>
								</div>
							{/if}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>
