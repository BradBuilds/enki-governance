<script lang="ts">
	/**
	 * Connector management page — create, view, and trigger data source connectors.
	 * Connectors pull data from external sources (IMAP, RSS, HTTP API, Database, MQTT).
	 */
	import { onMount } from 'svelte';
	import { api, getHeaders } from '$lib/api/mock';
	import Cable from 'lucide-svelte/icons/cable';
	import Plus from 'lucide-svelte/icons/plus';
	import Play from 'lucide-svelte/icons/play';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import AlertCircle from 'lucide-svelte/icons/alert-circle';
	import CheckCircle from 'lucide-svelte/icons/check-circle-2';
	import Clock from 'lucide-svelte/icons/clock';
	import X from 'lucide-svelte/icons/x';
	import Zap from 'lucide-svelte/icons/zap';
	import Loader from 'lucide-svelte/icons/loader';
	import { chat } from '$lib/stores/chat.svelte';
	import AskEnkiButton from '$lib/components/shared/AskEnkiButton.svelte';

	let connectors = $state<any[]>([]);
	let loading = $state(true);
	let showCreate = $state(false);
	let activeTab = $state<'active' | 'add'>('active');

	// Recipe system
	let recipes = $state<any[]>([]);
	let recipeFilter = $state('all');
	let enablingRecipe = $state<string | null>(null);
	let recipeParams = $state<Record<string, string>>({});
	let selectedRecipe = $state<any>(null);
	let toast = $state<string | null>(null);

	function showToast(msg: string) {
		toast = msg;
		setTimeout(() => toast = null, 3000);
	}
	let triggering = $state<string | null>(null);

	// Create form
	let newType = $state('rss_feed');
	let newDeviceId = $state('');
	let newConfig = $state('{}');
	let newPollInterval = $state('5 minutes');
	let createError = $state('');
	let creating = $state(false);

	const connectorTypes = [
		{ value: 'rss_feed', label: 'RSS Feed', example: '{"url": "https://example.com/feed.xml"}' },
		{ value: 'http_api', label: 'HTTP API', example: '{"url": "https://api.example.com/data", "headers": {"Authorization": "Bearer TOKEN"}}' },
		{ value: 'imap', label: 'IMAP Email', example: '{"host": "imap.gmail.com", "port": 993, "username": "user@gmail.com", "password": "app-password", "use_ssl": true}' },
		{ value: 'database', label: 'Database', example: '{"dsn": "postgresql://user:pass@host/db", "query": "SELECT * FROM events WHERE id > {watermark.last_id}"}' },
		{ value: 'mqtt', label: 'MQTT', example: '{"broker": "mqtt.example.com", "port": 1883, "topics": ["sensors/#"]}' },
	];

	onMount(async () => {
		await loadConnectors();
		await loadRecipes();
	});

	$effect(() => {
		if (connectors) {
			chat.setContext({ currentPage: '/connectors', pageData: { connectorCount: connectors?.length || 0 } });
		}
	});

	async function loadRecipes() {
		try {
			const data: any = await api.listRecipes();
			recipes = data.recipes || [];
		} catch { recipes = []; }
	}

	async function enableRecipe(recipe: any) {
		enablingRecipe = recipe.recipe_id;
		try {
			await api.enableRecipe(recipe.recipe_id, recipeParams);
			showToast(`${recipe.name} enabled!`);
			selectedRecipe = null;
			recipeParams = {};
			activeTab = 'active';
			await loadConnectors();
		} catch { showToast('Failed to enable'); }
		enablingRecipe = null;
	}

	const filteredRecipes = $derived(
		recipeFilter === 'all' ? recipes : recipes.filter((r: any) => r.category === recipeFilter)
	);
	const categories = $derived(['all', ...new Set(recipes.map((r: any) => r.category))]);

	async function loadConnectors() {
		loading = true;
		try {
			const data = await fetch('/api/connectors', {
				headers: getHeaders(),
			}).then(r => r.json());
			connectors = data?.connectors || [];
		} catch { connectors = []; }
		loading = false;
	}

	async function triggerFetch(connectorId: string) {
		triggering = connectorId;
		try {
			await fetch(`/api/connectors/${connectorId}/fetch`, {
				method: 'POST',
				headers: getHeaders(),
			});
			await loadConnectors();
		} catch { /* ignore */ }
		triggering = null;
	}

	async function createConnector() {
		if (!newDeviceId.trim()) { createError = 'Device ID required'; return; }
		creating = true;
		createError = '';

		try {
			let config;
			try { config = JSON.parse(newConfig); } catch { createError = 'Invalid JSON config'; creating = false; return; }

			const res = await fetch('/api/connectors', {
				method: 'POST',
				headers: getHeaders(),
				body: JSON.stringify({
					device_id: newDeviceId.trim(),
					connector_type: 'pull',
					source_type: newType,
					source_config: config,
					poll_interval: newPollInterval,
				}),
			});

			if (res.ok) {
				showCreate = false;
				newDeviceId = '';
				newConfig = '{}';
				await loadConnectors();
			} else {
				const err = await res.json();
				createError = err.detail || err.error || 'Creation failed';
			}
		} catch (e: any) {
			createError = e?.message || 'Connection failed';
		}
		creating = false;
	}

	function selectType(type: string) {
		newType = type;
		const t = connectorTypes.find(c => c.value === type);
		if (t) newConfig = t.example;
	}

	function timeAgo(ts: string): string {
		if (!ts) return 'never';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}
