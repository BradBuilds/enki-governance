<script lang="ts">
	import { chat } from '$lib/stores/chat.svelte';
	import { screen } from '$lib/stores/screen.svelte';
	import ChatPanel from '../panels/ChatPanel.svelte';
	import MessageCircle from 'lucide-svelte/icons/message-circle';

	// Bubble drag state
	let bubbleX = $state(0);
	let bubbleY = $state(0);
	let isDraggingBubble = $state(false);
	let dragOffset = { x: 0, y: 0 };
	let hasDragged = $state(false);
	let bubbleEl: HTMLButtonElement;

	// Window resize state
	let windowWidth = $state(25); // percentage of screen
	let isResizing = $state(false);
	let resizeStartX = 0;
	let resizeStartWidth = 0;

	// Initialize bubble position (bottom-right)
	function initBubblePos() {
		if (typeof window === 'undefined') return;
		const saved = localStorage.getItem('enki-chat-bubble-pos');
		if (saved) {
			const pos = JSON.parse(saved);
			bubbleX = Math.min(pos.x, window.innerWidth - 60);
			bubbleY = Math.min(pos.y, window.innerHeight - 60);
		} else {
			bubbleX = window.innerWidth - 72;
			bubbleY = window.innerHeight - 72;
		}
		// Restore width
		const savedW = localStorage.getItem('enki-chat-width');
		if (savedW) windowWidth = Math.max(20, Math.min(50, parseInt(savedW)));
	}

	// Save bubble position
	function saveBubblePos() {
		localStorage.setItem('enki-chat-bubble-pos', JSON.stringify({ x: bubbleX, y: bubbleY }));
	}

	// Bubble drag handlers
	function onBubbleDown(e: MouseEvent | TouchEvent) {
		isDraggingBubble = true;
		hasDragged = false;
		const clientX = 'touches' in e ? e.touches[0].clientX : e.clientX;
		const clientY = 'touches' in e ? e.touches[0].clientY : e.clientY;
		dragOffset = { x: clientX - bubbleX, y: clientY - bubbleY };
		e.preventDefault();
	}

	function onBubbleMove(e: MouseEvent | TouchEvent) {
		if (!isDraggingBubble) {
			if (isResizing) onResizeMove(e as MouseEvent);
			return;
		}
		hasDragged = true;
		const clientX = 'touches' in e ? e.touches[0].clientX : e.clientX;
		const clientY = 'touches' in e ? e.touches[0].clientY : e.clientY;
		bubbleX = Math.max(0, Math.min(window.innerWidth - 56, clientX - dragOffset.x));
		bubbleY = Math.max(0, Math.min(window.innerHeight - 56, clientY - dragOffset.y));
	}

	function onBubbleUp() {
		if (isDraggingBubble) {
			isDraggingBubble = false;
			if (hasDragged) saveBubblePos();
		}
		if (isResizing) {
			isResizing = false;
			localStorage.setItem('enki-chat-width', String(windowWidth));
		}
	}

	function onBubbleClick() {
		if (!hasDragged) chat.toggle();
	}

	// Resize handlers (drag left edge of chat window)
	function onResizeStart(e: MouseEvent) {
		isResizing = true;
		resizeStartX = e.clientX;
		resizeStartWidth = windowWidth;
		e.preventDefault();
	}

	function onResizeMove(e: MouseEvent) {
		if (!isResizing) return;
		const dx = resizeStartX - e.clientX;
		const dPct = (dx / window.innerWidth) * 100;
		windowWidth = Math.max(20, Math.min(50, resizeStartWidth + dPct));
	}

	// Mode for ChatPanel
	let chatMode = $derived<'inline' | 'overlay' | 'fullscreen'>(
		screen.isMobile ? 'fullscreen' : 'inline'
	);

	import { onMount } from 'svelte';
	onMount(() => { initBubblePos(); });
</script>

<!-- Global mouse listeners for drag/resize -->
<svelte:window
	onmousemove={onBubbleMove}
	onmouseup={onBubbleUp}
	ontouchmove={onBubbleMove}
	ontouchend={onBubbleUp}
/>

{#if chat.isOpen}
	<!-- Chat Window -->
	<div
		class="chat-window"
		class:chat-mobile={screen.isMobile}
		style:width={screen.isMobile ? '100%' : `${windowWidth}%`}
	>
		<!-- Resize handle (left edge, desktop only) -->
		{#if !screen.isMobile}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="resize-handle" onmousedown={onResizeStart}>
				<div class="resize-line"></div>
			</div>
		{/if}

		<!-- Chat content (ChatPanel owns the single header + close) -->
		<div class="chat-body">
			<ChatPanel mode={chatMode} />
		</div>
	</div>
{/if}

<!-- Floating bubble (always visible when chat is closed) -->
{#if !chat.isOpen}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<button
		bind:this={bubbleEl}
		class="chat-bubble"
		class:dragging={isDraggingBubble}
		style:left="{bubbleX}px"
		style:top="{bubbleY}px"
		onmousedown={onBubbleDown}
		ontouchstart={onBubbleDown}
		onclick={onBubbleClick}
	>
		<MessageCircle class="w-5 h-5" />
	</button>
{/if}

<style>
	/* Floating chat bubble */
	.chat-bubble {
		position: fixed;
		z-index: 9999;
		width: 48px;
		height: 48px;
		border-radius: 50%;
		background: hsl(var(--primary));
		color: hsl(var(--primary-foreground));
		display: flex;
		align-items: center;
		justify-content: center;
		box-shadow: 0 4px 16px rgba(0,0,0,0.25);
		cursor: grab;
		transition: transform 0.15s, box-shadow 0.15s;
		border: none;
		outline: none;
	}
	.chat-bubble:hover {
		transform: scale(1.08);
		box-shadow: 0 6px 24px rgba(0,0,0,0.3);
	}
	.chat-bubble.dragging {
		cursor: grabbing;
		transform: scale(1.12);
		box-shadow: 0 8px 32px rgba(0,0,0,0.35);
	}

	/* Chat window */
	.chat-window {
		position: fixed;
		top: 0;
		right: 0;
		height: 100vh;
		z-index: 9998;
		display: flex;
		flex-direction: column;
		background: hsl(var(--card));
		border-left: 1px solid hsl(var(--border));
		box-shadow: -4px 0 24px rgba(0,0,0,0.15);
		animation: chat-slide-in 0.2s ease-out;
	}
	.chat-window.chat-mobile {
		width: 100% !important;
		border-left: none;
	}

	/* Chat body */
	.chat-body {
		flex: 1;
		overflow: hidden;
		display: flex;
		flex-direction: column;
	}

	/* Resize handle */
	.resize-handle {
		position: absolute;
		left: -3px;
		top: 0;
		bottom: 0;
		width: 6px;
		cursor: col-resize;
		z-index: 10;
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.resize-handle:hover .resize-line,
	.resize-handle:active .resize-line {
		background: hsl(var(--primary) / 0.5);
		width: 3px;
	}
	.resize-line {
		width: 1px;
		height: 40px;
		background: hsl(var(--border));
		border-radius: 1px;
		transition: all 0.15s;
	}

	@keyframes chat-slide-in {
		from { transform: translateX(100%); opacity: 0; }
		to { transform: translateX(0); opacity: 1; }
	}
</style>
