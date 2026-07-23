<script lang="ts">
	import { toggleMode } from 'mode-watcher';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import Sun from 'lucide-svelte/icons/sun';
	import Moon from 'lucide-svelte/icons/moon';
	import Search from 'lucide-svelte/icons/search';
	import LogOut from 'lucide-svelte/icons/log-out';
	import Menu from 'lucide-svelte/icons/menu';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import { page } from '$app/stores';
	import MobileSearchOverlay from '$lib/components/layout/MobileSearchOverlay.svelte';
	import NotificationBell from '$lib/components/layout/NotificationBell.svelte';
	import SpaceTabs from '$lib/components/layout/SpaceTabs.svelte';
	import { space } from '$lib/stores/space.svelte';

	interface Props {
		user?: any;
		compact?: boolean;
		onHamburger?: () => void;
	}

	let { user, compact = false, onHamburger }: Props = $props();

	let showMobileSearch = $state(false);
	let showUserMenu = $state(false);
	let showPalette = $state(false);
	let paletteQuery = $state('');
	let paletteIndex = $state(0);

	const displayName = $derived(user?.identity?.display_name || user?.name || user?.email || 'User');
	const email = $derived(user?.identity?.metadata?.email || user?.email || '');
	const role = $derived(user?.identity?.role || 'member');
	const initial = $derived(displayName?.[0]?.toUpperCase() || 'U');

	// Breadcrumb from current page
	const pageTitle = $derived.by(() => {
		const p = $page.url.pathname;
		if (p === '/') return 'Home';
		const segment = p.slice(1).split('/')[0];
		return segment.charAt(0).toUpperCase() + segment.slice(1);
	});

	const paletteItems = [
		{ label: 'Hub',       path: '/hub',       keywords: 'home dashboard overview activity' },
		{ label: 'Library',   path: '/library',   keywords: 'research topics references books' },
		{ label: 'Map',       path: '/map',       keywords: 'globe geo location spatial events trips' },
		{ label: 'Feed',      path: '/feed',      keywords: 'signals news live stream' },
		{ label: 'Cams',      path: '/cams',      keywords: 'cameras faces plates detection' },
		{ label: 'Chat',      path: '/chat',      keywords: 'ask ai question history' },
		{ label: 'Finance',   path: '/financial', keywords: 'money transactions accounts net worth' },
		{ label: 'Chronicle', path: '/chronicle', keywords: 'daily summary rollup ledger' },
		{ label: 'Cases',     path: '/cases',     keywords: 'investigate case saved' },
		{ label: 'Files',     path: '/files',     keywords: 'documents pdfs upload' },
		{ label: 'Gallery',   path: '/gallery',   keywords: 'images photos media' },
		{ label: 'Connectors',path: '/connectors',keywords: 'rss imap mqtt data source' },
		{ label: 'Devices',   path: '/admin/devices', keywords: 'phone pair qr mobile gps device' },
		{ label: 'Governance',path: '/governance',keywords: 'admin queue review' },
		{ label: 'Settings',  path: '/settings',  keywords: 'preferences theme ai config' },
	];

	const filteredItems = $derived(
		paletteQuery.trim()
			? paletteItems.filter(i =>
				(i.label + ' ' + i.keywords).toLowerCase().includes(paletteQuery.toLowerCase())
			)
			: paletteItems
	);

	function handleKeydown(e: KeyboardEvent) {
		if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
			e.preventDefault();
			showPalette = !showPalette;
			paletteQuery = '';
			paletteIndex = 0;
		}
		if (e.key === 'Escape' && showPalette) {
			showPalette = false;
		}
	}

	function handlePaletteKeydown(e: KeyboardEvent) {
		if (e.key === 'ArrowDown') { e.preventDefault(); paletteIndex = Math.min(paletteIndex + 1, filteredItems.length - 1); }
		else if (e.key === 'ArrowUp') { e.preventDefault(); paletteIndex = Math.max(paletteIndex - 1, 0); }
		else if (e.key === 'Enter' && filteredItems[paletteIndex]) { e.preventDefault(); selectPaletteItem(filteredItems[paletteIndex]); }
	}

	function selectPaletteItem(item: typeof paletteItems[0]) {
		showPalette = false;
		if (paletteQuery.trim() && !paletteItems.some(i => i.label.toLowerCase() === paletteQuery.toLowerCase())) {
			goto(`/chat?q=${encodeURIComponent(paletteQuery)}`);
		} else {
			goto(item.path);
		}
	}

	onMount(() => {
		window.addEventListener('keydown', handleKeydown);
		return () => window.removeEventListener('keydown', handleKeydown);
	});
</script>

