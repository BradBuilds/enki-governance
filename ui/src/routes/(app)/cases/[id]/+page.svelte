<script lang="ts">
	import { page } from '$app/stores';
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import { chat } from '$lib/stores/chat.svelte';
	import Badge from '$lib/components/shared/Badge.svelte';
	import MapView from '$lib/components/map/MapView.svelte';
	import EntityMap from '$lib/components/map/EntityMap.svelte';
	import EntityCard from '$lib/components/cards/EntityCard.svelte';
	import LoadingSkeleton from '$lib/components/shared/LoadingSkeleton.svelte';
	import ArrowLeft from 'lucide-svelte/icons/arrow-left';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import Plus from 'lucide-svelte/icons/plus';
	import Eye from 'lucide-svelte/icons/eye';
	import Clock from 'lucide-svelte/icons/clock';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Network from 'lucide-svelte/icons/network';
	import Shield from 'lucide-svelte/icons/shield';
	import Radio from 'lucide-svelte/icons/radio';
	import Search from 'lucide-svelte/icons/search';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import Building from 'lucide-svelte/icons/building';
	import User from 'lucide-svelte/icons/user';
	import X from 'lucide-svelte/icons/x';
	import Contact from 'lucide-svelte/icons/contact';
	import PackageIcon from 'lucide-svelte/icons/package';
	import FileText from 'lucide-svelte/icons/file-text';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import EyeIcon from 'lucide-svelte/icons/eye';
	import AskEnkiButton from '$lib/components/shared/AskEnkiButton.svelte';
	import DollarSign from 'lucide-svelte/icons/dollar-sign';
	import Car from 'lucide-svelte/icons/car';
	import HomeIcon from 'lucide-svelte/icons/home';
	import GemIcon from 'lucide-svelte/icons/gem';
	import LaptopIcon from 'lucide-svelte/icons/laptop';

	const caseId = $derived($page.params.id ?? '');
	let caseData = $state<any>(null);
	let timeline = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let toast = $state<string | null>(null);
	let caseGeoSignals = $state<any[]>([]);
	let caseNodes = $state<any[]>([]);
	let caseContacts = $state<any[]>([]);
	let caseAssets = $state<any[]>([]);
	let caseDocs = $state<any[]>([]);
	let caseFeeds = $state<any[]>([]);

	// Combined map entities from all case data
	let caseMapEntities = $derived.by(() => {
		const points: any[] = [];
		for (const sig of caseGeoSignals) {
			if (sig.latitude != null && sig.longitude != null) {
				points.push({ id: sig.signal_id || sig.event_type, latitude: sig.latitude, longitude: sig.longitude,
					title: sig.title || sig.event_type || 'Signal', type: 'signal' });
			}
		}
		for (const node of caseNodes) {
			if (node.latitude != null && node.longitude != null) {
				points.push({ id: node.node_id, latitude: node.latitude, longitude: node.longitude,
					title: node.title, type: 'location' });
			}
		}
		for (const c of caseContacts) {
			if (c.metadata?.latitude != null && c.metadata?.longitude != null) {
				points.push({ id: c.contact_id, latitude: c.metadata.latitude, longitude: c.metadata.longitude,
					title: c.primary_name, type: c.contact_type === 'country' ? 'country' : 'person' });
			}
		}
		for (const a of caseAssets) {
			if (a.latitude != null && a.longitude != null) {
				points.push({ id: a.asset_id, latitude: a.latitude, longitude: a.longitude,
					title: a.display_name, type: 'asset' });
			}
		}
		return points;
	});

	// New doc form
	let showNewDoc = $state(false);
	let newDocTitle = $state('');
	let newDocType = $state('note');
	let newDocClassification = $state('unclassified');
	let newDocContent = $state('');
	let newDocTags = $state('');
	let creatingDoc = $state(false);

	// Tabs
	let activeTab = $state<string>('timeline');
	const tabs = [
		{ id: 'timeline', label: 'Timeline', icon: Clock },
		{ id: 'documents', label: 'Evidence', icon: FileText },
		{ id: 'graph', label: 'Graph', icon: Network },
		{ id: 'map', label: 'Map', icon: MapPin },
		{ id: 'contacts', label: 'Contacts', icon: Contact },
		{ id: 'assets', label: 'Assets', icon: PackageIcon },
		{ id: 'watchlist', label: 'Watchlist', icon: Eye },
		{ id: 'observables', label: 'Add Data', icon: Plus },
	];

	// Status management
	let updatingStatus = $state(false);
	const statusOrder = ['open', 'investigating', 'resolved', 'closed'];
	const statusVariant: Record<string, 'info' | 'medium' | 'success' | 'neutral'> = {
		open: 'info', investigating: 'medium', resolved: 'success', closed: 'neutral',
	};
	const priorityVariant: Record<string, 'critical' | 'high' | 'medium' | 'low'> = {
		P1: 'critical', P2: 'high', P3: 'medium', P4: 'low',
		critical: 'critical', high: 'high', medium: 'medium', low: 'low',
	};

	// Add observable form
	let showAddObs = $state(false);
	let obsType = $state('ip_address');
	let obsValue = $state('');
	let addingObs = $state(false);

	const obsTypes = ['ip_address', 'email_address', 'domain_name', 'license_plate', 'url',
		'phone_number', 'callsign', 'aircraft_registration', 'icao_hex', 'vessel_mmsi',
		'company_name', 'person_name', 'lei', 'ein', 'sec_cik',
		'file_hash_sha256', 'hostname', 'custom'];

	// Entity resolution
	let showEntityModal = $state(false);
	let entityType = $state<'company' | 'person' | 'address'>('company');
	let entityValue = $state('');
	let resolving = $state(false);
	let resolveResult = $state<any>(null);
	let crossCaseHits = $state<any[]>([]);

	// Watchlist data
	let watchlistItems = $state<any[]>([]);

	async function resolveEntity() {
		if (!entityValue.trim()) return;
		resolving = true;
		resolveResult = null;
		crossCaseHits = [];
		try {
			const data = await api.resolveEntity(entityType, entityValue.trim(), caseId);
			resolveResult = data;
			crossCaseHits = data?.graph?.cross_case_hits || [];
			// Refresh case data to show new nodes + observables
			await loadCase();
			showToast(`Resolved: ${data?.graph?.nodes_created?.length || 0} nodes, ${data?.graph?.observables_added?.length || 0} watchlist items`);
		} catch (e: any) {
			showToast('Resolution failed');
		}
		resolving = false;
	}

	async function loadWatchlist() {
		try {
			const data: any = await api.caseObservables(caseId);
			watchlistItems = data?.observables || [];
		} catch { watchlistItems = []; }
	}

	onMount(loadCase);

	async function loadCase() {
		try {
			caseData = await api.getCase(caseId);
			try {
				const tl: any = await api.caseTimeline(caseId);
				timeline = tl.events || [];
				// Extract geo signals for map tab
				caseGeoSignals = (tl.entries || tl.events || []).filter(
					(e: any) => e.latitude != null && e.longitude != null
				);
			} catch { timeline = []; }
			// Load linked knowledge nodes
			try {
				const nd: any = await api.caseNodes(caseId);
				caseNodes = nd.nodes || [];
			} catch { caseNodes = []; }
			// Load watchlist + contacts + assets
			await loadWatchlist();
			try {
				const cc: any = await api.caseContacts(caseId);
				caseContacts = cc.contacts || [];
			} catch { caseContacts = []; }
			try {
				const ca: any = await api.caseAssets(caseId);
				caseAssets = ca.assets || [];
			} catch { caseAssets = []; }
			try {
				const cd: any = await api.caseDocuments(caseId);
				caseDocs = cd.documents || [];
			} catch { caseDocs = []; }
			try {
				const cf: any = await api.caseTopics(caseId);
				caseFeeds = cf.topics || [];
			} catch { caseFeeds = []; }
		} catch {
			error = 'Case not found or API unreachable';
		}
		loading = false;
	}

	$effect(() => {
		if (caseData) {
			chat.setContext({ currentPage: '/cases/' + caseId, selectedItem: { type: 'case', id: caseId, title: caseData?.title } });
		}
	});

	function showToast(msg: string) {
		toast = msg;
		setTimeout(() => toast = null, 3000);
	}

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const hrs = Math.floor(diff / 3600000);
		if (hrs < 1) return `${Math.floor(diff / 60000)}m ago`;
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}

	async function updateStatus(newStatus: string) {
		updatingStatus = true;
		try {
			await api.updateCaseStatus(caseId, newStatus);
			caseData = { ...caseData, status: newStatus };
			showToast(`Status updated to ${newStatus}`);
		} catch { showToast('Failed to update status'); }
		updatingStatus = false;
	}

	const docTypeIcons: Record<string, string> = {
		note: '📝', evidence: '🔒', warrant: '⚖️', report: '📊', photo: '📷',
		video: '🎬', filing: '📋', transcript: '🎙️', social_media: '📱', summary: '📄', other: '📎',
	};

	const classificationColors: Record<string, string> = {
		unclassified: 'bg-muted text-muted-foreground border-border',
		sensitive: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
		confidential: 'bg-[var(--severity-high)]/15 text-[var(--severity-high)] border-[var(--severity-high)]/30',
		restricted: 'bg-[var(--severity-critical)]/15 text-[var(--severity-critical)] border-[var(--severity-critical)]/30',
	};

	async function addCaseDocument() {
		if (!newDocTitle.trim()) return;
		creatingDoc = true;
		try {
			await api.addCaseDocument(caseId, {
				title: newDocTitle,
				doc_type: newDocType,
				classification: newDocClassification,
				content: newDocContent || undefined,
				tags: newDocTags ? newDocTags.split(',').map((t: string) => t.trim()).filter(Boolean) : [],
			});
			showToast('Document added');
			showNewDoc = false;
			newDocTitle = ''; newDocContent = ''; newDocType = 'note'; newDocClassification = 'unclassified'; newDocTags = '';
			const cd: any = await api.caseDocuments(caseId);
			caseDocs = cd.documents || [];
		} catch { showToast('Failed to add document'); }
		creatingDoc = false;
	}

	async function addObservable() {
		if (!obsValue.trim()) return;
		addingObs = true;
		try {
			await api.addObservable(caseId, { observable_type: obsType, observable_value: obsValue });
			showToast(`Observable added: ${obsValue}`);
			obsValue = '';
			showAddObs = false;
			await loadCase();
		} catch { showToast('Failed to add observable'); }
		addingObs = false;
	}

	// ─── Link Contact ──────────────────────────────────────────────────
	let showLinkContact = $state(false);
	let linkContactId = $state('');
	let linkContactRel = $state('subject');
	let linkingContact = $state(false);
	const contactRelTypes = ['subject', 'witness', 'suspect', 'informant', 'victim', 'associate', 'owner', 'employee'];

	async function linkContact() {
		if (!linkContactId.trim()) return;
		linkingContact = true;
		try {
			await api.linkCaseContact(caseId, { contact_id: linkContactId, relationship: linkContactRel });
			showToast('Contact linked');
			showLinkContact = false; linkContactId = ''; linkContactRel = 'subject';
			const cc: any = await api.caseContacts(caseId);
			caseContacts = cc.contacts || [];
		} catch { showToast('Failed to link contact'); }
		linkingContact = false;
	}

	// ─── Link Asset ────────────────────────────────────────────────────
	let showLinkAsset = $state(false);
	let linkAssetId = $state('');
	let linkAssetRel = $state('evidence');
	let linkingAsset = $state(false);
	const assetRelTypes = ['evidence', 'seized', 'recovered', 'stolen', 'subject', 'forfeited'];

	async function linkAsset() {
		if (!linkAssetId.trim()) return;
		linkingAsset = true;
		try {
			await api.linkCaseAsset(caseId, { asset_id: linkAssetId, relationship: linkAssetRel });
			showToast('Asset linked');
			showLinkAsset = false; linkAssetId = ''; linkAssetRel = 'evidence';
			const ca: any = await api.caseAssets(caseId);
			caseAssets = ca.assets || [];
		} catch { showToast('Failed to link asset'); }
		linkingAsset = false;
	}

	// ─── Delete Document ───────────────────────────────────────────────
	let deletingDocId = $state<string | null>(null);

	async function deleteDocument(docId: string) {
		deletingDocId = docId;
		try {
			await api.deleteCaseDocument(caseId, docId);
			caseDocs = caseDocs.filter(d => d.document_id !== docId);
			showToast('Document deleted');
		} catch { showToast('Failed to delete document'); }
		deletingDocId = null;
	}

	// ─── Edit Case Fields ──────────────────────────────────────────────
	let showEditCase = $state(false);
	let editTitle = $state('');
	let editDescription = $state('');
	let editPriority = $state('');
	let editOwner = $state('');
	let editTags = $state('');
	let savingEdit = $state(false);

	function openEditCase() {
		editTitle = caseData?.title || '';
		editDescription = caseData?.description || '';
		editPriority = caseData?.priority || 'medium';
		editOwner = caseData?.owner || '';
		editTags = (caseData?.tags || []).join(', ');
		showEditCase = true;
	}

	async function saveEditCase() {
		savingEdit = true;
		try {
			await api.updateCase(caseId, {
				title: editTitle, description: editDescription,
				priority: editPriority, owner: editOwner || null,
				tags: editTags ? editTags.split(',').map((t: string) => t.trim()).filter(Boolean) : [],
			});
			caseData = { ...caseData, title: editTitle, description: editDescription,
				priority: editPriority, owner: editOwner, tags: editTags.split(',').map((t: string) => t.trim()).filter(Boolean) };
			showEditCase = false;
			showToast('Case updated');
		} catch { showToast('Failed to update case'); }
		savingEdit = false;
	}

	// ─── Delete Case ───────────────────────────────────────────────────
	let showDeleteConfirm = $state(false);
	let deletingCase = $state(false);

	async function deleteCase() {
		deletingCase = true;
		try {
			await api.deleteCase(caseId);
			showToast('Case deleted');
			const { goto } = await import('$app/navigation');
			goto('/cases');
		} catch { showToast('Failed to delete case'); }
		deletingCase = false;
	}
