/**
 * Svelte action: pull to refresh at scroll top.
 * Usage: <div use:pullRefresh={{ onRefresh }}>
 */

interface PullRefreshOptions {
	onRefresh: () => Promise<void>;
	threshold?: number;
}

export function pullRefresh(node: HTMLElement, options: PullRefreshOptions) {
	let startY = 0;
	let pulling = false;
	let refreshing = false;
	const threshold = options.threshold ?? 60;
	let indicator: HTMLElement | null = null;

	function createIndicator() {
		indicator = document.createElement('div');
		indicator.className = 'pull-refresh-indicator';
		indicator.style.cssText = `
			position: absolute; top: 0; left: 0; right: 0; height: 0;
			display: flex; align-items: center; justify-content: center;
			overflow: hidden; transition: height 0.2s ease;
			color: var(--color-muted-foreground); font-size: 0.75rem;
			z-index: 10; pointer-events: none;
		`;
		indicator.textContent = 'Pull to refresh';
		node.style.position = node.style.position || 'relative';
		node.prepend(indicator);
	}

	function onTouchStart(e: TouchEvent) {
		if (refreshing || node.scrollTop > 0) return;
		startY = e.touches[0].clientY;
		pulling = true;
		if (!indicator) createIndicator();
	}

	function onTouchMove(e: TouchEvent) {
		if (!pulling || refreshing) return;
		const dy = e.touches[0].clientY - startY;
		if (dy > 0 && node.scrollTop === 0) {
			const clamped = Math.min(dy * 0.4, threshold * 2);
			if (indicator) {
				indicator.style.height = `${clamped}px`;
				indicator.textContent = clamped >= threshold ? 'Release to refresh' : 'Pull to refresh';
			}
		}
	}

	async function onTouchEnd() {
		if (!pulling) return;
		pulling = false;

		const h = indicator ? parseFloat(indicator.style.height) : 0;
		if (h >= threshold && !refreshing) {
			refreshing = true;
			if (indicator) {
				indicator.textContent = 'Refreshing...';
				indicator.style.height = '40px';
			}
			try {
				await options.onRefresh();
			} finally {
				refreshing = false;
				if (indicator) {
					indicator.style.height = '0';
				}
			}
		} else if (indicator) {
			indicator.style.height = '0';
		}
	}

	node.addEventListener('touchstart', onTouchStart, { passive: true });
	node.addEventListener('touchmove', onTouchMove, { passive: true });
	node.addEventListener('touchend', onTouchEnd, { passive: true });

	return {
		update(newOptions: PullRefreshOptions) {
			options = newOptions;
		},
		destroy() {
			node.removeEventListener('touchstart', onTouchStart);
			node.removeEventListener('touchmove', onTouchMove);
			node.removeEventListener('touchend', onTouchEnd);
			if (indicator) indicator.remove();
		}
	};
}
