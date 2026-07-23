<script lang="ts">
	import { onMount } from 'svelte';
	import Satellite from 'lucide-svelte/icons/satellite';
	import Key from 'lucide-svelte/icons/key';
	import Check from 'lucide-svelte/icons/check';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Save from 'lucide-svelte/icons/save';

	// Fixed secret names the backend reads (see services/api/src/routes/satellite.py).
	const SECRETS = {
		instance: 'copernicus-wms-instance',
		clientId: 'copernicus-client-id',
		clientSecret: 'copernicus-client-secret',
	};

	let loading = $state(true);
	let status = $state<{ configured: boolean; has_instance: boolean; has_oauth: boolean } | null>(null);
	let setNames = $state<Set<string>>(new Set());

	// input buffers
	let instanceInput = $state('');
	let clientIdInput = $state('');
	let clientSecretInput = $state('');

	let saving = $state('');
	let testResult = $state<{ success: boolean; layers?: string[]; error?: string } | null>(null);
	let testing = $state(false);

	async function loadState() {
		try {
			const [st, secs] = await Promise.all([
				fetch('/api/satellite/status').then(r => r.json()),
				fetch('/api/secrets?category=api_key').then(r => r.json()).catch(() => ({ secrets: [] })),
			]);
			status = st;
			setNames = new Set((secs.secrets || []).map((s: any) => s.name));
		} catch {}
		loading = false;
	}

	onMount(loadState);

	async function saveSecret(name: string, value: string, description: string) {
		if (!value.trim()) return;
		saving = name;
		try {
			await fetch('/api/secrets', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ name, value: value.trim(), category: 'api_key', description }),
			});
			await loadState();
		} catch {}
		saving = '';
	}

	async function testConnection() {
		testing = true;
		testResult = null;
		try {
			testResult = await fetch('/api/satellite/test', { method: 'POST' }).then(r => r.json());
		} catch (e: any) {
			testResult = { success: false, error: e?.message || 'Request failed' };
		}
		testing = false;
	}

	const instanceSet = $derived(setNames.has(SECRETS.instance));
	const oauthSet = $derived(setNames.has(SECRETS.clientId) && setNames.has(SECRETS.clientSecret));
</script>

