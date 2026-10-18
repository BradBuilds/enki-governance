<script lang="ts">
	import { goto } from '$app/navigation';
	import Send from 'lucide-svelte/icons/send';
	import Paperclip from 'lucide-svelte/icons/paperclip';
	import Mic from 'lucide-svelte/icons/mic';
	import Loader2 from 'lucide-svelte/icons/loader-2';
	import { api } from '$lib/api/mock';

	let message = $state('');
	let sending = $state(false);
	let inlineResponse = $state<string | null>(null);

	async function handleSubmit() {
		if (!message.trim() || sending) return;
		const msg = message.trim();
		message = '';
		sending = true;
		inlineResponse = null;
		try {
			const res: any = await api.chat(msg);
			if (res.response) {
				inlineResponse = res.response;
				setTimeout(() => inlineResponse = null, 5000);
			}
			goto(`/chat?q=${encodeURIComponent(msg)}`);
		} catch {
			goto(`/chat?q=${encodeURIComponent(msg)}`);
		}
		sending = false;
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			handleSubmit();
		}
	}
</script>

<div class="border-t border-border bg-card px-4 py-2 shrink-0">
	<div class="flex items-center gap-2 max-w-4xl mx-auto">
		<button class="p-2 text-muted-foreground hover:text-foreground rounded-md" aria-label="Attach file">
			<Paperclip class="w-5 h-5" />
		</button>
		<input
			type="text"
			bind:value={message}
			onkeydown={handleKeydown}
			placeholder="Ask Enki anything..."
			disabled={sending}
			class="flex-1 bg-muted rounded-md px-3 py-2 text-sm text-foreground
				   placeholder:text-muted-foreground outline-none
				   focus:ring-2 focus:ring-ring disabled:opacity-50"
		/>
		<button class="p-2 text-muted-foreground hover:text-foreground rounded-md" aria-label="Voice input">
			<Mic class="w-5 h-5" />
		</button>
		<button
			onclick={handleSubmit}
			disabled={sending}
			class="p-2 rounded-md transition-colors
				   {sending ? 'text-muted-foreground animate-pulse' : message.trim() ? 'text-primary hover:bg-primary/10' : 'text-muted-foreground'}"
			aria-label="Send"
		>
			{#if sending}
				<Loader2 class="w-5 h-5 animate-spin" />
			{:else}
				<Send class="w-5 h-5" />
			{/if}
		</button>
	</div>
	{#if inlineResponse}
		<div class="max-w-4xl mx-auto mt-1 px-2">
			<p class="text-xs text-muted-foreground truncate">{inlineResponse}</p>
		</div>
	{/if}
</div>
