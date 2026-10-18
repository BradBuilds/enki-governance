<script lang="ts">
	import { page } from '$app/stores';
	import { space } from '$lib/stores/space.svelte';

	const tabs = $derived(space.tabs);
</script>

{#if tabs.length > 0}
	<div class="flex items-center gap-1">
		{#each tabs as tab}
			{@const active = space.isTabActive(tab, $page.url.pathname)}
			<a
				href={tab.href}
				class="px-3 py-1.5 text-sm font-medium rounded-md transition-colors relative
					{active
						? 'text-primary'
						: 'text-muted-foreground hover:text-foreground hover:bg-muted/50'}"
			>
				{tab.label}
				{#if active}
					<span class="absolute bottom-0 left-1/2 -translate-x-1/2 w-4 h-0.5 rounded-full bg-primary"></span>
				{/if}
			</a>
		{/each}
	</div>
{/if}