</script>

{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg">
		{toast}
	</div>
{/if}

<div class="space-y-6">
	<div class="flex items-center justify-between">
		<div>
			<div class="flex items-center gap-3">
				<h1 class="text-xl font-semibold text-foreground">Connectors</h1>
				<AskEnkiButton prompt="What data sources are connected?" variant="compact" />
			</div>
			<p class="text-sm text-muted-foreground">Data sources — toggle on pre-built recipes or create custom connectors</p>
		</div>
		<div class="flex items-center gap-2">
			<a
				href="/connect"
				class="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary/10 text-primary text-sm font-medium hover:bg-primary/20 transition-colors border border-primary/20"
			>
				<Zap class="w-4 h-4" />
				Connect with AI
			</a>
			<button
				onclick={() => showCreate = !showCreate}
				class="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
			>
				{#if showCreate}<X class="w-4 h-4" />{:else}<Plus class="w-4 h-4" />{/if}
				{showCreate ? 'Cancel' : 'Custom'}
			</button>
		</div>
	</div>

	<!-- Tab bar -->
	<div class="flex items-center gap-1 border-b border-border">
		<button onclick={() => activeTab = 'active'}
			class="px-4 py-2 text-sm transition-colors {activeTab === 'active' ? 'text-primary border-b-2 border-primary font-medium' : 'text-muted-foreground hover:text-foreground'}">
			Active ({connectors.length})
		</button>
		<button onclick={() => activeTab = 'add'}
			class="px-4 py-2 text-sm transition-colors {activeTab === 'add' ? 'text-primary border-b-2 border-primary font-medium' : 'text-muted-foreground hover:text-foreground'}">
			Add Source
		</button>
	</div>

	<!-- ADD SOURCE TAB: Recipe Grid -->
	{#if activeTab === 'add'}
		<!-- Category filter -->
		<div class="flex items-center gap-2 flex-wrap">
			{#each categories as cat}
				<button onclick={() => recipeFilter = cat}
					class="px-3 py-1 text-xs rounded-full transition-colors
						{recipeFilter === cat ? 'bg-primary/10 text-primary font-medium' : 'bg-muted text-muted-foreground hover:text-foreground'}">
					{cat === 'all' ? 'All' : cat.charAt(0).toUpperCase() + cat.slice(1)}
				</button>
			{/each}
		</div>

		<!-- Recipe enable modal -->
		{#if selectedRecipe}
			<div class="rounded-lg border border-primary/50 bg-card p-4 space-y-3">
				<div class="flex items-center justify-between">
					<h3 class="text-sm font-semibold text-foreground">{selectedRecipe.name}</h3>
					<button onclick={() => { selectedRecipe = null; recipeParams = {}; }} class="text-muted-foreground hover:text-foreground">
						<X class="w-4 h-4" />
					</button>
				</div>
				<p class="text-xs text-muted-foreground">{selectedRecipe.description}</p>

				{#if selectedRecipe.requires_key && selectedRecipe.key_url}
					<p class="text-xs text-primary">
						Free API key required: <a href={selectedRecipe.key_url} target="_blank" class="underline">{selectedRecipe.key_url}</a>
					</p>
				{/if}

				{#each selectedRecipe.user_params || [] as param}
					<div>
						<label class="text-xs text-muted-foreground uppercase tracking-wide">{param.label}</label>
						<input type="text"
							value={recipeParams[param.name] || param.default || ''}
							oninput={(e) => recipeParams[param.name] = (e.target as HTMLInputElement).value}
							placeholder={param.placeholder || ''}
							class="w-full mt-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring" />
					</div>
				{/each}

				<button onclick={() => enableRecipe(selectedRecipe)}
					disabled={enablingRecipe === selectedRecipe.recipe_id}
					class="w-full px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors">
					{#if enablingRecipe === selectedRecipe.recipe_id}
						<Loader class="w-4 h-4 animate-spin inline mr-1" /> Enabling...
					{:else}
						<Zap class="w-4 h-4 inline mr-1" /> Enable {selectedRecipe.name}
					{/if}
				</button>
			</div>
		{/if}

		<!-- Recipe grid -->
		<div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
			{#each filteredRecipes as recipe}
				<button onclick={() => {
					selectedRecipe = recipe;
					// Pre-populate params with defaults so they're sent even if user doesn't edit
					const defaults: Record<string, string> = {};
					for (const p of recipe.user_params || []) {
						if (p.default !== undefined) defaults[p.name] = String(p.default);
						else if (p.placeholder) defaults[p.name] = p.placeholder;
					}
					recipeParams = defaults;
				}}
					class="text-left rounded-lg border border-border bg-card p-4 hover:border-primary/50 transition-colors">
					<div class="flex items-center gap-2 mb-2">
						<div class="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center text-primary text-xs font-bold">
							{recipe.name[0]}
						</div>
						<div>
							<p class="text-sm font-medium text-foreground">{recipe.name}</p>
							<p class="text-[10px] text-muted-foreground">{recipe.category}</p>
						</div>
						{#if recipe.requires_key}
							<span class="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-yellow-500/10 text-yellow-400 border border-yellow-500/30">key</span>
						{:else}
							<span class="ml-auto text-[10px] px-1.5 py-0.5 rounded bg-enki-green/10 text-enki-green border border-enki-green/30">free</span>
						{/if}
					</div>
					<p class="text-xs text-muted-foreground line-clamp-2">{recipe.description}</p>
				</button>
			{:else}
				<div class="col-span-full text-center py-8 text-muted-foreground">
					<Cable class="w-8 h-8 mx-auto mb-2 opacity-50" />
					<p class="text-sm">No recipes available</p>
				</div>
			{/each}
		</div>
	{/if}

	{#if activeTab === 'active'}

	<!-- Create form -->
	{#if showCreate}
		<div class="rounded-xl border border-border bg-card p-5 space-y-4">
			<h3 class="text-sm font-medium text-foreground">Create Connector</h3>

			<div class="grid grid-cols-2 gap-4">
				<div>
					<label class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1">Source Type</label>
					<select
						bind:value={newType}
						onchange={(e) => selectType((e.target as HTMLSelectElement).value)}
						class="w-full text-sm bg-muted border border-border rounded-lg px-3 py-2 text-foreground"
					>
						{#each connectorTypes as ct}
							<option value={ct.value}>{ct.label}</option>
						{/each}
					</select>
				</div>
				<div>
					<label class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1">Device ID</label>
					<input bind:value={newDeviceId} placeholder="rss-tech-news"
						class="w-full text-sm bg-muted border border-border rounded-lg px-3 py-2 text-foreground placeholder:text-muted-foreground" />
				</div>
			</div>

			<div>
				<label class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1">Configuration (JSON)</label>
				<textarea bind:value={newConfig} rows="4"
					class="w-full text-xs font-mono bg-muted border border-border rounded-lg px-3 py-2 text-foreground resize-none"></textarea>
			</div>

			<div>
				<label class="text-[10px] uppercase tracking-wide text-muted-foreground block mb-1">Poll Interval</label>
				<select bind:value={newPollInterval}
					class="w-full text-sm bg-muted border border-border rounded-lg px-3 py-2 text-foreground">
					<option value="1 minute">Every 1 minute</option>
					<option value="5 minutes">Every 5 minutes</option>
					<option value="15 minutes">Every 15 minutes</option>
					<option value="1 hour">Every 1 hour</option>
					<option value="6 hours">Every 6 hours</option>
					<option value="1 day">Every 1 day</option>
				</select>
			</div>

			{#if createError}
				<p class="text-xs text-enki-red">{createError}</p>
			{/if}

			<button onclick={createConnector} disabled={creating}
				class="px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 disabled:opacity-50">
				{creating ? 'Creating...' : 'Create Connector'}
			</button>
		</div>
	{/if}

	<!-- Connector list -->
	{#if loading}
		<div class="space-y-3">
			{#each [1,2,3] as _}
				<div class="rounded-lg border border-border bg-card p-4 animate-pulse">
					<div class="h-4 w-32 bg-muted rounded mb-2"></div>
					<div class="h-3 w-48 bg-muted rounded"></div>
				</div>
			{/each}
		</div>
	{:else if connectors.length === 0}
		<div class="rounded-xl border border-dashed border-border bg-card/50 p-12 text-center">
			<Cable class="w-8 h-8 text-muted-foreground mx-auto mb-3" />
			<p class="text-sm text-muted-foreground">No connectors configured</p>
			<p class="text-xs text-muted-foreground/60 mt-1">Create a connector to start pulling data from external sources</p>
		</div>
	{:else}
		<div class="space-y-3">
			{#each connectors as conn}
				<div class="rounded-xl border border-border bg-card p-4 hover:border-primary/20 transition-colors">
					<div class="flex items-start justify-between">
						<div class="flex items-center gap-3">
							<div class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center">
								<Cable class="w-5 h-5 text-primary" />
							</div>
							<div>
								<div class="flex items-center gap-2">
									<span class="text-sm font-medium text-foreground">{conn.device_id}</span>
									<span class="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{conn.source_type}</span>
									{#if conn.status === 'active'}
										<span class="text-[10px] px-1.5 py-0.5 rounded bg-enki-green/10 text-enki-green">active</span>
									{:else}
										<span class="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{conn.status}</span>
									{/if}
								</div>
								<div class="flex items-center gap-3 mt-1 text-[10px] text-muted-foreground">
									<span class="flex items-center gap-1">
										<CheckCircle class="w-3 h-3" />
										{conn.items_ingested || 0} ingested
									</span>
									{#if conn.last_fetch_at}
										<span class="flex items-center gap-1">
											<Clock class="w-3 h-3" />
											{timeAgo(conn.last_fetch_at)}
										</span>
									{/if}
									{#if conn.last_error}
										<span class="flex items-center gap-1 text-enki-red">
											<AlertCircle class="w-3 h-3" />
											{conn.error_count} errors
										</span>
									{/if}
								</div>
							</div>
						</div>

						<button
							onclick={() => triggerFetch(conn.connector_id)}
							disabled={triggering === conn.connector_id}
							class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-xs text-muted-foreground hover:text-foreground hover:border-primary/30 transition-colors disabled:opacity-50"
						>
							{#if triggering === conn.connector_id}
								<RefreshCw class="w-3 h-3 animate-spin" /> Fetching...
							{:else}
								<Play class="w-3 h-3" /> Fetch Now
							{/if}
						</button>
					</div>

					{#if conn.last_error}
						<div class="mt-3 px-3 py-2 rounded-lg bg-enki-red/5 border border-enki-red/20 text-[10px] text-enki-red font-mono truncate">
							{conn.last_error}
						</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
	{/if}
</div>
