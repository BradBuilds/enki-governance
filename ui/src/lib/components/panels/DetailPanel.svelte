<script lang="ts">
	import { swipeDismiss } from '$lib/actions/swipeDismiss';
	import X from 'lucide-svelte/icons/x';
	import ArrowLeft from 'lucide-svelte/icons/arrow-left';
	import type { Snippet } from 'svelte';

	interface Props {
		mode: 'inline' | 'overlay' | 'fullscreen';
		onClose: () => void;
		title?: string;
		children: Snippet;
	}

	let { mode, onClose, title, children }: Props = $props();
</script>

{#if mode === 'inline'}
	<div class="w-[350px] border-l border-border bg-card overflow-y-auto flex flex-col shrink-0">
		<div class="flex items-center justify-between px-4 py-3 border-b border-border sticky top-0 bg-card z-10">
			{#if title}<h3 class="text-sm font-medium text-foreground truncate">{title}</h3>{/if}
			<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground"><X size={16} /></button>
		</div>
		<div class="flex-1 overflow-y-auto p-4">
			{@render children()}
		</div>
	</div>
{:else if mode === 'overlay'}
	<!-- Backdrop -->
	<button class="fixed inset-0 z-40 bg-black/40" onclick={onClose} aria-label="Close"></button>
	<!-- Panel -->
	<div class="fixed right-0 top-0 h-full w-[400px] max-w-[90vw] z-50 bg-card border-l border-border shadow-xl flex flex-col animate-slide-in-right">
		<div class="flex items-center justify-between px-4 py-3 border-b border-border">
			{#if title}<h3 class="text-sm font-medium text-foreground truncate">{title}</h3>{/if}
			<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground"><X size={16} /></button>
		</div>
		<div class="flex-1 overflow-y-auto p-4">
			{@render children()}
		</div>
	</div>
{:else}
	<!-- Fullscreen -->
	<div
		class="fixed inset-0 z-50 bg-background flex flex-col"
		use:swipeDismiss={{ onDismiss: onClose, direction: 'horizontal' }}
	>
		<div class="flex items-center gap-2 px-4 py-3 border-b border-border pt-safe">
			<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground"><ArrowLeft size={20} /></button>
			{#if title}<h3 class="text-sm font-medium text-foreground truncate">{title}</h3>{/if}
		</div>
		<div class="flex-1 overflow-y-auto p-4">
			{@render children()}
		</div>
	</div>
{/if}

<style>
	@keyframes slide-in-right {
		from { transform: translateX(100%); }
		to { transform: translateX(0); }
	}
	.animate-slide-in-right {
		animation: slide-in-right 0.2s ease-out;
	}
</style>
