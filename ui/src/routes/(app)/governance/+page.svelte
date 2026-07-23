<script lang="ts">
	import { onMount } from 'svelte';
	import Shield from 'lucide-svelte/icons/shield';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import Check from 'lucide-svelte/icons/check';
	import X from 'lucide-svelte/icons/x';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import GitBranch from 'lucide-svelte/icons/git-branch';
	import Database from 'lucide-svelte/icons/database';
	import ArrowUp from 'lucide-svelte/icons/arrow-up';
	import Trash2 from 'lucide-svelte/icons/trash-2';
	import { api, apiSafe, ApiError } from '$lib/api/client';

	let loading = $state(true);
	let toast = $state<string | null>(null);
	let promoteTypeFor = $state<string | null>(null);
	let promoteSignalType = $state('');
	let conflicts = $state<any[]>([]);
	let summary = $state<any>({ counts: { urgent: 0, routine: 0, info: 0, total: 0 }, by_type: {} });
	let provisional = $state<any[]>([]);
	let chainState = $state<any>(null);
	let collapseStats = $state<any>(null);
	let recentEvents = $state<any[]>([]);
	let activeTab = $state<'conflicts' | 'ldu_p' | 'merge' | 'chain' | 'predicates'>('conflicts');
	let predicateQueue = $state<any[]>([]);
	let predicateEdgeTypes = $state<Array<{ edge_type: string; usage: number }>>([]);
	let predicateBusy = $state<number | null>(null);
	let predicateRemapFor = $state<number | null>(null);
	let predicateRemapValue = $state('');

	async function loadPredicateQueue() {
		const [q, et] = await Promise.all([
			apiSafe<{ items?: any[] }>('/governance/predicate-queue', { params: { status: 'pending', limit: 100 } }),
			apiSafe<{ edge_types?: any[] }>('/governance/predicate-queue/edge-types'),
		]);
		predicateQueue = q?.items || [];
		predicateEdgeTypes = et?.edge_types || [];
	}

	async function approvePredicate(queueId: number, edgeType?: string) {
		predicateBusy = queueId;
		try {
			const data = await api<any>(`/governance/predicate-queue/${queueId}/approve`, {
				method: 'POST',
				body: { reviewer: 'operator', ...(edgeType ? { edge_type: edgeType } : {}) },
			});
			toast = `Approved: ${data.lemma} → ${data.edge_type} · re-pended ${data.statements_repended} statements`;
			await loadPredicateQueue();
			predicateRemapFor = null;
		} catch (e) {
			toast = `Approve failed: ${e instanceof ApiError ? e.code || e.message : String(e)}`;
		}
		setTimeout(() => toast = null, 3500);
		predicateBusy = null;
	}

	async function rejectPredicate(queueId: number) {
		predicateBusy = queueId;
		try {
			const data = await api<any>(`/governance/predicate-queue/${queueId}/reject`, {
				method: 'POST',
				body: { reviewer: 'operator' },
			});
			toast = `Rejected: ${data.lemma}`;
			await loadPredicateQueue();
		} catch (e) {
			toast = `Reject failed: ${e instanceof Error ? e.message : String(e)}`;
		}
		setTimeout(() => toast = null, 2500);
		predicateBusy = null;
	}

	$effect(() => {
		if (activeTab === 'predicates') loadPredicateQueue();
	});
	let mergeCandidates = $state<any[]>([]);
	let merging = $state<string | null>(null);
	let gduHealth = $state<any>(null);
	let verifying = $state(false);
	let severityFilter = $state<string>('');

	async function loadAll() {
		loading = true;
		// Each call independent — a single hung endpoint must not block
		// the rest of the page. apiSafe() returns null on failure with the
		// client's default timeout (8 s here so a slow chain/state doesn't
		// stall the whole panel).
		const safe = <T,>(path: string, params?: Record<string, any>) =>
			apiSafe<T>(path, { params, timeoutMs: 8000 });
		const [confRes, summaryRes, lduPRes, chainRes, eventsRes, mergeRes, healthRes, collapseRes] = await Promise.all([
			safe<any>('/governance/conflicts', { reviewed: false, severity: severityFilter || undefined, limit: 100 }),
			safe<any>('/governance/conflicts/summary'),
			safe<any>('/governance/ldu-p', { status: 'pending', limit: 50 }),
			safe<any>('/governance/chain/state'),
			safe<any>('/governance/chain/recent-events', { limit: 20 }),
			safe<any>('/governance/merge-candidates', { limit: 20 }),
			safe<any>('/governance/chain/verification-status'),
			safe<any>('/governance/collapse-stats'),
		]);
		collapseStats = collapseRes;
		conflicts       = confRes?.conflicts || [];
		summary         = summaryRes || { counts: { urgent: 0, routine: 0, info: 0, total: 0 }, by_type: {} };
		provisional     = lduPRes?.entries || [];
		chainState      = chainRes || null;
		recentEvents    = eventsRes?.events || [];
		mergeCandidates = mergeRes?.candidates || [];
		gduHealth       = healthRes?.last_result || null;
		loading = false;
	}

	async function resolveConflict(conflictId: number, resolution: string, note: string = '') {
		try {
			await api(`/governance/conflicts/${conflictId}/resolve`, {
				method: 'POST',
				body: { resolution, note },
			});
			conflicts = conflicts.filter(c => c.conflict_id !== conflictId);
			await loadAll();
		} catch {}
	}

	async function promoteLduP(entryId: string, signalType: string) {
		try {
			await api(`/governance/ldu-p/${entryId}/promote`, {
				method: 'POST',
				body: { entity_type: signalType, resolved_by: 'governance_ui' },
			});
			toast = 'Promoted to canonical signal';
			promoteTypeFor = null;
			promoteSignalType = '';
			provisional = provisional.filter(p => p.ldu_p_id !== entryId && p.id !== entryId);
		} catch (e) {
			toast = e instanceof ApiError ? `Promote failed: ${e.message}` : 'Promote failed';
		}
		setTimeout(() => toast = null, 3000);
	}

	async function mergeEntities(keepId: number, mergeId: number) {
		merging = `${keepId}-${mergeId}`;
		try {
			await api('/governance/merge', {
				method: 'POST',
				body: { keep_id: keepId, merge_id: mergeId },
			});
			toast = 'Entities merged successfully';
			mergeCandidates = mergeCandidates.filter(c =>
				!(c.entity_a.entity_id === keepId && c.entity_b.entity_id === mergeId) &&
				!(c.entity_a.entity_id === mergeId && c.entity_b.entity_id === keepId)
			);
		} catch {
			toast = 'Merge failed';
		}
		merging = null;
		setTimeout(() => toast = null, 3000);
	}

	async function verifyChainNow() {
		verifying = true;
		try {
			gduHealth = await api<any>('/governance/chain/verify', { method: 'POST' });
			toast = gduHealth.valid ? 'Chain verified — all entries intact' : 'CHAIN BREAK DETECTED';
		} catch { toast = 'Verification failed'; }
		verifying = false;
		setTimeout(() => toast = null, 4000);
	}

	function dismissCandidate(a: number, b: number) {
		mergeCandidates = mergeCandidates.filter(c =>
			!(c.entity_a.entity_id === a && c.entity_b.entity_id === b)
		);
	}

	async function rejectLduP(entryId: string) {
		try {
			await api(`/governance/ldu-p/${entryId}/reject`, {
				method: 'PATCH',
				body: { resolved_by: 'governance_ui' },
			});
			toast = 'Entry rejected';
			provisional = provisional.filter(p => p.ldu_p_id !== entryId && p.id !== entryId);
		} catch {}
		setTimeout(() => toast = null, 3000);
	}

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	const severityColors: Record<string, string> = {
		urgent: 'border-red-500/40 bg-red-500/5',
		routine: 'border-amber-500/30 bg-amber-500/5',
		info: 'border-blue-500/20 bg-blue-500/5',
	};

	const conflictTypeLabels: Record<string, string> = {
		alias: 'Alias merge',
		type: 'Type ambiguity',
		property: 'Property contradiction',
		fact: 'Fact disagreement',
		merge_proposal: 'Merge proposal',
	};

	onMount(() => loadAll());
