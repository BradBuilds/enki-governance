<script lang="ts">
	/**
	 * Floating AI action button for mobile.
	 * Tap to expand into a slide-up input — same power as the desktop CommandBar.
	 * Shows above the BottomTabBar (z-30 so it doesn't fight the More sheet z-40+).
	 */
	import Send from 'lucide-svelte/icons/send';
	import Paperclip from 'lucide-svelte/icons/paperclip';
	import Mic from 'lucide-svelte/icons/mic';
	import Bot from 'lucide-svelte/icons/bot';
	import X from 'lucide-svelte/icons/x';
	import { goto } from '$app/navigation';

	let expanded = $state(false);
	let message = $state('');

	function handleSubmit() {
		if (!message.trim()) return;
		// Navigate to chat with the message as a query param
		const encoded = encodeURIComponent(message.trim());
		message = '';
		expanded = false;
		goto(`/chat?q=${encoded}`);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			handleSubmit();
		}
	}

	let inputEl: HTMLInputElement;

	function open() {
		expanded = true;
		// Focus after transition
		setTimeout(() => inputEl?.focus(), 100);
	}
</script>

{#if expanded}
	<!-- Backdrop -->
	<button
		onclick={() => expanded = false}
		class="fixed inset-0 bg-black/30 z-30"
		aria-label="Close AI input"
	></button>

	<!-- Expanded input panel -->
	<div class="fixed bottom-20 left-3 right-3 z-30 bg-card border border-border rounded-2xl shadow-2xl safe-bottom">
		<div class="flex items-center gap-2 px-3 py-1.5 border-b border-border">
			<Bot class="w-4 h-4 text-enki-purple" />
			<span class="text-xs font-medium text-foreground">Ask Enki</span>
			<button onclick={() => expanded = false} class="ml-auto p-1 rounded hover:bg-muted">
				<X class="w-4 h-4 text-muted-foreground" />
			</button>
		</div>
		<div class="flex items-center gap-2 p-3">
			<button class="p-1.5 text-muted-foreground hover:text-foreground rounded-md" aria-label="Attach file">
				<Paperclip class="w-4 h-4" />
			</button>
			<input
				bind:this={inputEl}
				type="text"
				bind:value={message}
				onkeydown={handleKeydown}
				placeholder="Search, ask, or command..."
				class="flex-1 bg-muted rounded-lg px-3 py-2 text-sm text-foreground
					   placeholder:text-muted-foreground outline-none focus:ring-2 focus:ring-ring"
			/>
			<button class="p-1.5 text-muted-foreground hover:text-foreground rounded-md" aria-label="Voice input">
				<Mic class="w-4 h-4" />
			</button>
			<button
				onclick={handleSubmit}
				class="p-1.5 rounded-md transition-colors
					   {message.trim() ? 'text-primary hover:bg-primary/10' : 'text-muted-foreground'}"
				aria-label="Send"
			>
				<Send class="w-4 h-4" />
			</button>
		</div>
		<!-- Quick actions -->
		<div class="flex gap-2 px-3 pb-3 overflow-x-auto">
			<button onclick={() => { message = 'Show recent alerts'; handleSubmit(); }}
				class="shrink-0 text-[10px] px-2.5 py-1 rounded-full bg-muted text-muted-foreground hover:text-foreground transition-colors">
				Recent alerts
			</button>
			<button onclick={() => { message = 'Search signals last 24h'; handleSubmit(); }}
				class="shrink-0 text-[10px] px-2.5 py-1 rounded-full bg-muted text-muted-foreground hover:text-foreground transition-colors">
				Signals 24h
			</button>
			<button onclick={() => { message = 'Show dashboard summary'; handleSubmit(); }}
				class="shrink-0 text-[10px] px-2.5 py-1 rounded-full bg-muted text-muted-foreground hover:text-foreground transition-colors">
				Dashboard
			</button>
			<button onclick={() => { message = 'List open cases'; handleSubmit(); }}
				class="shrink-0 text-[10px] px-2.5 py-1 rounded-full bg-muted text-muted-foreground hover:text-foreground transition-colors">
				Open cases
			</button>
		</div>
	</div>
{:else}
	<!-- FAB button -->
	<button
		onclick={open}
		class="fixed bottom-20 right-4 z-30 w-12 h-12 rounded-full bg-enki-purple text-white
			   shadow-lg shadow-enki-purple/30 flex items-center justify-center
			   hover:bg-enki-purple/90 active:scale-95 transition-all"
		aria-label="Ask Enki AI"
	>
		<Bot class="w-5 h-5" />
	</button>
{/if}

<style>
	.safe-bottom {
		padding-bottom: env(safe-area-inset-bottom, 0px);
	}
</style>
