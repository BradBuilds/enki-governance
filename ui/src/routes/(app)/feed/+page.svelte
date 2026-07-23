<script lang="ts">
	// The one feed: your followed topics' news, from every source, private to
	// your node. Manage sources/topics on /feed/manage.
	let items = $state<any[]>([]);
	let topic = $state('All');
	let loading = $state(true);

	async function load() {
		try {
			const r = await fetch('/api/feed/following');
			if (r.ok) items = (await r.json()).items || [];
		} finally {
			loading = false;
		}
	}
	$effect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t); });

	const topics = $derived(['All', ...Array.from(new Set(items.map(i => i.topic).filter(Boolean)))]);
	const shown = $derived(topic === 'All' ? items : items.filter(i => i.topic === topic));
</script>

<div class="wrap">
	<header>
		<div>
			<h1>Feed</h1>
			<p class="sub">Your followed topics, pulled from every source — private to your node.</p>
		</div>
		<a class="manage" href="/feed/manage">Manage sources &amp; topics →</a>
	</header>

	{#if !loading && items.length === 0}
		<div class="empty">Nothing yet. Follow topics in the <a href="/catalog">Catalog</a> and add sources in <a href="/feed/manage">Manage</a>.</div>
	{:else}
		<div class="tabs">
			{#each topics as t}
				<button class:on={topic === t} onclick={() => topic = t}>{t}{#if t !== 'All'}<span class="c">{items.filter(i => i.topic === t).length}</span>{/if}</button>
			{/each}
		</div>

		<div class="stream">
			{#each shown as it}
				<a class="card" href={it.link} target="_blank" rel="noopener">
					{#if it.thumbnail}
						<img class="thumb" src={it.thumbnail} alt="" loading="lazy" onerror={(e) => (e.currentTarget as HTMLImageElement).style.display = 'none'} />
					{/if}
					<div class="body">
						<div class="hd">{it.headline}</div>
						<div class="mt"><span class="topic">{it.topic}</span> · {it.source}</div>
						{#if it.summary}<div class="sm">{it.summary}</div>{/if}
					</div>
				</a>
			{/each}
		</div>
	{/if}
</div>

<style>
	.wrap { max-width: 780px; margin: 0 auto; padding: 1.5rem; }
	header { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }
	header h1 { font-size: 1.5rem; margin: 0; }
	.sub { color: #8aa; margin: .25rem 0 1rem; font-size: .85rem; }
	.manage { color: #6cf; font-size: .82rem; text-decoration: none; white-space: nowrap; }
	.tabs { display: flex; gap: .4rem; flex-wrap: wrap; margin-bottom: 1rem; position: sticky; top: 0; }
	.tabs button { background: #10151c; border: 1px solid #22303c; color: #9ab; padding: .35rem .8rem; border-radius: 999px; cursor: pointer; font-size: .82rem; }
	.tabs button.on { background: #143; border-color: #3a7; color: #6f9; }
	.tabs .c { color: #6b8; margin-left: .3rem; font-size: .72rem; }
	.card { display: flex; gap: .8rem; padding: .8rem 0; border-bottom: 1px solid #1a2530; text-decoration: none; color: inherit; }
	.thumb { width: 120px; height: 80px; object-fit: cover; border-radius: 8px; flex-shrink: 0; background: #0c1117; }
	.body { min-width: 0; }
	.card:hover .hd { color: #cde; }
	.hd { font-size: .98rem; font-weight: 500; line-height: 1.3; }
	.mt { font-size: .74rem; color: #7a9; margin-top: .2rem; }
	.topic { color: #6f9; }
	.sm { font-size: .84rem; color: #9ab; margin-top: .3rem; line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
	.empty { text-align: center; color: #789; padding: 3rem 1rem; }
	.empty a, .sub a { color: #6cf; }
</style>
