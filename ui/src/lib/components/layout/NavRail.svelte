<script lang="ts">
	import { page } from '$app/stores';

	import { browser } from '$app/environment';
	import Home from 'lucide-svelte/icons/home';
	import Library from 'lucide-svelte/icons/library';
	import Globe from 'lucide-svelte/icons/globe';
	import Rss from 'lucide-svelte/icons/rss';
	import Video from 'lucide-svelte/icons/video';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import DollarSign from 'lucide-svelte/icons/dollar-sign';
	import CalendarDays from 'lucide-svelte/icons/calendar-days';
	import Settings from 'lucide-svelte/icons/settings';
	import ArrowLeft from 'lucide-svelte/icons/arrow-left';
	import Archive from 'lucide-svelte/icons/archive';
	import Search from 'lucide-svelte/icons/search';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import Package from 'lucide-svelte/icons/package';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Radar from 'lucide-svelte/icons/radar';
	import FlaskConical from 'lucide-svelte/icons/flask-conical';

	// Admin drawer icons
	import Plug from 'lucide-svelte/icons/plug';
	import Smartphone from 'lucide-svelte/icons/smartphone';
	import Shield from 'lucide-svelte/icons/shield';
	import ScrollText from 'lucide-svelte/icons/scroll-text';
	import Download from 'lucide-svelte/icons/download';
	import Users from 'lucide-svelte/icons/users';
	import Network from 'lucide-svelte/icons/network';
	import ImageIcon from 'lucide-svelte/icons/image';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import FileText from 'lucide-svelte/icons/file-text';

	interface Props {
		expanded: boolean;
		role?: string;
	}

	let { expanded: propExpanded, role = 'member' }: Props = $props();

	let hovered = $state(false);
	let adminMode = $state(false);
	// Two product modes: Research (archive + topics + feed) and Signals (live
	// physical-sensor monitoring). See the internal ui ia rethink notes.
	let mode = $state<'research' | 'signals'>(
		(browser && (localStorage.getItem('enki_mode') as any)) || 'research'
	);
	$effect(() => { if (browser) localStorage.setItem('enki_mode', mode); });
	const expanded = $derived(propExpanded ? hovered : false);

	function isActive(route: string): boolean {
		const path = $page.url.pathname;
		if (route === '/hub') return path === '/hub' || path === '/';
		return path.startsWith(route);
	}

	// Research mode — the topic spine: discover (Catalog) → read (Library/
	// Timeline) → your private Feed → ask (Chat).
	const researchNav = [
		{ icon: Home,          label: 'Hub',      route: '/hub' },
		{ icon: Globe,         label: 'Map',      route: '/map' },
		{ icon: Archive,       label: 'Catalog',  route: '/catalog' },
		{ icon: Library,       label: 'Topics',   route: '/library' },
		{ icon: CalendarDays,  label: 'Timeline', route: '/timeline' },
		{ icon: Rss,           label: 'Feed',     route: '/feed' },
		{ icon: Search,        label: 'Search',   route: '/search' },
		{ icon: MessageCircle, label: 'Chat',     route: '/chat' },
	];

	// Signals mode — live physical-sensor monitoring (the v1 OSINT surface).
	const signalsNav = [
		{ icon: Home,        label: 'Hub',      route: '/hub' },
		{ icon: Globe,       label: 'Map',      route: '/map' },
		{ icon: Video,       label: 'Cams',     route: '/cams' },
		{ icon: Briefcase,   label: 'Cases',    route: '/cases' },
		{ icon: Package,     label: 'Assets',   route: '/assets' },
		{ icon: MapPin,      label: 'Geofences', route: '/geofences' },
		{ icon: DollarSign,  label: 'Finance',  route: '/financial' },
	];

	const primaryNav = $derived(mode === 'signals' ? signalsNav : researchNav);

	// Admin drawer — everything that was a top-level route but
	// belongs under /admin/* now.
	interface AdminSection {
		title: string;
		items: { icon: any; label: string; route: string }[];
	}
	const adminSections: AdminSection[] = [
		{
			title: 'Sources',
			items: [
				{ icon: Plug,       label: 'Connectors', route: '/connectors' },
				{ icon: Smartphone, label: 'Devices',    route: '/connect' },
				{ icon: FolderOpen, label: 'Files',      route: '/files' },
				{ icon: ImageIcon,  label: 'Gallery',    route: '/gallery' },
			],
		},
		{
			title: 'Governance',
			items: [
				{ icon: Shield,     label: 'Governance', route: '/governance' },
				{ icon: ScrollText, label: 'Audit',      route: '/audit' },
				{ icon: Download,   label: 'Exports',    route: '/exports' },
			],
		},
		{
			title: 'System',
			items: [
				{ icon: Users,    label: 'Settings',   route: '/settings' },
				{ icon: Network,  label: 'Network',    route: '/admin/network' },
				{ icon: FileText, label: 'Developer',  route: '/developer' },
			],
		},
	];

	const allAdminRoutes = adminSections.flatMap(s => s.items.map(i => i.route));
	const adminActive = $derived(
		allAdminRoutes.some(r => $page.url.pathname.startsWith(r))
	);
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<nav
	class="nav-rail {expanded ? 'nav-expanded' : 'nav-collapsed'}"
	onmouseenter={() => hovered = true}
	onmouseleave={() => hovered = false}
