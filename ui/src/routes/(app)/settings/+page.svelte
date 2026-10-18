<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import { toggleMode } from 'mode-watcher';
	import { api } from '$lib/api/mock';
	import { intelligencePrefs } from '$lib/stores/intelligencePrefs.svelte';
	import { feedPrefs } from '$lib/stores/feedPrefs.svelte';
	import Newspaper from 'lucide-svelte/icons/newspaper';
	import Settings from 'lucide-svelte/icons/settings';
	import Network from 'lucide-svelte/icons/network';
	import Sun from 'lucide-svelte/icons/sun';
	import Moon from 'lucide-svelte/icons/moon';
	import Bot from 'lucide-svelte/icons/bot';
	import Cable from 'lucide-svelte/icons/cable';
	import Zap from 'lucide-svelte/icons/zap';
	import Layers from 'lucide-svelte/icons/layers';
	import Code from 'lucide-svelte/icons/code';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import List from 'lucide-svelte/icons/list';
	import Bell from 'lucide-svelte/icons/bell';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import Smartphone from 'lucide-svelte/icons/smartphone';
	import Contact from 'lucide-svelte/icons/contact';
	import PackageIcon from 'lucide-svelte/icons/package';
	import Search from 'lucide-svelte/icons/search';
	import Shield from 'lucide-svelte/icons/shield';
	import LayoutGrid from 'lucide-svelte/icons/layout-grid';
	import ImageIcon from 'lucide-svelte/icons/image';
	import FolderOpenIcon from 'lucide-svelte/icons/folder-open';
	import Radio from 'lucide-svelte/icons/radio';
	import Users from 'lucide-svelte/icons/users';
	import ScrollText from 'lucide-svelte/icons/scroll-text';
	import Palette from 'lucide-svelte/icons/palette';
	import Satellite from 'lucide-svelte/icons/satellite';

	let profile = $state<any>(null);
	let providers = $state<any[]>([]);
	let aiProvider = $state('');
	let aiModel = $state('');
	let aiKey = $state('');
	let aiSaving = $state(false);
	let aiStatus = $state<{ok: boolean; message: string} | null>(null);

	// Tab system
	type Tab = 'profile' | 'feed' | 'hub' | 'sources' | 'ai' | 'intelligence';
	let activeTab = $state<Tab>('profile');

	let tabBarEl = $state<HTMLDivElement | null>(null);

	$effect(() => {
		const t = $page.url.searchParams.get('tab');
		if (t === 'ai') activeTab = 'ai';
		if (t === 'intelligence') activeTab = 'intelligence';
		if (t === 'feed') activeTab = 'feed';
		if (t === 'hub') activeTab = 'hub';
		if (t === 'sources') activeTab = 'sources';
		// If we arrived with a ?tab= URL, jump past the navigation grid so the
		// activated tab is the focal point.
		if (t && tabBarEl) tabBarEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
	});

	const tabs: { id: Tab; label: string; icon: any }[] = [
		{ id: 'profile', label: 'Profile', icon: Settings },
		{ id: 'hub', label: 'Hub', icon: LayoutGrid },
		{ id: 'feed', label: 'Feed', icon: Newspaper },
		{ id: 'sources', label: 'Sources', icon: Cable },
		{ id: 'intelligence', label: 'Intelligence', icon: Network },
		{ id: 'ai', label: 'AI', icon: Bot },
	];

	// ─── Sources state ─────────────────────────────────────────────
	let sources = $state<any[]>([]);
	let library = $state<any[]>([]);
	let sourcesLoading = $state(false);
	let addUrl = $state('');
	let addLabel = $state('');
	let addCategory = $state('general');
	let addPreview = $state<any>(null);
	let addError = $state<string | null>(null);
	let adding = $state(false);
	let sourceFilter = $state('');

	async function loadSources() {
		sourcesLoading = true;
		try {
			const [s, l] = await Promise.all([
				fetch('/api/sources').then(r => r.json()),
				fetch('/api/sources/library').then(r => r.json()),
			]);
			sources = s.sources || [];
			library = l.categories || [];
		} catch {}
		sourcesLoading = false;
	}

	async function previewUrl() {
		if (!addUrl.trim()) return;
		addError = null;
		addPreview = null;
		try {
			const res = await fetch(`/api/sources/preview?url=${encodeURIComponent(addUrl.trim())}`);
			const data = await res.json();
			if (!data.ok) {
				addError = data.error || 'Preview failed';
			} else {
				addPreview = data;
				if (!addLabel) addLabel = data.title || '';
			}
		} catch (e: any) {
			addError = e.message || 'Preview failed';
		}
	}

	async function addRssSource() {
		if (!addUrl.trim()) return;
		adding = true;
		addError = null;
		try {
			const res = await fetch('/api/sources/add', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					url: addUrl.trim(),
					label: addLabel.trim() || null,
					category: addCategory,
				}),
			});
			if (!res.ok) {
				const err = await res.json();
				addError = err.detail || 'Add failed';
			} else {
				addUrl = '';
				addLabel = '';
				addPreview = null;
				await loadSources();
			}
		} catch (e: any) {
			addError = e.message || 'Add failed';
		}
		adding = false;
	}

	async function toggleSource(connectorId: string, currentStatus: string) {
		const newStatus = currentStatus === 'active' ? 'inactive' : 'active';
		await fetch(`/api/sources/${connectorId}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ status: newStatus }),
		});
		await loadSources();
	}

	async function deleteSource(connectorId: string) {
		if (!confirm('Remove this source? Articles already ingested will remain.')) return;
		await fetch(`/api/sources/${connectorId}`, { method: 'DELETE' });
		await loadSources();
	}

	async function addFromLibrary(libraryId: string) {
		try {
			const res = await fetch(`/api/sources/library/${libraryId}`, { method: 'POST' });
			if (res.ok) await loadSources();
		} catch {}
	}

	$effect(() => {
		if (activeTab === 'sources' && sources.length === 0 && !sourcesLoading) {
			loadSources();
		}
	});

	let filteredSources = $derived(
		sourceFilter
			? sources.filter(s =>
				s.label?.toLowerCase().includes(sourceFilter.toLowerCase()) ||
				s.url?.toLowerCase().includes(sourceFilter.toLowerCase()))
			: sources
	);

	let activeSourceCount = $derived(sources.filter(s => s.status === 'active').length);
	let alreadyAddedUrls = $derived(new Set(sources.map(s => s.url).filter(Boolean)));

	// ─── Hub state ────────────────────────────────────────────────
	type HubBucket = { name: string; categories: string[]; top_n: number; order: number };
	let hubBuckets = $state<HubBucket[]>([]);
	let hubLoading = $state(false);
	let hubSaving = $state(false);
	let hubSaveStatus = $state<{ ok: boolean; message: string } | null>(null);

	let catalogItems = $state<any[]>([]);
	let catalogLoading = $state(false);
	let catalogCategoryFilter = $state<string>('');
	let catalogSearch = $state('');
	let catalogTogglingId = $state<string>('');
	let bulkBusy = $state<string>('');

	// ─── Resources / LLM budget ───────────────────────────────────
	let newsLlmCap = $state(30);
	let newsLlmBounds = $state<[number, number]>([0, 600]);
	let resourcesLoading = $state(false);
	let resourcesSaving = $state(false);
	let resourcesSaveStatus = $state<{ ok: boolean; message: string } | null>(null);

	const catalogCategories = $derived(
		Array.from(new Set(catalogItems.map(c => c.category))).sort()
	);
	const catalogFiltered = $derived(catalogItems.filter(c => {
		if (catalogCategoryFilter && c.category !== catalogCategoryFilter) return false;
		if (catalogSearch) {
			const q = catalogSearch.toLowerCase();
			if (!c.name.toLowerCase().includes(q) && !(c.description || '').toLowerCase().includes(q)) {
				return false;
			}
		}
		return true;
	}));
	const enabledByCategory = $derived(catalogItems.reduce((acc, c) => {
		if (!acc[c.category]) acc[c.category] = { total: 0, enabled: 0 };
		acc[c.category].total++;
		if (c.enabled) acc[c.category].enabled++;
		return acc;
	}, {} as Record<string, { total: number; enabled: number }>));

	async function loadHubLayout() {
		hubLoading = true;
		try {
			const r = await fetch('/api/hub/layout');
			if (r.ok) {
				const j = await r.json();
				hubBuckets = (j.buckets || []).map((b: any, i: number) => ({
					name: b.name, categories: b.categories || [],
					top_n: b.top_n ?? 2, order: b.order ?? i,
				}));
			}
		} finally { hubLoading = false; }
	}

	async function loadCatalog() {
		catalogLoading = true;
		try {
			const r = await fetch('/api/sources/catalog');
			if (r.ok) {
				const j = await r.json();
				catalogItems = j.items || [];
			}
		} finally { catalogLoading = false; }
	}

	async function saveHubLayout() {
		hubSaving = true;
		hubSaveStatus = null;
		try {
			const buckets = hubBuckets.map((b, i) => ({ ...b, order: i }));
			const r = await fetch('/api/hub/layout', {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ buckets }),
			});
			if (r.ok) hubSaveStatus = { ok: true, message: 'Layout saved.' };
			else hubSaveStatus = { ok: false, message: `Save failed (${r.status})` };
		} catch (e: any) {
			hubSaveStatus = { ok: false, message: e?.message || 'Save failed' };
		} finally { hubSaving = false; }
	}

	function addBucket() {
		hubBuckets = [...hubBuckets, {
			name: 'New section',
			categories: [],
			top_n: 2,
			order: hubBuckets.length,
		}];
	}

	function removeBucket(idx: number) {
		hubBuckets = hubBuckets.filter((_, i) => i !== idx);
	}

	function moveBucket(idx: number, dir: -1 | 1) {
		const tgt = idx + dir;
		if (tgt < 0 || tgt >= hubBuckets.length) return;
		const next = [...hubBuckets];
		[next[idx], next[tgt]] = [next[tgt], next[idx]];
		hubBuckets = next;
	}

	function toggleCategoryOnBucket(idx: number, cat: string) {
		const next = [...hubBuckets];
		const b = { ...next[idx] };
		b.categories = b.categories.includes(cat)
			? b.categories.filter(c => c !== cat)
			: [...b.categories, cat];
		next[idx] = b;
		hubBuckets = next;
	}

	async function toggleCatalogSource(catalog_id: string, enabled: boolean) {
		catalogTogglingId = catalog_id;
		try {
			const r = await fetch('/api/sources/catalog/toggle', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ catalog_id, enabled }),
			});
			if (r.ok) {
				catalogItems = catalogItems.map(c =>
					c.catalog_id === catalog_id ? { ...c, enabled } : c);
			}
		} finally { catalogTogglingId = ''; }
	}

	async function loadResources() {
		resourcesLoading = true;
		try {
			const r = await fetch('/api/tenant/settings');
			if (r.ok) {
				const j = await r.json();
				newsLlmCap = Number(j.settings?.news_llm_max_per_min ?? 30);
				const b = j.bounds?.news_llm_max_per_min;
				if (Array.isArray(b) && b.length === 2) newsLlmBounds = [Number(b[0]), Number(b[1])];
			}
		} finally { resourcesLoading = false; }
	}

	async function saveResources() {
		resourcesSaving = true;
		resourcesSaveStatus = null;
		try {
			const r = await fetch('/api/tenant/settings', {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ settings: { news_llm_max_per_min: newsLlmCap } }),
			});
			if (r.ok) {
				const j = await r.json();
				newsLlmCap = Number(j.settings?.news_llm_max_per_min ?? newsLlmCap);
				resourcesSaveStatus = { ok: true, message: 'Saved. Worker picks it up within a minute.' };
			} else {
				resourcesSaveStatus = { ok: false, message: `Save failed (${r.status})` };
			}
		} catch (e: any) {
			resourcesSaveStatus = { ok: false, message: e?.message || 'Save failed' };
		} finally { resourcesSaving = false; }
	}

	async function bulkEnableCategory(category: string, top_n: number) {
		bulkBusy = category;
		try {
			const r = await fetch('/api/sources/catalog/bulk-toggle', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ category, top_n, enabled: true }),
			});
			if (r.ok) await loadCatalog();
		} finally { bulkBusy = ''; }
	}

	$effect(() => {
		if (activeTab === 'hub') {
			if (hubBuckets.length === 0 && !hubLoading) loadHubLayout();
			if (catalogItems.length === 0 && !catalogLoading) loadCatalog();
			if (!resourcesLoading) loadResources();
		}
	});

	// ─── TTS config state ─────────────────────────────────────────
	let ttsStatus = $state<any>(null);
	let googleKeyInput = $state('');
	let googleSaving = $state(false);
	let googleStatus = $state<{ ok: boolean; message: string } | null>(null);
	let openaiKeyInput = $state('');
	let openaiSaving = $state(false);
	let openaiStatus = $state<{ ok: boolean; message: string } | null>(null);

	async function loadTtsStatus() {
		try {
			const res = await fetch('/api/tts/status');
			if (res.ok) ttsStatus = await res.json();
		} catch {}
	}

	async function saveGoogleKey() {
		googleSaving = true;
		googleStatus = null;
		try {
			const res = await fetch('/api/tts/configure/google', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ service_account_json: googleKeyInput }),
			});
			const data = await res.json();
			if (res.ok) {
				googleStatus = { ok: true, message: data.message || 'Activated' };
				googleKeyInput = '';
				await loadTtsStatus();
			} else {
				googleStatus = { ok: false, message: data.detail || 'Failed' };
			}
		} catch (e: any) {
			googleStatus = { ok: false, message: e.message || 'Request failed' };
		}
		googleSaving = false;
	}

	async function removeGoogleConfig() {
		if (!confirm('Remove Google Cloud TTS credentials?')) return;
		try {
			await fetch('/api/tts/configure/google', { method: 'DELETE' });
			await loadTtsStatus();
		} catch {}
	}

	async function saveOpenAIKey() {
		openaiSaving = true;
		openaiStatus = null;
		try {
			const res = await fetch('/api/tts/configure/openai', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ api_key: openaiKeyInput }),
			});
			const data = await res.json();
			if (res.ok) {
				openaiStatus = { ok: true, message: data.message || 'Activated' };
				openaiKeyInput = '';
				await loadTtsStatus();
			} else {
				openaiStatus = { ok: false, message: data.detail || 'Failed' };
			}
		} catch (e: any) {
			openaiStatus = { ok: false, message: e.message || 'Request failed' };
		}
		openaiSaving = false;
	}

	$effect(() => {
		if (activeTab === 'ai' && !ttsStatus) loadTtsStatus();
	});

	// Feed preferences helpers
	let muteInput = $state('');
	let boostInput = $state('');
	const categoryNames = ['World News', 'Cybersecurity', 'Tech', 'AI & ML', 'Finance', 'Military & Defense', 'Science & Space', 'Environment', 'Health', 'Automotive', 'General'];

	import Database from 'lucide-svelte/icons/database';

	const hubSections = [
		{ title: 'Intelligence', items: [
			{ href: '/reports', icon: FolderOpenIcon, label: 'Reports', desc: 'AI intelligence briefs and scheduled reporting' },
			{ href: '/timeline', icon: FolderOpenIcon, label: 'Timeline', desc: 'Unified activity feed across all system events' },
		]},
		{ title: 'Investigation', items: [
			{ href: '/projects', icon: FolderOpenIcon, label: 'Projects', desc: 'Research spaces with knowledge graphs and cases' },
			{ href: '/cases', icon: Briefcase, label: 'Cases', desc: 'Active investigations and evidence' },
			{ href: '/contacts', icon: Contact, label: 'Contacts', desc: 'People, companies, countries' },
			{ href: '/assets', icon: PackageIcon, label: 'Assets', desc: 'Vehicles, property, crypto, equipment' },
			{ href: '/search', icon: Search, label: 'Search', desc: 'Search observables across all signals' },
		]},
		{ title: 'Data', items: [
			{ href: '/signals', icon: List, label: 'Signals', desc: 'Raw signal log and filters' },
			{ href: '/governance', icon: Bell, label: 'Alerts', desc: 'Governance conflicts + collapse alerts' },
			{ href: '/feed', icon: Radio, label: 'Feed', desc: 'Device signal feed grouped by source' },
			{ href: '/gallery', icon: ImageIcon, label: 'Gallery', desc: 'Media gallery — images, docs, articles' },
			{ href: '/files', icon: FolderOpenIcon, label: 'Files', desc: 'File browser for all ingested data' },
			{ href: '/exports', icon: FolderOpenIcon, label: 'Exports', desc: 'Download signals, cases, contacts as CSV/JSON' },
			{ href: '/import', icon: FolderOpenIcon, label: 'Import', desc: 'Bulk import contacts, assets, observables from CSV' },
		]},
		{ title: 'Configuration', items: [
			{ href: '/connectors', icon: Cable, label: 'Connectors', desc: 'Data source connectors and recipes' },
			{ href: '/connect', icon: Smartphone, label: 'Devices', desc: 'Connect & pair devices and sources' },
			{ href: '/rules', icon: Shield, label: 'Rules', desc: 'Detection rules and alert conditions' },
			{ href: '/organize', icon: LayoutGrid, label: 'Organize', desc: 'Signal type taxonomy and domains' },
			{ href: '/processing', icon: Layers, label: 'Processing', desc: 'Ingestion queue, workers, imports' },
			{ href: '/settings/data-sources', icon: Database, label: 'Data Sources', desc: 'Enrichment sources and API keys' },
			{ href: '/settings/satellite', icon: Satellite, label: 'Satellite Imagery', desc: 'Connect ESA/Copernicus Sentinel-2 imagery' },
		]},
		{ title: 'Admin', items: [
			{ href: '/team', icon: Users, label: 'Team', desc: 'Members, roles, invite codes' },
			{ href: '/audit', icon: ScrollText, label: 'Audit', desc: 'Audit trail and integrity chain' },
			{ href: '/templates', icon: Palette, label: 'Templates', desc: 'Signal card display layouts' },
			{ href: '/developer', icon: Code, label: 'Developer', desc: 'API docs, SDK, webhooks' },
		]},
	];

	async function saveAndTest() {
		if (!aiProvider) return;
		aiSaving = true;
		aiStatus = null;
		try {
			await api.saveAiConfig({ provider: aiProvider, model: aiModel, api_key: aiKey || undefined });
			const testResult: any = await api.testAiConnection();
			aiStatus = { ok: testResult.success !== false, message: testResult.success !== false ? 'Connected successfully' : (testResult.error || 'Connection failed') };
		} catch (e: any) {
			aiStatus = { ok: false, message: e.message || 'Failed to save configuration' };
		}
		aiSaving = false;
	}

	onMount(async () => {
		profile = await api.profile();
		const [providerData, configData] = await Promise.all([
			api.aiProviders(),
			api.aiConfig(),
		]);
		providers = providerData.providers || [];
		if (configData.configured) {
			aiProvider = configData.provider || '';
			aiModel = configData.model || '';
		}
	});
</script>

<div class="space-y-6 max-w-4xl">
	<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
		<Settings class="w-5 h-5" /> Settings & Tools
	</h1>

	<!-- Hub grid -->
	{#each hubSections as section}
		<div>
			<h2 class="text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/50 mb-2">{section.title}</h2>
			<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
				{#each section.items as item}
					<a href={item.href} class="flex items-start gap-3 p-3 rounded-lg border border-border bg-card hover:border-primary/40 hover:bg-muted/30 transition-colors group">
						<div class="w-8 h-8 rounded-lg bg-muted flex items-center justify-center shrink-0 group-hover:bg-primary/10 transition-colors">
							<svelte:component this={item.icon} size={16} class="text-muted-foreground group-hover:text-primary transition-colors" />
						</div>
						<div class="min-w-0">
							<p class="text-xs font-medium text-foreground">{item.label}</p>
							<p class="text-[10px] text-muted-foreground line-clamp-2 mt-0.5">{item.desc}</p>
						</div>
					</a>
				{/each}
			</div>
		</div>
	{/each}

	<div class="border-t border-border pt-6"></div>

	<!-- Tab bar -->
	<div bind:this={tabBarEl} class="flex gap-1 text-sm border-b border-border pb-0">
		{#each tabs as tab}
			<button
				onclick={() => activeTab = tab.id}
				class="flex items-center gap-1.5 px-3 py-2 rounded-t-md transition-colors -mb-px
					{activeTab === tab.id ? 'bg-card border border-border border-b-card text-foreground font-medium' : 'text-muted-foreground hover:text-foreground'}"
			>
				<svelte:component this={tab.icon} class="w-3.5 h-3.5" />
				{tab.label}
			</button>
		{/each}
	</div>

	<!-- Profile Tab -->
	{#if activeTab === 'profile'}
		<div class="space-y-6">
			{#if profile}
				<div class="rounded-lg border border-border bg-card p-4 space-y-4">
					<h2 class="text-sm font-semibold text-foreground">Profile</h2>
					<div class="grid grid-cols-2 gap-4 text-sm">
						<div>
							<label class="text-xs text-muted-foreground uppercase tracking-wide">Name</label>
							<p class="text-foreground">{profile.identity.display_name}</p>
						</div>
						<div>
							<label class="text-xs text-muted-foreground uppercase tracking-wide">Role</label>
							<p class="text-foreground">{profile.identity.role}</p>
						</div>
					</div>
				</div>
			{/if}

			<!-- Theme -->
			<div class="rounded-lg border border-border bg-card p-4 space-y-3">
				<h2 class="text-sm font-semibold text-foreground">Appearance</h2>
				<div class="flex gap-2">
					<button onclick={() => toggleMode()}
						class="flex items-center gap-2 px-3 py-2 rounded-md text-sm transition-colors bg-muted hover:bg-accent/50">
						<Moon class="w-4 h-4 hidden dark:inline" /><Sun class="w-4 h-4 dark:hidden inline" />
						<span class="dark:inline hidden">Dark Mode</span><span class="inline dark:hidden">Light Mode</span>
					</button>
					<p class="text-xs text-muted-foreground self-center">Click to toggle theme</p>
				</div>
			</div>

			<!-- Stats -->
			{#if profile}
				<div class="rounded-lg border border-border bg-card p-4">
					<h2 class="text-sm font-semibold text-foreground mb-3">Your Data</h2>
					<div class="grid grid-cols-3 gap-4 text-center">
						<div>
							<p class="text-lg font-bold text-foreground">{profile.stats.signal_count.toLocaleString()}</p>
							<p class="text-xs text-muted-foreground">Signals</p>
						</div>
						<div>
							<p class="text-lg font-bold text-foreground">{profile.stats.project_count}</p>
							<p class="text-xs text-muted-foreground">Projects</p>
						</div>
						<div>
							<p class="text-lg font-bold text-foreground">{profile.stats.active_case_count}</p>
							<p class="text-xs text-muted-foreground">Active Cases</p>
						</div>
					</div>
				</div>
			{/if}

		</div>
	{/if}

	<!-- Hub Tab — bucket layout + source catalog -->
	{#if activeTab === 'hub'}
		<div class="space-y-4">
			<!-- Section A: Hub Layout -->
			<div class="rounded-lg border border-border bg-card p-4 space-y-3">
				<div class="flex items-center justify-between">
					<div>
						<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
							<LayoutGrid class="w-4 h-4" /> Hub Layout
						</h2>
						<p class="text-xs text-muted-foreground mt-0.5">
							Buckets shown on the Hub. Each bucket pulls top-N from one or more source categories.
						</p>
					</div>
					<button onclick={addBucket}
						class="text-xs px-2.5 py-1 rounded-md border border-border hover:border-primary/50 hover:text-primary">
						+ Add bucket
					</button>
				</div>

				{#if hubLoading}
					<p class="text-xs text-muted-foreground">Loading…</p>
				{:else if hubBuckets.length === 0}
					<p class="text-xs text-muted-foreground">No buckets. Click "+ Add bucket" to start.</p>
				{:else}
					<div class="space-y-2">
						{#each hubBuckets as b, idx (idx)}
							<div class="rounded-md border border-border/60 p-3 space-y-2">
								<div class="flex items-center gap-2">
									<div class="flex flex-col">
										<button onclick={() => moveBucket(idx, -1)} disabled={idx === 0}
											class="text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-30">▲</button>
										<button onclick={() => moveBucket(idx, 1)} disabled={idx === hubBuckets.length - 1}
											class="text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-30">▼</button>
									</div>
									<input bind:value={b.name}
										class="flex-1 bg-muted rounded px-2 py-1 text-sm text-foreground border-none outline-none focus:ring-1 focus:ring-ring"
										placeholder="Bucket name (e.g. Military)" />
									<label class="text-xs text-muted-foreground">Top
										<input type="number" min="1" max="5" bind:value={b.top_n}
											class="ml-1 w-12 bg-muted rounded px-1.5 py-1 text-sm text-foreground border-none outline-none" />
									</label>
									<button onclick={() => removeBucket(idx)}
										class="text-xs text-rose-400 hover:text-rose-300 px-2">×</button>
								</div>
								<div class="flex flex-wrap gap-1">
									{#each catalogCategories as cat}
										{@const active = b.categories.includes(cat)}
										<button onclick={() => toggleCategoryOnBucket(idx, cat)}
											class="text-[11px] px-2 py-0.5 rounded-full border"
											class:bg-primary={active}
											class:text-primary-foreground={active}
											class:border-primary={active}
											class:border-border={!active}
											class:text-muted-foreground={!active}
											class:hover:border-primary={!active}>
											{cat}
										</button>
									{/each}
								</div>
							</div>
						{/each}
					</div>
				{/if}

				<div class="flex items-center justify-between pt-2 border-t border-border/40">
					{#if hubSaveStatus}
						<span class="text-xs" class:text-emerald-400={hubSaveStatus.ok} class:text-rose-400={!hubSaveStatus.ok}>
							{hubSaveStatus.message}
						</span>
					{:else}
						<span></span>
					{/if}
					<button onclick={saveHubLayout} disabled={hubSaving || hubBuckets.length === 0}
						class="inline-flex items-center gap-1 px-3 py-1.5 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
						{hubSaving ? 'Saving…' : 'Save layout'}
					</button>
				</div>
			</div>

			<!-- Section B: Source Catalog -->
			<div class="rounded-lg border border-border bg-card p-4 space-y-3">
				<div>
					<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
						<Cable class="w-4 h-4" /> Source Catalog
					</h2>
					<p class="text-xs text-muted-foreground mt-0.5">
						All sources from the catalog. Toggle individually, or bulk-enable the top sources of a category.
					</p>
				</div>

				<div class="flex flex-wrap items-center gap-2">
					<input bind:value={catalogSearch} placeholder="Search sources…"
						class="flex-1 min-w-32 bg-muted rounded px-2.5 py-1 text-xs text-foreground border-none outline-none" />
					<button onclick={() => catalogCategoryFilter = ''}
						class="text-[11px] px-2 py-0.5 rounded-full border"
						class:border-primary={catalogCategoryFilter === ''}
						class:text-primary={catalogCategoryFilter === ''}
						class:border-border={catalogCategoryFilter !== ''}
						class:text-muted-foreground={catalogCategoryFilter !== ''}>
						all
					</button>
					{#each catalogCategories as cat}
						{@const stats = enabledByCategory[cat] || { total: 0, enabled: 0 }}
						<button onclick={() => catalogCategoryFilter = cat}
							class="text-[11px] px-2 py-0.5 rounded-full border"
							class:border-primary={catalogCategoryFilter === cat}
							class:text-primary={catalogCategoryFilter === cat}
							class:border-border={catalogCategoryFilter !== cat}
							class:text-muted-foreground={catalogCategoryFilter !== cat}>
							{cat} <span class="opacity-60">{stats.enabled}/{stats.total}</span>
						</button>
					{/each}
				</div>

				{#if catalogCategoryFilter}
					<div class="flex items-center gap-2 text-xs text-muted-foreground">
						<span>Bulk enable top</span>
						{#each [2, 3, 5] as n}
							<button onclick={() => bulkEnableCategory(catalogCategoryFilter, n)}
								disabled={bulkBusy === catalogCategoryFilter}
								class="px-2 py-0.5 rounded border border-border hover:border-primary hover:text-primary disabled:opacity-50">
								{n}
							</button>
						{/each}
						<span>sources in {catalogCategoryFilter}</span>
						{#if bulkBusy === catalogCategoryFilter}
							<span class="text-emerald-400">enabling…</span>
						{/if}
					</div>
				{/if}

				{#if catalogLoading}
					<p class="text-xs text-muted-foreground">Loading catalog…</p>
				{:else}
					<div class="max-h-[28rem] overflow-y-auto rounded border border-border/40 divide-y divide-border/40">
						{#each catalogFiltered as src (src.catalog_id)}
							<div class="flex items-center gap-3 p-2.5">
								<div class="flex-1 min-w-0">
									<div class="flex items-baseline gap-2">
										<span class="text-sm text-foreground truncate">{src.name}</span>
										<span class="text-[10px] uppercase tracking-wider text-muted-foreground/70 shrink-0">
											{src.category} · {src.region}
										</span>
										{#if src.requires_key}
											<span class="text-[10px] text-amber-400 shrink-0">key required</span>
										{/if}
									</div>
									{#if src.description}
										<div class="text-[11px] text-muted-foreground truncate">{src.description}</div>
									{/if}
								</div>
								<label class="inline-flex items-center cursor-pointer shrink-0">
									<input type="checkbox" checked={src.enabled}
										disabled={catalogTogglingId === src.catalog_id || src.requires_key}
										onchange={(e) => toggleCatalogSource(src.catalog_id, (e.target as HTMLInputElement).checked)}
										class="sr-only peer" />
									<div class="w-9 h-5 bg-muted peer-checked:bg-primary rounded-full relative transition-colors peer-disabled:opacity-40">
										<div class="w-4 h-4 bg-card rounded-full absolute top-0.5 left-0.5 peer-checked:translate-x-4 transition-transform"></div>
									</div>
								</label>
							</div>
						{/each}
						{#if catalogFiltered.length === 0}
							<p class="text-xs text-muted-foreground p-3">No sources match the filter.</p>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Section C: Resources / LLM budget -->
			<div class="rounded-lg border border-border bg-card p-4 space-y-3">
				<div>
					<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
						<Zap class="w-4 h-4" /> Resources — News LLM budget
					</h2>
					<p class="text-xs text-muted-foreground mt-0.5">
						Cap on how many news articles the LLM enriches per minute. The rest of GPU/Ollama
						capacity stays available for research (investigations, dossier work). 0 = pause news LLM.
					</p>
				</div>

				{#if resourcesLoading}
					<p class="text-xs text-muted-foreground">Loading…</p>
				{:else}
					<div class="space-y-2">
						<div class="flex items-baseline gap-3">
							<label class="text-xs text-muted-foreground uppercase tracking-wide">News LLM cap</label>
							<input type="range" min={newsLlmBounds[0]} max={Math.min(newsLlmBounds[1], 120)}
								bind:value={newsLlmCap}
								class="flex-1 accent-primary" />
							<input type="number" min={newsLlmBounds[0]} max={newsLlmBounds[1]} bind:value={newsLlmCap}
								class="w-20 bg-muted rounded px-2 py-1 text-sm text-foreground border-none outline-none text-right" />
							<span class="text-xs text-muted-foreground">/ min</span>
						</div>
						<div class="flex flex-wrap gap-2 text-[11px]">
							{#each [0, 4, 15, 30, 60, 120] as preset}
								<button onclick={() => newsLlmCap = preset}
									class="px-2 py-0.5 rounded-full border"
									class:bg-primary={newsLlmCap === preset}
									class:text-primary-foreground={newsLlmCap === preset}
									class:border-primary={newsLlmCap === preset}
									class:border-border={newsLlmCap !== preset}
									class:text-muted-foreground={newsLlmCap !== preset}>
									{preset === 0 ? 'paused' : `${preset}/min`}
								</button>
							{/each}
						</div>
						<p class="text-[11px] text-muted-foreground">
							At <span class="text-foreground font-medium">{newsLlmCap}/min</span>:
							{#if newsLlmCap === 0}
								news LLM enrichment is paused. Headlines still ingest; entities/topics/summaries don't.
							{:else if newsLlmCap < 8}
								tight budget — only the freshest items get LLM enrichment. Research has full GPU headroom.
							{:else if newsLlmCap < 30}
								balanced — keeps up with steady news flow, leaves significant capacity for research.
							{:else if newsLlmCap < 90}
								wide budget — handles bursts; research may briefly slow during news spikes.
							{:else}
								near-unlimited — news enrichment will compete directly with research for LLM time.
							{/if}
						</p>
					</div>

					<div class="flex items-center justify-between pt-2 border-t border-border/40">
						{#if resourcesSaveStatus}
							<span class="text-xs" class:text-emerald-400={resourcesSaveStatus.ok} class:text-rose-400={!resourcesSaveStatus.ok}>
								{resourcesSaveStatus.message}
							</span>
						{:else}
							<span class="text-[11px] text-muted-foreground/60">Range: {newsLlmBounds[0]}–{newsLlmBounds[1]}</span>
						{/if}
						<button onclick={saveResources} disabled={resourcesSaving}
							class="inline-flex items-center gap-1 px-3 py-1.5 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
							{resourcesSaving ? 'Saving…' : 'Save cap'}
						</button>
					</div>
				{/if}
			</div>
		</div>
	{/if}

	<!-- Feed Preferences Tab -->
	{#if activeTab === 'feed'}
		<div class="space-y-4">
			<div class="rounded-lg border border-border bg-card p-4 space-y-4">
				<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
					<Newspaper class="w-4 h-4" /> Feed Preferences
				</h2>
				<p class="text-xs text-muted-foreground">
					Adjust which topics are prominent in your News Feed. Higher weight = more visible.
					Set to 0 to hide a category. These preferences only affect sort order —
					you can always see everything via the filter chips.
				</p>

				<!-- Category Weights -->
				<div class="space-y-2 pt-2 border-t border-border/50">
					<h3 class="text-xs font-semibold text-muted-foreground uppercase tracking-wider">Category Interest</h3>
					{#each categoryNames as cat}
						{@const weight = feedPrefs.categoryWeights[cat] ?? 0.5}
						<div class="flex items-center gap-3">
							<span class="text-xs text-foreground w-32 truncate">{cat}</span>
							<input
								type="range" min="0" max="100" value={Math.round(weight * 100)}
								oninput={(e) => feedPrefs.setCategoryWeight(cat, parseInt((e.target as HTMLInputElement).value) / 100)}
								class="flex-1 accent-primary h-1.5"
							/>
							<span class="text-[10px] text-muted-foreground w-8 text-right font-mono">{Math.round(weight * 100)}%</span>
						</div>
					{/each}
				</div>

				<!-- Muted Entities -->
				<div class="space-y-2 pt-3 border-t border-border/50">
					<h3 class="text-xs font-semibold text-muted-foreground uppercase tracking-wider">Muted Entities</h3>
					<p class="text-[10px] text-muted-foreground">Articles mentioning these entities will be collapsed in the feed.</p>
					<div class="flex items-center gap-2">
						<input type="text" bind:value={muteInput} placeholder="Entity name to mute..."
							class="flex-1 bg-muted rounded-md px-2.5 py-1.5 text-xs text-foreground outline-none focus:ring-1 focus:ring-ring"
							onkeydown={(e) => { if (e.key === 'Enter' && muteInput.trim()) { feedPrefs.muteEntity(muteInput.trim()); muteInput = ''; }}}
						/>
						<button onclick={() => { if (muteInput.trim()) { feedPrefs.muteEntity(muteInput.trim()); muteInput = ''; }}}
							class="px-2.5 py-1.5 text-xs rounded-md bg-muted text-muted-foreground hover:text-foreground">Add</button>
					</div>
					{#if feedPrefs.mutedEntities.length > 0}
						<div class="flex flex-wrap gap-1">
							{#each feedPrefs.mutedEntities as entity}
								<button onclick={() => feedPrefs.unmuteEntity(entity)}
									class="text-[10px] px-2 py-0.5 rounded-full bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20">
									× {entity}
								</button>
							{/each}
						</div>
					{/if}
				</div>

				<!-- Boosted Entities -->
				<div class="space-y-2 pt-3 border-t border-border/50">
					<h3 class="text-xs font-semibold text-muted-foreground uppercase tracking-wider">Boosted Entities</h3>
					<p class="text-[10px] text-muted-foreground">Articles mentioning these entities will be pinned to the top of the feed.</p>
					<div class="flex items-center gap-2">
						<input type="text" bind:value={boostInput} placeholder="Entity name to boost..."
							class="flex-1 bg-muted rounded-md px-2.5 py-1.5 text-xs text-foreground outline-none focus:ring-1 focus:ring-ring"
							onkeydown={(e) => { if (e.key === 'Enter' && boostInput.trim()) { feedPrefs.boostEntity(boostInput.trim()); boostInput = ''; }}}
						/>
						<button onclick={() => { if (boostInput.trim()) { feedPrefs.boostEntity(boostInput.trim()); boostInput = ''; }}}
							class="px-2.5 py-1.5 text-xs rounded-md bg-muted text-muted-foreground hover:text-foreground">Add</button>
					</div>
					{#if feedPrefs.boostedEntities.length > 0}
						<div class="flex flex-wrap gap-1">
							{#each feedPrefs.boostedEntities as entity}
								<button onclick={() => feedPrefs.unboostEntity(entity)}
									class="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 hover:bg-emerald-500/20">
									★ {entity}
								</button>
							{/each}
						</div>
					{/if}
				</div>

				<!-- Feed Display Options -->
				<div class="space-y-3 pt-3 border-t border-border/50">
					<h3 class="text-xs font-semibold text-muted-foreground uppercase tracking-wider">Display</h3>
					<div class="flex items-center gap-4">
						<span class="text-xs text-foreground w-24">Density</span>
						<div class="flex gap-1">
							{#each [['compact', 'Compact'], ['grouped', 'Grouped'], ['expanded', 'Expanded']] as [val, label]}
								<button onclick={() => feedPrefs.setDensity(val as any)}
									class="text-[10px] px-2.5 py-1 rounded transition-colors
										{feedPrefs.density === val ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}"
								>{label}</button>
							{/each}
						</div>
					</div>
					<div class="flex items-center gap-4">
						<span class="text-xs text-foreground w-24">Time window</span>
						<div class="flex gap-1">
							{#each [[12, '12h'], [24, '24h'], [48, '48h'], [72, '3d'], [168, '7d']] as [val, label]}
								<button onclick={() => feedPrefs.setHoursWindow(val as number)}
									class="text-[10px] px-2.5 py-1 rounded transition-colors
										{feedPrefs.hoursWindow === val ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}"
								>{label}</button>
							{/each}
						</div>
					</div>
					<div class="flex items-center gap-4">
						<span class="text-xs text-foreground w-24">Auto-refresh</span>
						<label class="inline-flex items-center cursor-pointer">
							<input type="checkbox" checked={feedPrefs.autoRefresh}
								onchange={(e) => feedPrefs.setAutoRefresh((e.target as HTMLInputElement).checked)}
								class="sr-only peer" />
							<div class="w-9 h-5 bg-muted peer-checked:bg-primary rounded-full relative transition-colors">
								<div class="w-4 h-4 bg-card rounded-full absolute top-0.5 left-0.5 peer-checked:translate-x-4 transition-transform"></div>
							</div>
						</label>
						{#if feedPrefs.autoRefresh}
							<span class="text-[10px] text-muted-foreground">every {feedPrefs.refreshInterval}s</span>
						{/if}
					</div>
				</div>

				<!-- Reset -->
				<div class="pt-3 border-t border-border/50">
					<button onclick={() => feedPrefs.reset()}
						class="text-xs text-muted-foreground hover:text-destructive transition-colors">
						Reset to defaults
					</button>
				</div>
			</div>
		</div>
	{/if}

	<!-- Feed Sources Tab -->
	{#if activeTab === 'sources'}
		<div class="space-y-4">
			<!-- My sources (now at top) -->
			<div class="rounded-lg border border-border bg-card p-4 space-y-3">
				<div class="flex items-center justify-between">
					<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
						<Cable class="w-4 h-4" /> My Sources
						<span class="text-[10px] text-muted-foreground">({activeSourceCount} active of {sources.length})</span>
					</h2>
					<input type="text" bind:value={sourceFilter} placeholder="Filter sources..."
						class="bg-muted rounded-md px-2 py-1 text-xs text-foreground outline-none focus:ring-1 focus:ring-ring w-40" />
				</div>
				{#if sourcesLoading}
					<p class="text-xs text-muted-foreground">Loading...</p>
				{:else if filteredSources.length === 0}
					<p class="text-xs text-muted-foreground">No sources match your filter.</p>
				{:else}
					<div class="space-y-1 max-h-[500px] overflow-y-auto">
						{#each filteredSources as src}
							<div class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-muted/30 transition-colors">
								<button onclick={() => toggleSource(src.connector_id, src.status)}
									class="shrink-0 w-9 h-5 rounded-full transition-colors {src.status === 'active' ? 'bg-emerald-500' : 'bg-muted-foreground/30'}"
									title={src.status === 'active' ? 'Disable' : 'Enable'}>
									<div class="w-4 h-4 bg-card rounded-full shadow transition-transform {src.status === 'active' ? 'translate-x-4' : 'translate-x-0.5'}"></div>
								</button>
								<div class="flex-1 min-w-0">
									<div class="flex items-center gap-2">
										<span class="text-xs font-medium text-foreground truncate">{src.label}</span>
										{#if src.category && src.category !== 'general'}
											<span class="text-[9px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{src.category}</span>
										{/if}
									</div>
									<p class="text-[10px] text-muted-foreground truncate">{src.url}</p>
								</div>
								<div class="flex items-center gap-2 text-[10px] text-muted-foreground shrink-0">
									<span>{src.items_ingested} items</span>
									{#if src.last_error}<span class="text-red-400" title={src.last_error}>⚠</span>{/if}
								</div>
								<button onclick={() => deleteSource(src.connector_id)}
									class="text-muted-foreground hover:text-destructive p-1 shrink-0" title="Delete source">×</button>
							</div>
						{/each}
					</div>
				{/if}
			</div>

			<!-- Add new source -->
			<div class="rounded-lg border border-primary/30 bg-primary/5 p-4 space-y-3">
				<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
					<Cable class="w-4 h-4" /> Add an RSS Feed
				</h2>
				<p class="text-xs text-muted-foreground">
					Paste any RSS or Atom URL — niche blog, academic journal, podcast feed, anything.
				</p>
				<div class="flex items-center gap-2">
					<input type="text" bind:value={addUrl} placeholder="https://example.com/feed.xml"
						class="flex-1 bg-card border border-border rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					<button onclick={previewUrl} disabled={!addUrl.trim()}
						class="px-3 py-2 text-xs rounded-md bg-muted text-foreground hover:bg-accent disabled:opacity-50">Preview</button>
				</div>
				{#if addError}
					<p class="text-xs text-destructive">⚠ {addError}</p>
				{/if}
				{#if addPreview}
					<div class="rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3 space-y-2">
						<div class="flex items-center justify-between">
							<div>
								<p class="text-sm font-semibold text-foreground">{addPreview.title || 'Untitled feed'}</p>
								{#if addPreview.description}
									<p class="text-[10px] text-muted-foreground">{addPreview.description}</p>
								{/if}
							</div>
							<span class="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400">
								✓ Valid · {addPreview.entry_count} entries
							</span>
						</div>
						<div class="grid grid-cols-2 gap-2">
							<input type="text" bind:value={addLabel} placeholder="Display label"
								class="bg-card border border-border rounded-md px-2 py-1.5 text-xs outline-none focus:ring-1 focus:ring-ring" />
							<select bind:value={addCategory}
								class="bg-card border border-border rounded-md px-2 py-1.5 text-xs">
								<option value="general">General</option>
								<option value="science">Science</option>
								<option value="culture">Culture</option>
								<option value="space">Space</option>
								<option value="health">Health</option>
								<option value="business">Business</option>
								<option value="environment">Environment</option>
								<option value="security">Security</option>
								<option value="technology">Technology</option>
								<option value="world">World News</option>
								<option value="defense">Defense</option>
								<option value="other">Other</option>
							</select>
						</div>
						{#if addPreview.sample_entries?.length}
							<div class="border-t border-border/30 pt-2 space-y-0.5">
								<p class="text-[10px] text-muted-foreground font-semibold uppercase">Recent items:</p>
								{#each addPreview.sample_entries.slice(0, 3) as e}
									<p class="text-[10px] text-muted-foreground truncate">• {e.title}</p>
								{/each}
							</div>
						{/if}
						<button onclick={addRssSource} disabled={adding}
							class="w-full px-3 py-2 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
							{adding ? 'Adding...' : 'Add to my sources'}
						</button>
					</div>
				{/if}
			</div>

			<!-- Browse curated library -->
			{#if library.length > 0}
				<div class="rounded-lg border border-border bg-card p-4 space-y-3">
					<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
						📚 Browse Curated Library
					</h2>
					<p class="text-xs text-muted-foreground">
						Hand-picked feeds across niche topics. One-click add.
					</p>
					{#each library as cat}
						<div class="border-t border-border/30 pt-3">
							<h3 class="text-xs font-semibold text-foreground mb-2">{cat.category}</h3>
							<div class="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
								{#each cat.sources as item}
									{@const isAdded = alreadyAddedUrls.has(item.url)}
									<div class="flex items-center gap-2 px-2 py-1.5 rounded-md bg-muted/30 hover:bg-muted/50 transition-colors">
										<div class="flex-1 min-w-0">
											<p class="text-xs font-medium text-foreground truncate">{item.title}</p>
											<p class="text-[10px] text-muted-foreground truncate">{item.description}</p>
										</div>
										{#if isAdded}
											<span class="text-[10px] text-emerald-400">✓ added</span>
										{:else}
											<button onclick={() => addFromLibrary(item.id)}
												class="text-[10px] px-2 py-0.5 rounded bg-primary/10 text-primary hover:bg-primary/20 shrink-0">+ Add</button>
										{/if}
									</div>
								{/each}
							</div>
						</div>
					{/each}
				</div>
			{/if}

		</div>
	{/if}

	<!-- Intelligence Tab -->
	{#if activeTab === 'intelligence'}
		<div class="rounded-lg border border-border bg-card p-4 space-y-4">
			<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
				<Network class="w-4 h-4" /> Intelligence Surface
			</h2>
			<p class="text-xs text-muted-foreground">
				Controls how the dashboard renders live data. Intelligence is the
				synthesized, summarised view — alerts, cases, topics, and research
				nodes. The raw Live Feed firehose is an advanced surface that's
				hidden by default.
			</p>

			<div class="flex items-start justify-between gap-4 pt-2 border-t border-border/50">
				<div>
					<div class="text-sm text-foreground font-medium">Live Feed stream</div>
					<p class="text-xs text-muted-foreground mt-0.5">
						Show the Live Feed tab on the dashboard. When off, the firehose
						is hidden and Intelligence is the only signal surface.
					</p>
				</div>
				<label class="inline-flex items-center cursor-pointer shrink-0 pt-1">
					<input
						type="checkbox"
						checked={intelligencePrefs.streamEnabled}
						onchange={(e) => intelligencePrefs.setStreamEnabled((e.target as HTMLInputElement).checked)}
						class="sr-only peer"
					/>
					<div class="w-9 h-5 bg-muted peer-checked:bg-primary rounded-full relative transition-colors">
						<div class="w-4 h-4 bg-card rounded-full absolute top-0.5 left-0.5 peer-checked:translate-x-4 transition-transform"></div>
					</div>
				</label>
			</div>
		</div>
	{/if}

	<!-- AI Tab -->
	{#if activeTab === 'ai'}
		<div class="rounded-lg border border-border bg-card p-4 space-y-4">
			<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
				<Bot class="w-4 h-4" /> AI Configuration
			</h2>
			<p class="text-xs text-muted-foreground">Connect an AI provider to enable natural language chat, data enrichment, and contrast detection.</p>

			<div class="space-y-3">
				<div>
					<label class="text-xs text-muted-foreground uppercase tracking-wide">Provider</label>
					<select bind:value={aiProvider} class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground border-none outline-none focus:ring-2 focus:ring-ring">
						<option value="">Select a provider...</option>
						{#each providers as p}
							<option value={p.name}>{p.name} — {p.description}</option>
						{/each}
					</select>
				</div>

				{#if aiProvider}
					<div>
						<label class="text-xs text-muted-foreground uppercase tracking-wide">Model</label>
						<input type="text" bind:value={aiModel} placeholder="e.g., llama3.2:3b"
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>

					{#if aiProvider !== 'ollama'}
						<div>
							<label class="text-xs text-muted-foreground uppercase tracking-wide">API Key</label>
							<input type="password" bind:value={aiKey} placeholder="sk-..."
								class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
						</div>
					{/if}

					<div class="flex items-center gap-3">
						<button onclick={saveAndTest} disabled={aiSaving}
							class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
							{aiSaving ? 'Testing...' : 'Save & Test Connection'}
						</button>
						{#if aiStatus}
							<span class="text-xs {aiStatus.ok ? 'text-enki-green' : 'text-destructive'}">{aiStatus.message}</span>
						{/if}
					</div>
				{/if}
			</div>
		</div>

		<!-- Text-to-Speech Configuration -->
		<div class="rounded-lg border border-border bg-card p-4 space-y-4 mt-4">
			<div class="flex items-start justify-between">
				<div>
					<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
						🔊 Text-to-Speech
					</h2>
					<p class="text-xs text-muted-foreground mt-1">
						Voice for reading articles. Google Cloud TTS sounds most natural.
						Web Speech (built into browser) is free but robotic.
					</p>
				</div>
				{#if ttsStatus?.providers}
					<div class="flex items-center gap-1 text-[10px]">
						{#if ttsStatus.providers.google?.available}
							<span class="px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400">✓ Google</span>
						{/if}
						{#if ttsStatus.providers.openai?.available}
							<span class="px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400">✓ OpenAI</span>
						{/if}
						{#if !ttsStatus.providers.google?.available && !ttsStatus.providers.openai?.available}
							<span class="px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-400">Web Speech only</span>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Google TTS -->
			<div class="border-t border-border/50 pt-3 space-y-2">
				<div class="flex items-center justify-between">
					<h3 class="text-xs font-semibold text-foreground">Google Cloud TTS</h3>
					{#if ttsStatus?.providers?.google?.available}
						<button onclick={removeGoogleConfig}
							class="text-[10px] text-red-400 hover:underline">Remove credentials</button>
					{/if}
				</div>
				{#if ttsStatus?.providers?.google?.available}
					<p class="text-[11px] text-emerald-400">
						✓ Active — Studio voices recommended for best quality (en-US-Studio-O, en-US-Studio-Q)
					</p>
				{:else}
					<p class="text-[11px] text-muted-foreground">
						Paste your Google Cloud service account JSON key.
						<a href="https://console.cloud.google.com/apis/library/texttospeech.googleapis.com" target="_blank" rel="noopener" class="text-primary hover:underline">Enable API</a>
						·
						<a href="https://console.cloud.google.com/iam-admin/serviceaccounts" target="_blank" rel="noopener" class="text-primary hover:underline">Create service account</a>
					</p>
					<textarea bind:value={googleKeyInput}
						placeholder={'{"type": "service_account", "project_id": "...", ...}'}
						rows="6"
						class="w-full bg-muted rounded-md px-3 py-2 text-[10px] font-mono text-foreground outline-none focus:ring-1 focus:ring-ring"></textarea>
					<div class="flex items-center gap-2">
						<button onclick={saveGoogleKey} disabled={!googleKeyInput.trim() || googleSaving}
							class="px-3 py-1.5 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
							{googleSaving ? 'Validating...' : 'Activate Google TTS'}
						</button>
						{#if googleStatus}
							<span class="text-[11px] {googleStatus.ok ? 'text-emerald-400' : 'text-red-400'}">{googleStatus.message}</span>
						{/if}
					</div>
				{/if}
			</div>

			<!-- OpenAI TTS -->
			<div class="border-t border-border/50 pt-3 space-y-2">
				<h3 class="text-xs font-semibold text-foreground">OpenAI TTS</h3>
				{#if ttsStatus?.providers?.openai?.available}
					<p class="text-[11px] text-emerald-400">
						✓ Active — alloy, echo, fable, onyx, nova, shimmer voices available
					</p>
				{:else}
					<p class="text-[11px] text-muted-foreground">
						Paste your OpenAI API key.
						<a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener" class="text-primary hover:underline">Get key</a>
					</p>
					<input type="password" bind:value={openaiKeyInput} placeholder="sk-..."
						class="w-full bg-muted rounded-md px-3 py-2 text-xs font-mono text-foreground outline-none focus:ring-1 focus:ring-ring" />
					<div class="flex items-center gap-2">
						<button onclick={saveOpenAIKey} disabled={!openaiKeyInput.trim() || openaiSaving}
							class="px-3 py-1.5 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
							{openaiSaving ? 'Validating...' : 'Activate OpenAI TTS'}
						</button>
						{#if openaiStatus}
							<span class="text-[11px] {openaiStatus.ok ? 'text-emerald-400' : 'text-red-400'}">{openaiStatus.message}</span>
						{/if}
					</div>
				{/if}
			</div>

			<p class="text-[10px] text-muted-foreground/70 pt-2 border-t border-border/30">
				Web Speech API (browser built-in) is always available as fallback. No setup required.
			</p>
		</div>
	{/if}
</div>
