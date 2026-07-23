<script lang="ts">
	/**
	 * Horizontally scrollable pill tabs for mobile space sub-navigation.
	 * Renders at the top of each page within a space (Watch or Research).
	 * Hidden on desktop/tablet where SpaceTabs in TopBar handles this.
	 */
	import { page } from '$app/stores';
	import { screen } from '$lib/stores/screen.svelte';

	interface Tab {
		label: string;
		href: string;
	}

	interface Props {
		tabs: Tab[];
	}

	let { tabs }: Props = $props();

	function isActive(href: string): boolean {
		const path = $page.url.pathname;
		if (href === '/intelligence') {
			return path === '/intelligence' || path.startsWith('/intelligence/');
		}
		return path.startsWith(href);
	}
</script>

{#if screen.isMobile}
	<div class="flex items-center gap-1.5 px-4 py-2 overflow-x-auto scrollbar-none sticky top-0 z-10 bg-background/95 backdrop-blur-sm border-b border-border/50">
		{#each tabs as tab}
			{@const active = isActive(tab.href)}
			<a
				href={tab.href}
				class="shrink-0 px-3 py-1.5 text-xs font-medium rounded-full transition-colors
					{active
						? 'bg-primary/10 text-primary'
						: 'bg-muted/50 text-muted-foreground hover:text-foreground'}"
			>
				{tab.label}
			</a>
		{/each}
	</div>
{/if}

<style>
	.scrollbar-none {
		-ms-overflow-style: none;
		scrollbar-width: none;
	}
	.scrollbar-none::-webkit-scrollbar {
		display: none;
	}
</style>
