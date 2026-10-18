/**
 * Svelte action: swipe to dismiss.
 * Usage: <div use:swipeDismiss={{ onDismiss, direction: 'vertical' }}>
 */

interface SwipeDismissOptions {
	onDismiss: () => void;
	direction?: 'vertical' | 'horizontal';
	threshold?: number;
}

export function swipeDismiss(node: HTMLElement, options: SwipeDismissOptions) {
	let startX = 0;
	let startY = 0;
	let currentDelta = 0;
	let dragging = false;
	const threshold = options.threshold ?? 100;
	const isVertical = (options.direction ?? 'vertical') === 'vertical';

	function onTouchStart(e: TouchEvent) {
		startX = e.touches[0].clientX;
		startY = e.touches[0].clientY;
		currentDelta = 0;
		dragging = true;
		node.style.transition = 'none';
	}

	function onTouchMove(e: TouchEvent) {
		if (!dragging) return;
		const dx = e.touches[0].clientX - startX;
		const dy = e.touches[0].clientY - startY;
		currentDelta = isVertical ? Math.max(0, dy) : Math.max(0, dx);

		if (currentDelta > 10) {
			const prop = isVertical ? 'translateY' : 'translateX';
			node.style.transform = `${prop}(${currentDelta}px)`;
			node.style.opacity = String(Math.max(0.3, 1 - currentDelta / (threshold * 3)));
		}
	}

	function onTouchEnd() {
		if (!dragging) return;
		dragging = false;
		node.style.transition = 'transform 0.2s ease, opacity 0.2s ease';

		if (currentDelta > threshold) {
			options.onDismiss();
		} else {
			node.style.transform = '';
			node.style.opacity = '';
		}
		currentDelta = 0;
	}

	node.addEventListener('touchstart', onTouchStart, { passive: true });
	node.addEventListener('touchmove', onTouchMove, { passive: true });
	node.addEventListener('touchend', onTouchEnd, { passive: true });

	return {
		update(newOptions: SwipeDismissOptions) {
			options = newOptions;
		},
		destroy() {
			node.removeEventListener('touchstart', onTouchStart);
			node.removeEventListener('touchmove', onTouchMove);
			node.removeEventListener('touchend', onTouchEnd);
		}
	};
}
