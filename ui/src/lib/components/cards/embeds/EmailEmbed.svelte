<script lang="ts">
	/** Email embed — renders from/to/subject/body in a mail-like layout. */
	import Mail from 'lucide-svelte/icons/mail';

	let { data }: { data: any } = $props();

	const from = $derived(data?.from || '');
	const to = $derived(Array.isArray(data?.to) ? data.to.join(', ') : (data?.to || ''));
	const subject = $derived(data?.subject || '(no subject)');
	const body = $derived(data?.body || '');
</script>

<div class="rounded-lg border border-border bg-muted/30 overflow-hidden">
	<div class="px-3 py-2 space-y-1 border-b border-border bg-card">
		<div class="flex items-center gap-2 text-xs">
			<span class="text-muted-foreground w-10 shrink-0">From:</span>
			<span class="text-primary font-medium truncate">{from}</span>
		</div>
		<div class="flex items-center gap-2 text-xs">
			<span class="text-muted-foreground w-10 shrink-0">To:</span>
			<span class="text-foreground truncate">{to}</span>
		</div>
		<div class="flex items-center gap-2 text-xs">
			<span class="text-muted-foreground w-10 shrink-0">Subj:</span>
			<span class="text-foreground font-medium truncate">{subject}</span>
		</div>
	</div>
	{#if body}
		<div class="px-3 py-2 text-xs text-muted-foreground leading-relaxed max-h-32 overflow-y-auto whitespace-pre-wrap">
			{body}
		</div>
	{/if}
</div>
