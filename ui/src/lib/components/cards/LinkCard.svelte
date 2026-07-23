<script lang="ts">
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Globe from 'lucide-svelte/icons/globe';

	interface Props {
		url: string;
		title: string;
		description?: string;
		favicon?: string;
		source?: string;
		timestamp?: string;
	}

	let { url, title, description, favicon, source, timestamp }: Props = $props();

	const domain = $derived.by(() => {
		try { return new URL(url).hostname; } catch { return source ?? ''; }
	});
</script>

<a
	href={url}
	target="_blank"
	rel="noopener noreferrer"
	class="block rounded-lg border border-border bg-card p-3 hover:border-primary/30 hover:bg-muted/30 transition-colors group"
>
	<div class="flex items-start gap-3">
		<div class="w-8 h-8 rounded bg-muted flex items-center justify-center shrink-0 mt-0.5">
			{#if favicon}
				<img src={favicon} alt="" class="w-4 h-4 rounded" />
			{:else}
				<Globe size={14} class="text-muted-foreground" />
			{/if}
		</div>
		<div class="flex-1 min-w-0">
			<div class="flex items-center gap-1">
				<span class="text-sm font-medium text-foreground group-hover:text-primary truncate">{title}</span>
				<ExternalLink size={12} class="text-muted-foreground shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" />
			</div>
			{#if description}
				<p class="text-xs text-muted-foreground mt-0.5 line-clamp-2">{description}</p>
			{/if}
			<div class="flex items-center gap-2 mt-1 text-[10px] text-muted-foreground/70">
				<span>{domain}</span>
				{#if timestamp}
					<span>{timestamp}</span>
				{/if}
			</div>
		</div>
	</div>
</a>