<header class="flex items-center justify-between h-14 px-4 border-b border-border bg-card shrink-0">
	{#if compact}
		<!-- MOBILE TopBar -->
		<div class="flex items-center gap-2">
			{#if onHamburger}
				<button onclick={onHamburger} class="p-2 -ml-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted">
					<Menu size={20} />
				</button>
			{/if}
			<a href="/hub" class="flex items-center gap-1.5">
				<div class="w-7 h-7 rounded-lg bg-primary/10 flex items-center justify-center">
					<span class="text-xs font-bold text-primary">E</span>
				</div>
				<span class="text-sm font-semibold text-foreground">Enki</span>
			</a>
		</div>
		<div class="flex items-center gap-1">
			<button onclick={() => showMobileSearch = true} class="p-2 rounded-lg text-muted-foreground hover:text-foreground">
				<Search size={18} />
			</button>
			<NotificationBell />
			<button
				onclick={() => showUserMenu = !showUserMenu}
				class="w-8 h-8 rounded-full bg-primary/20 flex items-center justify-center text-primary text-xs font-semibold"
			>
				{initial}
			</button>
		</div>
	{:else}
		<!-- DESKTOP/TABLET TopBar -->
		<div class="flex items-center gap-3">
			{#if space.tabs.length > 0}
				<SpaceTabs />
			{:else}
				<span class="text-sm font-medium text-foreground">{pageTitle}</span>
			{/if}
		</div>

		<!-- Center: Command palette trigger -->
		<div class="hidden md:flex items-center flex-1 max-w-md mx-4">
			<button
				onclick={() => { showPalette = true; paletteQuery = ''; paletteIndex = 0; }}
				class="flex items-center gap-2 w-full px-3 py-1.5 rounded-lg bg-muted text-muted-foreground text-sm hover:bg-muted/80 transition-colors"
			>
				<Search size={14} />
				<span>Search or ask anything...</span>
				<kbd class="ml-auto text-[10px] bg-background px-1.5 py-0.5 rounded border border-border">⌘K</kbd>
			</button>
		</div>

		<div class="flex items-center gap-1.5">
			<NotificationBell />
			<button
				onclick={toggleMode}
				class="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
				aria-label="Toggle theme"
			>
				<Sun size={18} class="block dark:hidden" />
				<Moon size={18} class="hidden dark:block" />
			</button>
			<!-- User menu -->
			<div class="relative">
				<button
					onclick={() => showUserMenu = !showUserMenu}
					class="w-8 h-8 rounded-full bg-primary/20 flex items-center justify-center text-primary text-sm font-semibold"
				>
					{initial}
				</button>
				{#if showUserMenu}
					<button class="fixed inset-0 z-40" onclick={() => showUserMenu = false} aria-label="Close menu"></button>
					<div class="absolute right-0 top-10 w-48 rounded-lg border border-border bg-card shadow-lg z-50 py-1">
						<div class="px-3 py-2 border-b border-border">
							<p class="text-sm font-medium text-foreground">{displayName}</p>
							<p class="text-xs text-muted-foreground truncate">{email}</p>
							<p class="text-[10px] text-muted-foreground/60 uppercase mt-0.5">{role}</p>
						</div>
						<a href="/settings" onclick={() => showUserMenu = false} class="block px-3 py-2 text-sm text-foreground hover:bg-muted transition-colors">Settings</a>
						<a href="/auth/sign-out" data-sveltekit-reload class="flex items-center gap-2 px-3 py-2 text-sm text-destructive hover:bg-muted transition-colors">
							<LogOut size={14} /> Sign out
						</a>
					</div>
				{/if}
			</div>
		</div>
	{/if}
</header>

{#if showMobileSearch}
	<MobileSearchOverlay onClose={() => showMobileSearch = false} />
{/if}

<!-- Command Palette -->
{#if showPalette}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="fixed inset-0 z-[100] flex items-start justify-center pt-[15vh]"
		onkeydown={handlePaletteKeydown}
		onclick={() => showPalette = false}>
		<div class="absolute inset-0 bg-black/50 backdrop-blur-sm"></div>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="relative w-full max-w-lg mx-4 rounded-xl border border-border bg-card shadow-2xl overflow-hidden"
			onclick={(e) => e.stopPropagation()}>
			<div class="flex items-center gap-3 px-4 py-3 border-b border-border">
				<Search size={18} class="text-muted-foreground shrink-0" />
				<!-- svelte-ignore a11y_autofocus -->
				<input
					type="text"
					bind:value={paletteQuery}
					autofocus
					placeholder="Search pages, or type a question for Enki..."
					class="flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground"
				/>
				<kbd class="text-[10px] bg-muted px-1.5 py-0.5 rounded border border-border text-muted-foreground">ESC</kbd>
			</div>
			<div class="max-h-80 overflow-y-auto py-2">
				{#each filteredItems as item, i}
					<button
						onclick={() => selectPaletteItem(item)}
						class="w-full flex items-center gap-3 px-4 py-2.5 text-left text-sm transition-colors
							{i === paletteIndex ? 'bg-primary/10 text-primary' : 'text-foreground hover:bg-muted/50'}"
					>
						<span class="flex-1">{item.label}</span>
						{#if i === paletteIndex}
							<span class="text-[10px] text-muted-foreground">Enter</span>
						{/if}
					</button>
				{:else}
					<button
						onclick={() => { showPalette = false; goto(`/chat?q=${encodeURIComponent(paletteQuery)}`); }}
						class="w-full flex items-center gap-3 px-4 py-3 text-left text-sm text-primary hover:bg-primary/10 transition-colors"
					>
						<MessageCircle size={16} class="shrink-0" />
						<span>Ask Enki: "{paletteQuery}"</span>
					</button>
				{/each}
			</div>
		</div>
	</div>
{/if}
