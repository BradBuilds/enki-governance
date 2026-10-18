<script lang="ts">
	import { chat } from '$lib/stores/chat.svelte';
	import MessageCircle from 'lucide-svelte/icons/message-circle';

	interface Props {
		prompt?: string;
		context?: Record<string, any>;
		label?: string;
		variant?: 'default' | 'compact' | 'icon';
	}

	let { prompt, context, label = 'Ask Enki', variant = 'default' }: Props = $props();

	function ask() {
		chat.open();
		if (context) chat.setContext(context);
		if (prompt) chat.sendMessage(prompt);
	}
</script>

{#if variant === 'icon'}
	<button onclick={ask} title={label}
		class="p-1.5 rounded-md text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors">
		<MessageCircle class="w-4 h-4" />
	</button>
{:else if variant === 'compact'}
	<button onclick={ask}
		class="flex items-center gap-1.5 px-2 py-1 rounded-md text-xs text-muted-foreground hover:text-primary hover:bg-primary/10 transition-colors">
		<MessageCircle class="w-3.5 h-3.5" />
		{label}
	</button>
{:else}
	<button onclick={ask}
		class="flex items-center gap-2 px-3 py-1.5 rounded-md text-xs text-foreground hover:bg-muted/50 transition-colors border border-border/50">
		<MessageCircle class="w-3.5 h-3.5 text-primary" />
		{label}
	</button>
{/if}
