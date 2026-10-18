<script lang="ts">
	import { onMount } from 'svelte';
	import CardShell from './CardShell.svelte';
	import BookOpen from 'lucide-svelte/icons/book-open';
	import CheckCircle from 'lucide-svelte/icons/check-circle';
	import Clock from 'lucide-svelte/icons/clock';
	import XCircle from 'lucide-svelte/icons/x-circle';
	import ExternalLink from 'lucide-svelte/icons/external-link';

	interface Props {
		reference_id: number;
		size?: 'compact' | 'default' | 'detail';
		reference?: any;
	}

	let { reference_id, size = 'default', reference: prefetched }: Props = $props();

	let reference = $state<any>(prefetched ?? null);
	let loading = $state(!prefetched);
	let error = $state<string | null>(null);

	onMount(async () => {
		if (prefetched) return;
		try {
			const res = await fetch(`/api/references/${reference_id}`);
			if (res.ok) {
				reference = await res.json();
			} else {
				error = `reference ${reference_id} not found`;
			}
		} catch (e: any) {
			error = e?.message || 'fetch failed';
		} finally {
			loading = false;
		}
	});

	const statusInfo = $derived.by(() => {
		const s = reference?.acquisition_status;
		switch (s) {
			case 'acquired':    return { icon: CheckCircle, color: 'text-emerald-500', label: 'Acquired' };
			case 'ingested':    return { icon: CheckCircle, color: 'text-emerald-500', label: 'Ingested' };
			case 'pending':     return { icon: Clock,       color: 'text-amber-500',   label: 'Pending' };
			case 'unavailable': return { icon: XCircle,     color: 'text-muted-foreground', label: 'Unavailable' };
			case 'paywall':     return { icon: XCircle,     color: 'text-amber-500',   label: 'Paywalled' };
			default:            return { icon: Clock,       color: 'text-muted-foreground', label: s || 'Unknown' };
		}
	});

	const title = $derived(reference?.title || reference?.citation_text || `reference ${reference_id}`);
	const subtitle = $derived.by(() => {
		if (!reference) return '';
		const parts: string[] = [];
		if (reference.authors && reference.authors.length > 0) parts.push(reference.authors.slice(0, 2).join(', '));
		if (reference.publication_year) parts.push(String(reference.publication_year));
		if (reference.publisher) parts.push(reference.publisher);
		return parts.join(' · ');
	});

	const topLinks = $derived(
		(reference?.purchase_links || []).slice(0, 3)
	);
</script>

<CardShell
	{size}
	icon={BookOpen}
	{title}
	{subtitle}
	address={reference?.base60_address}
	href={`/reference/${reference_id}`}
	{loading}
	{error}
>
	{#snippet children()}
		{#if reference}
			<div class="flex items-center gap-3 text-xs">
				<div class="flex items-center gap-1">
					<svelte:component this={statusInfo.icon} size={12} class={statusInfo.color} />
					<span class="text-foreground">{statusInfo.label}</span>
				</div>
				<span class="text-muted-foreground">·</span>
				<span class="text-muted-foreground">via {reference.discovered_via}</span>
				{#if (reference.purchase_links || []).length > 0}
					<span class="text-muted-foreground">·</span>
					<span class="text-muted-foreground">{reference.purchase_links.length} acquisition paths</span>
				{/if}
			</div>

			{#if size !== 'compact' && topLinks.length > 0}
				<div class="mt-3 flex flex-wrap gap-1.5">
					{#each topLinks as link}
						<a
							href={link.url}
							target="_blank"
							rel="noopener noreferrer"
							class="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-muted text-[10px] text-foreground hover:bg-muted/80 transition-colors"
							title={link.cost_hint}
						>
							<ExternalLink size={10} />
							{link.vendor}
						</a>
					{/each}
				</div>
			{/if}
		{/if}
	{/snippet}
</CardShell>
