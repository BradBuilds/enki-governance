<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import { chat } from '$lib/stores/chat.svelte';
	import Badge from '$lib/components/shared/Badge.svelte';
	import Package from 'lucide-svelte/icons/package';
	import Car from 'lucide-svelte/icons/car';
	import Home from 'lucide-svelte/icons/home';
	import Gem from 'lucide-svelte/icons/gem';
	import Laptop from 'lucide-svelte/icons/laptop';
	import Bitcoin from 'lucide-svelte/icons/bitcoin';
	import Box from 'lucide-svelte/icons/box';
	import FileText from 'lucide-svelte/icons/file-text';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import User from 'lucide-svelte/icons/user';
	import Building from 'lucide-svelte/icons/building';
	import Radio from 'lucide-svelte/icons/radio';
	import Plus from 'lucide-svelte/icons/plus';
	import X from 'lucide-svelte/icons/x';
	import ArrowLeft from 'lucide-svelte/icons/arrow-left';
	import DollarSign from 'lucide-svelte/icons/dollar-sign';
	import Hash from 'lucide-svelte/icons/hash';
	import Tag from 'lucide-svelte/icons/tag';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import ChevronUp from 'lucide-svelte/icons/chevron-up';
	import Layers from 'lucide-svelte/icons/layers';
	import Link from 'lucide-svelte/icons/link';
	import IdCard from 'lucide-svelte/icons/id-card';
	import EntityDossier from '$lib/components/entities/EntityDossier.svelte';

	const assetId = $page.params.id;

	let asset = $state<any>(null);
	let documents = $state<any[]>([]);
	let linkedContacts = $state<any[]>([]);
	let linkedCases = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let toast = $state<string | null>(null);

	let activeTab = $state('properties');

	// Properties
	let showAllProps = $state(false);
	let showAddProp = $state(false);
	let newPropKey = $state('');
	let newPropValue = $state('');
	let addingProp = $state(false);

	// Documents
	let showNewDoc = $state(false);
	let newDocTitle = $state('');
	let newDocType = $state('photo');
	let newDocContent = $state('');
	let creatingDoc = $state(false);

	// Link contact
	let showLinkContact = $state(false);
	let linkContactId = $state('');
	let linkContactRel = $state('owner');
	let linkingContact = $state(false);

	// Link case
	let showLinkCase = $state(false);
	let linkCaseId = $state('');
	let linkCaseRel = $state('evidence');
	let linkingCase = $state(false);

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
	const docTypeIcons: Record<string, string> = {
		photo: '📷', video: '🎬', registration: '📋', title: '📜', receipt: '🧾',
		appraisal: '💰', inspection: '🔍', insurance: '🛡️', social_media: '📱', note: '📝', other: '📎',
	};

	const tabs = [
		{ id: 'properties', label: 'Properties', icon: Layers },
		{ id: 'dossier', label: 'Dossier', icon: IdCard },
		{ id: 'documents', label: 'Notes', icon: FileText },
		{ id: 'contacts', label: 'Contacts', icon: User },
		{ id: 'cases', label: 'Cases', icon: Briefcase },
	];

	// Sorted properties: manual entries + high confidence first
	let sortedProperties = $derived.by(() => {
		const props = asset?.properties || [];
		return [...props].sort((a: any, b: any) => {
			if (a.source === 'manual' && b.source !== 'manual') return -1;
			if (b.source === 'manual' && a.source !== 'manual') return 1;
			return (b.confidence ?? 0) - (a.confidence ?? 0);
		});
	});

	let visibleProperties = $derived(showAllProps ? sortedProperties : sortedProperties.slice(0, 8));

	onMount(async () => {
		try {
			const [a, docs, contacts, cases] = await Promise.all([
				api.asset(assetId),
				api.assetDocuments(assetId),
				api.assetContacts(assetId),
				api.assetCases(assetId),
			]);
			asset = a;
			documents = (docs as any).documents || [];
			linkedContacts = (contacts as any).contacts || [];
			linkedCases = (cases as any).cases || [];
		} catch { error = 'Failed to load asset'; }
		loading = false;
	});

	$effect(() => {
		if (asset) {
			chat.setContext({ currentPage: '/assets/' + assetId, selectedItem: { type: 'asset', id: assetId, title: asset?.display_name } });
		}
	});

	function showToast(msg: string) { toast = msg; setTimeout(() => toast = null, 3000); }

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	function formatCurrency(val: number, currency = 'USD'): string {
		if (!val) return '—';
		return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 0 }).format(val);
	}

	function sourceLabel(source: string): string {
		if (source === 'manual') return 'Manual entry';
		if (source.startsWith('sig_')) return source;
		return source;
	}

	function confidenceBar(c: number): string {
		if (c >= 0.95) return 'bg-emerald-400';
		if (c >= 0.8) return 'bg-blue-400';
		if (c >= 0.6) return 'bg-amber-400';
		return 'bg-red-400';
	}

	async function addProperty() {
		if (!newPropKey.trim() || !newPropValue.trim()) return;
		addingProp = true;
		try {
			await api.addAssetProperty(assetId, { key: newPropKey, value: newPropValue });
			// Optimistic update
			asset.properties = [...(asset.properties || []), {
				key: newPropKey, value: newPropValue, source: 'manual', confidence: 1.0,
				discovered_at: new Date().toISOString(),
			}];
			showToast('Property added');
			showAddProp = false;
			newPropKey = ''; newPropValue = '';
		} catch { showToast('Failed to add property'); }
		addingProp = false;
	}

	async function addDocument() {
		if (!newDocTitle.trim()) return;
		creatingDoc = true;
		try {
			await api.addAssetDocument(assetId, { title: newDocTitle, doc_type: newDocType, content: newDocContent || undefined });
			showToast('Document added');
			showNewDoc = false;
			newDocTitle = ''; newDocContent = ''; newDocType = 'photo';
			const docs: any = await api.assetDocuments(assetId);
			documents = docs.documents || [];
		} catch { showToast('Failed to add document'); }
		creatingDoc = false;
	}

	async function linkContact() {
		if (!linkContactId.trim()) return;
		linkingContact = true;
		try {
			await api.linkAssetContact(assetId, { contact_id: linkContactId, relationship: linkContactRel });
			showToast('Contact linked');
			showLinkContact = false;
			linkContactId = ''; linkContactRel = 'owner';
			const c: any = await api.assetContacts(assetId);
			linkedContacts = c.contacts || [];
		} catch { showToast('Failed to link contact'); }
		linkingContact = false;
	}

	async function linkCase() {
		if (!linkCaseId.trim()) return;
		linkingCase = true;
		try {
			await api.linkAssetCase(assetId, { case_id: linkCaseId, relationship: linkCaseRel });
			showToast('Case linked');
			showLinkCase = false;
			linkCaseId = ''; linkCaseRel = 'evidence';
			const c: any = await api.assetCases(assetId);
			linkedCases = c.cases || [];
		} catch { showToast('Failed to link case'); }
		linkingCase = false;
	}

	// ─── Edit Asset ────────────────────────────────────────────────────
	let showEditAsset = $state(false);
	let editName = $state('');
	let editCategory = $state('vehicle');
	let editStatus = $state('active');
	let editValue = $state<number | null>(null);
	let editSerial = $state('');
	let editDescription = $state('');
	let editTags = $state('');
	let savingEdit = $state(false);

	const categories = ['vehicle', 'real_estate', 'jewelry', 'electronics', 'crypto', 'equipment', 'art', 'other'];
	const statuses = ['active', 'seized', 'missing', 'recovered', 'disposed', 'transferred'];

	function openEditAsset() {
		editName = asset?.display_name || '';
		editCategory = asset?.asset_category || 'vehicle';
		editStatus = asset?.status || 'active';
		editValue = asset?.estimated_value || null;
		editSerial = asset?.serial_number || '';
		editDescription = asset?.description || '';
		editTags = (asset?.tags || []).join(', ');
		showEditAsset = true;
	}

	async function saveEditAsset() {
		savingEdit = true;
		try {
			const tags = editTags ? editTags.split(',').map((t: string) => t.trim()).filter(Boolean) : [];
			await api.updateAsset(assetId, {
				display_name: editName, asset_category: editCategory, status: editStatus,
				estimated_value: editValue, serial_number: editSerial, description: editDescription, tags,
			});
			asset = { ...asset, display_name: editName, asset_category: editCategory, status: editStatus,
				estimated_value: editValue, serial_number: editSerial, description: editDescription, tags };
			showEditAsset = false;
			showToast('Asset updated');
		} catch { showToast('Failed to update asset'); }
		savingEdit = false;
	}

	// ─── Delete Asset ──────────────────────────────────────────────────
	let showDeleteConfirm = $state(false);
	let deletingAsset = $state(false);

	async function deleteAsset() {
		deletingAsset = true;
		try {
			await api.deleteAsset(assetId);
			showToast('Asset deleted');
			const { goto } = await import('$app/navigation');
			goto('/assets');
		} catch { showToast('Failed to delete asset'); }
		deletingAsset = false;
	}

	// ─── Delete Document ───────────────────────────────────────────────
	let deletingDocId = $state<string | null>(null);

	async function deleteDocument(docId: string) {
		deletingDocId = docId;
		try {
			await api.deleteAssetDocument(assetId, docId);
			documents = documents.filter(d => d.doc_id !== docId);
			showToast('Document deleted');
		} catch { showToast('Failed to delete document'); }
		deletingDocId = null;
	}

	const caseStatusColors: Record<string, string> = {
		open: 'bg-[var(--severity-info)]/15 text-[var(--severity-info)] border-[var(--severity-info)]/30',
		investigating: 'bg-[var(--severity-medium)]/15 text-[var(--severity-medium)] border-[var(--severity-medium)]/30',
		resolved: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
		closed: 'bg-muted text-muted-foreground border-border',
	};
