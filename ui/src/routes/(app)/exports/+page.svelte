<script lang="ts">
	import AskEnkiButton from '$lib/components/shared/AskEnkiButton.svelte';
	import Badge from '$lib/components/shared/Badge.svelte';
	import Download from 'lucide-svelte/icons/download';
	import FileText from 'lucide-svelte/icons/file-text';
	import List from 'lucide-svelte/icons/list';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import Contact from 'lucide-svelte/icons/contact';
	import ScrollText from 'lucide-svelte/icons/scroll-text';
	import Calendar from 'lucide-svelte/icons/calendar';
	import Loader from 'lucide-svelte/icons/loader-circle';

	let entityType = $state<'signals' | 'cases' | 'contacts' | 'audit'>('signals');
	let format = $state<'csv' | 'json'>('csv');
	let dateFrom = $state('');
	let dateTo = $state('');
	let limit = $state(1000);
	let filterType = $state('');
	let filterStatus = $state('');
	let exporting = $state(false);
	let toast = $state<string | null>(null);
	let recentExports = $state<any[]>([]);

	function showToast(msg: string) { toast = msg; setTimeout(() => toast = null, 3000); }

	const entityOptions = [
		{ id: 'signals' as const, label: 'Signals', icon: List, desc: 'Raw signal data with metadata' },
		{ id: 'cases' as const, label: 'Cases', icon: Briefcase, desc: 'Investigation cases and status' },
		{ id: 'contacts' as const, label: 'Contacts', icon: Contact, desc: 'People, companies, entities' },
		{ id: 'audit' as const, label: 'Audit Trail', icon: ScrollText, desc: 'System audit events' },
	];

	function buildExportUrl(): string {
		const params = new URLSearchParams();
		params.set('format', format);
		if (limit) params.set('limit', String(limit));
		if (dateFrom) params.set('after', new Date(dateFrom).toISOString());
		if (dateTo) params.set('before', new Date(dateTo).toISOString());
		if (entityType === 'signals' && filterType) params.set('type', filterType);
		if (entityType === 'cases' && filterStatus) params.set('status', filterStatus);
		return `/api/exports/${entityType}?${params.toString()}`;
	}

	function startExport() {
		exporting = true;
		const url = buildExportUrl();
		// Open download in new tab
		window.open(url, '_blank');
		// Track in recent exports
		recentExports = [
			{ type: entityType, format, timestamp: new Date().toISOString(), url, limit },
			...recentExports.slice(0, 9),
		];
		showToast(`Exporting ${entityType} as ${format.toUpperCase()}`);
		setTimeout(() => exporting = false, 1500);
	}

	function quickExport(type: string, fmt: string, params: string) {
		window.open(`/api/exports/${type}?format=${fmt}${params}`, '_blank');
		showToast(`Quick export: ${type} as ${fmt.toUpperCase()}`);
	}
</script>

