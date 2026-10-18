<script lang="ts">
	/** Image embed — lazy-loaded with lightbox on click. */
	let { data }: { data: any } = $props();
	let fullscreen = $state(false);

	const url = $derived(data?.url || '');
	const caption = $derived(data?.caption || '');
</script>

{#if url}
	<div class="relative">
		<button onclick={() => fullscreen = true} class="block w-full rounded-lg overflow-hidden border border-border hover:border-primary/30 transition-colors cursor-zoom-in">
			<img src={url} alt={caption} class="w-full max-h-64 object-cover" loading="lazy" />
		</button>
		{#if caption}
			<span class="absolute bottom-2 left-2 text-[10px] bg-black/60 text-white px-2 py-0.5 rounded">{caption}</span>
		{/if}
	</div>

	<!-- Lightbox -->
	{#if fullscreen}
		<button
			onclick={() => fullscreen = false}
			class="fixed inset-0 z-50 bg-black/90 flex items-center justify-center cursor-zoom-out"
		>
			<img src={url} alt={caption} class="max-w-[90vw] max-h-[90vh] object-contain" />
			<span class="absolute top-4 right-4 text-white/60 text-sm">Click to close</span>
		</button>
	{/if}
{/if}
