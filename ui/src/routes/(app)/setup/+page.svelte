<script lang="ts">
	import { onMount } from 'svelte';
	import { api } from '$lib/api/mock';
	import Cpu from 'lucide-svelte/icons/cpu';
	import Wifi from 'lucide-svelte/icons/wifi';
	import WifiOff from 'lucide-svelte/icons/wifi-off';
	import Signal from 'lucide-svelte/icons/signal';
	import Brain from 'lucide-svelte/icons/brain';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Check from 'lucide-svelte/icons/check';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import ChevronLeft from 'lucide-svelte/icons/chevron-left';
	import Download from 'lucide-svelte/icons/download';
	import Loader from 'lucide-svelte/icons/loader-circle';
	import Shield from 'lucide-svelte/icons/shield';
	import Cloud from 'lucide-svelte/icons/cloud';
	import Zap from 'lucide-svelte/icons/zap';

	let step = $state(1);
	let loading = $state(true);
	let saving = $state(false);
	let toast = $state<string | null>(null);

	// Hardware
	let hardware = $state<any>(null);

	// Step 1: Network Mode
	let networkMode = $state<'connected' | 'offline' | 'intermittent'>('connected');

	// Step 2: AI Model
	let aiMode = $state<'local' | 'cloud' | 'hybrid'>('local');
	let selectedModel = $state('');
	let modelOptions = $state<any[]>([]);
	let pullingModel = $state(false);
	let pullStatus = $state('');
	let localModels = $state<string[]>([]);

	// Cloud config (for hybrid/cloud mode)
	let cloudProvider = $state('');
	let cloudModel = $state('');
	let cloudApiKey = $state('');

	// Step 3: Routing Regions
	let regions = $state<any[]>([]);
	let selectedRegions = $state<string[]>([]);
	let downloadingRegion = $state<string | null>(null);

	// Step 4: Summary
	let setupComplete = $state(false);

	// Re-run detection
	let previousSetup = $state(false);

	// ─── Auto-save draft to localStorage ───────────────────────────────
	const DRAFT_KEY = 'enki-setup-draft';

	function saveDraft() {
		try {
			localStorage.setItem(DRAFT_KEY, JSON.stringify({
				step, networkMode, aiMode, selectedModel,
				cloudProvider, cloudModel, selectedRegions,
				savedAt: new Date().toISOString(),
			}));
		} catch { /* localStorage unavailable */ }
	}

	function loadDraft(): boolean {
		try {
			const raw = localStorage.getItem(DRAFT_KEY);
			if (!raw) return false;
			const draft = JSON.parse(raw);
			step = draft.step || 1;
			networkMode = draft.networkMode || 'connected';
			aiMode = draft.aiMode || 'local';
			selectedModel = draft.selectedModel || '';
			cloudProvider = draft.cloudProvider || '';
			cloudModel = draft.cloudModel || '';
			selectedRegions = draft.selectedRegions || [];
			return true;
		} catch { return false; }
	}

	function clearDraft() {
		try { localStorage.removeItem(DRAFT_KEY); } catch {}
	}

	// Auto-save on every state change (debounced via $effect)
	$effect(() => {
		// Touch all the values we want to track
		void [step, networkMode, aiMode, selectedModel, cloudProvider, cloudModel, selectedRegions.length];
		if (!loading && !setupComplete) saveDraft();
	});

	const steps = [
		{ num: 1, label: 'Network', icon: Wifi },
		{ num: 2, label: 'AI Model', icon: Brain },
		{ num: 3, label: 'Routing', icon: MapPin },
		{ num: 4, label: 'Confirm', icon: Check },
	];

	onMount(async () => {
		try {
			const status = await apiFetch('/setup/status');
			hardware = status.hardware;
			localModels = status.ollama_models || [];
			selectedModel = status.hardware?.recommended_model || 'gemma4:e2b';
			modelOptions = status.hardware?.models || [];

			if (status.setup_completed) {
				// Previously completed setup — load saved config
				previousSetup = true;
				networkMode = status.network_mode || 'connected';
				aiMode = status.ai_mode || 'local';
				selectedModel = status.ai_model || selectedModel;
				selectedRegions = status.osrm_regions || [];
			} else {
				// No completed setup — try restoring draft
				const hasDraft = loadDraft();
				if (hasDraft && !selectedModel) {
					selectedModel = status.hardware?.recommended_model || 'gemma4:e2b';
				}
			}

			const regionData = await apiFetch('/setup/osrm/regions');
			regions = regionData.regions || [];
		} catch (e) {
			console.error('Setup load failed:', e);
		}
		loading = false;
	});

	function showToast(msg: string) {
		toast = msg;
		setTimeout(() => toast = null, 3000);
	}

	async function pullModel() {
		if (!selectedModel) return;
		pullingModel = true;
		pullStatus = 'Downloading model... This may take several minutes.';
		try {
			const result = await apiFetch('/setup/ai/pull', {
				method: 'POST',
				body: JSON.stringify({ model_id: selectedModel }),
			});
			if (result.success) {
				pullStatus = 'Model ready!';
				localModels = [...localModels, selectedModel];
				showToast(`${selectedModel} downloaded successfully`);
			} else {
				pullStatus = `Failed: ${result.error}`;
			}
		} catch (e: any) {
			pullStatus = `Error: ${e.message}`;
		}
		pullingModel = false;
	}

	async function downloadRegion(regionId: string) {
		downloadingRegion = regionId;
		try {
			const result = await apiFetch('/setup/osrm/download', {
				method: 'POST',
				body: JSON.stringify({ region_id: regionId }),
			});
			if (result.success) {
				showToast(`Routing data ready for ${regionId}`);
				if (!selectedRegions.includes(regionId)) {
					selectedRegions = [...selectedRegions, regionId];
				}
			} else {
				showToast(`Failed: ${result.error}`);
			}
		} catch (e: any) {
			showToast(`Download failed: ${e.message}`);
		}
		downloadingRegion = null;
	}

	async function completeSetup() {
		saving = true;
		try {
			const body: any = {
				network_mode: networkMode,
				ai_model: selectedModel,
				ai_mode: aiMode,
				osrm_regions: selectedRegions,
			};
			if (aiMode === 'hybrid' || aiMode === 'cloud') {
				body.cloud_provider = cloudProvider;
				body.cloud_model = cloudModel;
				body.cloud_api_key = cloudApiKey;
			}

			await apiFetch('/setup/complete', {
				method: 'POST',
				body: JSON.stringify(body),
			});
			setupComplete = true;
			previousSetup = false;
			clearDraft();
			showToast('Setup complete!');
		} catch (e: any) {
			showToast(`Setup failed: ${e.message}`);
		}
		saving = false;
	}

	function modelIsLocal(modelId: string): boolean {
		return localModels.some(m => m.startsWith(modelId.split(':')[0]));
	}

	const BACKEND_URL = '/api';
	async function apiFetch(path: string, opts: RequestInit = {}) {
		const res = await fetch(`${BACKEND_URL}${path}`, {
			...opts,
			headers: { 'Content-Type': 'application/json', 'X-Tenant-ID': 'default', ...(opts.headers || {}) },
		});
		return res.json();
	}
