<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import ImageIcon from 'lucide-svelte/icons/image';
	import Newspaper from 'lucide-svelte/icons/newspaper';
	import Camera from 'lucide-svelte/icons/camera';
	import Shield from 'lucide-svelte/icons/shield';
	import Search from 'lucide-svelte/icons/search';
	import X from 'lucide-svelte/icons/x';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import Eye from 'lucide-svelte/icons/eye';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Tag from 'lucide-svelte/icons/tag';
	import MobileSpaceTabs from '$lib/components/layout/MobileSpaceTabs.svelte';

	const researchTabs = [
		{ label: 'Graph', href: '/graph' },
		{ label: 'Search', href: '/search' },
		{ label: 'Projects', href: '/projects' },
		{ label: 'Entities', href: '/contacts' },
		{ label: 'Assets', href: '/assets' },
	];

	// State
	let loading = $state(true);
	let view = $state<'news' | 'personal' | 'intel' | 'all'>('all');
	let searchQuery = $state('');
	let tagFilter = $state('');

	// Gallery data (from /api/gallery)
	let galleryItems = $state<any[]>([]);

	// Media library data (from /api/media — deduplicated images with AI analysis)
	let mediaItems = $state<any[]>([]);

	// Lightbox
	let lightboxItem = $state<any>(null);

	// Computed
	let allItems = $derived.by(() => {
		// Merge gallery + media items, prioritizing media (has AI analysis)
		const combined = [...mediaItems.map(m => ({
			...m,
			_source: 'media',
			_thumbnail: m.bytes_path ? `/api/media/${m.node_id}/image` : null,
			_title: m.analysis?.description || m.first_seen_outlet || 'Image',
			_tags: m.analysis?.tags || [],
			_classification: m.analysis?.classification || '',
			_scene: m.analysis?.scene || '',
			_isIntel: m.analysis?.is_intelligence || false,
			_isPersonal: false,
			_articleNodeId: null,
		})), ...galleryItems.map(g => ({
			...g,
			_source: 'gallery',
			_thumbnail: g.has_thumbnail ? `/api/thumbnails/${g.cached_thumbnail || g.signal_id}` : null,
			_title: g.title || g.signal_type || 'Item',
			_tags: g.tags || [],
			_classification: '',
			_scene: '',
			_isIntel: false,
			_isPersonal: (g.signal_type || '').includes('personal'),
			_articleNodeId: g.article_node_id || null,
		}))];
		return combined;
	});

	let filteredItems = $derived.by(() => {
		let items = allItems;

		// View filter
		if (view === 'news') items = items.filter(i => i._source === 'media' || (i.signal_type || '').includes('rss') || (i.signal_type || '').includes('news'));
		else if (view === 'personal') items = items.filter(i => i._isPersonal || (i.signal_type || '').includes('photo') || (i.signal_type || '').includes('image'));
		else if (view === 'intel') items = items.filter(i => i._isIntel || i._classification === 'evidence');

		// Tag filter
		if (tagFilter) {
			items = items.filter(i => i._tags.some((t: string) => t.toLowerCase().includes(tagFilter.toLowerCase())));
		}

		// Search
		if (searchQuery.trim()) {
			const q = searchQuery.toLowerCase();
			items = items.filter(i =>
				(i._title || '').toLowerCase().includes(q) ||
				(i._tags || []).some((t: string) => t.toLowerCase().includes(q)) ||
				(i._classification || '').toLowerCase().includes(q)
			);
		}

		return items;
	});

	// All unique tags for filter dropdown
	let allTags = $derived(
		[...new Set(allItems.flatMap(i => i._tags || []))].sort().slice(0, 50)
	);

	async function loadData() {
		loading = true;
		try {
			const [galleryRes, mediaRes] = await Promise.all([
				fetch('/api/gallery?limit=200').then(r => r.ok ? r.json() : { items: [] }),
				fetch('/api/media?limit=200&sort=recent').then(r => r.ok ? r.json() : { items: [] }),
			]);
			galleryItems = galleryRes.items || [];
			mediaItems = mediaRes.items || mediaRes.media || [];
		} catch {}
		loading = false;
	}

	function openLightbox(item: any) {
		lightboxItem = item;
	}

	function closeLightbox() {
		lightboxItem = null;
	}

	function goToArticle(item: any) {
		closeLightbox();
		if (item.signal_rid || item.first_seen_signal_rid) {
			goto(`/signals/${item.signal_rid || item.first_seen_signal_rid}`);
		} else if (item._articleNodeId) {
			goto(`/nodes/${item._articleNodeId}`);
		} else if (item.signal_id) {
			goto(`/signals/${item.signal_id}`);
		}
	}

	function goToMap(item: any) {
		closeLightbox();
		const lat = item.latitude || item.exif_coordinates?.lat;
		const lon = item.longitude || item.exif_coordinates?.lon;
		if (lat && lon) goto(`/map?lat=${lat}&lon=${lon}&z=15`);
	}

	onMount(() => loadData());
