<script lang="ts">
	// Clean, reusable chat surface = message list + input. No thread-management
	// chrome. Mounted by the bubble (ChatPanel), the /chat page, and the library
	// Chat tab — one conversation UI, one 62-action brain, everywhere.
	import { tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { dispatchMapCommand } from '$lib/stores/mapCommands';
	import type { ChatMessage } from '$lib/stores/chat.svelte';
	import MessageList from './MessageList.svelte';
	import Send from 'lucide-svelte/icons/send';

	interface Props {
		messages: ChatMessage[];
		sending?: boolean;
		placeholder?: string;
		/** Send handler — returns the raw response (for map-command dispatch). */
		onSend: (text: string) => Promise<any>;
	}
	let { messages, sending = false, placeholder = 'Ask Enki anything…', onSend }: Props = $props();

	let input = $state('');
	let scrollEl: HTMLElement;

	async function scrollToBottom() {
		await tick();
		if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
	}

	// Auto-scroll when new messages arrive.
	$effect(() => {
		void messages.length;
		scrollToBottom();
	});

	async function submit() {
		const text = input.trim();
		if (!text || sending) return;
		input = '';
		const data = await onSend(text);
		await scrollToBottom();

		// Map commands from action results (navigate/search/geofence/highlight).
		for (const action of data?.action_results || []) {
			const aType = action.action || action.type;
			const aData = action.data || {};
			if (aType === 'navigate_to') {
				dispatchMapCommand({ type: 'navigate', lat: aData.lat, lon: aData.lon, zoom: aData.zoom || 12, label: aData.label });
				goto('/map');
			} else if (aType === 'search_nearby') {
				dispatchMapCommand({ type: 'search_nearby', lat: aData.lat, lon: aData.lon, radius_km: aData.radius_km, signals: aData.signals });
				goto('/map');
			} else if (aType === 'create_geofence') {
				dispatchMapCommand({ type: 'geofence_created', lat: aData.lat, lon: aData.lon, radius_km: aData.radius_km, name: aData.name });
				goto('/map');
			} else if (aType === 'highlight_area') {
				dispatchMapCommand({ type: 'highlight', west: aData.west, south: aData.south, east: aData.east, north: aData.north, label: aData.label, color: aData.color });
				goto('/map');
			}
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			submit();
		}
	}
</script>

<div class="flex flex-col h-full min-h-0">
	<div bind:this={scrollEl} class="flex-1 overflow-y-auto p-4 space-y-4">
		<MessageList {messages} {sending} />
	</div>

	<div class="border-t border-border p-3 shrink-0">
		<div class="flex items-end gap-2">
			<textarea
				bind:value={input}
				onkeydown={handleKeydown}
				{placeholder}
				rows="1"
				class="flex-1 resize-none bg-muted rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary max-h-32"
			></textarea>
			<button
				onclick={submit}
				disabled={!input.trim() || sending}
				class="p-2 rounded-lg bg-primary text-primary-foreground shrink-0 hover:bg-primary/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
			>
				<Send size={16} />
			</button>
		</div>
	</div>
</div>
