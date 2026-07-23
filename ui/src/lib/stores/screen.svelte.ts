/**
 * Reactive viewport store. Bound once at root layout,
 * imported anywhere to get current breakpoint.
 *
 * Usage:
 *   import { screen } from '$lib/stores/screen.svelte';
 *   if (screen.isMobile) { ... }
 */

class ScreenState {
	width = $state(1280);
	height = $state(800);

	get breakpoint(): 'mobile' | 'tablet' | 'desktop' | 'wide' {
		if (this.width < 768) return 'mobile';
		if (this.width < 1024) return 'tablet';
		if (this.width < 1440) return 'desktop';
		return 'wide';
	}

	get isMobile() { return this.width < 768; }
	get isTablet() { return this.width >= 768 && this.width < 1024; }
	get isDesktop() { return this.width >= 1024; }
	get isWide() { return this.width >= 1440; }
	get isTouch() {
		if (typeof window === 'undefined') return false;
		return window.matchMedia('(pointer: coarse)').matches;
	}
	get orientation(): 'portrait' | 'landscape' {
		return this.height > this.width ? 'portrait' : 'landscape';
	}
}

export const screen = new ScreenState();
