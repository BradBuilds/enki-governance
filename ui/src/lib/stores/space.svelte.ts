/**
 * Active space store. Maps any pathname to one of three spaces:
 *   world  – The 3D map (everything geospatial)
 *   watch  – Intelligence feed, topics, alerts, cases
 *   research – Knowledge graph, entities, projects, search
 *
 * Usage:
 *   import { space } from '$lib/stores/space.svelte';
 *   space.sync('/intelligence'); // sets activeSpace = 'watch'
 */

export type Space = 'world' | 'watch' | 'research' | 'system';

export interface SpaceTab {
	label: string;
	href: string;
}

const WATCH_ROUTES = [
	'/intelligence', '/watch', '/alerts', '/cases', '/timeline',
	'/dashboard', '/feed', '/chronicle', '/reports', '/remembered',
	'/envelope-feed', '/financial',
];

const RESEARCH_ROUTES = [
	'/graph', '/search', '/projects', '/contacts', '/assets',
	'/nodes', '/organize', '/gallery',
];

const WORLD_ROUTES = ['/map', '/geofences'];

const SYSTEM_ROUTES = [
	'/settings', '/connectors', '/devices', '/rules', '/ops',
	'/processing', '/signals', '/developer', '/governance',
	'/audit', '/team', '/templates', '/setup', '/import',
	'/exports', '/connect', '/files',
];

const WATCH_TABS: SpaceTab[] = [
	{ label: 'Feed', href: '/intelligence' },
	{ label: 'Topics', href: '/watch' },
	{ label: 'Alerts', href: '/alerts' },
	{ label: 'Cases', href: '/cases' },
	{ label: 'Timeline', href: '/timeline' },
];

const RESEARCH_TABS: SpaceTab[] = [
	{ label: 'Graph', href: '/graph' },
	{ label: 'Search', href: '/search' },
	{ label: 'Projects', href: '/projects' },
	{ label: 'Entities', href: '/contacts' },
	{ label: 'Assets', href: '/assets' },
];

class SpaceState {
	activeSpace: Space = $state('watch');

	/** Call from $effect when $page.url.pathname changes */
	sync(pathname: string) {
		this.activeSpace = this.spaceFromPath(pathname);
	}

	spaceFromPath(pathname: string): Space {
		if (WORLD_ROUTES.some(r => pathname.startsWith(r))) return 'world';
		if (WATCH_ROUTES.some(r => pathname.startsWith(r))) return 'watch';
		if (RESEARCH_ROUTES.some(r => pathname.startsWith(r))) return 'research';
		if (SYSTEM_ROUTES.some(r => pathname.startsWith(r))) return 'system';
		// Default: watch (dashboard/home)
		if (pathname === '/' || pathname === '') return 'watch';
		return 'system';
	}

	defaultRouteForSpace(s: Space): string {
		switch (s) {
			case 'world': return '/map';
			case 'watch': return '/intelligence';
			case 'research': return '/graph';
			case 'system': return '/settings';
		}
	}

	subTabsForSpace(s: Space): SpaceTab[] {
		switch (s) {
			case 'watch': return WATCH_TABS;
			case 'research': return RESEARCH_TABS;
			default: return [];
		}
	}

	get tabs(): SpaceTab[] {
		return this.subTabsForSpace(this.activeSpace);
	}

	isTabActive(tab: SpaceTab, pathname: string): boolean {
		if (tab.href === '/intelligence') {
			return pathname === '/intelligence' || pathname.startsWith('/intelligence/');
		}
		return pathname.startsWith(tab.href);
	}
}

export const space = new SpaceState();
