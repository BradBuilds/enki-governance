<script lang="ts">
	/**
	 * Mobile bottom navigation bar.
	 * 5 primary tabs from the F1 redesign (the most reach-friendly subset).
	 * Chat is handled by FloatingChat bubble; the rest live in HamburgerMenu.
	 */
	import { page } from '$app/stores';
	import Home from 'lucide-svelte/icons/home';
	import Library from 'lucide-svelte/icons/library';
	import Globe from 'lucide-svelte/icons/globe';
	import Rss from 'lucide-svelte/icons/rss';
	import Video from 'lucide-svelte/icons/video';

	const tabs = [
		{ href: '/hub',     icon: Home,    label: 'Hub'     },
		{ href: '/library', icon: Library, label: 'Library' },
		{ href: '/map',     icon: Globe,   label: 'Map'     },
		{ href: '/feed',    icon: Rss,     label: 'Feed'    },
		{ href: '/cams',    icon: Video,   label: 'Cams'    },
	];

	function isActive(href: string): boolean {
		const path = $page.url.pathname;
		if (href === '/hub') return path === '/hub' || path === '/';
		return path.startsWith(href);
	}
</script>

<nav class="fixed bottom-0 left-0 right-0 bg-card border-t border-border z-40 safe-bottom">
	<div class="flex items-center justify-around h-16">
		{#each tabs as tab}
			{@const active = isActive(tab.href)}
			<a
				href={tab.href}
				class="flex flex-col items-center justify-center gap-0.5 w-full h-full
					   {active ? 'text-primary' : 'text-muted-foreground'}"
			>
				<svelte:component this={tab.icon} size={20} />
				<span class="text-[10px] font-medium">{tab.label}</span>
			</a>
		{/each}
	</div>
</nav>

<style>
	.safe-bottom {
		padding-bottom: env(safe-area-inset-bottom, 0px);
	}
</style>
