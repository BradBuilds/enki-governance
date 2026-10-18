<script lang="ts">
	import { goto } from '$app/navigation';
	import Camera from 'lucide-svelte/icons/camera';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import ImageIcon from 'lucide-svelte/icons/image';
	import X from 'lucide-svelte/icons/x';

	interface Props {
		photos: any[];
		count?: number;
	}

	let { photos, count = 0 }: Props = $props();
	let lightbox = $state<any>(null);

	function showOnMap() {
		// Find first photo with GPS and navigate there
		const geo = photos.find(p => p.lat && p.lon);
		if (geo) goto(`/map?lat=${geo.lat}&lon=${geo.lon}&z=14`);
	}
</script>

{#if lightbox}
	<div class="fixed inset-0 z-[100] bg-black/90 flex items-center justify-center" onclick={() => lightbox = null}>
		<button onclick={() => lightbox = null} class="absolute top-4 right-4 p-2 text-white/70 hover:text-white">
			<X class="w-6 h-6" />
		</button>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="max-w-3xl max-h-[85vh]" onclick={(e) => e.stopPropagation()}>
			{#if lightbox.thumbnail_url}
				<img src={lightbox.thumbnail_url} alt="" class="max-w-full max-h-[75vh] object-contain rounded-lg" />
			{/if}
			<div class="mt-2 text-xs text-white/80">
				{#if lightbox.description}<p>{lightbox.description}</p>{/if}
				{#if lightbox.tags?.length}
					<div class="flex flex-wrap gap-1 mt-1">
						{#each lightbox.tags.slice(0, 6) as tag}
							<span class="px-1.5 py-0.5 rounded bg-white/10 text-[9px]">{tag}</span>
						{/each}
					</div>
				{/if}
			</div>
		</div>
	</div>
{/if}

<div class="rounded-lg border bg-card p-3">
	<div class="flex items-center justify-between mb-2">
		<div class="flex items-center gap-2">
			<Camera size={14} class="text-purple-400" />
			<span class="text-xs font-medium text-muted-foreground">{count || photos.length} photos</span>
		</div>
		{#if photos.some(p => p.lat && p.lon)}
			<button onclick={showOnMap} class="flex items-center gap-1 text-[10px] px-2 py-1 rounded bg-blue-500/10 text-blue-400 hover:bg-blue-500/20">
				<MapPin class="w-3 h-3" /> Map
			</button>
		{/if}
	</div>

	{#if photos.length === 0}
		<p class="text-xs text-muted-foreground py-2">No photos found for this query.</p>
	{:else}
		<div class="grid grid-cols-3 gap-1.5">
			{#each photos.slice(0, 9) as photo}
				<button
					onclick={() => lightbox = photo}
					class="aspect-square rounded overflow-hidden bg-muted relative group cursor-pointer"
				>
					{#if photo.thumbnail_url}
						<img src={photo.thumbnail_url} alt="" loading="lazy"
							class="w-full h-full object-cover transition-transform group-hover:scale-105" />
					{:else}
						<div class="w-full h-full flex items-center justify-center">
							<ImageIcon class="w-6 h-6 text-muted-foreground/30" />
						</div>
					{/if}
					{#if photo.lat && photo.lon}
						<span class="absolute top-0.5 right-0.5 w-3.5 h-3.5 rounded-full bg-blue-500/70 flex items-center justify-center">
							<MapPin class="w-2 h-2 text-white" />
						</span>
					{/if}
				</button>
			{/each}
		</div>
		{#if (count || photos.length) > 9}
			<a href="/gallery" class="block text-center text-[10px] text-primary mt-2 hover:underline">
				View all {count || photos.length} in Gallery
			</a>
		{/if}
	{/if}
</div>
