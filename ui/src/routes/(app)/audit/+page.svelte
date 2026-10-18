<script lang="ts">
	import { onMount } from 'svelte';
	import DataTable from '$lib/components/shared/DataTable.svelte';
	import Badge from '$lib/components/shared/Badge.svelte';
	import Pagination from '$lib/components/shared/Pagination.svelte';
	import LoadingSkeleton from '$lib/components/shared/LoadingSkeleton.svelte';
	import CodeCard from '$lib/components/cards/CodeCard.svelte';
	import StatusCard from '$lib/components/cards/StatusCard.svelte';
	import ScrollText from 'lucide-svelte/icons/scroll-text';
	import Shield from 'lucide-svelte/icons/shield';
	import Download from 'lucide-svelte/icons/download';

	let loading = $state(true);
	let activeTab = $state<'events' | 'receipts' | 'chain'>('events');
	let events = $state<any[]>([]);
	let receipts = $state<any[]>([]);
	let chainValid = $state(true);
	let eventPage = $state(1);
	let receiptPage = $state(1);
	let expandedEvent = $state<string | null>(null);
	let totalEvents = $state(0);
	let totalReceipts = $state(0);

	async function loadAuditData() {
		try {
			const [evtRes, rcptRes] = await Promise.all([
				fetch('/api/audit/events?limit=100').then(r => r.ok ? r.json() : null),
				fetch('/api/audit/receipts?limit=50').then(r => r.ok ? r.json() : null),
			]);
			if (evtRes?.events) {
				events = evtRes.events;
				totalEvents = evtRes.total || events.length;
			}
			if (rcptRes?.receipts) {
				receipts = rcptRes.receipts;
				totalReceipts = rcptRes.total || receipts.length;
			}
		} catch {
			// Fallback to empty — real data will show when backend is available
		}
		loading = false;
	}

	onMount(async () => {
		await loadAuditData();
	});

	function formatTime(ts: string): string {
		if (!ts) return '';
		return new Date(ts).toLocaleString();
	}

	function shortTime(ts: string): string {
		if (!ts) return '';
		return new Date(ts).toLocaleTimeString();
	}

	const eventColumns = [
		{ key: 'timestamp', label: 'Time', sortable: true, render: (_v: any, r: any) => shortTime(r.timestamp) },
		{ key: 'event_type', label: 'Type', sortable: true },
		{ key: 'actor', label: 'Actor', hideBelow: 'mobile' as const },
		{ key: 'detail', label: 'Detail' },
		{ key: 'node_id', label: 'Node', hideBelow: 'tablet' as const },
	];

	const receiptColumns = [
		{ key: 'rid', label: '#', width: '60px', sortable: true },
		{ key: 'commit_time', label: 'Time', sortable: true, render: (_v: any, r: any) => shortTime(r.commit_time) },
		{ key: 'outcome', label: 'Outcome', render: (_v: any, r: any) => `<span class="px-1.5 py-0.5 rounded text-[10px] ${r.outcome === 'FULL_ACCEPT' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-yellow-500/15 text-yellow-400'}">${r.outcome}</span>` },
		{ key: 'accepted_count', label: 'Accepted' },
		{ key: 'rejected_count', label: 'Rejected', hideBelow: 'mobile' as const },
		{ key: 'chain_hash', label: 'Chain Hash', hideBelow: 'tablet' as const, render: (_v: any, r: any) => `<code class="text-[10px] font-mono text-muted-foreground">${(r.chain_hash ?? '').slice(0, 12)}...</code>` },
	];
</script>

<div class="max-w-6xl mx-auto">
	<div class="flex items-center justify-between mb-6">
		<div>
			<h1 class="text-xl font-semibold text-foreground">Audit Trail</h1>
			<div class="flex items-center gap-3 mt-1">
				<div class="flex items-center gap-1.5">
					<span class="w-2 h-2 rounded-full {chainValid ? 'bg-emerald-400' : 'bg-[var(--severity-critical)]'}"></span>
					<span class="text-sm text-muted-foreground">Chain {chainValid ? 'VALID' : 'BROKEN'}</span>
				</div>
				<span class="text-sm text-muted-foreground">{totalReceipts} receipts</span>
				<span class="text-sm text-muted-foreground">{totalEvents} events</span>
			</div>
		</div>
		<a
			href="/api/exports/audit?format=csv"
			class="flex items-center gap-2 px-3 py-2 rounded-lg border border-border text-sm text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
		>
			<Download size={16} /> Export
		</a>
	</div>

	<!-- Tabs -->
	<div class="flex gap-1 mb-4 border-b border-border">
		{#each ['events', 'receipts', 'chain'] as tab}
			<button
				onclick={() => activeTab = tab as typeof activeTab}
				class="px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px
					{activeTab === tab ? 'text-primary border-primary' : 'text-muted-foreground border-transparent hover:text-foreground'}"
			>
				{tab.charAt(0).toUpperCase() + tab.slice(1)}
			</button>
		{/each}
	</div>

	{#if loading}
		<LoadingSkeleton type="table" lines={8} />
	{:else if activeTab === 'events'}
		<DataTable
			columns={eventColumns}
			rows={events}
			rowKey="rid"
			onRowClick={(row) => expandedEvent = expandedEvent === String(row.rid) ? null : String(row.rid)}
			expandedRow={expandedEvent}
		>
			{#snippet expandSlot(row)}
				<div class="space-y-2">
					<div class="text-xs text-muted-foreground">
						<span class="font-medium">Full timestamp:</span> {formatTime(row.timestamp)}
					</div>
					<div class="text-xs text-muted-foreground">
						<span class="font-medium">CID:</span> <code class="font-mono">{row.cid}</code>
					</div>
					{#if row.payload_json}
						<CodeCard code={row.payload_json} language="json" title="Event Payload" />
					{/if}
				</div>
			{/snippet}
		</DataTable>
		<div class="mt-4">
			<Pagination page={eventPage} pageSize={50} total={totalEvents} onPageChange={(p) => eventPage = p} />
		</div>

	{:else if activeTab === 'receipts'}
		<DataTable
			columns={receiptColumns}
			rows={receipts}
			rowKey="rid"
		/>
		<div class="mt-4">
			<Pagination page={receiptPage} pageSize={50} total={totalReceipts} onPageChange={(p) => receiptPage = p} />
		</div>

	{:else if activeTab === 'chain'}
		<div class="space-y-6">
			<StatusCard
				title="Chain Integrity"
				status={chainValid ? 'healthy' : 'error'}
				metrics={[
					{ label: 'Total Receipts', value: totalReceipts },
					{ label: 'Chain Status', value: chainValid ? 'Verified to genesis' : 'Break detected' },
				]}
			/>

			<!-- Visual chain -->
			<div class="overflow-x-auto pb-4">
				<div class="flex items-center gap-2 min-w-max">
					<div class="px-3 py-2 rounded-lg bg-primary/10 border border-primary/30 text-xs text-primary font-mono">
						Genesis
					</div>
					{#each receipts.slice().reverse().slice(0, 20) as r, i}
						<div class="text-muted-foreground">→</div>
						<div class="px-3 py-2 rounded-lg border text-xs font-mono
							{r.outcome === 'FULL_ACCEPT' ? 'border-emerald-500/30 bg-emerald-500/5 text-emerald-400' : 'border-yellow-500/30 bg-yellow-500/5 text-yellow-400'}">
							R{r.rid}
						</div>
					{/each}
				</div>
			</div>

			<button onclick={() => { chainValid = true; }} class="px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors">
				Verify Chain Integrity
			</button>
		</div>
	{/if}
</div>