</script>

<MobileSpaceTabs tabs={researchTabs} />

{#if lightboxItem}
	<!-- Lightbox overlay -->
	<div class="fixed inset-0 z-[100] bg-black/90 flex items-center justify-center" onclick={closeLightbox}>
		<button onclick={closeLightbox} class="absolute top-4 right-4 p-2 text-white/70 hover:text-white z-10">
			<X class="w-6 h-6" />
		</button>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="max-w-4xl max-h-[90vh] relative" onclick={(e) => e.stopPropagation()}>
			{#if lightboxItem._thumbnail}
				<img src={lightboxItem._thumbnail} alt={lightboxItem._title} class="max-w-full max-h-[80vh] object-contain rounded-lg" />
			{:else}
				<div class="w-96 h-64 bg-muted rounded-lg flex items-center justify-center">
					<ImageIcon class="w-16 h-16 text-muted-foreground/30" />
				</div>
			{/if}
			<div class="mt-3 flex items-center justify-between">
				<div>
					<p class="text-sm text-white">{lightboxItem._title}</p>
					{#if lightboxItem._tags?.length}
						<div class="flex flex-wrap gap-1 mt-1">
							{#each lightboxItem._tags.slice(0, 8) as tag}
								<span class="text-[9px] px-1.5 py-0.5 rounded bg-white/10 text-white/70">{tag}</span>
							{/each}
						</div>
					{/if}
				</div>
				<div class="flex gap-2">
					{#if lightboxItem.latitude || lightboxItem.exif_coordinates}
						<button onclick={() => goToMap(lightboxItem)} class="px-3 py-1.5 text-xs rounded bg-white/10 text-white hover:bg-white/20">
							<MapPin class="w-3 h-3 inline mr-1" /> Map
						</button>
					{/if}
					<button onclick={() => goToArticle(lightboxItem)} class="px-3 py-1.5 text-xs rounded bg-white/10 text-white hover:bg-white/20">
						<Eye class="w-3 h-3 inline mr-1" /> Source
					</button>
				</div>
			</div>
		</div>
	</div>
{/if}

<div class="space-y-4">
	<!-- Header -->
	<div class="flex items-center justify-between flex-wrap gap-2">
		<h1 class="text-xl font-semibold text-foreground flex items-center gap-2">
			<ImageIcon class="w-5 h-5" /> Gallery
		</h1>
		<div class="flex items-center gap-2">
			<button onclick={loadData} class="p-1.5 rounded text-muted-foreground hover:text-foreground" title="Refresh">
				<RefreshCw class="w-4 h-4" />
			</button>
		</div>
	</div>

	<!-- View tabs + filters -->
	<div class="flex items-center gap-2 flex-wrap">
		<!-- View mode -->
		{#each [
			{ id: 'all', label: 'All', icon: ImageIcon },
			{ id: 'news', label: 'News', icon: Newspaper },
			{ id: 'personal', label: 'My Photos', icon: Camera },
			{ id: 'intel', label: 'Intelligence', icon: Shield },
		] as v}
			<button
				onclick={() => view = v.id as any}
				class="flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg transition-colors
					{view === v.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground hover:text-foreground'}"
			>
				<svelte:component this={v.icon} class="w-3.5 h-3.5" />
				{v.label}
			</button>
		{/each}

		<!-- Separator -->
		<div class="w-px h-5 bg-border mx-1"></div>

		<!-- Search -->
		<div class="flex items-center gap-1 px-2 py-1 rounded-lg bg-muted border border-border/50">
			<Search class="w-3.5 h-3.5 text-muted-foreground" />
			<input type="text" bind:value={searchQuery} placeholder="Search images..."
				class="bg-transparent text-xs text-foreground outline-none w-32" />
			{#if searchQuery}
				<button onclick={() => searchQuery = ''} class="text-muted-foreground hover:text-foreground">
					<X class="w-3 h-3" />
				</button>
			{/if}
		</div>

		<!-- Tag filter -->
		{#if allTags.length > 0}
			<div class="flex items-center gap-1">
				<Tag class="w-3.5 h-3.5 text-muted-foreground" />
				<select bind:value={tagFilter}
					class="text-xs bg-muted border border-border/50 rounded px-2 py-1 text-foreground">
					<option value="">All tags</option>
					{#each allTags as tag}
						<option value={tag}>{tag}</option>
					{/each}
				</select>
			</div>
		{/if}
	</div>

	<!-- Count -->
	<p class="text-xs text-muted-foreground">
		{filteredItems.length} items
		{#if view !== 'all'} in {view}{/if}
		{#if tagFilter} tagged "{tagFilter}"{/if}
	</p>

	{#if loading}
		<div class="text-center py-12 text-muted-foreground">
			<ImageIcon class="w-8 h-8 mx-auto mb-2 animate-pulse" />
			<p class="text-sm">Loading gallery...</p>
		</div>
	{:else if filteredItems.length === 0}
		<div class="text-center py-16 text-muted-foreground">
			<ImageIcon class="w-10 h-10 mx-auto mb-3 opacity-30" />
			{#if view === 'news'}
				<p class="text-sm font-medium">No news images yet</p>
				<p class="text-xs mt-1">Images appear here as articles are ingested from RSS feeds and connectors.</p>
			{:else if view === 'personal'}
				<p class="text-sm font-medium">No personal photos yet</p>
				<p class="text-xs mt-1">Connect a device to start capturing photos with GPS metadata.</p>
			{:else if view === 'intel'}
				<p class="text-sm font-medium">No intelligence images flagged</p>
				<p class="text-xs mt-1">The image analyst flags photos containing faces, IDs, license plates, and other evidence.</p>
			{:else}
				<p class="text-sm font-medium">No media items yet</p>
				<p class="text-xs mt-1">Connect a data source or pair a device to start capturing images.</p>
			{/if}
		</div>
	{:else}
		<!-- Image grid — clean, no titles, just images -->
		<div class="grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-1.5">
			{#each filteredItems as item}
				<button
					onclick={() => openLightbox(item)}
					class="aspect-square rounded-lg overflow-hidden bg-muted relative group cursor-pointer"
				>
					{#if item._thumbnail}
						<img
							src={item._thumbnail}
							alt=""
							loading="lazy"
							class="w-full h-full object-cover transition-transform group-hover:scale-105"
						/>
					{:else}
						<div class="w-full h-full flex items-center justify-center bg-gradient-to-br from-muted to-accent/20">
							<ImageIcon class="w-8 h-8 text-muted-foreground/30" />
						</div>
					{/if}

					<!-- Intelligence badge -->
					{#if item._isIntel}
						<div class="absolute top-1 left-1 w-5 h-5 rounded-full bg-red-500/80 flex items-center justify-center">
							<Shield class="w-3 h-3 text-white" />
						</div>
					{/if}

					<!-- GPS badge -->
					{#if item.latitude || item.exif_coordinates}
						<div class="absolute top-1 right-1 w-5 h-5 rounded-full bg-blue-500/80 flex items-center justify-center">
							<MapPin class="w-3 h-3 text-white" />
						</div>
					{/if}

					<!-- Hover overlay — shows classification + first tag -->
					<div class="absolute inset-0 bg-black/50 opacity-0 group-hover:opacity-100 transition-opacity flex items-end p-2">
						{#if item._classification || item._tags?.length}
							<div class="flex flex-wrap gap-0.5">
								{#if item._classification}
									<span class="text-[8px] px-1 py-0.5 rounded bg-white/20 text-white">{item._classification}</span>
								{/if}
								{#each (item._tags || []).slice(0, 2) as tag}
									<span class="text-[8px] px-1 py-0.5 rounded bg-white/10 text-white/80">{tag}</span>
								{/each}
							</div>
						{/if}
					</div>
				</button>
			{/each}
		</div>
	{/if}
</div>
