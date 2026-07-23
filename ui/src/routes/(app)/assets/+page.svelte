<script lang="ts">
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import DataTable from '$lib/components/shared/DataTable.svelte';
	import type { ColumnDef } from '$lib/components/shared/DataTable.svelte';
	import Badge from '$lib/components/shared/Badge.svelte';
	import Package from 'lucide-svelte/icons/package';
	import Car from 'lucide-svelte/icons/car';
	import Home from 'lucide-svelte/icons/home';
	import Gem from 'lucide-svelte/icons/gem';
	import Laptop from 'lucide-svelte/icons/laptop';
	import Bitcoin from 'lucide-svelte/icons/bitcoin';
	import Box from 'lucide-svelte/icons/box';
	import Plus from 'lucide-svelte/icons/plus';
	import X from 'lucide-svelte/icons/x';
	import Search from 'lucide-svelte/icons/search';
	import LayoutGrid from 'lucide-svelte/icons/layout-grid';
	import List from 'lucide-svelte/icons/list';
	import DollarSign from 'lucide-svelte/icons/dollar-sign';
	import AskEnkiButton from '$lib/components/shared/AskEnkiButton.svelte';
	import MobileSpaceTabs from '$lib/components/layout/MobileSpaceTabs.svelte';

	const researchTabs = [
		{ label: 'Graph', href: '/graph' },
		{ label: 'Search', href: '/search' },
		{ label: 'Projects', href: '/projects' },
		{ label: 'Entities', href: '/contacts' },
		{ label: 'Assets', href: '/assets' },
	];

	let assets = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let toast = $state<string | null>(null);

	// Filters
	let filterCategory = $state('all');
	let filterStatus = $state('all');
	let searchQuery = $state('');

	// View toggle
	let view = $state<'grid' | 'list'>('grid');

	// New asset modal
	let showNewAsset = $state(false);
	let newCategory = $state('vehicle');
	let newName = $state('');
	let newDescription = $state('');
	let newSerial = $state('');
	let newValue = $state('');
	let newStatus = $state('active');
	let newTags = $state('');
	let creating = $state(false);

	const categoryIcons: Record<string, any> = {
		vehicle: Car, real_estate: Home, jewelry: Gem, electronics: Laptop,
		crypto: Bitcoin, equipment: Box, art: Package, other: Package,
	};

	const categoryColors: Record<string, string> = {
		vehicle: 'bg-blue-500/10 text-blue-400',
		real_estate: 'bg-amber-500/10 text-amber-400',
		jewelry: 'bg-purple-500/10 text-purple-400',
		electronics: 'bg-cyan-500/10 text-cyan-400',
		crypto: 'bg-orange-500/10 text-orange-400',
		equipment: 'bg-emerald-500/10 text-emerald-400',
		art: 'bg-pink-500/10 text-pink-400',
		other: 'bg-muted text-muted-foreground',
	};

	const statusVariant: Record<string, 'success' | 'high' | 'critical' | 'medium' | 'info' | 'neutral'> = {
		active: 'success', seized: 'critical', missing: 'high',
		recovered: 'info', disposed: 'neutral', transferred: 'medium',
	};

	const tableColumns: ColumnDef[] = [
		{ key: 'display_name', label: 'Asset', sortable: true,
			render: (val: string, row: any) => {
				const icons: Record<string, string> = { vehicle: '🚗', real_estate: '🏠', jewelry: '💎', electronics: '💻', crypto: '₿', equipment: '🔧', art: '🎨' };
				return `<span class="flex items-center gap-2"><span>${icons[row.asset_category] ?? '📦'}</span><span class="font-medium truncate">${val}</span></span>`;
			}
		},
		{ key: 'asset_category', label: 'Category', width: '110px', sortable: true,
			render: (val: string) => `<span class="capitalize text-xs text-muted-foreground">${(val || '').replace(/_/g, ' ')}</span>`,
		},
		{ key: 'status', label: 'Status', width: '100px',
			render: (val: string) => {
				const colors: Record<string, string> = {
					active: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
					seized: 'bg-[var(--severity-critical)]/15 text-[var(--severity-critical)] border-[var(--severity-critical)]/30',
					missing: 'bg-[var(--severity-high)]/15 text-[var(--severity-high)] border-[var(--severity-high)]/30',
					recovered: 'bg-[var(--severity-info)]/15 text-[var(--severity-info)] border-[var(--severity-info)]/30',
					disposed: 'bg-muted text-muted-foreground border-border',
					transferred: 'bg-[var(--severity-medium)]/15 text-[var(--severity-medium)] border-[var(--severity-medium)]/30',
				};
				return `<span class="inline-flex items-center rounded-full border font-medium text-xs px-1.5 py-0.5 ${colors[val] ?? colors.active}">${val}</span>`;
			},
		},
		{ key: 'estimated_value', label: 'Value', width: '110px', sortable: true, hideBelow: 'mobile' as const,
			render: (val: any, row: any) => val ? `<span class="text-xs font-mono text-foreground">${formatCurrency(val, row.currency)}</span>` : '<span class="text-xs text-muted-foreground">—</span>',
		},
		{ key: 'serial_number', label: 'Serial / ID', hideBelow: 'mobile' as const,
			render: (val: string) => val ? `<span class="text-xs font-mono text-muted-foreground truncate">${val.length > 20 ? val.slice(0, 20) + '...' : val}</span>` : '<span class="text-xs text-muted-foreground">—</span>',
		},
		{ key: 'updated_at', label: 'Updated', width: '100px', sortable: true,
			render: (val: string) => `<span class="text-xs text-muted-foreground">${timeAgo(val)}</span>`,
		},
	];

	let sortKey = $state('updated_at');
	let sortDir = $state<'asc' | 'desc'>('desc');

	let filteredAssets = $derived.by(() => {
		let result = assets;
		if (filterCategory !== 'all') result = result.filter(a => a.asset_category === filterCategory);
		if (filterStatus !== 'all') result = result.filter(a => a.status === filterStatus);
		if (searchQuery.trim()) {
			const q = searchQuery.toLowerCase();
			result = result.filter(a =>
				a.display_name?.toLowerCase().includes(q) ||
				a.serial_number?.toLowerCase().includes(q) ||
				a.description?.toLowerCase().includes(q) ||
				a.tags?.some((t: string) => t.toLowerCase().includes(q))
			);
		}
		return result;
	});

	let sortedAssets = $derived.by(() => {
		const rows = [...filteredAssets];
		if (!sortKey) return rows;
		rows.sort((a, b) => {
			const aVal = a[sortKey] ?? '';
			const bVal = b[sortKey] ?? '';
			if (sortKey === 'estimated_value') return sortDir === 'asc' ? Number(aVal) - Number(bVal) : Number(bVal) - Number(aVal);
			const cmp = String(aVal).localeCompare(String(bVal));
			return sortDir === 'asc' ? cmp : -cmp;
		});
		return rows;
	});

	let stats = $derived.by(() => {
		const totalValue = assets.reduce((sum, a) => sum + (a.estimated_value || 0), 0);
		return {
			total: assets.length,
			totalValue,
			seized: assets.filter(a => a.status === 'seized').length,
			missing: assets.filter(a => a.status === 'missing').length,
		};
	});

	function formatCurrency(val: number, currency = 'USD'): string {
		if (!val) return '—';
		return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 0 }).format(val);
	}

	function onSort(key: string) {
		if (sortKey === key) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		else { sortKey = key; sortDir = 'asc'; }
	}

	function onRowClick(row: any) {
		window.location.href = `/assets/${row.asset_id}`;
	}

	onMount(async () => {
		await loadAssets();
		loading = false;
	});

	async function loadAssets() {
		try {
			error = null;
			const data: any = await api.assets();
			assets = data.assets || [];
		} catch { error = 'Failed to load assets'; }
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

	function showToast(msg: string) { toast = msg; setTimeout(() => toast = null, 3000); }

	async function createAsset() {
		if (!newName.trim()) return;
		creating = true;
		try {
			await api.createAsset({
				asset_category: newCategory,
				display_name: newName,
				description: newDescription || undefined,
				serial_number: newSerial || undefined,
				estimated_value: newValue ? parseFloat(newValue) : undefined,
				status: newStatus,
				tags: newTags ? newTags.split(',').map(t => t.trim()).filter(Boolean) : [],
			});
			showToast('Asset created');
			showNewAsset = false;
			newName = ''; newDescription = ''; newSerial = ''; newValue = ''; newTags = '';
			newCategory = 'vehicle'; newStatus = 'active';
			await loadAssets();
		} catch { showToast('Failed to create asset'); }
		creating = false;
	}
</script>

<MobileSpaceTabs tabs={researchTabs} />

{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg animate-in">{toast}</div>
{/if}

<div class="space-y-4">
	<!-- Header -->
	<div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
		<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
			<Package class="w-5 h-5" /> Assets
			<AskEnkiButton prompt="Show me tracked assets" variant="compact" />
		</h1>
		<div class="flex items-center gap-3">
			<button onclick={() => showNewAsset = !showNewAsset}
				class="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">
				{#if showNewAsset}<X class="w-4 h-4" />{:else}<Plus class="w-4 h-4" />{/if}
				{showNewAsset ? 'Cancel' : 'New Asset'}
			</button>
			<div class="flex items-center gap-1 bg-muted rounded-lg p-0.5">
				<button onclick={() => view = 'grid'}
					class="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm transition-colors {view === 'grid' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}">
					<LayoutGrid class="w-4 h-4" /> Grid
				</button>
				<button onclick={() => view = 'list'}
					class="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm transition-colors {view === 'list' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}">
					<List class="w-4 h-4" /> List
				</button>
			</div>
		</div>
	</div>

	<!-- Stats -->
	<div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
		<div class="rounded-lg border border-border bg-card p-3">
			<p class="text-xs text-muted-foreground">Total Assets</p>
			<p class="text-lg font-semibold text-foreground">{stats.total}</p>
		</div>
		<div class="rounded-lg border border-border bg-card p-3">
			<p class="text-xs text-muted-foreground">Total Value</p>
			<p class="text-lg font-semibold text-foreground">{formatCurrency(stats.totalValue)}</p>
		</div>
		<div class="rounded-lg border border-border bg-card p-3">
			<p class="text-xs text-muted-foreground">Seized</p>
			<p class="text-lg font-semibold text-[var(--severity-critical)]">{stats.seized}</p>
		</div>
		<div class="rounded-lg border border-border bg-card p-3">
			<p class="text-xs text-muted-foreground">Missing</p>
			<p class="text-lg font-semibold text-[var(--severity-high)]">{stats.missing}</p>
		</div>
	</div>

	<!-- Filters -->
	<div class="flex flex-col sm:flex-row gap-2">
		<div class="relative flex-1">
			<Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
			<input type="text" bind:value={searchQuery} placeholder="Search assets..."
				class="w-full pl-9 pr-3 py-2 bg-muted rounded-md text-sm text-foreground placeholder:text-muted-foreground outline-none focus:ring-2 focus:ring-ring" />
		</div>
		<select bind:value={filterCategory}
			class="bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
			<option value="all">All Categories</option>
			<option value="vehicle">Vehicles</option>
			<option value="real_estate">Real Estate</option>
			<option value="jewelry">Jewelry</option>
			<option value="electronics">Electronics</option>
			<option value="crypto">Crypto</option>
			<option value="equipment">Equipment</option>
			<option value="art">Art</option>
			<option value="other">Other</option>
		</select>
		<select bind:value={filterStatus}
			class="bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
			<option value="all">All Status</option>
			<option value="active">Active</option>
			<option value="seized">Seized</option>
			<option value="missing">Missing</option>
			<option value="recovered">Recovered</option>
			<option value="disposed">Disposed</option>
			<option value="transferred">Transferred</option>
		</select>
	</div>

	<!-- New Asset Form -->
	{#if showNewAsset}
		<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
			<h3 class="text-sm font-semibold text-foreground">New Asset</h3>
			<div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
				<div>
					<label class="text-xs text-muted-foreground uppercase tracking-wide">Category</label>
					<select bind:value={newCategory}
						class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
						<option value="vehicle">Vehicle</option>
						<option value="real_estate">Real Estate</option>
						<option value="jewelry">Jewelry</option>
						<option value="electronics">Electronics</option>
						<option value="crypto">Crypto</option>
						<option value="equipment">Equipment</option>
						<option value="art">Art</option>
						<option value="other">Other</option>
					</select>
				</div>
				<div>
					<label class="text-xs text-muted-foreground uppercase tracking-wide">Status</label>
					<select bind:value={newStatus}
						class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
						<option value="active">Active</option>
						<option value="seized">Seized</option>
						<option value="missing">Missing</option>
						<option value="recovered">Recovered</option>
					</select>
				</div>
				<div>
					<label class="text-xs text-muted-foreground uppercase tracking-wide">Estimated Value</label>
					<input type="number" bind:value={newValue} placeholder="0.00"
						class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
				</div>
			</div>
			<div>
				<label class="text-xs text-muted-foreground uppercase tracking-wide">Display Name</label>
				<input type="text" bind:value={newName} placeholder="2019 BMW X5 — ABC-1234"
					class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
			</div>
			<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
				<div>
					<label class="text-xs text-muted-foreground uppercase tracking-wide">Serial / ID</label>
					<input type="text" bind:value={newSerial} placeholder="VIN, serial number, wallet address..."
						class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring font-mono text-xs" />
				</div>
				<div>
					<label class="text-xs text-muted-foreground uppercase tracking-wide">Tags</label>
					<input type="text" bind:value={newTags} placeholder="Comma-separated tags..."
						class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
				</div>
			</div>
			<div>
				<label class="text-xs text-muted-foreground uppercase tracking-wide">Description</label>
				<textarea bind:value={newDescription} rows="2" placeholder="What is this asset?"
					class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring resize-none"></textarea>
			</div>
			<button onclick={createAsset} disabled={!newName.trim() || creating}
				class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
				{creating ? 'Creating...' : 'Create Asset'}
			</button>
		</div>
	{/if}

	<!-- Content -->
	{#if loading}
		<div class="text-center py-12 text-muted-foreground">
			<Package class="w-8 h-8 mx-auto mb-2 animate-pulse" />
			<p>Loading assets...</p>
		</div>
	{:else if error}
		<div class="text-center py-12">
			<Package class="w-8 h-8 mx-auto mb-2 text-destructive opacity-50" />
			<p class="text-sm text-destructive">{error}</p>
			<button onclick={loadAssets} class="mt-2 px-3 py-1.5 text-xs rounded-md bg-muted text-foreground hover:bg-accent/50">Retry</button>
		</div>
	{:else if view === 'grid'}
		{#if filteredAssets.length === 0}
			<div class="text-center py-12 text-muted-foreground">
				<Package class="w-6 h-6 mx-auto mb-2 opacity-50" />
				<p class="text-sm">No assets match your filters</p>
			</div>
		{:else}
			<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
				{#each filteredAssets as asset}
					{@const Icon = categoryIcons[asset.asset_category] || Package}
					<a href="/assets/{asset.asset_id}" class="block rounded-lg border border-border bg-card hover:border-primary/40 transition-colors overflow-hidden">
						<div class="p-4">
							<div class="flex items-start gap-3">
								<div class="w-10 h-10 rounded-lg flex items-center justify-center shrink-0 {categoryColors[asset.asset_category] || categoryColors.other}">
									<svelte:component this={Icon} size={20} />
								</div>
								<div class="flex-1 min-w-0">
									<h3 class="text-sm font-medium text-foreground truncate">{asset.display_name}</h3>
									<p class="text-xs text-muted-foreground capitalize">{(asset.asset_category || '').replace(/_/g, ' ')}</p>
									{#if asset.serial_number}
										<p class="text-[10px] text-muted-foreground font-mono truncate mt-0.5">{asset.serial_number}</p>
									{/if}
								</div>
								<Badge text={asset.status} variant={statusVariant[asset.status] ?? 'neutral'} size="sm" />
							</div>

							<div class="flex items-center justify-between mt-3 pt-2 border-t border-border/50">
								{#if asset.estimated_value}
									<span class="text-sm font-semibold text-foreground flex items-center gap-1">
										<DollarSign class="w-3 h-3 text-emerald-400" />
										{formatCurrency(asset.estimated_value, asset.currency)}
									</span>
								{:else}
									<span class="text-xs text-muted-foreground">No value set</span>
								{/if}
								<span class="text-[10px] text-muted-foreground">{asset.properties?.length ?? 0} properties</span>
							</div>

							{#if asset.tags?.length}
								<div class="flex flex-wrap gap-1 mt-2">
									{#each asset.tags.slice(0, 3) as tag}
										<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground">{tag}</span>
									{/each}
									{#if asset.tags.length > 3}
										<span class="text-[10px] px-1.5 py-0.5 text-muted-foreground">+{asset.tags.length - 3}</span>
									{/if}
								</div>
							{/if}
						</div>
					</a>
				{/each}
			</div>
		{/if}
	{:else}
		<div class="rounded-lg border border-border bg-card overflow-hidden">
			<DataTable columns={tableColumns} rows={sortedAssets} rowKey="asset_id" {sortKey} {sortDir} {onSort} {onRowClick} />
		</div>
	{/if}
</div>
