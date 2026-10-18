/**
 * Card Preferences Store — localStorage-backed per-signal-type field preferences.
 *
 * Tracks which fields users have pinned (always show at top) or hidden
 * (never show on card). Persists across sessions.
 *
 * These are CLIENT-SIDE preferences that overlay the server-side template
 * column visibility. The server template handles "what fields exist and
 * their defaults." This store handles "what this user wants to see."
 */

import { writable } from 'svelte/store';

interface TypePreferences {
	pinned: string[];  // field paths pinned to top
	hidden: string[];  // field paths hidden from card
}

type AllPreferences = Record<string, TypePreferences>;

const STORAGE_KEY = 'enki_card_preferences';

function loadPreferences(): AllPreferences {
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		return raw ? JSON.parse(raw) : {};
	} catch {
		return {};
	}
}

function savePreferences(prefs: AllPreferences) {
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
	} catch {
		// localStorage full or unavailable — silent fail
	}
}

const { subscribe, update, set } = writable<AllPreferences>(loadPreferences());

// Auto-save on every change
subscribe((prefs) => {
	savePreferences(prefs);
});

export const cardPreferences = { subscribe, set, update };

/**
 * Get preferences for a signal type.
 */
export function getTypePrefs(prefs: AllPreferences, signalType: string): TypePreferences {
	return prefs[signalType] || { pinned: [], hidden: [] };
}

/**
 * Toggle a field as pinned for a signal type.
 */
export function togglePin(signalType: string, fieldPath: string) {
	cardPreferences.update((prefs) => {
		const tp = prefs[signalType] || { pinned: [], hidden: [] };
		const idx = tp.pinned.indexOf(fieldPath);
		if (idx >= 0) {
			tp.pinned.splice(idx, 1);
		} else {
			tp.pinned.push(fieldPath);
			// Unpin removes from hidden too
			const hidIdx = tp.hidden.indexOf(fieldPath);
			if (hidIdx >= 0) tp.hidden.splice(hidIdx, 1);
		}
		prefs[signalType] = tp;
		return { ...prefs };
	});
}

/**
 * Toggle a field as hidden for a signal type.
 */
export function toggleHide(signalType: string, fieldPath: string) {
	cardPreferences.update((prefs) => {
		const tp = prefs[signalType] || { pinned: [], hidden: [] };
		const idx = tp.hidden.indexOf(fieldPath);
		if (idx >= 0) {
			tp.hidden.splice(idx, 1);
		} else {
			tp.hidden.push(fieldPath);
			// Hide removes from pinned too
			const pinIdx = tp.pinned.indexOf(fieldPath);
			if (pinIdx >= 0) tp.pinned.splice(pinIdx, 1);
		}
		prefs[signalType] = tp;
		return { ...prefs };
	});
}

/**
 * Apply preferences to a field list — sort pinned to top, remove hidden.
 */
export function applyPreferences(
	fields: Array<{ label: string; path?: string; [key: string]: any }>,
	prefs: TypePreferences,
): Array<{ label: string; path?: string; [key: string]: any }> {
	const pinnedSet = new Set(prefs.pinned);
	const hiddenSet = new Set(prefs.hidden);

	return fields
		.filter((f) => !f.path || !hiddenSet.has(f.path))
		.sort((a, b) => {
			const aPin = a.path && pinnedSet.has(a.path) ? 0 : 1;
			const bPin = b.path && pinnedSet.has(b.path) ? 0 : 1;
			return aPin - bPin;
		});
}
