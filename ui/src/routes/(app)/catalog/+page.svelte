<script lang="ts">
	// The declassified-archive catalog: browse ready+hashed libraries, click
	// Process to mine a slice, and watch GPU/compute live while it runs.
	let libraries = $state<any[]>([]);
	let loading = $state(true);
	let compute = $state<any>(null);
	let grab = $state(10);
	let filter = $state('');

	async function loadCatalog() {
		try {
			const r = await fetch('/api/catalog/libraries');
			if (r.ok) libraries = (await r.json()).libraries || [];
		} finally {
			loading = false;
		}
	}

	async function loadCompute() {
		try {
			const r = await fetch('/api/system/compute');
			if (r.ok) compute = await r.json();
		} catch {}
	}

	async function process(lib: any) {
		lib._starting = true;
		await fetch(`/api/libraries/${lib.library_id}/process?limit=${grab}`, { method: 'POST' });
		lib._starting = false;
	}

	async function toggleFollow(lib: any) {
		const on = !lib.followed;
		lib.followed = on;
		libraries = libraries;
		await fetch(`/api/libraries/${lib.library_id}/follow?on=${on}`, { method: 'POST' });
	}

	async function refreshStatuses() {
		// pull per-library process status for anything active
		await loadCompute();
		const active = new Set((compute?.processing || []).map((p: any) => p.library_id));
		for (const lib of libraries) {
			if (active.has(lib.library_id) || lib.process?.phase === 'collapsing' || lib._starting) {
				const r = await fetch(`/api/libraries/${lib.library_id}/process/status`);
				if (r.ok) lib.process = (await r.json()).process;
			}
		}
		libraries = libraries;
	}

	$effect(() => {
		loadCatalog();
		loadCompute();
		const t = setInterval(refreshStatuses, 4000);
		return () => clearInterval(t);
	});

	const gpu = $derived(compute?.gpu?.host || null);
	const byAgency = $derived.by(() => {
		const q = filter.toLowerCase();
		const groups: Record<string, any[]> = {};
		for (const l of libraries) {
			if (q && !(`${l.title} ${l.about} ${l.agency}`.toLowerCase().includes(q))) continue;
			(groups[l.agency || 'other'] ||= []).push(l);
		}
		return Object.entries(groups).sort((a, b) => {
			// keep 'other' last; otherwise biggest agency first
			if (a[0] === 'other') return 1;
			if (b[0] === 'other') return -1;
			return b[1].length - a[1].length;
		});
	});
</script>

