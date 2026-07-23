<script lang="ts">
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import { screen } from '$lib/stores/screen.svelte';
	import KanbanBoard from '$lib/components/shared/KanbanBoard.svelte';
	import type { KanbanColumn } from '$lib/components/shared/KanbanBoard.svelte';
	import DataTable from '$lib/components/shared/DataTable.svelte';
	import type { ColumnDef } from '$lib/components/shared/DataTable.svelte';
	import CaseCard from '$lib/components/cards/CaseCard.svelte';
	import Badge from '$lib/components/shared/Badge.svelte';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import Plus from 'lucide-svelte/icons/plus';
	import X from 'lucide-svelte/icons/x';
	import LayoutGrid from 'lucide-svelte/icons/layout-grid';
	import List from 'lucide-svelte/icons/list';
	import Search from 'lucide-svelte/icons/search';
	import MobileSpaceTabs from '$lib/components/layout/MobileSpaceTabs.svelte';

	const watchTabs = [
		{ label: 'Feed', href: '/intelligence' },
		{ label: 'Topics', href: '/watch' },
		{ label: 'Alerts', href: '/governance' },
		{ label: 'Cases', href: '/cases' },
		{ label: 'Timeline', href: '/timeline' },
	];

	let cases = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let toast = $state<string | null>(null);

	// View toggle
	let view = $state<'kanban' | 'list'>(screen.isMobile ? 'list' : 'kanban');

	// Search
	let searchQuery = $state('');

	// New case modal
	let showNewCase = $state(false);
	let newTitle = $state('');
	let newPriority = $state('P3');
	let newDescription = $state('');
	let creating = $state(false);

	// Kanban columns
	const kanbanStatuses = [
		{ id: 'open', title: 'Open' },
		{ id: 'investigating', title: 'Investigating' },
		{ id: 'resolved', title: 'Resolved' },
		{ id: 'closed', title: 'Closed' },
	];

	let kanbanColumns = $derived<KanbanColumn[]>(
		kanbanStatuses.map(s => ({
			id: s.id,
			title: s.title,
			items: cases.filter(c => c.status === s.id),
		}))
	);

	// DataTable columns
	const statusVariant: Record<string, string> = {
		open: 'info', investigating: 'medium', resolved: 'success', closed: 'neutral',
	};

	const priorityVariant: Record<string, string> = {
		P1: 'critical', P2: 'high', P3: 'medium', P4: 'low',
		critical: 'critical', high: 'high', medium: 'medium', low: 'low',
	};

	const tableColumns: ColumnDef[] = [
		{ key: 'case_id', label: 'Case ID', width: '120px', sortable: true,
			render: (val: string) => `<span class="font-mono text-xs text-muted-foreground">${val?.slice(0, 12) ?? ''}</span>`,
		},
		{ key: 'title', label: 'Title', sortable: true },
		{
			key: 'priority', label: 'Priority', width: '90px', sortable: true,
			render: (val: string) => {
				const v = priorityVariant[val] ?? 'neutral';
				const colorMap: Record<string, string> = {
					critical: 'bg-[var(--severity-critical)]/15 text-[var(--severity-critical)] border-[var(--severity-critical)]/30',
					high: 'bg-[var(--severity-high)]/15 text-[var(--severity-high)] border-[var(--severity-high)]/30',
					medium: 'bg-[var(--severity-medium)]/15 text-[var(--severity-medium)] border-[var(--severity-medium)]/30',
					low: 'bg-[var(--severity-low)]/15 text-[var(--severity-low)] border-[var(--severity-low)]/30',
					neutral: 'bg-muted text-muted-foreground border-border',
				};
				const cls = colorMap[v] ?? colorMap.neutral;
				return `<span class="inline-flex items-center rounded-full border font-medium leading-none text-xs px-1.5 py-0.5 ${cls}">${val}</span>`;
			},
		},
		{
			key: 'status', label: 'Status', width: '120px',
			render: (val: string) => {
				const colorMap: Record<string, string> = {
					open: 'bg-[var(--severity-info)]/15 text-[var(--severity-info)] border-[var(--severity-info)]/30',
					investigating: 'bg-[var(--severity-medium)]/15 text-[var(--severity-medium)] border-[var(--severity-medium)]/30',
					resolved: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
					closed: 'bg-muted text-muted-foreground border-border',
				};
				const cls = colorMap[val] ?? 'bg-muted text-muted-foreground border-border';
				return `<span class="inline-flex items-center rounded-full border font-medium leading-none text-xs px-1.5 py-0.5 ${cls}">${val}</span>`;
			},
		},
		{ key: 'owner', label: 'Owner', hideBelow: 'mobile' as const },
		{
			key: 'updated_at', label: 'Updated', width: '100px', sortable: true,
			render: (val: string) => timeAgo(val),
		},
	];

	// Sort state
	let sortKey = $state('updated_at');
	let sortDir = $state<'asc' | 'desc'>('desc');

	let sortedCases = $derived(() => {
		let rows = [...cases];
		if (searchQuery) {
			const q = searchQuery.toLowerCase();
			rows = rows.filter(c =>
				c.title?.toLowerCase().includes(q) ||
				c.case_id?.toLowerCase().includes(q) ||
				c.owner?.toLowerCase().includes(q) ||
				c.tags?.some((t: string) => t.toLowerCase().includes(q))
			);
		}
		if (!sortKey) return rows;
		rows.sort((a, b) => {
			const aVal = a[sortKey] ?? '';
			const bVal = b[sortKey] ?? '';
			const cmp = String(aVal).localeCompare(String(bVal));
			return sortDir === 'asc' ? cmp : -cmp;
		});
		return rows;
	});

	function onSort(key: string) {
		if (sortKey === key) {
			sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		} else {
			sortKey = key;
			sortDir = 'asc';
		}
	}

	function onRowClick(row: any) {
		window.location.href = `/cases/${row.case_id}`;
	}

	// Data loading
	onMount(async () => {
		await loadCases();
		loading = false;
	});

	async function loadCases() {
		try {
			error = null;
			const data = await api.cases();
			cases = data.cases || [];
		} catch (e: any) {
			error = 'Failed to load cases';
		}
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

	function showToast(msg: string) {
		toast = msg;
		setTimeout(() => toast = null, 3000);
	}

	// Kanban move
	async function onKanbanMove(itemId: string, _fromCol: string, toCol: string) {
		try {
			await api.updateCaseStatus(itemId, toCol);
			showToast(`Case moved to ${toCol}`);
			await loadCases();
		} catch { showToast('Failed to move case'); }
	}

	// New case
	async function createCase() {
		if (!newTitle.trim()) return;
		creating = true;
		try {
			const result: any = await api.createCase({
				title: newTitle,
				priority: newPriority,
				description: newDescription || undefined,
			});
			showToast(`Case ${result.case_id} created`);
			showNewCase = false;
			newTitle = '';
			newPriority = 'P3';
			newDescription = '';
			await loadCases();
		} catch { showToast('Failed to create case'); }
		creating = false;
	}
</script>

<MobileSpaceTabs tabs={watchTabs} />

<!-- Toast notification -->
{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg animate-in">
		{toast}
	</div>
{/if}

<div class="space-y-4">
	<!-- Header -->
	<div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
		<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
			<Briefcase class="w-5 h-5" /> Cases
		</h1>
		<div class="flex items-center gap-3">
			<div class="relative">
				<Search class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
				<input type="text" bind:value={searchQuery} placeholder="Search cases..."
					class="w-48 pl-8 pr-3 py-1.5 text-sm bg-muted rounded-md text-foreground outline-none focus:ring-1 focus:ring-ring" />
			</div>
			<button
				onclick={() => showNewCase = !showNewCase}
				class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
			>
				{#if showNewCase}<X class="w-4 h-4" />{:else}<Plus class="w-4 h-4" />{/if}
				{showNewCase ? 'Cancel' : 'New Case'}
			</button>
			<div class="flex items-center gap-1 bg-muted rounded-lg p-0.5">
				<button
					onclick={() => view = 'kanban'}
					class="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm transition-colors
						{view === 'kanban' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
				>
					<LayoutGrid class="w-4 h-4" /> Kanban
				</button>
				<button
					onclick={() => view = 'list'}
					class="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm transition-colors
						{view === 'list' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}"
				>
					<List class="w-4 h-4" /> List
				</button>
			</div>
		</div>
	</div>

	<!-- New Case Form -->
	{#if showNewCase}
		<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
			<h3 class="text-sm font-semibold text-foreground">Create New Case</h3>
			<div>
				<label class="text-xs text-muted-foreground uppercase tracking-wide">Title</label>
				<input type="text" bind:value={newTitle} placeholder="What is this case about?"
					class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
			</div>
			<div class="grid grid-cols-2 gap-3">
				<div>
					<label class="text-xs text-muted-foreground uppercase tracking-wide">Priority</label>
					<select bind:value={newPriority}
						class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
						<option value="P1">P1 - Critical</option>
						<option value="P2">P2 - High</option>
						<option value="P3">P3 - Medium</option>
						<option value="P4">P4 - Low</option>
					</select>
				</div>
			</div>
			<div>
				<label class="text-xs text-muted-foreground uppercase tracking-wide">Description</label>
				<textarea bind:value={newDescription} rows="2" placeholder="Optional details..."
					class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring resize-none"></textarea>
			</div>
			<button onclick={createCase} disabled={!newTitle.trim() || creating}
				class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
				{creating ? 'Creating...' : 'Create Case'}
			</button>
		</div>
	{/if}

	<!-- Loading / Error states -->
	{#if loading}
		<div class="text-center py-12 text-muted-foreground">
			<Briefcase class="w-8 h-8 mx-auto mb-2 animate-pulse" />
			<p>Loading cases...</p>
		</div>
	{:else if error}
		<div class="text-center py-12">
			<Briefcase class="w-8 h-8 mx-auto mb-2 text-destructive opacity-50" />
			<p class="text-sm text-destructive">{error}</p>
			<button onclick={loadCases} class="mt-2 px-3 py-1.5 text-xs rounded-md bg-muted text-foreground hover:bg-accent/50">Retry</button>
		</div>
	{:else if view === 'kanban'}
		<!-- Kanban View -->
		<KanbanBoard columns={kanbanColumns} onMove={onKanbanMove} itemKey="case_id">
			{#snippet cardSlot(caseItem)}
				<a href="/cases/{caseItem.case_id}" class="block">
					<CaseCard
						caseData={caseItem}
						variant="standard"
						onStatusChange={(newStatus) => onKanbanMove(caseItem.case_id, caseItem.status, newStatus)}
					/>
				</a>
			{/snippet}
		</KanbanBoard>
	{:else}
		<!-- List View -->
		<div class="rounded-lg border border-border bg-card overflow-hidden">
			<DataTable
				columns={tableColumns}
				rows={sortedCases()}
				rowKey="case_id"
				{sortKey}
				{sortDir}
				{onSort}
				{onRowClick}
			/>
		</div>
	{/if}
</div>
