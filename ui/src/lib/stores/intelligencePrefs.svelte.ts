/**
 * User preferences for the Intelligence surface of the dashboard.
 * Persisted in localStorage under `enki:intel-prefs`.
 *
 * streamEnabled — controls whether the Live Feed tab is visible on
 *   the dashboard. Default false: the synthesized view is shown by
 *   default, and the raw firehose only when explicitly opted in via
 *   Settings → Intelligence → Stream.
 */

import { browser } from '$app/environment';

type Prefs = {
	streamEnabled: boolean;
};

const KEY = 'enki:intel-prefs';
const DEFAULTS: Prefs = { streamEnabled: false };

function load(): Prefs {
	if (!browser) return { ...DEFAULTS };
	try {
		const raw = localStorage.getItem(KEY);
		if (!raw) return { ...DEFAULTS };
		return { ...DEFAULTS, ...JSON.parse(raw) };
	} catch {
		return { ...DEFAULTS };
	}
}

function createStore() {
	let prefs = $state<Prefs>(load());

	return {
		get streamEnabled() {
			return prefs.streamEnabled;
		},
		setStreamEnabled(v: boolean) {
			prefs = { ...prefs, streamEnabled: v };
			if (browser) {
				try {
					localStorage.setItem(KEY, JSON.stringify(prefs));
				} catch {}
			}
		},
	};
}

export const intelligencePrefs = createStore();
