/**
 * User feed preferences — controls what content surfaces in the News Feed.
 * Persisted in localStorage under `enki:feed-prefs`.
 *
 * Category weights adjust sort order (higher = more prominent, 0 = hidden).
 * Muted/boosted entities override category weights for specific items.
 * Density controls how much detail each topic group shows.
 */

import { browser } from '$app/environment';

export type FeedDensity = 'compact' | 'grouped' | 'expanded';

export type FeedPrefs = {
	categoryWeights: Record<string, number>;
	mutedEntities: string[];
	boostedEntities: string[];
	density: FeedDensity;
	autoRefresh: boolean;
	refreshInterval: number; // seconds
	hoursWindow: number; // feed lookback
};

const KEY = 'enki:feed-prefs';

const DEFAULT_WEIGHTS: Record<string, number> = {
	'World News': 0.8,
	'Cybersecurity': 0.8,
	'Tech': 0.6,
	'AI & ML': 0.8,
	'Finance': 0.5,
	'Military & Defense': 0.7,
	'Science & Space': 0.7,
	'Environment': 0.6,
	'Health': 0.6,
	'Automotive': 0.3,
	'General': 0.4,
};

const DEFAULTS: FeedPrefs = {
	categoryWeights: { ...DEFAULT_WEIGHTS },
	mutedEntities: [],
	boostedEntities: [],
	density: 'grouped',
	autoRefresh: true,
	refreshInterval: 60,
	hoursWindow: 48,
};

function load(): FeedPrefs {
	if (!browser) return { ...DEFAULTS, categoryWeights: { ...DEFAULT_WEIGHTS } };
	try {
		const raw = localStorage.getItem(KEY);
		if (!raw) return { ...DEFAULTS, categoryWeights: { ...DEFAULT_WEIGHTS } };
		const parsed = JSON.parse(raw);
		return {
			...DEFAULTS,
			...parsed,
			categoryWeights: { ...DEFAULT_WEIGHTS, ...(parsed.categoryWeights || {}) },
		};
	} catch {
		return { ...DEFAULTS, categoryWeights: { ...DEFAULT_WEIGHTS } };
	}
}

function save(prefs: FeedPrefs) {
	if (!browser) return;
	try {
		localStorage.setItem(KEY, JSON.stringify(prefs));
	} catch {}
}

function createStore() {
	let prefs = $state<FeedPrefs>(load());

	return {
		get categoryWeights() { return prefs.categoryWeights; },
		get mutedEntities() { return prefs.mutedEntities; },
		get boostedEntities() { return prefs.boostedEntities; },
		get density() { return prefs.density; },
		get autoRefresh() { return prefs.autoRefresh; },
		get refreshInterval() { return prefs.refreshInterval; },
		get hoursWindow() { return prefs.hoursWindow; },
		get all() { return prefs; },

		setCategoryWeight(category: string, weight: number) {
			prefs = { ...prefs, categoryWeights: { ...prefs.categoryWeights, [category]: Math.max(0, Math.min(1, weight)) } };
			save(prefs);
		},

		muteEntity(name: string) {
			if (!prefs.mutedEntities.includes(name)) {
				prefs = { ...prefs, mutedEntities: [...prefs.mutedEntities, name] };
				// Remove from boosted if present
				prefs.boostedEntities = prefs.boostedEntities.filter(e => e !== name);
				save(prefs);
			}
		},

		unmuteEntity(name: string) {
			prefs = { ...prefs, mutedEntities: prefs.mutedEntities.filter(e => e !== name) };
			save(prefs);
		},

		boostEntity(name: string) {
			if (!prefs.boostedEntities.includes(name)) {
				prefs = { ...prefs, boostedEntities: [...prefs.boostedEntities, name] };
				// Remove from muted if present
				prefs.mutedEntities = prefs.mutedEntities.filter(e => e !== name);
				save(prefs);
			}
		},

		unboostEntity(name: string) {
			prefs = { ...prefs, boostedEntities: prefs.boostedEntities.filter(e => e !== name) };
			save(prefs);
		},

		setDensity(d: FeedDensity) {
			prefs = { ...prefs, density: d };
			save(prefs);
		},

		setAutoRefresh(v: boolean) {
			prefs = { ...prefs, autoRefresh: v };
			save(prefs);
		},

		setRefreshInterval(s: number) {
			prefs = { ...prefs, refreshInterval: Math.max(15, Math.min(300, s)) };
			save(prefs);
		},

		setHoursWindow(h: number) {
			prefs = { ...prefs, hoursWindow: Math.max(6, Math.min(168, h)) };
			save(prefs);
		},

		reset() {
			prefs = { ...DEFAULTS, categoryWeights: { ...DEFAULT_WEIGHTS } };
			save(prefs);
		},
	};
}

export const feedPrefs = createStore();
