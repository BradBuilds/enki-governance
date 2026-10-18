<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { hasRole } from '$lib/utils/roles';

	import X from 'lucide-svelte/icons/x';
	import Home from 'lucide-svelte/icons/home';
	import LibraryIcon from 'lucide-svelte/icons/library';
	import Globe from 'lucide-svelte/icons/globe';
	import Rss from 'lucide-svelte/icons/rss';
	import Video from 'lucide-svelte/icons/video';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import DollarSign from 'lucide-svelte/icons/dollar-sign';
	import CalendarDays from 'lucide-svelte/icons/calendar-days';

	import Smartphone from 'lucide-svelte/icons/smartphone';
	import Plug from 'lucide-svelte/icons/plug';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import ImageIcon from 'lucide-svelte/icons/image';
	import Shield from 'lucide-svelte/icons/shield';
	import Users from 'lucide-svelte/icons/users';
	import ScrollText from 'lucide-svelte/icons/scroll-text';
	import Download from 'lucide-svelte/icons/download';
	import Code from 'lucide-svelte/icons/code';
	import Settings from 'lucide-svelte/icons/settings';
	import LogOut from 'lucide-svelte/icons/log-out';

	interface Props {
		role?: string;
		onClose: () => void;
	}

	let { role = 'member', onClose }: Props = $props();

	interface MenuItem { icon: any; label: string; route: string; requireRole?: string; }
	interface MenuSection { title: string; items: MenuItem[]; }

	// Primary work surfaces (mirrors NavRail.primaryNav)
	const primary: MenuItem[] = [
		{ icon: Home,          label: 'Hub',       route: '/hub'       },
		{ icon: LibraryIcon,   label: 'Library',   route: '/library'   },
		{ icon: Globe,         label: 'Map',       route: '/map'       },
		{ icon: Rss,           label: 'Feed',      route: '/feed'      },
		{ icon: Video,         label: 'Cams',      route: '/cams'      },
		{ icon: MessageCircle, label: 'Chat',      route: '/chat'      },
		{ icon: DollarSign,    label: 'Finance',   route: '/financial' },
		{ icon: CalendarDays,  label: 'Timeline',  route: '/timeline' },
	];

	// Admin sections (mirrors NavRail.adminSections)
	const sections: MenuSection[] = [
		{
			title: 'Sources',
			items: [
				{ icon: Plug,       label: 'Connectors', route: '/connectors' },
				{ icon: Smartphone, label: 'Devices',    route: '/admin/devices' },
				{ icon: FolderOpen, label: 'Files',      route: '/files'      },
				{ icon: ImageIcon,  label: 'Gallery',    route: '/gallery'    },
			]
		},
		{
			title: 'Governance',
			items: [
				{ icon: Shield,     label: 'Governance', route: '/governance', requireRole: 'admin' },
				{ icon: Shield,     label: 'Validation', route: '/validation', requireRole: 'admin' },
				{ icon: ScrollText, label: 'Audit',      route: '/audit',      requireRole: 'admin' },
				{ icon: Download,   label: 'Exports',    route: '/exports'     },
			]
		},
		{
			title: 'System',
			items: [
				{ icon: Users,    label: 'Settings',  route: '/settings'  },
				{ icon: Code,     label: 'Developer', route: '/developer' },
			]
		},
	];

	function navigate(route: string) {
		onClose();
		goto(route);
	}
</script>

<div class="fixed inset-0 z-50 bg-background flex flex-col">
	<!-- Header -->
	<div class="flex items-center justify-between px-4 py-3 border-b border-border pt-safe">
		<div class="flex items-center gap-2">
			<div class="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
				<span class="text-sm font-bold text-primary">E</span>
			</div>
			<span class="text-sm font-semibold text-foreground">Enki</span>
		</div>
		<button onclick={onClose} class="p-2 rounded-lg hover:bg-muted text-muted-foreground">
			<X size={20} />
		</button>
	</div>

	<!-- Menu items -->
	<div class="flex-1 overflow-y-auto py-2">
		<!-- Primary -->
		<div class="px-4 pt-2 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/50">
			Workspaces
		</div>
		{#each primary as item}
			{@const active = $page.url.pathname.startsWith(item.route)}
			<button
				onclick={() => navigate(item.route)}
				class="flex items-center gap-3 w-full px-4 py-3 text-left transition-colors
					{active ? 'bg-primary/10 text-primary' : 'text-foreground hover:bg-muted'}"
			>
				<svelte:component this={item.icon} size={20} />
				<span class="text-sm font-semibold">{item.label}</span>
			</button>
		{/each}

		<div class="mx-4 my-2 border-t border-border/30"></div>

		{#each sections as section}
			<div class="px-4 pt-4 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/50">
				{section.title}
			</div>
			{#each section.items as item}
				{#if !item.requireRole || hasRole(role, item.requireRole)}
					{@const active = $page.url.pathname.startsWith(item.route)}
					<button
						onclick={() => navigate(item.route)}
						class="flex items-center gap-3 w-full px-4 py-3 text-left transition-colors
							{active ? 'bg-primary/10 text-primary' : 'text-foreground hover:bg-muted'}"
					>
						<svelte:component this={item.icon} size={20} />
						<span class="text-sm">{item.label}</span>
					</button>
				{/if}
			{/each}
		{/each}
	</div>

	<!-- Sign out -->
	<div class="border-t border-border p-2 safe-bottom">
		<button
			onclick={() => navigate('/auth/sign-out')}
			class="flex items-center gap-3 w-full px-4 py-3 text-left text-muted-foreground hover:text-foreground hover:bg-muted rounded-lg transition-colors"
		>
			<LogOut size={20} />
			<span class="text-sm">Sign Out</span>
		</button>
	</div>
</div>

<style>
	.safe-bottom { padding-bottom: env(safe-area-inset-bottom, 0px); }
	.pt-safe { padding-top: env(safe-area-inset-top, 0px); }
</style>
