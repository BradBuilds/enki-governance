<script lang="ts">
	/**
	 * Heatmap cell detail panel — top stories for a clicked hex cell.
	 *
	 * Phase 4.5 of the collapse rebuild plan. Sorts by mention_count × recency.
	 */
	import { onMount } from 'svelte';
	import { getHeaders } from '$lib/api/mock';

	type StoryRow = {
		signal_rid: number;
		title: string | null;
		published_at: string | null;
		source_url: string | null;
		mention_count: number;
		hours_ago: number | null;
	};

	interface Props {
		lat: number;
		lng: number;
		count: number;
		storyIds: number[];
		onClose: () => void;
	}

	let { lat, lng, count, storyIds, onClose }: Props = $props();

	let stories = $state<StoryRow[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			// Use /api/feed/articles-near — purpose-built for lat/lon → articles
			// lookups and returns { articles: [...] }. Old code called /api/feed
			// with rids= filter, which (a) /api/feed doesn't support and (b)
			// returns { cards, sections } anyway, causing (data.items || data
			// || []).slice is not a function in the panel.
			const params = new URLSearchParams({
				lat: String(lat),
				lon: String(lng),
				// Tight radius — the heatmap cell is already aggregated to a
				// neighbourhood-scale h3 bin; 25km is plenty.
				radius_km: '25',
				hours: '168',
				limit: '40',
			});
			const r = await fetch(`/api/feed/articles-near?${params.toString()}`, {
				headers: getHeaders()
			});
			if (!r.ok) throw new Error(`fetch failed (${r.status})`);
			const data = await r.json();
			const articles = Array.isArray(data?.articles) ? data.articles : [];
			stories = articles.slice(0, 20).map((it: any) => ({
				signal_rid: it.signal_rid || it.rid || 0,
				title: it.headline || it.title || '(untitled)',
				published_at: it.timestamp || it.published_at || null,
				source_url: it.link || it.source_url || null,
				mention_count: it.mention_count || 1,
				hours_ago: it.timestamp
					? Math.round((Date.now() - new Date(it.timestamp).getTime()) / 3.6e6)
					: null
			}));
		} catch (e: any) {
			error = e.message || 'Failed to load stories';
		}
		loading = false;
	}

	onMount(load);

	const scored = $derived(
		stories
			.map((s) => ({
				...s,
				score: s.mention_count * (1 / (1 + (s.hours_ago ?? 24)))
			}))
			.sort((a, b) => b.score - a.score)
	);
</script>

<aside class="panel" role="dialog" aria-label="Heatmap cell detail">
	<header>
		<div>
			<h3>{count} mentions</h3>
			<p class="coord">{lat.toFixed(2)}°, {lng.toFixed(2)}°</p>
		</div>
		<button class="close" onclick={onClose} aria-label="Close">×</button>
	</header>

	{#if loading}
		<p class="muted">Loading…</p>
	{:else if error}
		<p class="error">{error}</p>
	{:else if scored.length === 0}
		<p class="muted">No stories surfaced for this cell.</p>
	{:else}
		<ol class="stories">
			{#each scored as s (s.signal_rid)}
				<li>
					{#if s.source_url}
						<a href={s.source_url} target="_blank" rel="noopener noreferrer">{s.title}</a>
					{:else}
						<span>{s.title}</span>
					{/if}
					<div class="row-meta">
						{#if s.hours_ago !== null}
							<span>{s.hours_ago}h ago</span>
							<span>·</span>
						{/if}
						<span>{s.mention_count}× mentioned here</span>
					</div>
				</li>
			{/each}
		</ol>
	{/if}
</aside>

<style>
	.panel {
		position: absolute;
		top: 16px;
		right: 16px;
		width: 360px;
		max-height: calc(100vh - 32px);
		overflow-y: auto;
		background: var(--surface, #0f0f0f);
		border: 1px solid var(--border, #2a2a2a);
		border-radius: 10px;
		padding: 14px 16px;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
		z-index: 800;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 12px;
		margin-bottom: 12px;
	}
	header h3 {
		margin: 0;
		font-size: 16px;
		font-weight: 600;
	}
	.coord {
		margin: 2px 0 0;
		color: var(--muted, #888);
		font-size: 12px;
		font-family: var(--font-mono, monospace);
	}
	.close {
		background: transparent;
		border: none;
		color: var(--muted, #888);
		font-size: 20px;
		line-height: 1;
		cursor: pointer;
		padding: 0;
	}
	.close:hover {
		color: var(--fg, #fff);
	}
	.stories {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.stories li {
		padding: 8px;
		border: 1px solid var(--border, #2a2a2a);
		border-radius: 6px;
		font-size: 13px;
	}
	.stories a {
		color: rgb(120, 180, 255);
		text-decoration: none;
	}
	.stories a:hover {
		text-decoration: underline;
	}
	.row-meta {
		font-size: 11px;
		color: var(--muted, #888);
		margin-top: 4px;
		display: flex;
		gap: 6px;
	}
	.muted {
		color: var(--muted, #888);
		font-size: 13px;
		text-align: center;
		padding: 12px;
	}
	.error {
		color: rgb(220, 90, 90);
		font-size: 13px;
	}
</style>
