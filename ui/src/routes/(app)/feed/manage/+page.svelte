<script lang="ts">
	// Feed manager — your private, untrackable stream. Follow topics (the router
	// pulls each from every source into one stream), add your own RSS sources.
	// Everything here is local to your node; your interests never leave it.
	let following = $state<any[]>([]);
	let topics = $state<any[]>([]);
	let sources = $state<any[]>([]);
	let rssUrl = $state('');
	let rssName = $state('');
	let adding = $state(false);
	let msg = $state('');

	async function load() {
		const [f, c, s] = await Promise.all([
			fetch('/api/feed/following').then(r => r.ok ? r.json() : { items: [] }),
			fetch('/api/catalog/libraries').then(r => r.ok ? r.json() : { libraries: [] }),
			fetch('/api/connectors').then(r => r.ok ? r.json() : { connectors: [] })
		]);
		following = f.items || [];
		topics = (c.libraries || []).filter((l: any) => l.followed);
		sources = s.connectors || [];
	}

	async function unfollow(t: any) {
		await fetch(`/api/libraries/${t.library_id}/follow?on=false`, { method: 'POST' });
		topics = topics.filter(x => x.library_id !== t.library_id);
	}

	async function addRss() {
		if (!rssUrl.trim()) return;
		adding = true; msg = '';
		try {
			let host = rssName.trim();
			try { host = host || new URL(rssUrl).hostname.replace(/^www\./, ''); } catch {}
			const r = await fetch('/api/connectors', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					device_id: host || 'custom-feed',
					connector_type: 'pull',
					source_type: 'rss_feed',
					source_config: { url: rssUrl.trim() },
					poll_interval: '1 hour'
				})
			});
			msg = r.ok ? `Added ${host}` : `Failed (${r.status})`;
			if (r.ok) { rssUrl = ''; rssName = ''; await load(); }
		} finally {
			adding = false;
		}
	}

	$effect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); });
</script>

<div class="wrap">
	<header>
		<h1>Manage feed</h1>
		<p class="sub">Follow topics — we find them across every source; add your own feeds. Nothing here leaves your node.</p>
	</header>

	<!-- Add your own -->
	<section class="box">
		<h2>Add a feed source</h2>
		<div class="addrow">
			<input placeholder="RSS / Atom URL (https://…/rss)" bind:value={rssUrl} />
			<input placeholder="name (optional)" bind:value={rssName} class="nm" />
			<button onclick={addRss} disabled={adding || !rssUrl.trim()}>{adding ? 'adding…' : 'Add'}</button>
		</div>
		{#if msg}<p class="msg">{msg}</p>{/if}
	</section>

	<div class="cols">
		<!-- Followed topics -->
		<section class="box">
			<h2>Topics you follow <span class="n">{topics.length}</span></h2>
			{#if topics.length === 0}
				<p class="dim">No topics yet — open the <a href="/catalog">Catalog</a> and hit ★ Follow. Each followed topic is pulled from every source into your feed.</p>
			{:else}
				{#each topics as t}
					<div class="row">
						<span class="tt">{t.title}</span>
						<span class="ag">{t.agency}</span>
						<button class="x" onclick={() => unfollow(t)}>unfollow</button>
					</div>
				{/each}
			{/if}
		</section>

		<!-- Sources -->
		<section class="box">
			<h2>Sources <span class="n">{sources.length}</span></h2>
			{#if sources.length === 0}
				<p class="dim">No sources yet — add an RSS feed above.</p>
			{:else}
				{#each sources.slice(0, 40) as s}
					<div class="row">
						<span class="tt">{s.device_id || s.name || s.source_type}</span>
						<span class="ag">{s.source_type}</span>
						<span class="st {s.status}">{s.status || ''}</span>
					</div>
				{/each}
			{/if}
		</section>
	</div>

	<p class="backlink"><a href="/feed">← Back to your feed</a></p>
</div>

<style>
	.wrap { max-width: 1000px; margin: 0 auto; padding: 1.5rem; }
	header h1 { font-size: 1.5rem; margin: 0; }
	.sub { color: #8aa; margin: .25rem 0 1rem; font-size: .85rem; max-width: 60ch; }
	.box { background: #10151c; border: 1px solid #22303c; border-radius: 10px; padding: 1rem; margin-bottom: 1rem; }
	.box h2 { font-size: .95rem; margin: 0 0 .6rem; }
	.n { color: #6b8; font-size: .8rem; }
	.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
	.addrow { display: flex; gap: .5rem; }
	.addrow input { flex: 1; background: #0c1117; border: 1px solid #22303c; color: inherit; padding: .45rem .6rem; border-radius: 6px; }
	.addrow .nm { flex: .5; }
	.msg { font-size: .78rem; color: #6c9; margin: .4rem 0 0; }
	.row { display: flex; align-items: center; gap: .6rem; padding: .35rem 0; border-bottom: 1px solid #1a2530; font-size: .82rem; }
	.tt { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.ag { color: #6b8; font-size: .72rem; }
	.st { font-size: .68rem; color: #7a9; }
	.x { background: transparent; border: 1px solid #533; color: #d88; padding: .15rem .5rem; border-radius: 5px; cursor: pointer; font-size: .72rem; }
	.item { display: block; padding: .5rem 0; border-bottom: 1px solid #1a2530; text-decoration: none; color: inherit; }
	.hd { font-size: .9rem; }
	.mt { font-size: .72rem; color: #7a9; margin-top: .1rem; }
	.topic { color: #6f9; }
	.sm { font-size: .78rem; color: #9ab; margin-top: .2rem; }
	.dim { color: #789; font-size: .82rem; }
	.dim a { color: #6cf; }
	.backlink { margin-top: .5rem; }
	.backlink a { color: #6cf; text-decoration: none; font-size: .85rem; }
	button { background: #1a6; border: none; color: #fff; padding: .45rem .8rem; border-radius: 6px; cursor: pointer; font-size: .82rem; }
	button:disabled { opacity: .5; cursor: default; }
</style>
