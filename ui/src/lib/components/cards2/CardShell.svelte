<script lang="ts">
	import type { Snippet } from 'svelte';
	import ExternalLink from 'lucide-svelte/icons/external-link';

	interface Props {
		size?: 'compact' | 'default' | 'detail';
		href?: string;
		icon?: any;
		title: string;
		subtitle?: string;
		address?: string;
		loading?: boolean;
		error?: string | null;
		children: Snippet;
		actions?: Snippet;
	}

	let {
		size = 'default',
		href,
		icon,
		title,
		subtitle,
		address,
		loading = false,
		error = null,
		children,
		actions,
	}: Props = $props();

	const padding = $derived(
		size === 'compact' ? 'p-3' : size === 'detail' ? 'p-6' : 'p-4'
	);
	const titleSize = $derived(
		size === 'compact' ? 'text-sm' : size === 'detail' ? 'text-xl' : 'text-base'
	);
</script>

<div class="rounded-xl border border-border bg-card {padding} hover:border-primary/40 transition-colors group">
	{#if loading}
		<div class="space-y-2 animate-pulse">
			<div class="h-4 bg-muted rounded w-2/3"></div>
			<div class="h-3 bg-muted rounded w-1/2"></div>
		</div>
	{:else if error}
		<div class="text-sm text-destructive">⚠ {error}</div>
	{:else}
		<div class="flex items-start justify-between gap-3">
			<div class="flex-1 min-w-0">
				<div class="flex items-center gap-2 mb-0.5">
					{#if icon}
						<svelte:component this={icon} size={size === 'detail' ? 18 : 14} class="text-primary shrink-0" />
					{/if}
					{#if href}
						<a {href} class="font-semibold text-foreground hover:text-primary transition-colors truncate {titleSize}">
							{title}
						</a>
					{:else}
						<span class="font-semibold text-foreground truncate {titleSize}">{title}</span>
					{/if}
					{#if address}
						<span class="text-[10px] text-muted-foreground font-mono shrink-0 ml-1">{address}</span>
					{/if}
				</div>
				{#if subtitle}
					<p class="text-xs text-muted-foreground truncate">{subtitle}</p>
				{/if}
			</div>
			{#if actions}
				<div class="flex items-center gap-1 shrink-0">
					{@render actions()}
				</div>
			{:else if href}
				<a {href} class="text-muted-foreground hover:text-primary opacity-0 group-hover:opacity-100 transition-opacity shrink-0" aria-label="Open">
					<ExternalLink size={14} />
				</a>
			{/if}
		</div>

		<div class={size === 'compact' ? 'mt-2' : 'mt-3'}>
			{@render children()}
		</div>
	{/if}
</div>