<div class="wrap">
	<header>
		<h1>Declassified Archive Catalog</h1>
		<p class="sub">{libraries.length} libraries · documented, ready & hashed · click Process to mine a slice</p>
	</header>

	<!-- Live GPU / compute panel -->
	<div class="panel">
		{#if gpu && !gpu.error}
			<div class="stat"><span class="k">GPU</span><span class="v">{gpu.gpu_util_pct}%</span></div>
			<div class="stat"><span class="k">VRAM</span><span class="v">{(gpu.vram_used_mb/1024).toFixed(1)}/{(gpu.vram_total_mb/1024).toFixed(1)} GB</span></div>
			<div class="stat"><span class="k">Temp</span><span class="v">{gpu.temp_c}°C</span></div>
			<div class="stat"><span class="k">Power</span><span class="v">{gpu.power_w} W</span></div>
		{:else}
			<div class="stat"><span class="k">GPU</span><span class="v">—</span></div>
		{/if}
		{#if compute?.gpu?.ollama?.length}
			<div class="stat"><span class="k">Model</span><span class="v">{compute.gpu.ollama[0].model} ({compute.gpu.ollama[0].vram_gb}GB)</span></div>
		{/if}
		<div class="stat"><span class="k">Active</span><span class="v">{compute?.processing?.length || 0} processing</span></div>
	</div>

	<div class="controls">
		<input placeholder="filter topics…" bind:value={filter} />
		<label>grab <input type="number" min="1" max="500" bind:value={grab} class="grab" /> docs</label>
	</div>

	{#if loading}
		<p class="dim">loading catalog…</p>
	{:else}
		{#each byAgency as [agency, libs]}
			<div class="ag-head">{agency}<span class="count">{libs.length}</span></div>
			<div class="list">
				{#each libs as lib}
					<div class="row" class:busy={lib.process && !['done','error'].includes(lib.process?.phase)}>
						<a class="main" href={`/library/${lib.library_id}`}>
							<span class="rtitle">{lib.title}</span>
							<span class="rabout">{lib.about}</span>
						</a>
						{#if lib.process?.phase === 'done'}
							<span class="rproc done" title="{lib.process.mentions} entities · {lib.process.statements} facts">✓ mined</span>
						{:else if lib.process && lib.process.phase !== 'error'}
							<span class="rproc run">{lib.process.phase}… {lib.process.docs_done ?? 0}/{lib.process.docs_total ?? '?'}</span>
						{/if}
						<span class="rdocs">{lib.documents.toLocaleString()} docs</span>
						<button class="follow {lib.followed ? 'on' : ''}" title="Follow — pulls this topic's news into your private feed" onclick={() => toggleFollow(lib)}>{lib.followed ? '★' : '☆'}</button>
						<button class="proc" onclick={() => process(lib)} disabled={lib._starting}>
							{lib._starting ? '…' : lib.process?.phase && !['done','error'].includes(lib.process.phase) ? '…' : 'Process'}
						</button>
					</div>
				{/each}
			</div>
		{/each}
	{/if}
</div>

<style>
	.wrap { max-width: 1100px; margin: 0 auto; padding: 1.5rem; }
	header h1 { font-size: 1.5rem; margin: 0; }
	.sub { color: var(--text-dim, #8aa); margin: .25rem 0 1rem; font-size: .85rem; }
	.panel { display: flex; gap: 1.5rem; flex-wrap: wrap; background: var(--card, #10151c);
		border: 1px solid var(--border, #22303c); border-radius: 10px; padding: .75rem 1rem; margin-bottom: 1rem; }
	.stat { display: flex; flex-direction: column; }
	.stat .k { font-size: .65rem; text-transform: uppercase; color: #6b8; letter-spacing: .05em; }
	.stat .v { font-size: 1rem; font-variant-numeric: tabular-nums; }
	.controls { display: flex; gap: 1rem; align-items: center; margin-bottom: 1rem; }
	.controls input { background: #0c1117; border: 1px solid #22303c; color: inherit; padding: .4rem .6rem; border-radius: 6px; }
	.controls input:not(.grab) { flex: 1; }
	.grab { width: 4rem; }
	.ag-head { display: flex; gap: .5rem; align-items: baseline; font-size: .8rem; font-weight: 600;
		color: #9cb; text-transform: uppercase; letter-spacing: .04em; margin: 1.1rem 0 .3rem; }
	.ag-head .count { color: #6b8; font-size: .72rem; font-weight: 400; }
	.list { border: 1px solid #1a2530; border-radius: 8px; overflow: hidden; }
	.row { display: flex; align-items: center; gap: .75rem; padding: .5rem .75rem;
		border-bottom: 1px solid #161f28; }
	.row:last-child { border-bottom: none; }
	.row:hover { background: #0e141b; }
	.row.busy { background: #0d1a14; }
	.main { flex: 1; min-width: 0; display: flex; flex-direction: column; text-decoration: none; color: inherit; }
	.rtitle { font-size: .88rem; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.main:hover .rtitle { color: #cde; }
	.rabout { font-size: .74rem; color: #7a8b98; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.rdocs { font-size: .74rem; color: #7a9; white-space: nowrap; font-variant-numeric: tabular-nums; }
	.rproc { font-size: .7rem; white-space: nowrap; }
	.rproc.done { color: #6f9; }
	.rproc.run { color: #6c9; }
	button { background: #1a6; border: none; color: #fff; padding: .3rem .7rem;
		border-radius: 6px; cursor: pointer; font-size: .78rem; white-space: nowrap; }
	button.follow { background: transparent; border: 1px solid #2a4; color: #7d9; padding: .3rem .5rem; }
	button.follow.on { color: #fd6; border-color: #663; }
	button:disabled { opacity: .5; cursor: default; }
	.dim { color: #789; }
</style>
