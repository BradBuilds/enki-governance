/**
 * Svelte action: long press for context menu.
 * Usage: <div use:longPress={{ onPress, duration: 500 }}>
 */

interface LongPressOptions {
	onPress: (e: TouchEvent | MouseEvent) => void;
	duration?: number;
}

export function longPress(node: HTMLElement, options: LongPressOptions) {
	let timer: ReturnType<typeof setTimeout> | null = null;
	let fired = false;
	let startX = 0;
	let startY = 0;
	const duration = options.duration ?? 500;

	function start(e: TouchEvent | MouseEvent) {
		fired = false;
		const point = 'touches' in e ? e.touches[0] : e;
		startX = point.clientX;
		startY = point.clientY;

		timer = setTimeout(() => {
			fired = true;
			options.onPress(e);
		}, duration);
	}

	function move(e: TouchEvent | MouseEvent) {
		if (!timer) return;
		const point = 'touches' in e ? e.touches[0] : e;
		const dx = Math.abs(point.clientX - startX);
		const dy = Math.abs(point.clientY - startY);
		if (dx > 10 || dy > 10) {
			cancel();
		}
	}

	function cancel() {
		if (timer) {
			clearTimeout(timer);
			timer = null;
		}
	}

	function preventClick(e: Event) {
		if (fired) {
			e.preventDefault();
			e.stopPropagation();
			fired = false;
		}
	}

	node.addEventListener('touchstart', start, { passive: true });
	node.addEventListener('mousedown', start);
	node.addEventListener('touchmove', move, { passive: true });
	node.addEventListener('mousemove', move);
	node.addEventListener('touchend', cancel, { passive: true });
	node.addEventListener('mouseup', cancel);
	node.addEventListener('click', preventClick, { capture: true });

	return {
		update(newOptions: LongPressOptions) {
			options = newOptions;
		},
		destroy() {
			cancel();
			node.removeEventListener('touchstart', start);
			node.removeEventListener('mousedown', start);
			node.removeEventListener('touchmove', move);
			node.removeEventListener('mousemove', move);
			node.removeEventListener('touchend', cancel);
			node.removeEventListener('mouseup', cancel);
			node.removeEventListener('click', preventClick, { capture: true });
		}
	};
}
