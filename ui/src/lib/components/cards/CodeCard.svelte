<script lang="ts">
	import Copy from 'lucide-svelte/icons/copy';
	import Check from 'lucide-svelte/icons/check';

	interface Props {
		code: string;
		language?: string;
		title?: string;
		copyable?: boolean;
	}

	let { code, language = '', title, copyable = true }: Props = $props();
	let copied = $state(false);

	async function copyCode() {
		try {
			await navigator.clipboard.writeText(code);
			copied = true;
			setTimeout(() => copied = false, 2000);
		} catch { /* clipboard not available */ }
	}
</script>

<div class="rounded-lg border border-border bg-card overflow-hidden">
	{#if title || copyable}
		<div class="flex items-center justify-between px-3 py-1.5 border-b border-border/50 bg-muted/30">
			<span class="text-xs text-muted-foreground">{title || language}</span>
			{#if copyable}
				<button
					onclick={copyCode}
					class="p-1 rounded hover:bg-muted transition-colors text-muted-foreground hover:text-foreground"
					title="Copy code"
				>
					{#if copied}
						<Check size={14} class="text-emerald-400" />
					{:else}
						<Copy size={14} />
					{/if}
				</button>
			{/if}
		</div>
	{/if}
	<pre class="p-3 overflow-x-auto text-xs font-mono text-foreground/90 leading-relaxed max-h-80">{code}</pre>
</div>