>
	<!-- Logo -->
	<div class="flex items-center justify-center {expanded ? 'px-3 justify-start' : ''} h-12 border-b border-border/50 shrink-0">
		<img src="/enki-symbol.png" alt="Enki" class="w-7 h-8 object-contain shrink-0" />
		{#if expanded}
			<div class="ml-2 min-w-0">
				<span class="text-sm font-bold text-foreground tracking-tight">ENKI</span>
				<p class="text-[8px] text-muted-foreground/60 -mt-0.5">Command Center</p>
			</div>
		{/if}
	</div>

	{#if !adminMode}
		<!-- Primary nav -->
		<div class="flex-1 flex flex-col py-2 overflow-y-auto scrollbar-thin">
			{#each primaryNav as item}
				{@const active = isActive(item.route)}
				<a
					href={item.route}
					class="nav-item {active ? 'nav-active' : ''}"
					title={!expanded ? item.label : undefined}
				>
					<item.icon size={20} class="shrink-0" />
					{#if expanded}
						<span class="text-[13px] truncate">{item.label}</span>
					{/if}
				</a>
			{/each}
		</div>
	{:else}
		<!-- Admin drawer -->
		<div class="flex-1 py-1 overflow-y-auto scrollbar-thin">
			{#each adminSections as section}
				{#if expanded}
					<div class="px-4 pt-3 pb-1 text-[9px] font-semibold uppercase tracking-widest text-muted-foreground/40">
						{section.title}
					</div>
				{:else}
					<div class="mx-auto my-1 w-4 border-t border-border/30"></div>
				{/if}
				{#each section.items as item}
					{@const active = isActive(item.route)}
					<a
						href={item.route}
						class="nav-item {active ? 'nav-active' : ''}"
						title={!expanded ? item.label : undefined}
					>
						<item.icon size={18} class="shrink-0" />
						{#if expanded}
							<span class="text-[13px] truncate">{item.label}</span>
						{/if}
					</a>
				{/each}
			{/each}
		</div>
	{/if}

	<!-- Bottom: mode switch + admin/back toggle -->
	<div class="border-t border-border/30 p-1.5 shrink-0 space-y-1">
		{#if !adminMode}
			<button
				onclick={() => mode = mode === 'research' ? 'signals' : 'research'}
				class="nav-item w-full"
				title={!expanded ? (mode === 'research' ? 'Switch to Signals' : 'Switch to Research') : undefined}
			>
				{#if mode === 'research'}
					<Radar size={18} class="shrink-0" />
					{#if expanded}<span class="text-[13px]">Signals mode</span>{/if}
				{:else}
					<FlaskConical size={18} class="shrink-0" />
					{#if expanded}<span class="text-[13px]">Research mode</span>{/if}
				{/if}
			</button>
		{/if}
		<button
			onclick={() => adminMode = !adminMode}
			class="nav-item {adminMode ? '' : adminActive ? 'nav-active' : ''} w-full"
			title={!expanded ? (adminMode ? 'Back' : 'Admin') : undefined}
		>
			{#if adminMode}
				<ArrowLeft size={18} class="shrink-0" />
				{#if expanded}<span class="text-[13px]">Back</span>{/if}
			{:else}
				<Settings size={18} class="shrink-0" />
				{#if expanded}<span class="text-[13px]">Admin</span>{/if}
			{/if}
		</button>
	</div>
</nav>

<style>
	.nav-rail {
		height: 100%;
		background: hsl(var(--card));
		border-right: 1px solid hsl(var(--border));
		display: flex;
		flex-direction: column;
		transition: width 0.2s ease;
		shrink: 0;
		overflow: hidden;
		z-index: 50;
	}

	.nav-collapsed { width: 3.25rem; }
	.nav-expanded  { width: 13rem; }

	.nav-item {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.65rem;
		margin: 0.15rem auto;
		padding: 0.5rem;
		border-radius: 0.6rem;
		width: calc(100% - 0.7rem);
		transition: all 0.15s ease;
		position: relative;
		color: hsl(var(--muted-foreground));
		text-decoration: none;
		white-space: nowrap;
	}

	.nav-expanded .nav-item { justify-content: flex-start; }
	.nav-item:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}
	.nav-active {
		background: hsl(var(--primary) / 0.1);
		color: hsl(var(--primary));
	}
</style>
