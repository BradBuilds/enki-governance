<script lang="ts">
	import { page } from '$app/stores';
	import LayoutDashboard from 'lucide-svelte/icons/layout-dashboard';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import MessageSquare from 'lucide-svelte/icons/message-square';
	import Search from 'lucide-svelte/icons/search';
	import Boxes from 'lucide-svelte/icons/boxes';
	import CalendarDays from 'lucide-svelte/icons/calendar-days';
	import Settings from 'lucide-svelte/icons/settings';
	import ChevronsLeft from 'lucide-svelte/icons/chevrons-left';
	import ChevronsRight from 'lucide-svelte/icons/chevrons-right';

	import { onMount } from 'svelte';
	import { browser } from '$app/environment';

	// Default to collapsed
	let collapsed = $state(true);

	onMount(() => {
		const saved = localStorage.getItem('enki:sidebar-collapsed');
		// Only expand if user explicitly expanded before
		if (saved === 'false') collapsed = false;
	});

	$effect(() => {
		if (browser) {
			localStorage.setItem('enki:sidebar-collapsed', String(collapsed));
		}
	});

	const navItems = [
		{ href: '/dashboard', icon: LayoutDashboard, label: 'Dashboard' },
		{ href: '/map', icon: MapPin, label: 'Map' },
		{ href: '/chronicle', icon: CalendarDays, label: 'Chronicle' },
		{ href: '/envelope-feed', icon: Boxes, label: 'Envelope' },
		{ href: '/cases', icon: Briefcase, label: 'Cases' },
		{ href: '/search', icon: Search, label: 'Search' },
		{ href: '/chat', icon: MessageSquare, label: 'Chat' },
	];
</script>

<aside
	class="flex flex-col border-r border-border bg-card h-full transition-all duration-200
		   {collapsed ? 'w-14' : 'w-56'}"
>
	<!-- Logo -->
	<a href="/dashboard" class="flex items-center h-14 border-b border-border hover:bg-accent/20 transition-colors {collapsed ? 'justify-center' : 'px-3 gap-2'}">
		<img src="/enki-symbol.png" alt="Enki" class="h-8 shrink-0" />
		{#if !collapsed}
			<span class="font-semibold text-foreground text-sm">Enki</span>
		{/if}
	</a>

	<!-- Nav items -->
	<nav class="flex-1 flex flex-col justify-center space-y-1 {collapsed ? 'px-1.5' : 'px-2'}">
		{#each navItems as item}
			{@const active = $page.url.pathname === item.href ||
				(item.href !== '/' && $page.url.pathname.startsWith(item.href))}
			<a
				href={item.href}
				title={collapsed ? item.label : undefined}
				class="flex items-center rounded-md text-sm transition-colors
					   {collapsed ? 'justify-center p-2.5' : 'gap-3 px-3 py-2'}
					   {active
						? 'bg-accent text-accent-foreground'
						: 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'}"
			>
				<item.icon class="w-5 h-5 shrink-0" />
				{#if !collapsed}
					<span>{item.label}</span>
				{/if}
			</a>
		{/each}
	</nav>

	<!-- Bottom: settings + collapse toggle -->
	<div class="border-t border-border py-2 {collapsed ? 'px-1.5' : 'px-2'} space-y-1">
		<a
			href="/settings"
			title={collapsed ? 'Settings' : undefined}
			class="flex items-center rounded-md text-sm transition-colors
				{collapsed ? 'justify-center p-2.5' : 'gap-3 px-3 py-2'}
				{$page.url.pathname.startsWith('/settings')
					? 'bg-accent text-accent-foreground'
					: 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'}"
		>
			<Settings class="w-5 h-5 shrink-0" />
			{#if !collapsed}
				<span>Settings</span>
			{/if}
		</a>
		<button
			onclick={() => collapsed = !collapsed}
			title={collapsed ? 'Expand' : 'Collapse'}
			class="flex items-center rounded-md text-sm text-muted-foreground hover:bg-accent/50 w-full transition-colors
				{collapsed ? 'justify-center p-2.5' : 'gap-3 px-3 py-2'}"
		>
			{#if collapsed}
				<ChevronsRight class="w-5 h-5 shrink-0" />
			{:else}
				<ChevronsLeft class="w-5 h-5 shrink-0" />
				<span>Collapse</span>
			{/if}
		</button>
	</div>
</aside>
