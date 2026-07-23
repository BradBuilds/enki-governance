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

	let { mode, onClose, title = 'Filters', children }: Props = $props();
</script>

{#if mode === 'inline'}
	<div class="w-[280px] border-r border-border bg-card overflow-y-auto flex flex-col shrink-0">
		<div class="flex items-center justify-between px-4 py-3 border-b border-border sticky top-0 bg-card z-10">
			<h3 class="text-sm font-medium text-foreground">{title}</h3>
			<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground"><X size={16} /></button>
		</div>
		<div class="flex-1 overflow-y-auto p-4">
			{@render children()}
		</div>
	</div>
{:else if mode === 'overlay'}
	<button class="fixed inset-0 z-40 bg-black/40" onclick={onClose} aria-label="Close"></button>
	<div class="fixed left-0 top-0 h-full w-[320px] max-w-[85vw] z-50 bg-card border-r border-border shadow-xl flex flex-col animate-slide-in-left">
		<div class="flex items-center justify-between px-4 py-3 border-b border-border">
			<h3 class="text-sm font-medium text-foreground">{title}</h3>
			<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground"><X size={16} /></button>
		</div>
		<div class="flex-1 overflow-y-auto p-4">
			{@render children()}
		</div>
	</div>
{:else}
	<div
		class="fixed inset-0 z-50 bg-background flex flex-col"
		use:swipeDismiss={{ onDismiss: onClose, direction: 'horizontal' }}
	>
		<div class="flex items-center gap-2 px-4 py-3 border-b border-border pt-safe">
			<button onclick={onClose} class="p-1 rounded hover:bg-muted text-muted-foreground"><ArrowLeft size={20} /></button>
			<h3 class="text-sm font-medium text-foreground">{title}</h3>
		</div>
		<div class="flex-1 overflow-y-auto p-4">
			{@render children()}
		</div>
	</div>
{/if}

<style>
	@keyframes slide-in-left {
		from { transform: translateX(-100%); }
		to { transform: translateX(0); }
	}
	.animate-slide-in-left {
		animation: slide-in-left 0.2s ease-out;
	}
</style>