</script>

{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg animate-in">{toast}</div>
{/if}

{#if loading}
	<div class="text-center py-12 text-muted-foreground">
		<Package class="w-8 h-8 mx-auto mb-2 animate-pulse" />
		<p>Loading asset...</p>
	</div>
{:else if error || !asset}
	<div class="text-center py-12">
		<Package class="w-8 h-8 mx-auto mb-2 text-destructive opacity-50" />
		<p class="text-sm text-destructive">{error || 'Asset not found'}</p>
		<a href="/assets" class="mt-2 inline-flex items-center gap-1 text-xs text-primary hover:underline">
			<ArrowLeft class="w-3 h-3" /> Back to assets
		</a>
	</div>
{:else}
	{@const Icon = categoryIcons[asset.asset_category] || Package}
	<div class="space-y-4 max-w-4xl">
		<a href="/assets" class="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors">
			<ArrowLeft class="w-3 h-3" /> All Assets
		</a>

		<!-- Header Card -->
		<div class="rounded-xl border border-border bg-card overflow-hidden">
			<div class="p-5">
				<div class="flex items-start gap-4">
					<div class="w-14 h-14 rounded-xl flex items-center justify-center shrink-0 {categoryColors[asset.asset_category] || categoryColors.other}">
						<svelte:component this={Icon} size={28} />
					</div>
					<div class="flex-1 min-w-0">
						<h1 class="text-xl font-semibold text-foreground">{asset.display_name}</h1>
						<p class="text-sm text-muted-foreground capitalize">{(asset.asset_category || '').replace(/_/g, ' ')}</p>
					</div>
					<div class="flex items-center gap-2">
						<Badge text={asset.status} variant={statusVariant[asset.status] ?? 'neutral'} />
					<button onclick={openEditAsset} class="text-[10px] text-muted-foreground hover:text-foreground px-2 py-1 rounded border border-border/50 hover:bg-muted transition-colors">Edit</button>
						<button onclick={() => showDeleteConfirm = true} class="text-[10px] text-muted-foreground hover:text-destructive px-2 py-1 rounded border border-border/50 hover:bg-destructive/10 transition-colors">Delete</button>
					</div>
				</div>

				<!-- Info row -->
				<div class="flex flex-wrap gap-4 mt-4 text-sm text-muted-foreground">
					{#if asset.estimated_value}
						<span class="flex items-center gap-1.5">
							<DollarSign class="w-3.5 h-3.5 text-emerald-400" />
							<span class="text-foreground font-semibold">{formatCurrency(asset.estimated_value, asset.currency)}</span>
						</span>
					{/if}
					{#if asset.serial_number}
						<span class="flex items-center gap-1.5">
							<Hash class="w-3.5 h-3.5" />
							<span class="font-mono text-xs">{asset.serial_number}</span>
						</span>
					{/if}
					{#if asset.observables?.length}
						<span class="flex items-center gap-1.5">
							<Radio class="w-3.5 h-3.5" />
							{asset.observables.length} observable{asset.observables.length !== 1 ? 's' : ''} tracked
						</span>
					{/if}
				</div>

				{#if asset.tags?.length}
					<div class="flex flex-wrap gap-1.5 mt-3">
						{#each asset.tags as tag}
							<span class="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground">{tag}</span>
						{/each}
					</div>
				{/if}

				{#if asset.description}
					<p class="text-sm text-muted-foreground mt-3 border-t border-border pt-3">{asset.description}</p>
				{/if}
			</div>
		</div>

		<!-- Tabs -->
		<div class="flex border-b border-border overflow-x-auto">
			{#each tabs as tab}
				<button
					onclick={() => activeTab = tab.id}
					class="flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium whitespace-nowrap border-b-2 -mb-px transition-colors
						{activeTab === tab.id ? 'text-primary border-primary' : 'text-muted-foreground border-transparent hover:text-foreground'}"
				>
					<svelte:component this={tab.icon} size={15} />
					{tab.label}
					{#if tab.id === 'properties'}
						<span class="text-xs bg-muted px-1.5 rounded-full">{(asset.properties || []).length}</span>
					{:else if tab.id === 'documents'}
						<span class="text-xs bg-muted px-1.5 rounded-full">{documents.length}</span>
					{:else if tab.id === 'contacts'}
						<span class="text-xs bg-muted px-1.5 rounded-full">{linkedContacts.length}</span>
					{:else if tab.id === 'cases'}
						<span class="text-xs bg-muted px-1.5 rounded-full">{linkedCases.length}</span>
					{/if}
				</button>
			{/each}
		</div>

		<!-- Tab Content -->
		<div class="min-h-[300px]">
			{#if activeTab === 'properties'}
				<!-- Properties — The expandable append-log -->
				<div class="space-y-3">
					<div class="flex items-center justify-between">
						<h3 class="text-sm font-semibold text-foreground">Properties</h3>
						<button onclick={() => showAddProp = !showAddProp}
							class="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">
							{#if showAddProp}<X class="w-3 h-3" />{:else}<Plus class="w-3 h-3" />{/if}
							{showAddProp ? 'Cancel' : 'Add Property'}
						</button>
					</div>

					{#if showAddProp}
						<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
							<p class="text-xs text-muted-foreground">Add any key-value property. No predefined schema — type whatever is relevant.</p>
							<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
								<div>
									<label class="text-xs text-muted-foreground uppercase tracking-wide">Key</label>
									<input type="text" bind:value={newPropKey} placeholder="e.g. tire_brand, bedrooms, inscription..."
										class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
								</div>
								<div>
									<label class="text-xs text-muted-foreground uppercase tracking-wide">Value</label>
									<input type="text" bind:value={newPropValue} placeholder="e.g. Michelin, 4, 'To my love'..."
										class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
								</div>
							</div>
							<button onclick={addProperty} disabled={!newPropKey.trim() || !newPropValue.trim() || addingProp}
								class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
								{addingProp ? 'Adding...' : 'Add Property'}
							</button>
						</div>
					{/if}

					{#if sortedProperties.length === 0}
						<div class="text-center py-8 text-muted-foreground">
							<Layers class="w-6 h-6 mx-auto mb-2 opacity-50" />
							<p class="text-sm">No properties yet</p>
							<p class="text-xs">Properties accumulate as signals match this asset's observables, or add them manually.</p>
						</div>
					{:else}
						<div class="rounded-lg border border-border bg-card overflow-hidden">
							<table class="w-full">
								<thead>
									<tr class="border-b border-border bg-muted/30">
										<th class="text-left px-4 py-2 text-[10px] text-muted-foreground uppercase tracking-wider font-medium">Key</th>
										<th class="text-left px-4 py-2 text-[10px] text-muted-foreground uppercase tracking-wider font-medium">Value</th>
										<th class="text-left px-4 py-2 text-[10px] text-muted-foreground uppercase tracking-wider font-medium hidden sm:table-cell">Source</th>
										<th class="text-left px-4 py-2 text-[10px] text-muted-foreground uppercase tracking-wider font-medium w-16 hidden sm:table-cell">Conf.</th>
										<th class="text-left px-4 py-2 text-[10px] text-muted-foreground uppercase tracking-wider font-medium hidden md:table-cell">Discovered</th>
									</tr>
								</thead>
								<tbody>
									{#each visibleProperties as prop}
										<tr class="border-b border-border/50 hover:bg-muted/20 transition-colors">
											<td class="px-4 py-2.5">
												<span class="text-xs font-medium text-foreground">{prop.key.replace(/_/g, ' ')}</span>
											</td>
											<td class="px-4 py-2.5">
												<span class="text-xs text-foreground font-mono">{prop.value}</span>
											</td>
											<td class="px-4 py-2.5 hidden sm:table-cell">
												{#if prop.source === 'manual'}
													<span class="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
														<span class="w-1.5 h-1.5 rounded-full bg-blue-400"></span>
														Manual
													</span>
												{:else}
													<a href="/signals/{prop.source}" class="inline-flex items-center gap-1 text-[10px] text-primary hover:underline">
														<span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
														{prop.source}
													</a>
												{/if}
											</td>
											<td class="px-4 py-2.5 w-16 hidden sm:table-cell">
												{#if prop.confidence != null}
													<div class="flex items-center gap-1.5">
														<div class="w-8 h-1.5 rounded-full bg-muted overflow-hidden">
															<div class="h-full rounded-full {confidenceBar(prop.confidence)}"
																style:width="{prop.confidence * 100}%"></div>
														</div>
														<span class="text-[10px] text-muted-foreground">{Math.round(prop.confidence * 100)}%</span>
													</div>
												{/if}
											</td>
											<td class="px-4 py-2.5 hidden md:table-cell">
												<span class="text-[10px] text-muted-foreground">{timeAgo(prop.discovered_at)}</span>
											</td>
										</tr>
									{/each}
								</tbody>
							</table>

							{#if sortedProperties.length > 8}
								<button
									onclick={() => showAllProps = !showAllProps}
									class="w-full px-4 py-2 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors flex items-center justify-center gap-1"
								>
									{#if showAllProps}
										<ChevronUp class="w-3 h-3" /> Show less
									{:else}
										<ChevronDown class="w-3 h-3" /> Show {sortedProperties.length - 8} more properties
									{/if}
								</button>
							{/if}
						</div>

						<!-- Seed Observables -->
						{#if asset.observables?.length}
							<div class="rounded-lg border border-border bg-card p-4">
								<h4 class="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2 flex items-center gap-1.5">
									<Radio class="w-3.5 h-3.5" /> Pipeline Observables
								</h4>
								<p class="text-[10px] text-muted-foreground mb-2">Signals matching these identifiers auto-append properties to this asset.</p>
								<div class="flex flex-wrap gap-1.5">
									{#each asset.observables as obs}
										<span class="text-xs px-2 py-1 rounded-md bg-muted text-foreground font-mono">{obs}</span>
									{/each}
								</div>
							</div>
						{/if}
					{/if}
				</div>

			{:else if activeTab === 'dossier'}
				<!-- Dossier Tab: VIN/MMSI/IMO identifiers + titles/registrations + photos -->
				<EntityDossier entityId={assetId} entityType="asset" />

			{:else if activeTab === 'documents'}
				<!-- Documents Tab -->
				<div class="space-y-3">
					<div class="flex items-center justify-between">
						<h3 class="text-sm font-semibold text-foreground">Documents & Files</h3>
						<button onclick={() => showNewDoc = !showNewDoc}
							class="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">
							{#if showNewDoc}<X class="w-3 h-3" />{:else}<Plus class="w-3 h-3" />{/if}
							{showNewDoc ? 'Cancel' : 'Add Document'}
						</button>
					</div>

					{#if showNewDoc}
						<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
							<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
								<div>
									<label class="text-xs text-muted-foreground uppercase tracking-wide">Title</label>
									<input type="text" bind:value={newDocTitle} placeholder="Document title..."
										class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
								</div>
								<div>
									<label class="text-xs text-muted-foreground uppercase tracking-wide">Type</label>
									<select bind:value={newDocType}
										class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
										<option value="photo">Photo</option>
										<option value="video">Video</option>
										<option value="registration">Registration</option>
										<option value="title">Title / Deed</option>
										<option value="receipt">Receipt</option>
										<option value="appraisal">Appraisal</option>
										<option value="inspection">Inspection</option>
										<option value="insurance">Insurance</option>
										<option value="social_media">Social Media</option>
										<option value="note">Note</option>
										<option value="other">Other</option>
									</select>
								</div>
							</div>
							<div>
								<label class="text-xs text-muted-foreground uppercase tracking-wide">Content</label>
								<textarea bind:value={newDocContent} rows="4" placeholder="Document content (markdown supported)..."
									class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring resize-none font-mono text-xs"></textarea>
							</div>
							<button onclick={addDocument} disabled={!newDocTitle.trim() || creatingDoc}
								class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
								{creatingDoc ? 'Saving...' : 'Save Document'}
							</button>
						</div>
					{/if}

					{#if documents.length === 0}
						<div class="text-center py-8 text-muted-foreground">
							<FileText class="w-6 h-6 mx-auto mb-2 opacity-50" />
							<p class="text-sm">No documents yet</p>
							<p class="text-xs">Add photos, registration, receipts, appraisals, or notes</p>
						</div>
					{:else}
						{#each documents as doc}
							<div class="rounded-lg border border-border bg-card overflow-hidden">
								<div class="px-4 py-3 flex items-start gap-3">
									<span class="text-lg">{docTypeIcons[doc.doc_type] ?? '📎'}</span>
									<div class="flex-1 min-w-0">
										<h4 class="text-sm font-medium text-foreground">{doc.title}</h4>
										<div class="flex items-center gap-2 mt-0.5">
											<span class="text-[10px] text-muted-foreground capitalize">{doc.doc_type}</span>
											<span class="text-[10px] text-muted-foreground">· {doc.created_by}</span>
											<span class="text-[10px] text-muted-foreground">· {timeAgo(doc.created_at)}</span>
										</div>
										{#if doc.content}
											<div class="mt-2 text-xs text-muted-foreground whitespace-pre-wrap border-t border-border/50 pt-2">{doc.content}</div>
										{/if}
										{#if doc.file_url}
											<a href={doc.file_url} target="_blank" rel="noopener"
												class="inline-flex items-center gap-1 mt-2 text-xs text-primary hover:underline">
												<ExternalLink class="w-3 h-3" /> View file
												{#if doc.file_size}
													<span class="text-muted-foreground">({(doc.file_size / 1024).toFixed(0)} KB)</span>
												{/if}
											</a>
										{/if}
										<button onclick={() => deleteDocument(doc.doc_id)}
											disabled={deletingDocId === doc.doc_id}
											class="ml-auto text-[10px] text-muted-foreground hover:text-destructive transition-colors disabled:opacity-50 mt-2">
											{deletingDocId === doc.doc_id ? 'Deleting...' : 'Delete'}
										</button>
									</div>
								</div>
							</div>
						{/each}
					{/if}
				</div>

			{:else if activeTab === 'contacts'}
				<!-- Contacts Tab -->
				<div class="space-y-3">
					<div class="flex items-center justify-between">
						<h3 class="text-sm font-semibold text-foreground">Linked Contacts</h3>
						<button onclick={() => showLinkContact = !showLinkContact}
							class="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">
							{#if showLinkContact}<X class="w-3 h-3" />{:else}<Plus class="w-3 h-3" />{/if}
							{showLinkContact ? 'Cancel' : 'Link Contact'}
						</button>
					</div>

					{#if showLinkContact}
						<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
							<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
								<div>
									<label class="text-xs text-muted-foreground uppercase tracking-wide">Contact ID</label>
									<input type="text" bind:value={linkContactId} placeholder="contact_001"
										class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring font-mono" />
								</div>
								<div>
									<label class="text-xs text-muted-foreground uppercase tracking-wide">Relationship</label>
									<select bind:value={linkContactRel}
										class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
										<option value="owner">Owner</option>
										<option value="possessor">Possessor</option>
										<option value="registered_to">Registered To</option>
										<option value="insurer">Insurer</option>
										<option value="custodian">Custodian</option>
										<option value="prior_owner">Prior Owner</option>
									</select>
								</div>
							</div>
							<button onclick={linkContact} disabled={!linkContactId.trim() || linkingContact}
								class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
								{linkingContact ? 'Linking...' : 'Link Contact'}
							</button>
						</div>
					{/if}

					{#if linkedContacts.length === 0}
						<div class="text-center py-8 text-muted-foreground">
							<User class="w-6 h-6 mx-auto mb-2 opacity-50" />
							<p class="text-sm">No linked contacts</p>
							<p class="text-xs">Link owners, custodians, or other related people</p>
						</div>
					{:else}
						{#each linkedContacts as c}
							<a href="/contacts/{c.contact_id}" class="flex items-center gap-3 px-4 py-3 rounded-lg border border-border bg-card hover:border-primary/40 transition-colors">
								<div class="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 {c.contact_type === 'company' ? 'bg-blue-500/10' : 'bg-primary/10'}">
									<svelte:component this={c.contact_type === 'company' ? Building : User} size={16}
										class="{c.contact_type === 'company' ? 'text-blue-400' : 'text-primary'}" />
								</div>
								<div class="flex-1 min-w-0">
									<h4 class="text-sm font-medium text-foreground">{c.primary_name}</h4>
									<p class="text-xs text-muted-foreground capitalize">{c.contact_type}</p>
								</div>
								<Badge text={c.relationship.replace(/_/g, ' ')} variant="info" size="sm" />
							</a>
						{/each}
					{/if}
				</div>

			{:else if activeTab === 'cases'}
				<!-- Cases Tab -->
				<div class="space-y-3">
					<div class="flex items-center justify-between">
						<h3 class="text-sm font-semibold text-foreground">Linked Cases</h3>
						<button onclick={() => showLinkCase = !showLinkCase}
							class="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">
							{#if showLinkCase}<X class="w-3 h-3" />{:else}<Plus class="w-3 h-3" />{/if}
							{showLinkCase ? 'Cancel' : 'Link Case'}
						</button>
					</div>

					{#if showLinkCase}
						<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
							<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
								<div>
									<label class="text-xs text-muted-foreground uppercase tracking-wide">Case ID</label>
									<input type="text" bind:value={linkCaseId} placeholder="case_47"
										class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring font-mono" />
								</div>
								<div>
									<label class="text-xs text-muted-foreground uppercase tracking-wide">Relationship</label>
									<select bind:value={linkCaseRel}
										class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
										<option value="evidence">Evidence</option>
										<option value="seized">Seized</option>
										<option value="recovered">Recovered</option>
										<option value="subject">Subject</option>
										<option value="stolen">Stolen</option>
										<option value="forfeited">Forfeited</option>
									</select>
								</div>
							</div>
							<button onclick={linkCase} disabled={!linkCaseId.trim() || linkingCase}
								class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
								{linkingCase ? 'Linking...' : 'Link Case'}
							</button>
						</div>
					{/if}

					{#if linkedCases.length === 0}
						<div class="text-center py-8 text-muted-foreground">
							<Briefcase class="w-6 h-6 mx-auto mb-2 opacity-50" />
							<p class="text-sm">No linked cases</p>
							<p class="text-xs">Link this asset to investigations</p>
						</div>
					{:else}
						{#each linkedCases as c}
							<a href="/cases/{c.case_id}" class="block rounded-lg border border-border bg-card p-4 hover:border-primary/40 transition-colors">
								<div class="flex items-center justify-between">
									<div class="flex items-center gap-3">
										<Briefcase class="w-4 h-4 text-muted-foreground shrink-0" />
										<div>
											<h4 class="text-sm font-medium text-foreground">{c.title}</h4>
											<div class="flex items-center gap-2 mt-0.5">
												<span class="text-[10px] font-mono text-muted-foreground">{c.case_id}</span>
												<span class="text-[10px] text-muted-foreground capitalize">· {c.relationship}</span>
											</div>
										</div>
									</div>
									<span class="inline-flex items-center rounded-full border font-medium text-xs px-1.5 py-0.5 {caseStatusColors[c.status] ?? caseStatusColors.closed}">{c.status}</span>
								</div>
							</a>
						{/each}
					{/if}
				</div>
			{/if}
		</div>
	</div>
{/if}

<!-- Edit Asset Modal -->
{#if showEditAsset}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="fixed inset-0 z-[200] flex items-center justify-center p-4" onclick={() => showEditAsset = false}>
		<div class="absolute inset-0 bg-black/50"></div>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="relative bg-card border border-border rounded-xl p-5 w-full max-w-md shadow-xl space-y-3" onclick={(e) => e.stopPropagation()}>
			<h3 class="text-sm font-semibold text-foreground">Edit Asset</h3>
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Name</label>
				<input type="text" bind:value={editName} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
			</div>
			<div class="grid grid-cols-2 gap-3">
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Category</label>
					<select bind:value={editCategory} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1">
						{#each categories as cat}
							<option value={cat}>{cat.replace(/_/g, ' ')}</option>
						{/each}
					</select>
				</div>
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Status</label>
					<select bind:value={editStatus} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1">
						{#each statuses as s}
							<option value={s}>{s}</option>
						{/each}
					</select>
				</div>
			</div>
			<div class="grid grid-cols-2 gap-3">
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Estimated Value</label>
					<input type="number" bind:value={editValue} placeholder="0" class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
				</div>
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Serial / ID</label>
					<input type="text" bind:value={editSerial} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
				</div>
			</div>
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Tags (comma-separated)</label>
				<input type="text" bind:value={editTags} placeholder="vehicle, evidence, case-47" class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
				<p class="text-[9px] text-muted-foreground/60 mt-1">Labels for organizing and searching assets</p>
			</div>
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Description</label>
				<textarea bind:value={editDescription} rows={2} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring resize-none"></textarea>
			</div>
			<div class="flex gap-2 pt-2">
				<button onclick={() => showEditAsset = false} class="flex-1 px-3 py-1.5 rounded-lg border border-border text-xs text-foreground hover:bg-muted transition-colors">Cancel</button>
				<button onclick={saveEditAsset} disabled={savingEdit} class="flex-1 px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 disabled:opacity-40 transition-colors">
					{savingEdit ? 'Saving...' : 'Save Changes'}
				</button>
			</div>
		</div>
	</div>
{/if}

<!-- Delete Asset Confirmation -->
{#if showDeleteConfirm}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="fixed inset-0 z-[200] flex items-center justify-center p-4" onclick={() => showDeleteConfirm = false}>
		<div class="absolute inset-0 bg-black/50"></div>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="relative bg-card border border-border rounded-xl p-5 w-full max-w-sm shadow-xl" onclick={(e) => e.stopPropagation()}>
			<h3 class="text-sm font-semibold text-foreground mb-2">Delete Asset</h3>
			<p class="text-xs text-muted-foreground mb-4">Are you sure you want to delete <strong>{asset?.display_name}</strong>? This will remove all linked documents, contacts, and case references.</p>
			<div class="flex gap-2">
				<button onclick={() => showDeleteConfirm = false} class="flex-1 px-3 py-1.5 rounded-lg border border-border text-xs text-foreground hover:bg-muted transition-colors">Cancel</button>
				<button onclick={deleteAsset} disabled={deletingAsset} class="flex-1 px-3 py-1.5 rounded-lg bg-destructive text-destructive-foreground text-xs font-medium hover:bg-destructive/90 disabled:opacity-40 transition-colors">
					{deletingAsset ? 'Deleting...' : 'Delete Asset'}
				</button>
			</div>
		</div>
	</div>
{/if}
