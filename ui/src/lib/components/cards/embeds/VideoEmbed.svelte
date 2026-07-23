<script lang="ts">
	/** Video embed — HTML5 player. */
	let { data }: { data: any } = $props();

	const url = $derived(data?.url || '');
	const caption = $derived(data?.caption || '');
</script>

{#if url}
	<div class="rounded-lg border border-border overflow-hidden">
		<!-- svelte-ignore a11y_media_has_caption -->
		<video controls preload="metadata" class="w-full max-h-64">
			<source src={url} />
			Your browser does not support video playback.
		</video>
		{#if caption}
			<div class="px-3 py-1.5 text-[10px] text-muted-foreground bg-card">{caption}</div>
		{/if}
	</div>
{/if}