{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg">{toast}</div>
{/if}

<div class="space-y-6 max-w-3xl">
	<div class="flex items-center justify-between">
		<div>
			<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
				<Download class="w-5 h-5" /> Export Data
			</h1>
			<p class="text-sm text-muted-foreground mt-0.5">Download signals, cases, contacts, and audit data</p>
		</div>
		<AskEnkiButton prompt="Help me export data" variant="compact" />
	</div>

	<!-- Quick exports -->
	<div class="grid grid-cols-2 md:grid-cols-4 gap-2">
		<button onclick={() => quickExport('signals', 'csv', '&limit=5000')}
			class="flex flex-col items-center gap-1.5 p-3 rounded-lg border border-border bg-card hover:border-primary/40 transition-colors">
			<List class="w-5 h-5 text-muted-foreground" />
			<span class="text-xs font-medium text-foreground">All Signals</span>
			<span class="text-[10px] text-muted-foreground">CSV · last 5000</span>
		</button>
		<button onclick={() => quickExport('cases', 'json', '')}
			class="flex flex-col items-center gap-1.5 p-3 rounded-lg border border-border bg-card hover:border-primary/40 transition-colors">
			<Briefcase class="w-5 h-5 text-muted-foreground" />
			<span class="text-xs font-medium text-foreground">All Cases</span>
			<span class="text-[10px] text-muted-foreground">JSON</span>
		</button>
		<button onclick={() => quickExport('contacts', 'csv', '')}
			class="flex flex-col items-center gap-1.5 p-3 rounded-lg border border-border bg-card hover:border-primary/40 transition-colors">
			<Contact class="w-5 h-5 text-muted-foreground" />
			<span class="text-xs font-medium text-foreground">All Contacts</span>
			<span class="text-[10px] text-muted-foreground">CSV</span>
		</button>
		<button onclick={() => quickExport('audit', 'json', '&limit=1000')}
			class="flex flex-col items-center gap-1.5 p-3 rounded-lg border border-border bg-card hover:border-primary/40 transition-colors">
			<ScrollText class="w-5 h-5 text-muted-foreground" />
			<span class="text-xs font-medium text-foreground">Audit Trail</span>
			<span class="text-[10px] text-muted-foreground">JSON · last 1000</span>
		</button>
	</div>

	<!-- Custom export builder -->
	<div class="rounded-xl border border-border bg-card p-5 space-y-4">
		<h2 class="text-sm font-semibold text-foreground">Custom Export</h2>

		<!-- Entity type selector -->
		<div class="grid grid-cols-2 md:grid-cols-4 gap-2">
			{#each entityOptions as opt}
				<button onclick={() => entityType = opt.id}
					class="p-3 rounded-lg border text-center text-xs transition-colors
						{entityType === opt.id ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted/50'}">
					<svelte:component this={opt.icon} class="w-4 h-4 mx-auto mb-1 {entityType === opt.id ? 'text-primary' : 'text-muted-foreground'}" />
					<div class="font-medium {entityType === opt.id ? 'text-foreground' : 'text-muted-foreground'}">{opt.label}</div>
				</button>
			{/each}
		</div>

		<!-- Format + Limit -->
		<div class="grid grid-cols-3 gap-3">
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Format</label>
				<select bind:value={format} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1">
					<option value="csv">CSV</option>
					<option value="json">JSON</option>
				</select>
			</div>
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Max Rows</label>
				<select bind:value={limit} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1">
					<option value={100}>100</option>
					<option value={1000}>1,000</option>
					<option value={5000}>5,000</option>
					<option value={10000}>10,000</option>
					<option value={50000}>50,000</option>
				</select>
			</div>
			<div>
				<!-- Context-sensitive filter -->
				{#if entityType === 'signals'}
					<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Signal Type</label>
					<input type="text" bind:value={filterType} placeholder="e.g. signal.financial.*"
						class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
				{:else if entityType === 'cases'}
					<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Status</label>
					<select bind:value={filterStatus} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1">
						<option value="">All</option>
						<option value="open">Open</option>
						<option value="investigating">Investigating</option>
						<option value="resolved">Resolved</option>
						<option value="closed">Closed</option>
					</select>
				{:else}
					<label class="text-[10px] text-muted-foreground uppercase tracking-wider">&nbsp;</label>
					<div class="px-3 py-1.5 text-xs text-muted-foreground mt-1">No additional filters</div>
				{/if}
			</div>
		</div>

		<!-- Date range -->
		<div class="grid grid-cols-2 gap-3">
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">From</label>
				<input type="date" bind:value={dateFrom}
					class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
			</div>
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">To</label>
				<input type="date" bind:value={dateTo}
					class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
			</div>
		</div>

		<!-- Export button -->
		<button onclick={startExport} disabled={exporting}
			class="w-full py-2.5 rounded-lg bg-primary text-primary-foreground font-medium text-sm hover:bg-primary/90 disabled:opacity-50 transition-colors flex items-center justify-center gap-2">
			{#if exporting}
				<Loader class="w-4 h-4 animate-spin" /> Exporting...
			{:else}
				<Download class="w-4 h-4" /> Export {entityType} as {format.toUpperCase()}
			{/if}
		</button>

		<p class="text-[10px] text-muted-foreground text-center">File will download in a new tab. Large exports may take a moment.</p>
	</div>

	<!-- Recent exports -->
	{#if recentExports.length > 0}
		<div class="rounded-lg border border-border bg-card p-4">
			<h3 class="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-2">Recent Exports</h3>
			<div class="space-y-1">
				{#each recentExports as exp}
					<div class="flex items-center gap-3 px-2 py-1.5 rounded hover:bg-muted/30">
						<FileText class="w-3.5 h-3.5 text-muted-foreground shrink-0" />
						<span class="text-xs text-foreground capitalize">{exp.type}</span>
						<Badge text={exp.format} variant="neutral" size="sm" />
						<span class="text-[10px] text-muted-foreground">{exp.limit} rows</span>
						<span class="text-[10px] text-muted-foreground ml-auto">{new Date(exp.timestamp).toLocaleTimeString()}</span>
						<a href={exp.url} target="_blank" class="text-[10px] text-primary hover:underline">Re-download</a>
					</div>
				{/each}
			</div>
		</div>
	{/if}
</div>