</script>

{#if toast}
	<div class="fixed top-4 right-4 z-50 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm shadow-lg">
		{toast}
	</div>
{/if}

<div class="max-w-2xl mx-auto space-y-6">
	<!-- Header -->
	<div class="text-center">
		<h1 class="text-2xl font-bold text-foreground">Enki Setup</h1>
		<p class="text-sm text-muted-foreground mt-1">Configure your Command Center</p>
	</div>

	<!-- Progress Steps -->
	<div class="flex items-center justify-center gap-2">
		{#each steps as s}
			<button
				onclick={() => { if (s.num <= step) step = s.num; }}
				class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors
					{step === s.num ? 'bg-primary text-primary-foreground' :
					 step > s.num ? 'bg-emerald-500/15 text-emerald-400' :
					 'bg-muted text-muted-foreground'}"
			>
				{#if step > s.num}
					<Check class="w-3.5 h-3.5" />
				{:else}
					<svelte:component this={s.icon} class="w-3.5 h-3.5" />
				{/if}
				{s.label}
			</button>
			{#if s.num < 4}
				<ChevronRight class="w-4 h-4 text-muted-foreground/30" />
			{/if}
		{/each}
	</div>

	<!-- Hardware Info Bar -->
	{#if hardware}
		<div class="flex items-center justify-center gap-4 text-xs text-muted-foreground bg-muted/50 rounded-lg px-4 py-2">
			<span><Cpu class="w-3 h-3 inline" /> {hardware.cpu_count} cores</span>
			<span>{hardware.total_ram_gb} GB RAM</span>
			<span>{hardware.free_disk_gb} GB disk free</span>
			<span>Budget for AI: ~{hardware.model_budget_gb} GB</span>
		</div>
	{/if}

	<!-- Re-run warning -->
	{#if previousSetup && !setupComplete}
		<div class="rounded-lg bg-amber-500/10 border border-amber-500/20 px-4 py-3 flex items-center justify-between">
			<div>
				<p class="text-sm font-medium text-amber-400">Setup already completed</p>
				<p class="text-xs text-amber-400/70 mt-0.5">Re-running will overwrite your current configuration. Proceed with caution.</p>
			</div>
			<a href="/dashboard" class="text-xs text-muted-foreground hover:text-foreground px-3 py-1.5 rounded-md border border-border hover:bg-muted transition-colors shrink-0">
				Back to Dashboard
			</a>
		</div>
	{/if}

	{#if loading}
		<div class="text-center py-16 text-muted-foreground">
			<Loader class="w-8 h-8 mx-auto mb-2 animate-spin" />
			<p>Detecting hardware...</p>
		</div>

	<!-- ═══ STEP 1: NETWORK MODE ═══ -->
	{:else if step === 1}
		<div class="space-y-3">
			<h2 class="text-lg font-semibold text-foreground">How will this server connect?</h2>

			{#each [
				{ id: 'connected', icon: Wifi, label: 'Connected', desc: 'Internet available. Cloud AI optional. External data sources active.' },
				{ id: 'intermittent', icon: Signal, label: 'Intermittent', desc: 'Connected sometimes. Caches aggressively when online. Works offline between syncs.' },
				{ id: 'offline', icon: WifiOff, label: 'Offline / Air-Gapped', desc: 'No internet. All AI runs locally. Must pre-load map tiles and routing data.' },
			] as opt}
				<button
					onclick={() => { networkMode = opt.id as any; if (opt.id === 'offline') aiMode = 'local'; }}
					class="w-full text-left p-4 rounded-lg border transition-colors
						{networkMode === opt.id ? 'border-primary bg-primary/5' : 'border-border bg-card hover:bg-accent/20'}"
				>
					<div class="flex items-center gap-3">
						<svelte:component this={opt.icon} class="w-5 h-5 {networkMode === opt.id ? 'text-primary' : 'text-muted-foreground'}" />
						<div>
							<div class="font-medium text-foreground">{opt.label}</div>
							<div class="text-xs text-muted-foreground mt-0.5">{opt.desc}</div>
						</div>
					</div>
				</button>
			{/each}

			{#if networkMode === 'offline'}
				<div class="rounded-lg bg-amber-500/10 border border-amber-500/20 px-4 py-3 text-xs text-amber-400">
					<strong>Offline mode:</strong> Cloud AI will be disabled. You must download AI models and routing data before disconnecting.
				</div>
			{/if}
		</div>

	<!-- ═══ STEP 2: AI MODEL ═══ -->
	{:else if step === 2}
		<div class="space-y-4">
			<h2 class="text-lg font-semibold text-foreground">AI Configuration</h2>

			<!-- AI Mode selector (only if connected) -->
			{#if networkMode !== 'offline'}
				<div>
					<div class="text-sm text-muted-foreground mb-2">Processing mode</div>
					<div class="grid grid-cols-3 gap-2">
						{#each [
							{ id: 'local', icon: Shield, label: 'Local Only', desc: 'Data stays on server' },
							{ id: 'hybrid', icon: Zap, label: 'Hybrid', desc: 'Local + cloud for complex tasks' },
							{ id: 'cloud', icon: Cloud, label: 'Cloud Only', desc: 'Requires API key' },
						] as opt}
							<button
								onclick={() => aiMode = opt.id as any}
								class="p-3 rounded-lg border text-center text-sm transition-colors
									{aiMode === opt.id ? 'border-primary bg-primary/5' : 'border-border bg-card hover:bg-accent/20'}"
							>
								<svelte:component this={opt.icon} class="w-4 h-4 mx-auto mb-1 {aiMode === opt.id ? 'text-primary' : 'text-muted-foreground'}" />
								<div class="font-medium text-foreground text-xs">{opt.label}</div>
								<div class="text-[10px] text-muted-foreground mt-0.5">{opt.desc}</div>
							</button>
						{/each}
					</div>
				</div>
			{/if}

			<!-- Local Model Selection -->
			{#if aiMode === 'local' || aiMode === 'hybrid'}
				<div>
					<div class="text-sm text-muted-foreground mb-2">Local AI Model</div>
					<div class="space-y-2">
						{#each modelOptions as model}
							<button
								onclick={() => selectedModel = model.model_id}
								disabled={!model.fits}
								class="w-full text-left p-3 rounded-lg border transition-colors
									{selectedModel === model.model_id ? 'border-primary bg-primary/5' :
									 !model.fits ? 'border-border/50 bg-muted/30 opacity-50 cursor-not-allowed' :
									 'border-border bg-card hover:bg-accent/20'}"
							>
								<div class="flex items-center justify-between">
									<div>
										<div class="flex items-center gap-2">
											<span class="text-sm font-medium text-foreground">{model.name}</span>
											{#if model.model_id === hardware?.recommended_model}
												<span class="text-[10px] bg-primary/15 text-primary px-1.5 py-0 rounded">Recommended</span>
											{/if}
											{#if modelIsLocal(model.model_id)}
												<span class="text-[10px] bg-emerald-500/15 text-emerald-400 px-1.5 py-0 rounded">Downloaded</span>
											{/if}
										</div>
										<div class="text-xs text-muted-foreground mt-0.5">{model.description}</div>
									</div>
									<div class="text-right text-xs text-muted-foreground shrink-0 ml-3">
										<div>{model.ram_gb} GB RAM</div>
										<div>{model.disk_gb} GB disk</div>
										{#if !model.fits}
											<div class="text-red-400">Won't fit</div>
										{/if}
									</div>
								</div>
							</button>
						{/each}
					</div>

					<!-- Pull button -->
					{#if selectedModel && !modelIsLocal(selectedModel)}
						<button
							onclick={pullModel}
							disabled={pullingModel}
							class="mt-3 flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
						>
							{#if pullingModel}
								<Loader class="w-4 h-4 animate-spin" /> Downloading...
							{:else}
								<Download class="w-4 h-4" /> Download {selectedModel}
							{/if}
						</button>
						{#if pullStatus}
							<p class="text-xs text-muted-foreground mt-1">{pullStatus}</p>
						{/if}
					{/if}
				</div>
			{/if}

			<!-- Cloud Provider (for hybrid/cloud mode) -->
			{#if aiMode === 'cloud' || aiMode === 'hybrid'}
				<div class="border-t border-border pt-4 space-y-3">
					<div class="text-sm text-muted-foreground">Cloud AI Provider</div>
					<select bind:value={cloudProvider}
						class="w-full px-3 py-2 rounded-lg border border-border bg-muted text-foreground text-sm">
						<option value="">Select provider...</option>
						<option value="anthropic">Anthropic (Claude)</option>
						<option value="openai">OpenAI (GPT-4o)</option>
						<option value="gemini">Google (Gemini)</option>
						<option value="grok">xAI (Grok)</option>
					</select>
					{#if cloudProvider}
						<input type="text" bind:value={cloudModel} placeholder="Model name (e.g. claude-sonnet-4-20250514)"
							class="w-full px-3 py-2 rounded-lg border border-border bg-muted text-foreground text-sm" />
						<input type="password" bind:value={cloudApiKey} placeholder="API Key"
							class="w-full px-3 py-2 rounded-lg border border-border bg-muted text-foreground text-sm" />
					{/if}
				</div>
			{/if}
		</div>

	<!-- ═══ STEP 3: ROUTING REGIONS ═══ -->
	{:else if step === 3}
		<div class="space-y-3">
			<h2 class="text-lg font-semibold text-foreground">Routing Regions</h2>
			<p class="text-xs text-muted-foreground">
				Select regions for road routing and GPS map-matching. Download only what you need — you can add more later.
			</p>

			<!-- US States -->
			<div class="text-xs font-medium text-muted-foreground uppercase tracking-wider mt-2">United States</div>
			<div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
				{#each regions.filter(r => r.country === 'US' && !r.region_id.includes('us-')) as region}
					<div class="flex items-center justify-between p-2.5 rounded-lg border border-border bg-card">
						<div class="flex items-center gap-2">
							<input
								type="checkbox"
								checked={selectedRegions.includes(region.region_id)}
								onchange={() => {
									if (selectedRegions.includes(region.region_id)) {
										selectedRegions = selectedRegions.filter(r => r !== region.region_id);
									} else {
										selectedRegions = [...selectedRegions, region.region_id];
									}
								}}
								class="rounded"
							/>
							<div>
								<span class="text-sm text-foreground">{region.name}</span>
								<span class="text-[10px] text-muted-foreground ml-1">{region.size_mb} MB</span>
							</div>
						</div>
						{#if region.status === 'loaded'}
							<span class="text-[10px] text-emerald-400">Loaded</span>
						{:else if !region.can_process}
							<span class="text-[10px] text-red-400">Needs {region.process_ram_gb}GB RAM</span>
						{:else if downloadingRegion === region.region_id}
							<Loader class="w-3.5 h-3.5 animate-spin text-primary" />
						{:else if selectedRegions.includes(region.region_id)}
							<button onclick={() => downloadRegion(region.region_id)}
								class="text-[10px] px-2 py-0.5 rounded bg-primary/10 text-primary hover:bg-primary/20">
								Download
							</button>
						{/if}
					</div>
				{/each}
			</div>

			<!-- US Regions -->
			<div class="text-xs font-medium text-muted-foreground uppercase tracking-wider mt-3">US Regions (large)</div>
			<div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
				{#each regions.filter(r => r.region_id.includes('us-')) as region}
					<div class="flex items-center justify-between p-2.5 rounded-lg border border-border bg-card">
						<div class="flex items-center gap-2">
							<input type="checkbox"
								checked={selectedRegions.includes(region.region_id)}
								onchange={() => {
									if (selectedRegions.includes(region.region_id)) {
										selectedRegions = selectedRegions.filter(r => r !== region.region_id);
									} else {
										selectedRegions = [...selectedRegions, region.region_id];
									}
								}}
								class="rounded" />
							<div>
								<span class="text-sm text-foreground">{region.name}</span>
								<span class="text-[10px] text-muted-foreground ml-1">{(region.size_mb / 1000).toFixed(1)} GB</span>
							</div>
						</div>
						{#if !region.can_process}
							<span class="text-[10px] text-red-400">Needs {region.process_ram_gb}GB</span>
						{/if}
					</div>
				{/each}
			</div>

			<!-- International -->
			<div class="text-xs font-medium text-muted-foreground uppercase tracking-wider mt-3">International</div>
			<div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
				{#each regions.filter(r => r.country !== 'US') as region}
					<div class="flex items-center justify-between p-2.5 rounded-lg border border-border bg-card">
						<div class="flex items-center gap-2">
							<input type="checkbox"
								checked={selectedRegions.includes(region.region_id)}
								onchange={() => {
									if (selectedRegions.includes(region.region_id)) {
										selectedRegions = selectedRegions.filter(r => r !== region.region_id);
									} else {
										selectedRegions = [...selectedRegions, region.region_id];
									}
								}}
								class="rounded" />
							<div>
								<span class="text-sm text-foreground">{region.name}</span>
								<span class="text-[10px] text-muted-foreground ml-1">{region.size_mb >= 1000 ? (region.size_mb / 1000).toFixed(1) + ' GB' : region.size_mb + ' MB'}</span>
							</div>
						</div>
					</div>
				{/each}
			</div>

			{#if selectedRegions.length === 0}
				<p class="text-xs text-muted-foreground italic">No regions selected. Routing will be unavailable. You can add regions later in Settings.</p>
			{/if}

			<!-- PMTiles for Offline Mode -->
			{#if networkMode === 'offline'}
				<div class="border-t border-border pt-4 mt-4">
					<div class="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-2">Offline Map Tiles</div>
					<div class="rounded-lg border border-amber-500/20 bg-amber-500/5 p-4 space-y-3">
						<p class="text-xs text-muted-foreground leading-relaxed">
							For offline use, you need a PMTiles basemap file. This provides satellite/terrain imagery without internet.
						</p>
						<div class="space-y-2">
							<div class="flex items-center justify-between p-2.5 rounded-lg border border-border bg-card">
								<div>
									<span class="text-sm text-foreground">Planet Basemap</span>
									<span class="text-[10px] text-muted-foreground ml-2">~80 GB (full global coverage)</span>
								</div>
								<a href="https://protomaps.com/downloads" target="_blank" rel="noopener"
									class="text-[10px] px-2 py-0.5 rounded bg-primary/10 text-primary hover:bg-primary/20">
									Download
								</a>
							</div>
							<div class="flex items-center justify-between p-2.5 rounded-lg border border-border bg-card">
								<div>
									<span class="text-sm text-foreground">Region Extract</span>
									<span class="text-[10px] text-muted-foreground ml-2">1-10 GB (single country/state)</span>
								</div>
								<a href="https://protomaps.com/downloads" target="_blank" rel="noopener"
									class="text-[10px] px-2 py-0.5 rounded bg-primary/10 text-primary hover:bg-primary/20">
									Download
								</a>
							</div>
						</div>
						<p class="text-[10px] text-muted-foreground">
							Place the <code class="bg-muted px-1 rounded">.pmtiles</code> file in <code class="bg-muted px-1 rounded">/var/lib/enki/tiles/</code> and restart the map service.
						</p>
					</div>
				</div>
			{/if}
		</div>

	<!-- ═══ STEP 4: CONFIRM ═══ -->
	{:else if step === 4}
		{#if setupComplete}
			<div class="text-center py-12">
				<div class="w-16 h-16 rounded-full bg-emerald-500/15 flex items-center justify-center mx-auto mb-4">
					<Check class="w-8 h-8 text-emerald-400" />
				</div>
				<h2 class="text-xl font-bold text-foreground mb-2">Setup Complete</h2>
				<p class="text-sm text-muted-foreground mb-6">Your Command Center is configured and ready.</p>
				<a href="/dashboard" class="inline-flex items-center gap-2 px-6 py-2.5 rounded-lg bg-primary text-primary-foreground font-medium hover:bg-primary/90 transition-colors">
					Go to Dashboard <ChevronRight class="w-4 h-4" />
				</a>
			</div>
		{:else}
			<div class="space-y-4">
				<h2 class="text-lg font-semibold text-foreground">Review & Confirm</h2>

				<div class="rounded-lg border border-border bg-card divide-y divide-border">
					<div class="px-4 py-3 flex justify-between">
						<span class="text-sm text-muted-foreground">Network Mode</span>
						<span class="text-sm font-medium text-foreground capitalize">{networkMode}</span>
					</div>
					<div class="px-4 py-3 flex justify-between">
						<span class="text-sm text-muted-foreground">AI Mode</span>
						<span class="text-sm font-medium text-foreground capitalize">{aiMode}</span>
					</div>
					<div class="px-4 py-3 flex justify-between">
						<span class="text-sm text-muted-foreground">Local Model</span>
						<span class="text-sm font-medium text-foreground">
							{selectedModel}
							{#if modelIsLocal(selectedModel)}
								<span class="text-emerald-400 ml-1">(ready)</span>
							{:else}
								<span class="text-amber-400 ml-1">(needs download)</span>
							{/if}
						</span>
					</div>
					{#if aiMode !== 'local' && cloudProvider}
						<div class="px-4 py-3 flex justify-between">
							<span class="text-sm text-muted-foreground">Cloud Provider</span>
							<span class="text-sm font-medium text-foreground capitalize">{cloudProvider}</span>
						</div>
					{/if}
					<div class="px-4 py-3 flex justify-between">
						<span class="text-sm text-muted-foreground">Routing Regions</span>
						<span class="text-sm font-medium text-foreground">
							{selectedRegions.length > 0 ? selectedRegions.length + ' selected' : 'None'}
						</span>
					</div>
					{#if networkMode === 'offline'}
						<div class="px-4 py-3 flex justify-between">
							<span class="text-sm text-muted-foreground">Map Tiles</span>
							<span class="text-sm font-medium text-amber-400">Local (needs PMTiles download)</span>
						</div>
					{/if}
				</div>

				{#if !modelIsLocal(selectedModel) && (aiMode === 'local' || aiMode === 'hybrid')}
					<div class="rounded-lg bg-amber-500/10 border border-amber-500/20 px-4 py-3 text-xs text-amber-400">
						The model <strong>{selectedModel}</strong> hasn't been downloaded yet. It will be pulled automatically on first use,
						or you can go back to Step 2 and download it now.
					</div>
				{/if}

				<button
					onclick={completeSetup}
					disabled={saving}
					class="w-full py-3 rounded-lg bg-primary text-primary-foreground font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
				>
					{saving ? 'Saving...' : 'Complete Setup'}
				</button>
			</div>
		{/if}
	{/if}

	<!-- Navigation -->
	{#if !loading && !setupComplete}
		<div class="flex justify-between pt-2">
			{#if step > 1}
				<button onclick={() => step--}
					class="flex items-center gap-1.5 px-4 py-2 rounded-lg border border-border text-sm text-foreground hover:bg-muted transition-colors">
					<ChevronLeft class="w-4 h-4" /> Back
				</button>
			{:else}
				<div></div>
			{/if}
			{#if step < 4}
				<button onclick={() => step++}
					class="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors">
					Next <ChevronRight class="w-4 h-4" />
				</button>
			{/if}
		</div>
	{/if}
</div>
