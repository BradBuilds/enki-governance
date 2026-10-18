<script lang="ts">
	import { onMount } from 'svelte';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import Sparkles from 'lucide-svelte/icons/sparkles';
	import { chat } from '$lib/stores/chat.svelte';
	import Conversation from '$lib/components/chat/Conversation.svelte';

	// Full-page view of the SAME assistant as the bubble: the 62-action thread
	// engine (quick-chat path → /api/ai/chat → process_chat_message). Replaces the
	// old standalone /api/chat/ask 8-tool page + its duplicate card renderer.

	const examples = [
		'libraries',
		'recent facts',
		'pipeline status',
		'do deep research on the James Webb telescope',
		'investigate Dana Vale',
		'what do we know about Baldwin?',
	];

	function runExample(ex: string) {
		chat.sendMessage(ex);
	}

	onMount(() => {
		chat.loadProviders();
		chat.setContext({ currentPage: '/chat' });
	});
</script>

<div class="max-w-4xl mx-auto h-[calc(100vh-7rem)] flex flex-col gap-3">
	<header class="shrink-0">
		<h1 class="text-2xl font-bold text-foreground flex items-center gap-2">
			<MessageCircle size={20} /> Chat
		</h1>
		<p class="text-sm text-muted-foreground mt-1">
			Ask anything about your data, run investigations, start deep research, and operate the platform. Responses include cards you can click into.
		</p>
	</header>

	{#if chat.currentMessages.length <= 1}
		<div class="shrink-0 rounded-xl border border-dashed border-border p-5 text-center">
			<Sparkles size={22} class="mx-auto text-muted-foreground/40 mb-2" />
			<p class="text-sm text-muted-foreground">Try one of these:</p>
			<div class="mt-3 flex flex-wrap gap-2 justify-center">
				{#each examples as ex}
					<button onclick={() => runExample(ex)}
						class="text-xs px-3 py-1.5 rounded-full bg-muted text-foreground hover:bg-primary/10 hover:text-primary transition-colors">
						{ex}
					</button>
				{/each}
			</div>
		</div>
	{/if}

	<div class="flex-1 min-h-0 rounded-xl border border-border bg-card overflow-hidden">
		<Conversation
			messages={chat.currentMessages}
			sending={chat.sending}
			onSend={(t) => chat.sendMessage(t)}
			placeholder="Ask anything… (Enter to send, Shift+Enter for newline)"
		/>
	</div>
</div>