<div class="max-w-3xl mx-auto">
	<div class="flex items-center gap-3 mb-2">
		<div class="w-9 h-9 rounded-lg bg-sky-500/15 flex items-center justify-center">
			<Satellite size={18} class="text-sky-400" />
		</div>
		<div>
			<h1 class="text-xl font-semibold text-foreground">Satellite Imagery</h1>
			<p class="text-sm text-muted-foreground mt-0.5">
				ESA / Copernicus Sentinel-2 — free, open optical imagery over the whole Earth.
			</p>
		</div>
		{#if status?.configured}
			<span class="ml-auto inline-flex items-center gap-1 text-xs px-2 py-1 rounded-full bg-emerald-500/15 text-emerald-400">
				<Check size={12} /> Connected
			</span>
		{/if}
	</div>

	{#if loading}
		<p class="text-sm text-muted-foreground mt-6">Loading…</p>
	{:else}
		<!-- How to get credentials -->
		<div class="mt-4 p-3 rounded-lg bg-muted/40 border border-border text-xs text-muted-foreground space-y-1.5">
			<p class="text-foreground font-medium">How to connect (one-time, free)</p>
			<ol class="list-decimal ml-4 space-y-0.5">
				<li>Register on the Copernicus Data Space Ecosystem (free).</li>
				<li>Open the Sentinel Hub dashboard → <span class="text-foreground">Configuration Utility</span> → create a config from the <span class="text-foreground">Full WMS template</span>.</li>
				<li>Copy the config's <span class="text-foreground">Instance ID</span> and paste it below.</li>
				<li>Optional: create an <span class="text-foreground">OAuth client</span> (for automated monitoring later) and paste the client ID/secret.</li>
			</ol>
			<a href="https://shapps.dataspace.copernicus.eu/dashboard/" target="_blank" rel="noopener"
				class="inline-flex items-center gap-1 mt-1 text-primary hover:underline">
				<ExternalLink size={11} /> Open Sentinel Hub dashboard
			</a>
		</div>

		<!-- WMS Instance (required) -->
		<div class="mt-4 rounded-lg border border-border bg-card p-4 {instanceSet ? 'border-l-4 border-l-emerald-500/50' : 'border-l-4 border-l-yellow-500/50'}">
			<div class="flex items-center gap-2 mb-1">
				<span class="text-sm font-medium text-foreground">WMS Instance ID</span>
				<span class="text-[10px] uppercase tracking-wider text-primary">required</span>
				{#if instanceSet}
					<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400">Set</span>
				{/if}
			</div>
			<p class="text-xs text-muted-foreground mb-2">
				Drives the live map overlays and on-demand overhead capture. Stored server-side; the
				value is never shown again and never reaches the browser (map tiles are proxied).
			</p>
			<div class="flex items-center gap-2">
				<Key size={14} class="text-muted-foreground shrink-0" />
				<input type="text" bind:value={instanceInput}
					placeholder={instanceSet ? '•••••••• (set — paste to replace)' : 'e.g. 0d5799ab-f7e9-4ff9-b9de-8153fef89ae5'}
					class="flex-1 px-2 py-1.5 rounded border border-border bg-card text-foreground text-xs font-mono" />
				<button onclick={() => saveSecret(SECRETS.instance, instanceInput, 'Copernicus/Sentinel Hub WMS instance ID').then(() => instanceInput = '')}
					disabled={!instanceInput.trim() || saving === SECRETS.instance}
					class="px-3 py-1.5 rounded bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 disabled:opacity-40">
					<Save size={12} class="inline mr-1" />Save
				</button>
			</div>
		</div>

		<!-- OAuth (optional) -->
		<div class="mt-3 rounded-lg border border-border bg-card p-4">
			<div class="flex items-center gap-2 mb-1">
				<span class="text-sm font-medium text-foreground">OAuth Client</span>
				<span class="text-[10px] uppercase tracking-wider text-muted-foreground">optional — for monitoring</span>
				{#if oauthSet}
					<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400">Set</span>
				{/if}
			</div>
			<p class="text-xs text-muted-foreground mb-2">
				Not needed for map overlays or capture. Reserved for automated area-of-interest
				monitoring and change alerts (Statistical API), coming in a later phase.
			</p>
			<div class="space-y-2">
				<div class="flex items-center gap-2">
					<input type="text" bind:value={clientIdInput}
						placeholder={setNames.has(SECRETS.clientId) ? '•••••••• (set)' : 'Client ID'}
						class="flex-1 px-2 py-1.5 rounded border border-border bg-card text-foreground text-xs font-mono" />
					<button onclick={() => saveSecret(SECRETS.clientId, clientIdInput, 'Copernicus OAuth client ID').then(() => clientIdInput = '')}
						disabled={!clientIdInput.trim() || saving === SECRETS.clientId}
						class="px-3 py-1.5 rounded bg-muted text-foreground text-xs hover:bg-accent disabled:opacity-40">Save</button>
				</div>
				<div class="flex items-center gap-2">
					<input type="password" bind:value={clientSecretInput}
						placeholder={setNames.has(SECRETS.clientSecret) ? '•••••••• (set)' : 'Client Secret'}
						class="flex-1 px-2 py-1.5 rounded border border-border bg-card text-foreground text-xs font-mono" />
					<button onclick={() => saveSecret(SECRETS.clientSecret, clientSecretInput, 'Copernicus OAuth client secret').then(() => clientSecretInput = '')}
						disabled={!clientSecretInput.trim() || saving === SECRETS.clientSecret}
						class="px-3 py-1.5 rounded bg-muted text-foreground text-xs hover:bg-accent disabled:opacity-40">Save</button>
				</div>
			</div>
		</div>

		<!-- Test connection -->
		<div class="mt-4 flex items-center gap-3">
			<button onclick={testConnection} disabled={testing || !instanceSet}
				class="px-4 py-2 text-sm rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40">
				{testing ? 'Testing…' : 'Test connection'}
			</button>
			{#if !instanceSet}
				<span class="text-xs text-muted-foreground">Save a WMS instance ID first.</span>
			{/if}
			{#if testResult}
				{#if testResult.success}
					<span class="text-xs text-emerald-400">✓ Connected — {testResult.layers?.length || 0} layers available</span>
				{:else}
					<span class="text-xs text-red-400">⚠ {testResult.error}</span>
				{/if}
			{/if}
		</div>
		{#if testResult?.success && testResult.layers?.length}
			<div class="mt-2 flex flex-wrap gap-1">
				{#each testResult.layers.slice(0, 24) as l}
					<span class="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground font-mono">{l}</span>
				{/each}
			</div>
		{/if}

		<p class="mt-6 text-[11px] text-muted-foreground/70 border-t border-border pt-3">
			Security: the WMS instance ID is quota-limited. Before exposing this deployment publicly,
			domain-lock the config in the Sentinel Hub dashboard. Imagery is ~10 m/px, ~5-day revisit —
			it detects change and presence, not identity or real-time motion.
		</p>
	{/if}
</div>
