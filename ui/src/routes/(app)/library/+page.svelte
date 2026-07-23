<script lang="ts">
	import { onMount } from 'svelte';
	import Plus from 'lucide-svelte/icons/plus';
	import Globe from 'lucide-svelte/icons/globe';
	import Download from 'lucide-svelte/icons/download';
	import { LibraryCard } from '$lib/components/cards2';
	import { apiSafe, api } from '$lib/api/client';

	let libraries = $state<any[]>([]);
	let mirrors = $state<any[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Network discovery state
	let showNetwork = $state(false);
	let networkLibraries = $state<any[]>([]);
	let networkLoading = $state(false);
	let networkError = $state<string | null>(null);
	let subscribing = $state<string | null>(null);

	onMount(async () => {
		const [libsData, mirrorsData] = await Promise.all([
			apiSafe<{ libraries?: any[] }>('/libraries'),
			apiSafe<{ mirrors?: any[] }>('/library-mirrors'),
		]);
		if (libsData) libraries = libsData.libraries || [];
		else error = 'Failed to load libraries';
		mirrors = mirrorsData?.mirrors || [];
		loading = false;
	});

	async function loadNetworkLibraries() {
		networkLoading = true;
		networkError = null;
		try {
			const data = await apiSafe<{ libraries?: any[]; polled_nodes?: number }>(
				'/fleet/libraries/network'
			);
			networkLibraries = data?.libraries || [];
		} catch (e) {
			networkError = e instanceof Error ? e.message : String(e);
		}
		networkLoading = false;
	}

	async function toggleNetwork() {
		showNetwork = !showNetwork;
		if (showNetwork && networkLibraries.length === 0) {
			await loadNetworkLibraries();
		}
	}

	async function mirrorLibrary(lib: any) {
		const key = `${lib.host_node_id}:${lib.library_id}`;
		subscribing = key;
		try {
			const result = await api<any>('/library-mirrors', {
				method: 'POST',
				body: {
					upstream_node_id: lib.host_node_id,
					upstream_library_id: lib.library_id,
					upstream_url: lib.host_url,
					canonical_name: lib.canonical_name,
				},
			});
			// Refresh local mirrors list
			const m = await apiSafe<{ mirrors?: any[] }>('/library-mirrors');
			mirrors = m?.mirrors || [];
			alert(
				`Mirror created. Local library #${result.local_library_id} ` +
				`will populate over the next 15 minutes as the subscriber worker pulls content.`
			);
		} catch (e) {
			alert(`Failed to mirror: ${e instanceof Error ? e.message : String(e)}`);
		}
		subscribing = null;
	}

	function isAlreadyMirrored(lib: any): boolean {
		return mirrors.some(m =>
			m.upstream_node_id === lib.host_node_id &&
			m.upstream_library_id === lib.library_id
		);
	}
</script>

<div class="max-w-5xl mx-auto space-y-6">
	<header class="flex items-center justify-between">
		<div>
			<h1 class="text-2xl font-bold text-foreground">Libraries</h1>
			<p class="text-sm text-muted-foreground mt-1">
				Topic-anchored research bundles. Each library auto-discovers references and ingests acquired PDFs.
			</p>
		</div>
		<div class="flex items-center gap-2">
			<button onclick={toggleNetwork}
				class="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-border text-sm font-medium hover:bg-muted transition-colors">
				<Globe size={16} />
				{showNetwork ? 'Hide network' : 'Browse network'}
			</button>
			<button class="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors">
				<Plus size={16} />
				Create library
			</button>
		</div>
	</header>

	{#if showNetwork}
		<section class="rounded-xl border border-primary/30 bg-primary/5 p-4 space-y-3">
			<div class="flex items-center justify-between">
				<div>
					<h2 class="text-sm font-semibold text-foreground flex items-center gap-2">
						<Globe size={14} class="text-primary" />
						Network libraries
					</h2>
					<p class="text-xs text-muted-foreground mt-0.5">
						Public libraries from other nodes in the federation. Mirror one to pull its content into your node.
					</p>
				</div>
				<button onclick={loadNetworkLibraries} class="text-xs px-2 py-1 rounded border border-border hover:bg-muted">
					Refresh
				</button>
			</div>

			{#if networkLoading}
				<p class="text-sm text-muted-foreground">Discovering…</p>
			{:else if networkError}
				<p class="text-sm text-destructive">{networkError}</p>
			{:else if networkLibraries.length === 0}
				<p class="text-sm text-muted-foreground italic">
					No public libraries found on the federation yet. (Hub at <code>example.com</code> aggregates these.)
				</p>
			{:else}
				<div class="space-y-2">
					{#each networkLibraries as lib}
						{@const mirrored = isAlreadyMirrored(lib)}
						{@const key = `${lib.host_node_id}:${lib.library_id}`}
						<div class="flex items-baseline gap-3 py-2 px-3 rounded-lg bg-background border border-border">
							<div class="flex-1 min-w-0">
								<div class="text-sm font-medium text-foreground">{lib.focal_term || lib.canonical_name}</div>
								<div class="text-[11px] text-muted-foreground mt-0.5 flex items-center gap-3">
									<span>{lib.document_count || 0} docs · {lib.entity_count || 0} entities</span>
									<span class="font-mono">{lib.host_node_id?.slice(0, 12) || 'unknown'}…</span>
									<span class="text-muted-foreground/70">{lib.host_url}</span>
								</div>
							</div>
							{#if mirrored}
								<span class="text-[11px] text-emerald-400 uppercase tracking-wider">mirrored</span>
							{:else}
								<button onclick={() => mirrorLibrary(lib)} disabled={subscribing === key}
									class="inline-flex items-center gap-1.5 px-3 py-1 rounded-md bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 disabled:opacity-50">
									<Download size={12} />
									{subscribing === key ? 'Subscribing…' : 'Mirror'}
								</button>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
		</section>
	{/if}

	{#if loading}
		<p class="text-sm text-muted-foreground">Loading libraries…</p>
	{:else if error}
		<p class="text-sm text-destructive">{error}</p>
	{:else if libraries.length === 0}
		<div class="rounded-xl border border-dashed border-border p-8 text-center">
			<p class="text-sm text-muted-foreground">
				No libraries yet. Use <code class="bg-muted px-1.5 py-0.5 rounded text-xs">build_library</code> from the command line, or click "Create library" above.
			</p>
		</div>
	{:else}
		<div class="space-y-3">
			{#each libraries as lib}
				<LibraryCard library_id={lib.library_id} library={lib} size="default" />
			{/each}
		</div>
	{/if}
</div>