</script>

{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg">{toast}</div>
{/if}

<div class="space-y-4 max-w-5xl">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
				<Shield class="w-5 h-5 text-primary" /> Governance
			</h1>
			<p class="text-xs text-muted-foreground mt-0.5">
				Entity conflicts, provisional staging, and chain audit trail
			</p>
		</div>
		<button onclick={loadAll} class="p-1.5 rounded text-muted-foreground hover:text-foreground" title="Refresh">
			<RefreshCw class="w-4 h-4" />
		</button>
	</div>

	<!-- Node-collapse hero — headline numbers across the swarm -->
	{#if collapseStats && !collapseStats.error}
		<section class="rounded-xl border border-primary/30 bg-gradient-to-br from-primary/10 via-card to-card p-5">
			<div class="flex items-center justify-between mb-3">
				<div class="flex items-center gap-2">
					<span class="text-[10px] uppercase tracking-[0.18em] text-primary/80">Node collapse</span>
					{#if collapseStats.last_hour_admissions > 0}
						<span class="inline-flex items-center gap-1 text-[10px] text-emerald-400/90">
							<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
							{collapseStats.last_hour_admissions} in the last hour
						</span>
					{/if}
				</div>
				<span class="text-[10px] text-muted-foreground/70">all decisions hash-chained</span>
			</div>
			<div class="grid grid-cols-2 md:grid-cols-4 gap-4">
				<div>
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Admission decisions</div>
					<div class="text-3xl font-bold tabular-nums text-foreground">
						{(collapseStats.total_admissions ?? 0).toLocaleString()}
					</div>
					<div class="text-[10px] text-muted-foreground/70 mt-0.5">total since first signal</div>
				</div>
				<div>
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Collapsed / strengthened</div>
					<div class="text-3xl font-bold tabular-nums text-emerald-400">
						{(collapseStats.merged_count ?? 0).toLocaleString()}
					</div>
					<div class="text-[10px] text-muted-foreground/70 mt-0.5">merged into existing canon</div>
				</div>
				<div>
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Collapse rate</div>
					<div class="text-3xl font-bold tabular-nums text-primary">
						{Math.round((collapseStats.collapse_rate ?? 0) * 100)}%
					</div>
					<div class="text-[10px] text-muted-foreground/70 mt-0.5">1 in {Math.round(1 / Math.max(collapseStats.collapse_rate ?? 0.01, 0.01))} known on arrival</div>
				</div>
				<div>
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Knowledge graph</div>
					<div class="text-3xl font-bold tabular-nums text-foreground">
						{(collapseStats.entities_total ?? 0).toLocaleString()}
					</div>
					<div class="text-[10px] text-muted-foreground/70 mt-0.5">
						canonical entities · {(collapseStats.edges_total ?? 0).toLocaleString()} edges
					</div>
				</div>
			</div>
			<div class="mt-4 pt-3 border-t border-border/40 flex items-center gap-6 text-[11px] text-muted-foreground">
				<span class="font-mono">
					{#each Object.entries(collapseStats.by_operation || {}) as [op, n]}
						<span class="mr-3"><span class="text-foreground">{op}</span> <span class="text-foreground/60">{n}</span></span>
					{/each}
				</span>
				<span class="ml-auto flex items-center gap-1.5">
					{#if collapseStats.chain?.hash}
						<span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
						<span class="text-emerald-400/90">chain ok</span>
						<span class="font-mono text-muted-foreground/60">{collapseStats.chain.hash}</span>
					{:else}
						<span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>
						<span class="text-amber-400/90">chain not yet initialized</span>
					{/if}
				</span>
			</div>
		</section>
	{/if}

	<!-- Conflict / staging stats -->
	<div class="grid grid-cols-4 gap-3">
		<div class="rounded-lg border border-red-500/30 bg-red-500/5 p-3">
			<div class="text-[10px] text-red-400 uppercase tracking-wider">Urgent</div>
			<div class="text-2xl font-bold text-red-400">{summary.counts.urgent}</div>
		</div>
		<div class="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
			<div class="text-[10px] text-amber-400 uppercase tracking-wider">Routine</div>
			<div class="text-2xl font-bold text-amber-400">{summary.counts.routine}</div>
		</div>
		<div class="rounded-lg border border-border bg-card p-3">
			<div class="text-[10px] text-muted-foreground uppercase tracking-wider">Provisional</div>
			<div class="text-2xl font-bold text-foreground">{provisional.length}</div>
		</div>
		<div class="rounded-lg border border-border bg-card p-3">
			<div class="text-[10px] text-muted-foreground uppercase tracking-wider">Mutations</div>
			<div class="text-2xl font-bold text-foreground">{chainState?.mutation_count || 0}</div>
		</div>
	</div>

	<!-- GDU Health -->
	{#if gduHealth}
		<div class="rounded-lg border {gduHealth.valid ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-red-500/30 bg-red-500/5'} p-3">
			<div class="flex items-center justify-between">
				<div class="flex items-center gap-2">
					<span class="w-2 h-2 rounded-full {gduHealth.valid ? 'bg-emerald-400' : 'bg-red-400 animate-pulse'}"></span>
					<span class="text-xs font-semibold {gduHealth.valid ? 'text-emerald-400' : 'text-red-400'}">
						{gduHealth.valid ? 'Chain Integrity Verified' : `Chain Broken — ${gduHealth.breaks_found} breaks`}
					</span>
				</div>
				<button
					onclick={verifyChainNow}
					disabled={verifying}
					class="text-[10px] px-2 py-1 rounded bg-muted text-muted-foreground hover:text-foreground disabled:opacity-30"
				>{verifying ? 'Verifying...' : 'Verify Now'}</button>
			</div>
			<div class="flex items-center gap-4 mt-2 text-[10px] text-muted-foreground">
				<span>{gduHealth.total_nodes || 0} total nodes</span>
				<span class="{gduHealth.ungoverned_nodes > 0 ? 'text-amber-400' : ''}">{gduHealth.ungoverned_nodes || 0} ungoverned</span>
				<span class="{gduHealth.nodes_without_address > 0 ? 'text-amber-400' : ''}">{gduHealth.nodes_without_address || 0} without address</span>
				<span>{gduHealth.entries_checked || 0} chain entries</span>
			</div>
		</div>
	{/if}

	<!-- Tabs -->
	<div class="flex items-center gap-1 border-b border-border">
		<button onclick={() => activeTab = 'conflicts'}
			class="flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 transition-colors
				{activeTab === 'conflicts' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}">
			<AlertTriangle class="w-3.5 h-3.5" /> Conflicts ({summary.counts.total})
		</button>
		<button onclick={() => activeTab = 'ldu_p'}
			class="flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 transition-colors
				{activeTab === 'ldu_p' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}">
			<Database class="w-3.5 h-3.5" /> Provisional ({provisional.length})
		</button>
		<button onclick={() => activeTab = 'merge'}
			class="flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 transition-colors
				{activeTab === 'merge' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}">
			<GitBranch class="w-3.5 h-3.5" /> Merge ({mergeCandidates.length})
		</button>
		<button onclick={() => activeTab = 'chain'}
			class="flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 transition-colors
				{activeTab === 'chain' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}">
			<GitBranch class="w-3.5 h-3.5" /> Chain
		</button>
		<button onclick={() => activeTab = 'predicates'}
			class="flex items-center gap-1.5 px-3 py-2 text-xs font-medium border-b-2 transition-colors
				{activeTab === 'predicates' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}">
			<Shield class="w-3.5 h-3.5" /> Predicates ({predicateQueue.length})
		</button>
	</div>

	{#if loading}
		<div class="text-center py-8 text-muted-foreground">Loading...</div>

	{:else if activeTab === 'conflicts'}
		<!-- Conflict filters -->
		<div class="flex items-center gap-2">
			<span class="text-xs text-muted-foreground">Filter:</span>
			<button onclick={() => { severityFilter = ''; loadAll(); }}
				class="text-[10px] px-2 py-0.5 rounded {severityFilter === '' ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}">All</button>
			<button onclick={() => { severityFilter = 'urgent'; loadAll(); }}
				class="text-[10px] px-2 py-0.5 rounded {severityFilter === 'urgent' ? 'bg-red-500/20 text-red-400 border border-red-500/40' : 'bg-muted text-muted-foreground'}">Urgent</button>
			<button onclick={() => { severityFilter = 'routine'; loadAll(); }}
				class="text-[10px] px-2 py-0.5 rounded {severityFilter === 'routine' ? 'bg-amber-500/20 text-amber-400 border border-amber-500/40' : 'bg-muted text-muted-foreground'}">Routine</button>
		</div>

		{#if conflicts.length === 0}
			<div class="text-center py-12 text-muted-foreground">
				<Check class="w-8 h-8 mx-auto mb-2 text-emerald-400/50" />
				<p class="text-sm">No conflicts to review</p>
				<p class="text-xs mt-1">The system is in agreement with itself.</p>
			</div>
		{:else}
			<div class="space-y-2">
				{#each conflicts as c}
					<div class="rounded-lg border {severityColors[c.severity] || 'border-border'} p-3">
						<div class="flex items-start justify-between mb-2">
							<div class="flex items-center gap-2">
								<span class="text-[9px] px-1.5 py-0.5 rounded uppercase tracking-wider font-semibold
									{c.severity === 'urgent' ? 'bg-red-500/20 text-red-400' :
									 c.severity === 'routine' ? 'bg-amber-500/20 text-amber-400' :
									 'bg-blue-500/20 text-blue-400'}">{c.severity}</span>
								<span class="text-xs font-semibold text-foreground">{conflictTypeLabels[c.conflict_type] || c.conflict_type}</span>
								{#if c.entity_name}
									<span class="text-xs text-muted-foreground">·</span>
									{#if c.entity_id}
										<a href={`/entity/${c.entity_id}`} class="text-xs text-foreground hover:text-primary">{c.entity_name}</a>
									{:else}
										<span class="text-xs text-foreground">{c.entity_name}</span>
									{/if}
									{#if c.entity_type}
										<span class="text-[9px] px-1 py-0.5 rounded bg-muted text-muted-foreground">{c.entity_type}</span>
									{/if}
								{/if}
							</div>
							<span class="text-[10px] text-muted-foreground">{timeAgo(c.detected_at)}</span>
						</div>

						<!-- Competing values -->
						<div class="space-y-1 mb-3 ml-2">
							{#each c.competing_values as cv, i}
								<div class="flex items-center gap-2 text-xs">
									<span class="text-muted-foreground/50 w-4">{String.fromCharCode(65 + i)}.</span>
									<span class="text-foreground font-medium">{cv.value}</span>
									{#if cv.source}
										<span class="text-[10px] text-muted-foreground">— {cv.source}</span>
									{/if}
									{#if cv.confidence}
										<span class="text-[10px] text-muted-foreground/50">({Math.round(cv.confidence * 100)}%)</span>
									{/if}
								</div>
							{/each}
						</div>

						{#if c.metadata && Object.keys(c.metadata).length > 0}
							<div class="text-[10px] text-muted-foreground/60 mb-2 ml-2">
								{#each Object.entries(c.metadata) as [k, v]}
									<span>{k}: {JSON.stringify(v)}</span>
									<span class="mx-1">·</span>
								{/each}
							</div>
						{/if}

						<!-- Actions -->
						<div class="flex items-center gap-2 pt-2 border-t border-border/30">
							{#each c.competing_values as cv, i}
								<button onclick={() => resolveConflict(c.conflict_id, `accepted_${String.fromCharCode(97 + i)}`, `Accepted: ${cv.value}`)}
									class="text-[10px] px-2 py-1 rounded bg-primary/10 text-primary hover:bg-primary/20">
									Accept {String.fromCharCode(65 + i)}
								</button>
							{/each}
							<button onclick={() => resolveConflict(c.conflict_id, 'merge', 'Merged variants')}
								class="text-[10px] px-2 py-1 rounded bg-muted text-muted-foreground hover:text-foreground">Merge</button>
							<button onclick={() => resolveConflict(c.conflict_id, 'split', 'Split into separate entities')}
								class="text-[10px] px-2 py-1 rounded bg-muted text-muted-foreground hover:text-foreground">Split</button>
							<button onclick={() => resolveConflict(c.conflict_id, 'skip', 'Skipped — needs more info')}
								class="ml-auto text-[10px] px-2 py-1 rounded text-muted-foreground hover:text-foreground">Skip</button>
						</div>

						<div class="text-[9px] text-muted-foreground/50 mt-1">Detected by: {c.detected_by}</div>
					</div>
				{/each}
			</div>
		{/if}

	{:else if activeTab === 'ldu_p'}
		{#if provisional.length === 0}
			<div class="text-center py-12 text-muted-foreground">
				<Database class="w-8 h-8 mx-auto mb-2 opacity-30" />
				<p class="text-sm">No provisional entities pending</p>
				<p class="text-xs mt-1">All extracted entities passed GDU vocab check.</p>
			</div>
		{:else}
			<div class="space-y-2">
				{#each provisional as p}
					{@const entryId = p.ldu_p_id || p.id}
					<div class="rounded-lg border border-border bg-card p-3">
						<div class="flex items-center justify-between">
							<div class="flex items-center gap-2">
								<span class="text-xs font-semibold text-foreground">{p.raw_name || p.proposed_signal_type || 'Unknown'}</span>
								<span class="text-[9px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400">{p.suspected_type || p.proposed_signal_type || '?'}</span>
								<span class="text-[10px] text-muted-foreground">from {p.source_pipeline || p.parser_used || 'extraction'}</span>
							</div>
							<span class="text-[10px] text-muted-foreground">{timeAgo(p.created_at)}</span>
						</div>
						{#if p.rejection_reason}
							<p class="text-[10px] text-muted-foreground/70 mt-1">{p.rejection_reason}</p>
						{/if}
						{#if p.evidence || p.extracted_structure}
							<p class="text-[10px] text-muted-foreground mt-1 truncate max-w-lg">
								{p.evidence || JSON.stringify(p.extracted_structure || {}).slice(0, 150)}
							</p>
						{/if}

						<!-- Promote / Reject actions -->
						<div class="flex items-center gap-2 mt-2 pt-2 border-t border-border/30">
							{#if promoteTypeFor === entryId}
								<input
									type="text"
									bind:value={promoteSignalType}
									placeholder="signal.type.name"
									class="text-xs px-2 py-1 rounded border border-border bg-background text-foreground w-48"
								/>
								<button
									onclick={() => promoteLduP(entryId, promoteSignalType)}
									disabled={!promoteSignalType.trim()}
									class="text-[10px] px-2 py-1 rounded bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 disabled:opacity-30"
								>Confirm</button>
								<button
									onclick={() => { promoteTypeFor = null; promoteSignalType = ''; }}
									class="text-[10px] px-2 py-1 rounded text-muted-foreground hover:text-foreground"
								>Cancel</button>
							{:else}
								<button
									onclick={() => { promoteTypeFor = entryId; promoteSignalType = p.proposed_signal_type || p.suspected_type || ''; }}
									class="flex items-center gap-1 text-[10px] px-2 py-1 rounded bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20"
								>
									<ArrowUp class="w-3 h-3" /> Promote
								</button>
								<button
									onclick={() => rejectLduP(entryId)}
									class="flex items-center gap-1 text-[10px] px-2 py-1 rounded bg-red-500/10 text-red-400 hover:bg-red-500/20"
								>
									<Trash2 class="w-3 h-3" /> Reject
								</button>
							{/if}
						</div>
					</div>
				{/each}
			</div>
		{/if}

	{:else if activeTab === 'merge'}
		{#if mergeCandidates.length === 0}
			<div class="text-center py-12 text-muted-foreground">
				<Check class="w-8 h-8 mx-auto mb-2 text-emerald-400/50" />
				<p class="text-sm">No merge candidates found</p>
				<p class="text-xs mt-1">Entity deduplication is clean.</p>
			</div>
		{:else}
			<div class="space-y-2">
				{#each mergeCandidates as c}
					{@const isMerging = merging === `${c.entity_a.entity_id}-${c.entity_b.entity_id}`}
					<div class="rounded-lg border border-border bg-card p-3">
						<div class="flex items-center justify-between mb-2">
							<span class="text-[9px] px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 font-semibold">{Math.round(c.similarity * 100)}% similar</span>
							<span class="text-[9px] text-muted-foreground">{c.entity_a.type}</span>
						</div>
						<div class="grid grid-cols-2 gap-3 mb-3">
							<a href={`/entity/${c.entity_a.entity_id}`}
								class="rounded border border-border/50 p-2 hover:border-primary/40 hover:bg-primary/5 transition-colors block">
								<p class="text-xs font-semibold text-foreground hover:text-primary">{c.entity_a.name}</p>
								<p class="text-[10px] text-muted-foreground">{c.entity_a.signal_count} signals</p>
							</a>
							<a href={`/entity/${c.entity_b.entity_id}`}
								class="rounded border border-border/50 p-2 hover:border-primary/40 hover:bg-primary/5 transition-colors block">
								<p class="text-xs font-semibold text-foreground hover:text-primary">{c.entity_b.name}</p>
								<p class="text-[10px] text-muted-foreground">{c.entity_b.signal_count} signals</p>
							</a>
						</div>
						<div class="flex items-center gap-2 pt-2 border-t border-border/30">
							<button
								onclick={() => mergeEntities(
									c.entity_a.signal_count >= c.entity_b.signal_count ? c.entity_a.entity_id : c.entity_b.entity_id,
									c.entity_a.signal_count >= c.entity_b.signal_count ? c.entity_b.entity_id : c.entity_a.entity_id
								)}
								disabled={isMerging}
								class="text-[10px] px-2.5 py-1 rounded bg-purple-500/10 text-purple-400 hover:bg-purple-500/20 disabled:opacity-30"
							>
								{isMerging ? 'Merging...' : 'Merge (keep higher count)'}
							</button>
							<button
								onclick={() => dismissCandidate(c.entity_a.entity_id, c.entity_b.entity_id)}
								class="text-[10px] px-2 py-1 rounded text-muted-foreground hover:text-foreground"
							>Not duplicates</button>
						</div>
					</div>
				{/each}
			</div>
		{/if}

	{:else if activeTab === 'chain'}
		<!-- Chain state -->
		{#if chainState?.initialized}
			<div class="rounded-lg border border-border bg-card p-3 space-y-2">
				<div class="flex items-center justify-between">
					<span class="text-xs text-muted-foreground">Current chain hash</span>
					<code class="text-[10px] font-mono text-foreground">{chainState.chain_hash}</code>
				</div>
				<div class="flex items-center justify-between">
					<span class="text-xs text-muted-foreground">Total mutations</span>
					<span class="text-xs font-mono text-foreground">{chainState.mutation_count}</span>
				</div>
				<div class="flex items-center justify-between">
					<span class="text-xs text-muted-foreground">Last update</span>
					<span class="text-xs text-foreground">{timeAgo(chainState.updated_at)}</span>
				</div>
			</div>
		{:else}
			<div class="rounded-lg border border-border bg-card p-3 text-xs text-muted-foreground">
				Chain not yet initialized. First entity mutation will create the genesis hash.
			</div>
		{/if}

		<!-- Recent events -->
		<div>
			<h3 class="text-xs font-semibold text-muted-foreground mb-2 uppercase tracking-wider">Recent Mutations</h3>
			{#if recentEvents.length === 0}
				<p class="text-xs text-muted-foreground">No mutation events yet.</p>
			{:else}
				<div class="space-y-1">
					{#each recentEvents as e}
						<div class="flex items-center gap-3 text-xs py-1.5 px-2 rounded hover:bg-muted/30">
							<span class="text-[9px] px-1.5 py-0.5 rounded font-medium
								{e.mutation_type === 'create' ? 'bg-emerald-500/10 text-emerald-400' :
								 e.mutation_type === 'merge' ? 'bg-purple-500/10 text-purple-400' :
								 e.mutation_type === 'alias_add' ? 'bg-blue-500/10 text-blue-400' :
								 'bg-muted text-muted-foreground'}">{e.mutation_type}</span>
							<span class="text-foreground flex-1">
								{#if e.payload?.name}
									{#if e.entity_ids?.length}
										<a href={`/entity/${e.entity_ids[0]}`} class="hover:text-primary">{e.payload.name}</a>
									{:else}
										{e.payload.name}
									{/if}
								{/if}
								{#if e.payload?.new_alias}new alias: "{e.payload.new_alias}"{/if}
								{#if e.entity_ids?.length && !e.payload?.name}
									<a href={`/entity/${e.entity_ids[0]}`} class="text-muted-foreground/70 hover:text-primary">entity {e.entity_ids[0]}</a>
								{/if}
							</span>
							<span class="text-[9px] text-muted-foreground/50 font-mono">{e.chain_hash}</span>
							<span class="text-[10px] text-muted-foreground">{timeAgo(e.commit_time)}</span>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{:else if activeTab === 'predicates'}
		<!-- Predicate review queue -->
		<div class="space-y-2">
			<p class="text-xs text-muted-foreground">
				Verbs the LLM extracted from articles but couldn't auto-promote at high confidence
				(≥0.93). Approve to add the mapping to <code class="px-1 rounded bg-muted">predicate_dictionary</code>
				and re-pend statements; reject if the lemma is junk.
			</p>

			{#if predicateQueue.length === 0}
				<div class="rounded-lg border border-dashed border-border p-6 text-center">
					<p class="text-sm text-muted-foreground">No predicates awaiting review.</p>
					<p class="text-[11px] text-muted-foreground/70 mt-1">
						Worker runs every 5 min on rejected statements. Auto-promotes at confidence ≥0.93.
					</p>
				</div>
			{:else}
				<div class="space-y-2">
					{#each predicateQueue as item (item.queue_id)}
						<div class="rounded-lg border border-border bg-card p-3">
							<div class="flex items-start gap-3">
								<div class="flex-1 min-w-0">
									<div class="flex items-baseline gap-3 flex-wrap">
										<code class="text-base font-bold text-foreground">{item.candidate_lemma}</code>
										<span class="text-[10px] text-muted-foreground">{item.occurrences} occurrences</span>
										<span class="text-[10px] text-muted-foreground">·</span>
										<span class="text-[10px] text-muted-foreground">{item.proposed_by}</span>
										{#if item.proposed_edge_type}
											<span class="text-xs text-muted-foreground">proposes →</span>
											<code class="text-xs px-1.5 py-0.5 rounded bg-primary/10 text-primary border border-primary/20">
												{item.proposed_edge_type}
											</code>
											<span class="text-[10px] font-mono
												{item.proposed_confidence >= 0.85 ? 'text-amber-400' : 'text-muted-foreground'}">
												{(item.proposed_confidence * 100).toFixed(0)}%
											</span>
										{:else}
											<span class="text-xs text-muted-foreground italic">no edge_type proposed (LLM said none_apply)</span>
										{/if}
									</div>

									{#if item.evidence?.llm_reasoning}
										<p class="text-[11px] text-muted-foreground italic mt-1">
											LLM: {item.evidence.llm_reasoning}
										</p>
									{/if}

									{#if item.evidence?.sample_sentences && item.evidence.sample_sentences.length > 0}
										<details class="mt-2">
											<summary class="text-[10px] text-muted-foreground cursor-pointer hover:text-foreground">
												show {item.evidence.sample_sentences.length} sample sentences
											</summary>
											<ul class="mt-1.5 space-y-1 pl-3 border-l-2 border-border/50">
												{#each item.evidence.sample_sentences.slice(0, 5) as s}
													<li class="text-[11px] text-muted-foreground">"{(s || '').slice(0, 180)}…"</li>
												{/each}
											</ul>
										</details>
									{/if}

									{#if predicateRemapFor === item.queue_id}
										<div class="mt-2 flex items-center gap-2">
											<select bind:value={predicateRemapValue}
												class="flex-1 text-xs rounded-md bg-muted px-2 py-1 text-foreground border border-border outline-none focus:ring-1 focus:ring-ring">
												<option value="">— pick edge_type —</option>
												{#each predicateEdgeTypes as et}
													<option value={et.edge_type}>{et.edge_type} ({et.usage})</option>
												{/each}
											</select>
											<button onclick={() => approvePredicate(item.queue_id, predicateRemapValue)}
												disabled={predicateBusy === item.queue_id || !predicateRemapValue}
												class="px-2 py-1 text-[11px] rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
												Apply
											</button>
											<button onclick={() => { predicateRemapFor = null; predicateRemapValue = ''; }}
												class="px-2 py-1 text-[11px] rounded-md text-muted-foreground hover:text-foreground">
												Cancel
											</button>
										</div>
									{/if}
								</div>

								{#if predicateRemapFor !== item.queue_id}
									<div class="flex flex-col gap-1 shrink-0">
										{#if item.proposed_edge_type}
											<button onclick={() => approvePredicate(item.queue_id)}
												disabled={predicateBusy === item.queue_id}
												class="flex items-center gap-1 px-2 py-1 text-[11px] rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/20 disabled:opacity-50">
												<Check class="w-3 h-3" /> Approve
											</button>
										{/if}
										<button onclick={() => { predicateRemapFor = item.queue_id; predicateRemapValue = item.proposed_edge_type || ''; }}
											disabled={predicateBusy === item.queue_id}
											class="flex items-center gap-1 px-2 py-1 text-[11px] rounded-md bg-amber-500/10 text-amber-400 border border-amber-500/30 hover:bg-amber-500/20 disabled:opacity-50">
											<ArrowUp class="w-3 h-3" /> Remap
										</button>
										<button onclick={() => rejectPredicate(item.queue_id)}
											disabled={predicateBusy === item.queue_id}
											class="flex items-center gap-1 px-2 py-1 text-[11px] rounded-md bg-red-500/10 text-red-400 border border-red-500/30 hover:bg-red-500/20 disabled:opacity-50">
											<X class="w-3 h-3" /> Reject
										</button>
									</div>
								{/if}
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</div>
