<script lang="ts">
	import { onMount } from 'svelte';
	import Send from 'lucide-svelte/icons/send';
	import MessageSquare from 'lucide-svelte/icons/message-square';
	import Loader2 from 'lucide-svelte/icons/loader-2';
	import AlertCircle from 'lucide-svelte/icons/alert-circle';

	type Turn = { role: 'user' | 'assistant'; content: string; ts?: string };
	type AssistantState = {
		mode: 'WAITLIST' | 'INSTALL' | 'HANDOFF' | 'REJECTED';
		wizard_step: string | null;
		turns: Turn[];
		turn_count: number;
		turn_cap: number;
		turns_remaining: number;
		assistant_available: boolean;
		node_alive: boolean;
	};

	let { statusToken }: { statusToken: string } = $props();

	let chat = $state<AssistantState | null>(null);
	let messages = $state<Turn[]>([]);
	let input = $state('');
	let sending = $state(false);
	let chatError = $state<string | null>(null);
	let initialLoaded = $state(false);
	let scrollEl = $state<HTMLDivElement | null>(null);

	async function loadState() {
		try {
			const res = await fetch(`/api/assistant/state?status_token=${encodeURIComponent(statusToken)}`);
			if (!res.ok) {
				if (res.status === 404) chatError = 'Unknown status token.';
				return;
			}
			chat = await res.json();
			if (chat && !initialLoaded) {
				messages = chat.turns ?? [];
				initialLoaded = true;
				scrollToBottom();
			}
		} catch (e: any) {
			console.error('assistant state load failed', e);
		}
	}

	function scrollToBottom() {
		setTimeout(() => {
			if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
		}, 0);
	}

	async function send() {
		const msg = input.trim();
		if (!msg || sending) return;
		if (chat && chat.turns_remaining <= 0) {
			chatError = 'Chat cap reached. Open a GitHub issue if you need more help.';
			return;
		}

		sending = true;
		chatError = null;
		const userTurn: Turn = { role: 'user', content: msg, ts: new Date().toISOString() };
		messages = [...messages, userTurn];
		input = '';
		scrollToBottom();

		try {
			const res = await fetch('/api/assistant/chat', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ status_token: statusToken, message: msg })
			});

			if (!res.ok) {
				const body = await res.json().catch(() => ({}));
				if (res.status === 410) {
					chatError = 'Your node is alive — the install companion has retired. Open your dashboard.';
				} else if (res.status === 429) {
					chatError = body.detail ?? 'Try again in a minute.';
				} else if (res.status === 503) {
					chatError = body.detail ?? 'Assistant unavailable.';
				} else {
					chatError = body.detail ?? `Error ${res.status}.`;
				}
				messages = messages.slice(0, -1);
				return;
			}

			const data = await res.json();
			messages = [
				...messages,
				{ role: 'assistant', content: data.text, ts: new Date().toISOString() }
			];
			if (chat) {
				chat.turn_count = data.turn_count;
				chat.turns_remaining = data.turns_remaining;
			}
			scrollToBottom();
		} catch (e: any) {
			chatError = `Network error: ${e?.message || e}`;
			messages = messages.slice(0, -1);
		} finally {
			sending = false;
		}
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			send();
		}
	}

	onMount(() => {
		loadState();
		const t = setInterval(loadState, 15000);
		return () => clearInterval(t);
	});
</script>

{#if chat && chat.mode === 'INSTALL' && chat.assistant_available}
	<div class="rounded-xl border border-border bg-card flex flex-col h-[520px]">
		<header class="flex items-center justify-between px-4 py-3 border-b border-border">
			<div class="flex items-center gap-2">
				<MessageSquare class="w-4 h-4 text-primary" />
				<h3 class="text-sm font-semibold text-foreground">Install companion</h3>
			</div>
			<span class="text-xs text-muted-foreground">
				{chat.turns_remaining} of {chat.turn_cap} turns left
			</span>
		</header>

		<div bind:this={scrollEl} class="flex-1 overflow-y-auto px-4 py-3 space-y-3">
			{#if messages.length === 0}
				<p class="text-xs text-muted-foreground leading-relaxed">
					Ask anything about getting Enki installed — OS requirements, the curl
					command, common errors. I can only help with the install; once your
					node is alive, the full assistant lives on your dashboard.
				</p>
			{/if}
			{#each messages as turn (turn.ts ?? Math.random())}
				<div class={turn.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
					<div
						class={turn.role === 'user'
							? 'max-w-[85%] rounded-lg bg-primary text-primary-foreground px-3 py-2 text-sm whitespace-pre-wrap'
							: 'max-w-[85%] rounded-lg bg-background border border-border px-3 py-2 text-sm text-foreground whitespace-pre-wrap'}
					>
						{turn.content}
					</div>
				</div>
			{/each}
			{#if sending}
				<div class="flex justify-start">
					<div class="rounded-lg bg-background border border-border px-3 py-2 text-sm text-muted-foreground">
						<Loader2 class="w-3.5 h-3.5 inline animate-spin mr-1" />
						thinking…
					</div>
				</div>
			{/if}
		</div>

		{#if chatError}
			<div class="px-4 py-2 border-t border-amber-500/30 bg-amber-500/5 flex items-start gap-2">
				<AlertCircle class="w-3.5 h-3.5 text-amber-400 mt-0.5 flex-shrink-0" />
				<p class="text-xs text-amber-200/90 leading-relaxed">{chatError}</p>
			</div>
		{/if}

		<form
			class="flex items-center gap-2 px-3 py-3 border-t border-border"
			onsubmit={(e) => {
				e.preventDefault();
				send();
			}}
		>
			<input
				type="text"
				bind:value={input}
				onkeydown={onKey}
				disabled={sending || (chat?.turns_remaining ?? 0) <= 0}
				placeholder={chat && chat.turns_remaining > 0 ? 'Ask about the install…' : 'Chat capped.'}
				class="flex-1 rounded-lg bg-background border border-border px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary disabled:opacity-50"
			/>
			<button
				type="submit"
				disabled={sending || !input.trim() || (chat?.turns_remaining ?? 0) <= 0}
				class="inline-flex items-center justify-center w-9 h-9 rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
				aria-label="Send"
			>
				<Send class="w-4 h-4" />
			</button>
		</form>
	</div>
{:else if chat && chat.mode === 'INSTALL' && !chat.assistant_available}
	<div class="rounded-xl border border-border bg-card p-4">
		<p class="text-xs text-muted-foreground leading-relaxed">
			<AlertCircle class="w-3.5 h-3.5 text-amber-400 inline mr-1" />
			Assistant is temporarily unavailable. The install command above still works —
			follow it as-is and open a GitHub issue if you get stuck.
		</p>
	</div>
{/if}
