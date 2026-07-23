<script lang="ts">
	import { page } from '$app/stores';
	import { screen } from '$lib/stores/screen.svelte';
	import { chat } from '$lib/stores/chat.svelte';
	import { notifications } from '$lib/stores/notifications.svelte';
	import { space } from '$lib/stores/space.svelte';
	import NavRail from './NavRail.svelte';
	import TopBar from './TopBar.svelte';
	import BottomTabBar from './BottomTabBar.svelte';
	import WorkerLiveStrip from './WorkerLiveStrip.svelte';
	import HamburgerMenu from './HamburgerMenu.svelte';
	import FloatingChat from './FloatingChat.svelte';
	import { onMount } from 'svelte';
	import type { Snippet } from 'svelte';

	interface Props {
		user: any;
		children: Snippet;
	}

	let { user, children }: Props = $props();

	const isFullScreen = $derived(space.activeSpace === 'world');
	const role = $derived(user?.identity?.role ?? user?.role ?? 'member');

	let hamburgerOpen = $state(false);

	// Sync space store + chat context when page changes
	let lastPage = '';
	$effect(() => {
		const currentPath = $page.url.pathname;
		if (currentPath !== lastPage) {
			lastPage = currentPath;
			space.sync(currentPath);
			chat.setContext({ currentPage: currentPath });
		}
	});

	// Poll for notification badges every 60s
	onMount(() => {
		async function pollBadges() {
			try {
				const res = await fetch('/api/dashboard/summary', { headers: { 'Content-Type': 'application/json' } });
				if (res.ok) {
					const data = await res.json();
					notifications.update({
						alertsNew: data?.alerts?.new ?? data?.alerts?.by_status?.new ?? 0,
						casesOpen: data?.cases?.open ?? data?.cases?.by_status?.open ?? 0,
						processingPending: data?.ldu_p_pending ?? 0,
					});
				}
			} catch { /* backend unavailable */ }
		}
		pollBadges();
		const interval = setInterval(pollBadges, 60000);
		return () => clearInterval(interval);
	});
</script>

<svelte:window bind:innerWidth={screen.width} bind:innerHeight={screen.height} />

<!-- DESKTOP: full rail + canvas + optional chat panel -->
{#if screen.isDesktop}
	<div class="flex h-screen overflow-hidden">
		<NavRail expanded={true} {role} />
		<div class="flex flex-col flex-1 overflow-hidden min-w-0">
			{#if !isFullScreen}
				<TopBar {user} onHamburger={() => {}} />
				<WorkerLiveStrip />
			{/if}
			<main class="flex-1 {isFullScreen ? 'overflow-hidden' : 'overflow-y-auto p-6'}">
				{@render children()}
			</main>
		</div>
	</div>

<!-- TABLET: icon rail + canvas -->
{:else if screen.isTablet}
	<div class="flex h-screen overflow-hidden">
		<NavRail expanded={false} {role} />
		<div class="flex flex-col flex-1 overflow-hidden min-w-0">
			{#if !isFullScreen}
				<TopBar {user} compact={false} onHamburger={() => hamburgerOpen = true} />
				<WorkerLiveStrip />
			{/if}
			<main class="flex-1 {isFullScreen ? 'overflow-hidden' : 'overflow-y-auto p-4'}">
				{@render children()}
			</main>
		</div>
	</div>

<!-- MOBILE: no rail, bottom tabs -->
{:else}
	<div class="flex flex-col h-screen overflow-hidden">
		{#if !isFullScreen}
			<TopBar {user} compact={true} onHamburger={() => hamburgerOpen = true} />
			<WorkerLiveStrip />
		{:else}
			<!-- Floating nav on full-screen mobile pages (map) -->
			<div class="absolute top-0 left-0 right-0 z-30 flex items-center justify-between px-3 pt-safe">
				<button
					onclick={() => hamburgerOpen = true}
					class="mt-2 w-8 h-8 rounded-full bg-card/90 backdrop-blur-sm border border-border shadow flex items-center justify-center"
				>
					<span class="text-xs font-bold text-primary">E</span>
				</button>
			</div>
		{/if}
		<main class="flex-1 {isFullScreen ? 'overflow-hidden' : 'overflow-y-auto p-4 pb-20'}">
			{@render children()}
		</main>
		{#if !chat.isOpen}
			<BottomTabBar />
		{/if}
	</div>
{/if}

<!-- Floating chat (all screen sizes) -->
<FloatingChat />

{#if hamburgerOpen}
	<HamburgerMenu {role} onClose={() => hamburgerOpen = false} />
{/if}
