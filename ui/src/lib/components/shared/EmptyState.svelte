<script lang="ts">
	import type { Component } from 'svelte';

	interface Props {
		icon?: Component;
		title: string;
		description?: string;
		action?: { label: string; href?: string; onclick?: () => void };
	}

	let { icon, title, description, action }: Props = $props();
</script>

<div class="flex flex-col items-center justify-center py-16 px-4 text-center">
	{#if icon}
		<div class="mb-4 text-muted-foreground/50">
			<svelte:component this={icon} size={48} strokeWidth={1.5} />
		</div>
	{/if}
	<h3 class="text-lg font-medium text-foreground mb-1">{title}</h3>
	{#if description}
		<p class="text-sm text-muted-foreground max-w-sm">{description}</p>
	{/if}
	{#if action}
		{#if action.onclick}
			<button onclick={action.onclick} class="mt-4 inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors">
				{action.label}
			</button>
		{:else if action.href}
			<a href={action.href} class="mt-4 inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors">
				{action.label}
			</a>
		{/if}
	{/if}
</div>