</script>

{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg">
		{toast}
	</div>
{/if}

<div class="space-y-4 max-w-4xl">
	<a href="/cases" class="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors">
		<ArrowLeft class="w-4 h-4" /> Back to Cases
	</a>

	{#if loading}
		<div class="text-center py-20 text-muted-foreground">
			<Briefcase class="w-8 h-8 mx-auto mb-2 animate-pulse" />
			<p>Loading case...</p>
		</div>
	{:else if error}
		<div class="text-center py-20 text-muted-foreground">
			<Briefcase class="w-8 h-8 mx-auto mb-2 opacity-50" />
			<p>{error}</p>
		</div>
	{:else if caseData}
		<!-- Breadcrumb -->
		{#if caseData.project_id}
			<a href="/projects/{caseData.project_id}" class="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors mb-2">
				<ArrowLeft class="w-3 h-3" /> Back to Project
			</a>
		{:else}
			<a href="/projects" class="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors mb-2">
				<ArrowLeft class="w-3 h-3" /> All Projects
			</a>
		{/if}

		<!-- Case Header Card -->
		<div class="rounded-lg border border-border bg-card p-4">
			<div class="flex items-start justify-between">
				<div>
					<p class="text-xs font-mono text-muted-foreground mb-1">{caseData.case_id}</p>
					<h1 class="text-lg font-semibold text-foreground">{caseData.title}</h1>
					{#if caseData.description}
						<p class="text-sm text-muted-foreground mt-1">{caseData.description}</p>
					{/if}
					<div class="flex items-center gap-3 mt-3">
						<Badge text={caseData.status ?? 'open'} variant={statusVariant[caseData.status] ?? 'info'} size="md" />
						<Badge text={caseData.priority ?? 'P3'} variant={priorityVariant[caseData.priority] ?? 'medium'} size="md" />
						{#if caseData.owner}
							<span class="text-xs text-muted-foreground">Owner: <span class="text-foreground">{caseData.owner}</span></span>
						{/if}
					</div>
				</div>
				<div class="text-right text-xs text-muted-foreground">
					<p>Created {timeAgo(caseData.created_at)}</p>
					<p>Updated {timeAgo(caseData.updated_at)}</p>
				</div>
			</div>

			<!-- Tags -->
			{#if caseData.tags?.length > 0}
				<div class="flex gap-1 mt-3 flex-wrap">
					{#each caseData.tags as tag}
						<span class="text-[10px] px-2 py-0.5 rounded bg-muted text-muted-foreground">{tag}</span>
					{/each}
				</div>
			{/if}

			<!-- Status progression -->
			<div class="flex items-center gap-2 mt-4 pt-3 border-t border-border">
				<span class="text-xs text-muted-foreground mr-2">Move to:</span>
				{#each statusOrder as s}
					{#if s !== caseData.status}
						<button onclick={() => updateStatus(s)} disabled={updatingStatus}
							class="px-3 py-1 text-[10px] rounded border border-border hover:border-primary/50 text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50">
							{s}
						</button>
					{/if}
				{/each}
				<AskEnkiButton prompt="Summarize case {caseData.case_id}" variant="compact" class="ml-auto" />
				<button onclick={openEditCase} class="text-[10px] text-muted-foreground hover:text-foreground px-2 py-1 rounded border border-border/50 hover:bg-muted transition-colors">Edit</button>
				<button onclick={() => showDeleteConfirm = true} class="text-[10px] text-muted-foreground hover:text-destructive px-2 py-1 rounded border border-border/50 hover:bg-destructive/10 transition-colors">Delete</button>
			</div>
		</div>

		<!-- Linked alerts (always visible above tabs) -->
		<!-- Feeding Topics -->
		{#if caseFeeds.length > 0}
			<div class="rounded-xl border border-border bg-card p-4">
				<h3 class="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2 flex items-center gap-1.5">
					<EyeIcon class="w-3.5 h-3.5" /> Feeding Topics
				</h3>
				<div class="flex flex-wrap gap-2">
					{#each caseFeeds as ft}
						<a href="/watch/{ft.topic_id}" class="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-border/50 hover:border-primary/40 transition-colors">
							<EyeIcon class="w-3.5 h-3.5 text-violet-400" />
							<span class="text-xs text-foreground">{ft.title}</span>
							<span class="text-[10px] text-muted-foreground">{ft.article_count} articles</span>
							<span class="w-1.5 h-1.5 rounded-full {ft.auto_sync ? 'bg-emerald-400' : 'bg-muted-foreground'}"></span>
						</a>
					{/each}
				</div>
			</div>
		{/if}

		{#if caseData.alerts?.length > 0}
			<div class="rounded-lg border border-border bg-card p-4">
				<h3 class="text-sm font-semibold text-foreground mb-3">Linked Alerts</h3>
				<div class="space-y-2">
					{#each caseData.alerts as alert}
						<div class="flex items-center gap-3 px-3 py-2 rounded-md bg-muted/30">
							<span class="text-xs font-mono text-muted-foreground">{alert.alert_id}</span>
							<span class="text-sm text-foreground flex-1">{alert.title}</span>
							<Badge text={alert.severity ?? 'medium'} variant={priorityVariant[alert.severity] ?? 'medium'} size="sm" />
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<!-- Tab navigation -->
		<div class="flex gap-1 border-b border-border">
			{#each tabs as tab}
				<button
					onclick={() => activeTab = tab.id}
					class="flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px
						{activeTab === tab.id
							? 'border-primary text-foreground'
							: 'border-transparent text-muted-foreground hover:text-foreground hover:border-border'}"
				>
					<tab.icon class="w-4 h-4" />
					{tab.label}
				</button>
			{/each}
		</div>

		<!-- Tab content -->
		{#if activeTab === 'timeline'}
			<div class="rounded-lg border border-border bg-card p-4">
				<h3 class="text-sm font-semibold text-foreground mb-3 flex items-center gap-2">
					<Clock class="w-4 h-4" /> Timeline
				</h3>
				{#if timeline.length > 0}
					<div class="space-y-3">
						{#each timeline as event}
							<div class="flex gap-3">
								<div class="w-px bg-border shrink-0 ml-1"></div>
								<div class="pb-3">
									<p class="text-xs text-foreground">{event.event_type}</p>
									{#if event.new_value}
										<p class="text-[10px] text-muted-foreground">{JSON.stringify(event.new_value)}</p>
									{/if}
									<p class="text-[10px] text-muted-foreground mt-0.5">{event.actor_id} &middot; {timeAgo(event.commit_time)}</p>
								</div>
							</div>
						{/each}
					</div>
				{:else}
					<div class="text-center py-6">
						<p class="text-xs text-muted-foreground">Case created {timeAgo(caseData.created_at)}</p>
						{#if caseData.owner}
							<p class="text-[10px] text-muted-foreground mt-1">Assigned to {caseData.owner}</p>
						{/if}
					</div>
				{/if}
			</div>

		{:else if activeTab === 'documents'}
			<!-- Evidence Locker -->
			<div class="space-y-3">
				<div class="flex items-center justify-between">
					<h3 class="text-sm font-semibold text-foreground">Evidence Locker</h3>
					<button onclick={() => showNewDoc = !showNewDoc}
						class="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors">
						{#if showNewDoc}<X class="w-3 h-3" />{:else}<Plus class="w-3 h-3" />{/if}
						{showNewDoc ? 'Cancel' : 'Add Document'}
					</button>
				</div>

				{#if showNewDoc}
					<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
						<div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
							<div>
								<label class="text-xs text-muted-foreground uppercase tracking-wide">Title</label>
								<input type="text" bind:value={newDocTitle} placeholder="Document title..."
									class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
							</div>
							<div>
								<label class="text-xs text-muted-foreground uppercase tracking-wide">Type</label>
								<select bind:value={newDocType}
									class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
									<option value="note">Note</option>
									<option value="evidence">Evidence</option>
									<option value="warrant">Warrant</option>
									<option value="report">Report</option>
									<option value="photo">Photo</option>
									<option value="video">Video</option>
									<option value="filing">Filing</option>
									<option value="transcript">Transcript</option>
									<option value="social_media">Social Media</option>
									<option value="summary">Summary</option>
									<option value="other">Other</option>
								</select>
							</div>
							<div>
								<label class="text-xs text-muted-foreground uppercase tracking-wide">Classification</label>
								<select bind:value={newDocClassification}
									class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
									<option value="unclassified">Unclassified</option>
									<option value="sensitive">Sensitive</option>
									<option value="confidential">Confidential</option>
									<option value="restricted">Restricted</option>
								</select>
							</div>
						</div>
						<div>
							<label class="text-xs text-muted-foreground uppercase tracking-wide">Content</label>
							<textarea bind:value={newDocContent} rows="5" placeholder="Document content (markdown supported)..."
								class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring resize-none font-mono text-xs"></textarea>
						</div>
						<div>
							<label class="text-xs text-muted-foreground uppercase tracking-wide">Tags</label>
							<input type="text" bind:value={newDocTags} placeholder="Comma-separated tags..."
								class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
						</div>
						<button onclick={addCaseDocument} disabled={!newDocTitle.trim() || creatingDoc}
							class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
							{creatingDoc ? 'Saving...' : 'Save Document'}
						</button>
					</div>
				{/if}

				{#if caseDocs.length === 0}
					<div class="text-center py-6 text-muted-foreground">
						<FileText class="w-6 h-6 mx-auto mb-2 opacity-50" />
						<p class="text-sm">No documents in evidence locker</p>
						<p class="text-xs">Add reports, photos, warrants, transcripts, and analyst notes</p>
					</div>
				{:else}
					{#each caseDocs as doc}
						<div class="rounded-lg border border-border bg-card overflow-hidden">
							<div class="px-4 py-3 flex items-start gap-3">
								<span class="text-lg shrink-0">{docTypeIcons[doc.doc_type] ?? '📎'}</span>
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2 flex-wrap">
										<h4 class="text-sm font-medium text-foreground">{doc.title}</h4>
										{#if doc.classification && doc.classification !== 'unclassified'}
											<span class="inline-flex items-center rounded-full border font-medium text-[10px] px-1.5 py-0.5 uppercase tracking-wider {classificationColors[doc.classification] ?? classificationColors.unclassified}">
												{doc.classification}
											</span>
										{/if}
									</div>
									<div class="flex items-center gap-2 mt-0.5 flex-wrap">
										<span class="text-[10px] text-muted-foreground capitalize">{doc.doc_type}</span>
										<span class="text-[10px] text-muted-foreground">· {doc.created_by}</span>
										<span class="text-[10px] text-muted-foreground">· {timeAgo(doc.created_at)}</span>
									</div>
									{#if doc.tags?.length}
										<div class="flex flex-wrap gap-1 mt-1.5">
											{#each doc.tags as tag}
												<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground">{tag}</span>
											{/each}
										</div>
									{/if}
									{#if doc.content}
										<div class="mt-2 text-xs text-muted-foreground whitespace-pre-wrap border-t border-border/50 pt-2 max-h-[200px] overflow-y-auto">
											{doc.content}
										</div>
									{/if}
									<div class="flex items-center gap-3 mt-2">
										{#if doc.file_url}
											<a href={doc.file_url} target="_blank" rel="noopener"
												class="inline-flex items-center gap-1 text-xs text-primary hover:underline">
												<ExternalLink class="w-3 h-3" /> View file
												{#if doc.file_size}
													<span class="text-muted-foreground">({(doc.file_size / 1024 / 1024).toFixed(1)} MB)</span>
												{/if}
											</a>
										{/if}
										<button onclick={() => deleteDocument(doc.document_id)}
											disabled={deletingDocId === doc.document_id}
											class="ml-auto text-[10px] text-muted-foreground hover:text-destructive transition-colors disabled:opacity-50">
											{deletingDocId === doc.document_id ? 'Deleting...' : 'Delete'}
										</button>
									</div>
								</div>
							</div>
						</div>
					{/each}
				{/if}
			</div>

		{:else if activeTab === 'graph'}
			<!-- Knowledge Graph -->
			<div class="rounded-lg border border-border bg-card p-4">
				<div class="flex items-center justify-between mb-3">
					<h3 class="text-sm font-semibold text-foreground flex items-center gap-2">
						<Network class="w-4 h-4" /> Entity Graph
					</h3>
					<button onclick={() => showEntityModal = true}
						class="flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg bg-primary text-primary-foreground hover:bg-primary/90">
						<Search class="w-3 h-3" /> Investigate Entity
					</button>
				</div>

				{#if crossCaseHits.length > 0}
					<div class="mb-3 p-3 rounded-lg border border-yellow-500/30 bg-yellow-500/5">
						<div class="flex items-center gap-2 text-sm text-yellow-400 font-medium mb-1">
							<AlertTriangle size={14} /> Cross-Case References Detected
						</div>
						{#each crossCaseHits as hit}
							<div class="flex items-center gap-2 mt-1 text-xs">
								<span class="text-foreground">{hit.match_value}</span>
								<span class="text-muted-foreground">also in</span>
								<a href="/cases/{hit.case_id}" class="text-primary hover:underline">{hit.case_title}</a>
								<Badge text={hit.case_status} variant="info" size="sm" />
							</div>
						{/each}
					</div>
				{/if}

				{#if caseNodes.length > 0}
					<div class="space-y-2">
						{#each caseNodes as node}
							{@const typeColors: Record<string, string> = { company: 'bg-blue-500', person: 'bg-purple-500', location: 'bg-emerald-500', aircraft: 'bg-yellow-500', document: 'bg-red-500' }}
							{@const typeIcons: Record<string, any> = { company: Building, person: User, location: MapPin, aircraft: Radio, document: Briefcase }}
							{@const Icon = typeIcons[node.epistemic_type] || Network}
							<div class="flex items-start gap-3 p-3 rounded-lg border border-border hover:border-primary/30 transition-colors">
								<div class="w-8 h-8 rounded-lg {typeColors[node.epistemic_type] || 'bg-muted'}/10 flex items-center justify-center shrink-0 mt-0.5">
									<svelte:component this={Icon} size={16} class="text-foreground/70" />
								</div>
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2">
										<span class="text-sm font-medium text-foreground">{node.title}</span>
										<Badge text={node.epistemic_type} variant="neutral" size="sm" />
									</div>
									{#if node.description}
										<p class="text-xs text-muted-foreground mt-0.5 line-clamp-2">{node.description}</p>
									{/if}
									{#if node.concepts?.length}
										<div class="flex gap-1 mt-1 flex-wrap">
											{#each node.concepts.slice(0, 5) as tag}
												<span class="text-[9px] px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground">{tag}</span>
											{/each}
										</div>
									{/if}
								</div>
								{#if node.latitude && node.longitude}
									<span class="text-[9px] text-muted-foreground font-mono">{node.latitude.toFixed(2)}, {node.longitude.toFixed(2)}</span>
								{/if}
							</div>
						{/each}
					</div>
				{:else}
					<div class="text-center py-8 text-muted-foreground">
						<Network class="w-8 h-8 mx-auto mb-2 opacity-30" />
						<p class="text-sm">No entities investigated yet</p>
						<p class="text-xs mt-1">Click "Investigate Entity" to discover companies, people, and connections</p>
					</div>
				{/if}

				{#if resolveResult}
					<div class="mt-4 pt-4 border-t border-border">
						<h4 class="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-2">Latest Investigation Result</h4>
						<EntityCard entityType={entityType} entityValue={entityValue} caseId={caseId} />
					</div>
				{/if}
			</div>

		{:else if activeTab === 'contacts'}
			<!-- Contacts Tab -->
			<div class="space-y-3">
				<div class="flex items-center justify-between">
					<h3 class="text-sm font-semibold text-foreground">Linked Contacts</h3>
					<button onclick={() => showLinkContact = !showLinkContact}
						class="flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg border border-border hover:bg-muted transition-colors">
						<Plus class="w-3 h-3" /> {showLinkContact ? 'Cancel' : 'Link Contact'}
					</button>
				</div>
				{#if showLinkContact}
					<div class="rounded-lg border border-border bg-muted/30 p-3 space-y-2">
						<input type="text" bind:value={linkContactId} placeholder="Contact ID"
							class="w-full px-3 py-1.5 rounded-md border border-border bg-card text-foreground text-sm outline-none focus:ring-2 focus:ring-ring" />
						<select bind:value={linkContactRel} class="w-full px-3 py-1.5 rounded-md border border-border bg-card text-foreground text-sm">
							{#each contactRelTypes as rel}
								<option value={rel}>{rel.replace(/_/g, ' ')}</option>
							{/each}
						</select>
						<button onclick={linkContact} disabled={!linkContactId.trim() || linkingContact}
							class="px-4 py-1.5 rounded-md bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 disabled:opacity-40 transition-colors">
							{linkingContact ? 'Linking...' : 'Link'}
						</button>
					</div>
				{/if}
				{#if caseContacts.length === 0 && !showLinkContact}
					<div class="text-center py-6 text-muted-foreground">
						<Contact class="w-6 h-6 mx-auto mb-2 opacity-50" />
						<p class="text-sm">No contacts linked to this case</p>
					</div>
				{:else}
					{#each caseContacts as c}
						<a href="/contacts/{c.contact_id}" class="flex items-center gap-3 px-4 py-3 rounded-lg border border-border bg-card hover:border-primary/40 transition-colors">
							<div class="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 {c.contact_type === 'company' ? 'bg-blue-500/10' : 'bg-primary/10'}">
								<svelte:component this={c.contact_type === 'company' ? Building : User} size={16}
									class="{c.contact_type === 'company' ? 'text-blue-400' : 'text-primary'}" />
							</div>
							<div class="flex-1 min-w-0">
								<h4 class="text-sm font-medium text-foreground">{c.primary_name}</h4>
								<span class="text-[10px] text-muted-foreground capitalize">{c.contact_type}</span>
							</div>
							<Badge text={(c.relationship || '').replace(/_/g, ' ')} variant="info" size="sm" />
						</a>
					{/each}
				{/if}
			</div>

		{:else if activeTab === 'assets'}
			<!-- Assets Tab -->
			<div class="space-y-3">
				<div class="flex items-center justify-between">
					<h3 class="text-sm font-semibold text-foreground">Linked Assets</h3>
					<button onclick={() => showLinkAsset = !showLinkAsset}
						class="flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg border border-border hover:bg-muted transition-colors">
						<Plus class="w-3 h-3" /> {showLinkAsset ? 'Cancel' : 'Link Asset'}
					</button>
				</div>
				{#if showLinkAsset}
					<div class="rounded-lg border border-border bg-muted/30 p-3 space-y-2">
						<input type="text" bind:value={linkAssetId} placeholder="Asset ID"
							class="w-full px-3 py-1.5 rounded-md border border-border bg-card text-foreground text-sm outline-none focus:ring-2 focus:ring-ring" />
						<select bind:value={linkAssetRel} class="w-full px-3 py-1.5 rounded-md border border-border bg-card text-foreground text-sm">
							{#each assetRelTypes as rel}
								<option value={rel}>{rel.replace(/_/g, ' ')}</option>
							{/each}
						</select>
						<button onclick={linkAsset} disabled={!linkAssetId.trim() || linkingAsset}
							class="px-4 py-1.5 rounded-md bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 disabled:opacity-40 transition-colors">
							{linkingAsset ? 'Linking...' : 'Link'}
						</button>
					</div>
				{/if}
				{#if caseAssets.length === 0 && !showLinkAsset}
					<div class="text-center py-6 text-muted-foreground">
						<PackageIcon class="w-6 h-6 mx-auto mb-2 opacity-50" />
						<p class="text-sm">No assets linked to this case</p>
					</div>
				{:else}
					{#each caseAssets as asset}
						{@const catIcons: Record<string, any> = { vehicle: Car, real_estate: HomeIcon, jewelry: GemIcon, electronics: LaptopIcon }}
						{@const AssetIcon = catIcons[asset.asset_category] || PackageIcon}
						{@const catColors: Record<string, string> = { vehicle: 'bg-blue-500/10 text-blue-400', real_estate: 'bg-amber-500/10 text-amber-400', jewelry: 'bg-purple-500/10 text-purple-400', electronics: 'bg-cyan-500/10 text-cyan-400' }}
						<a href="/assets/{asset.asset_id}" class="flex items-center gap-3 px-4 py-3 rounded-lg border border-border bg-card hover:border-primary/40 transition-colors">
							<div class="w-8 h-8 rounded-lg flex items-center justify-center shrink-0 {catColors[asset.asset_category] || 'bg-muted'}">
								<svelte:component this={AssetIcon} size={16} />
							</div>
							<div class="flex-1 min-w-0">
								<h4 class="text-sm font-medium text-foreground truncate">{asset.display_name}</h4>
								<div class="flex items-center gap-2 mt-0.5">
									<span class="text-[10px] text-muted-foreground capitalize">{(asset.asset_category || '').replace(/_/g, ' ')}</span>
									<span class="text-[10px] text-muted-foreground capitalize">· {(asset.relationship || '').replace(/_/g, ' ')}</span>
								</div>
							</div>
							<div class="flex items-center gap-2">
								{#if asset.estimated_value}
									<span class="text-xs font-mono text-foreground flex items-center gap-0.5">
										<DollarSign class="w-3 h-3 text-emerald-400" />
										{new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(asset.estimated_value)}
									</span>
								{/if}
								<Badge text={asset.status} variant={asset.status === 'active' ? 'success' : asset.status === 'seized' ? 'critical' : asset.status === 'missing' ? 'high' : 'neutral'} size="sm" />
							</div>
						</a>
					{/each}
				{/if}
			</div>

		{:else if activeTab === 'watchlist'}
			<!-- Watchlist / Observables -->
			<div class="rounded-lg border border-border bg-card p-4">
				<div class="flex items-center justify-between mb-3">
					<h3 class="text-sm font-semibold text-foreground flex items-center gap-2">
						<Eye class="w-4 h-4" /> Watchlist ({watchlistItems.length} observables)
					</h3>
					<button onclick={() => { showAddObs = true; activeTab = 'observables'; }}
						class="flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg border border-border hover:bg-muted">
						<Plus class="w-3 h-3" /> Add Observable
					</button>
				</div>

				{#if watchlistItems.length > 0}
					<div class="space-y-1">
						{#each watchlistItems as obs}
							{@const isEntity = ['company_name', 'person_name', 'lei', 'ein', 'sec_cik'].includes(obs.observable_type)}
							<div class="flex items-center gap-2 px-3 py-2 rounded-lg {isEntity ? 'bg-primary/5 border border-primary/20' : 'hover:bg-muted'}">
								<span class="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground font-mono min-w-[100px]">{obs.observable_type}</span>
								<span class="text-xs text-foreground flex-1 truncate font-mono">{obs.observable_value}</span>
								<span class="text-[9px] text-muted-foreground">{obs.added_by === 'graph_builder' ? 'auto' : obs.added_by === 'entity_resolver' ? 'auto' : 'manual'}</span>
							</div>
						{/each}
					</div>
				{:else}
					<p class="text-xs text-muted-foreground text-center py-4">No observables on watchlist yet</p>
				{/if}
			</div>

		{:else if activeTab === 'observables'}
			<div class="rounded-lg border border-border bg-card p-4">
				<div class="flex items-center justify-between mb-3">
					<h3 class="text-sm font-semibold text-foreground flex items-center gap-2">
						<Plus class="w-4 h-4" /> Add Data
					</h3>
				</div>

				<!-- Entity Investigation -->
				<div class="mb-4 p-4 rounded-lg border border-primary/20 bg-primary/5">
					<h4 class="text-sm font-medium text-foreground mb-2 flex items-center gap-2">
						<Search class="w-4 h-4 text-primary" /> Investigate Entity
					</h4>
					<p class="text-xs text-muted-foreground mb-3">Search SEC, GLEIF, FINRA, CourtListener, USAspending for an entity. Results are added to the knowledge graph and watchlist automatically.</p>
					<div class="flex gap-2 mb-2">
						{#each [['company', Building, 'Company'], ['person', User, 'Person'], ['address', MapPin, 'Address']] as [type, icon, label]}
							<button
								onclick={() => entityType = type as typeof entityType}
								class="flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs transition-colors
									{entityType === type ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}"
							>
								<svelte:component this={icon} size={12} />{label}
							</button>
						{/each}
					</div>
					<div class="flex gap-2">
						<input type="text" bind:value={entityValue}
							placeholder={entityType === 'company' ? 'e.g., Boeing' : entityType === 'person' ? 'e.g., John Smith' : 'e.g., 1247 Harbor Blvd, Tampa FL'}
							class="flex-1 px-3 py-2 rounded-lg border border-border bg-card text-foreground text-sm" />
						<button onclick={resolveEntity} disabled={!entityValue.trim() || resolving}
							class="px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 disabled:opacity-40">
							{resolving ? 'Resolving...' : 'Investigate'}
						</button>
					</div>
					{#if resolving}
						<p class="text-xs text-muted-foreground mt-2 animate-pulse">Querying SEC EDGAR, GLEIF, CourtListener, FINRA, USAspending, ProPublica...</p>
					{/if}
				</div>

				<!-- Manual Observable -->
				<div>
					<h4 class="text-sm font-medium text-foreground mb-2 flex items-center gap-2">
						<Eye class="w-4 h-4" /> Add Observable to Watchlist
					</h4>
					<button onclick={() => showAddObs = !showAddObs}
						class="flex items-center gap-1 px-2 py-1 text-xs rounded bg-primary/10 text-primary hover:bg-primary/20 transition-colors">
						{#if showAddObs}<X class="w-3 h-3" />{:else}<Plus class="w-3 h-3" />{/if}
						{showAddObs ? 'Cancel' : 'Add'}
					</button>
				</div>

				{#if showAddObs}
					<div class="mb-3 p-3 rounded-lg border border-primary/30 bg-primary/5 space-y-2">
						<div class="flex gap-2">
							<select bind:value={obsType}
								class="bg-muted rounded-md px-2 py-1.5 text-xs text-foreground border-none outline-none">
								{#each obsTypes as t}
									<option value={t}>{t.replace(/_/g, ' ')}</option>
								{/each}
							</select>
							<input type="text" bind:value={obsValue} placeholder="Enter value..."
								onkeydown={(e) => e.key === 'Enter' && addObservable()}
								class="flex-1 bg-muted rounded-md px-2 py-1.5 text-xs text-foreground outline-none" />
							<button onclick={addObservable} disabled={!obsValue.trim() || addingObs}
								class="px-3 py-1.5 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
								{addingObs ? '...' : 'Add'}
							</button>
						</div>
					</div>
				{/if}

				{#if caseData.observables?.length > 0}
					<div class="space-y-1">
						{#each caseData.observables as obs}
							<a href="/search?type={obs.observable_type}&value={obs.observable_value}"
								class="flex items-center gap-3 px-3 py-2 rounded-md hover:bg-muted/50 transition-colors">
								<Badge text={obs.observable_type?.replace(/_/g, ' ') ?? 'unknown'} variant="info" size="sm" />
								<span class="text-sm text-foreground font-mono flex-1 truncate">{obs.observable_value}</span>
								{#if obs.active === false}
									<Badge text="inactive" variant="neutral" size="sm" />
								{/if}
							</a>
						{/each}
					</div>
				{:else}
					<p class="text-xs text-muted-foreground text-center py-4">No observables yet. Add IPs, emails, plates, or any identifier to watch.</p>
				{/if}
			</div>

		{:else if activeTab === 'map'}
			<div class="space-y-3">
				{#if caseMapEntities.length > 0}
					<EntityMap entities={caseMapEntities} height="500px" />
				{:else}
					<div class="rounded-xl border border-border bg-card">
						<div class="flex items-center justify-center py-16 text-muted-foreground">
							<div class="text-center">
								<MapPin class="w-10 h-10 mx-auto mb-3 opacity-30" />
								<p class="text-sm">No geo-located data linked to this case yet</p>
								<p class="text-xs mt-1">Add signals with coordinates, link contacts with locations, or attach geotagged assets</p>
							</div>
						</div>
					</div>
				{/if}
				<!-- Linked locations list -->
				{#if caseNodes.filter(n => n.latitude != null).length > 0}
					<div class="rounded-lg border border-border bg-card p-4">
						<h4 class="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-2">Linked Locations</h4>
						<div class="space-y-2">
							{#each caseNodes.filter(n => n.latitude != null) as node}
								<div class="flex items-center gap-3 px-2 py-1.5 rounded hover:bg-muted">
									<MapPin size={14} class="text-emerald-400 shrink-0" />
									<div class="flex-1 min-w-0">
										<p class="text-xs text-foreground truncate">{node.title}</p>
										<p class="text-[10px] text-muted-foreground">{node.epistemic_type} · {node.latitude?.toFixed(4)}, {node.longitude?.toFixed(4)}</p>
									</div>
								</div>
							{/each}
						</div>
					</div>
				{/if}
			</div>
		{/if}
	{/if}
</div>

<!-- Edit Case Modal -->
{#if showEditCase}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="fixed inset-0 z-[200] flex items-center justify-center p-4" onclick={() => showEditCase = false}>
		<div class="absolute inset-0 bg-black/50"></div>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="relative bg-card border border-border rounded-xl p-5 w-full max-w-md shadow-xl space-y-3" onclick={(e) => e.stopPropagation()}>
			<h3 class="text-sm font-semibold text-foreground">Edit Case</h3>
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Title</label>
				<input type="text" bind:value={editTitle} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
			</div>
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Description</label>
				<textarea bind:value={editDescription} rows={3} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring resize-none"></textarea>
			</div>
			<div class="grid grid-cols-2 gap-3">
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Priority</label>
					<select bind:value={editPriority} class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1">
						<option value="low">Low</option>
						<option value="medium">Medium</option>
						<option value="high">High</option>
						<option value="critical">Critical</option>
					</select>
				</div>
				<div>
					<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Owner</label>
					<input type="text" bind:value={editOwner} placeholder="Unassigned" class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
				</div>
			</div>
			<div>
				<label class="text-[10px] text-muted-foreground uppercase tracking-wider">Tags (comma-separated)</label>
				<input type="text" bind:value={editTags} placeholder="security, theft" class="w-full px-3 py-1.5 rounded-md border border-border bg-muted text-foreground text-sm mt-1 outline-none focus:ring-2 focus:ring-ring" />
			</div>
			<div class="flex gap-2 pt-2">
				<button onclick={() => showEditCase = false} class="flex-1 px-3 py-1.5 rounded-lg border border-border text-xs text-foreground hover:bg-muted transition-colors">Cancel</button>
				<button onclick={saveEditCase} disabled={savingEdit} class="flex-1 px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 disabled:opacity-40 transition-colors">
					{savingEdit ? 'Saving...' : 'Save Changes'}
				</button>
			</div>
		</div>
	</div>
{/if}

<!-- Delete Case Confirmation -->
{#if showDeleteConfirm}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="fixed inset-0 z-[200] flex items-center justify-center p-4" onclick={() => showDeleteConfirm = false}>
		<div class="absolute inset-0 bg-black/50"></div>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="relative bg-card border border-border rounded-xl p-5 w-full max-w-sm shadow-xl" onclick={(e) => e.stopPropagation()}>
			<h3 class="text-sm font-semibold text-foreground mb-2">Delete Case</h3>
			<p class="text-xs text-muted-foreground mb-4">Are you sure you want to delete <strong>{caseData?.title}</strong>? This action cannot be undone. All linked evidence, observables, and timeline events will be removed.</p>
			<div class="flex gap-2">
				<button onclick={() => showDeleteConfirm = false} class="flex-1 px-3 py-1.5 rounded-lg border border-border text-xs text-foreground hover:bg-muted transition-colors">Cancel</button>
				<button onclick={deleteCase} disabled={deletingCase} class="flex-1 px-3 py-1.5 rounded-lg bg-destructive text-destructive-foreground text-xs font-medium hover:bg-destructive/90 disabled:opacity-40 transition-colors">
					{deletingCase ? 'Deleting...' : 'Delete Case'}
				</button>
			</div>
		</div>
	</div>
{/if}
