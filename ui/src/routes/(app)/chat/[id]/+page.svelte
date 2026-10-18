<script lang="ts">
	/**
	 * Thread detail — full-page view of one chat thread. Shows the
	 * message stream on the left, optional ThreadGraph on the right.
	 * Reuses the existing chat store methods (openThread, sendMessage,
	 * updateThread) — the panel version works on the same data.
	 */

	import { onMount, tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { chat } from '$lib/stores/chat.svelte';
	import ThreadGraph from '$lib/components/panels/ThreadGraph.svelte';
	import ArrowLeft from 'lucide-svelte/icons/arrow-left';
	import Send from 'lucide-svelte/icons/send';
	import Network from 'lucide-svelte/icons/network';
	import User from 'lucide-svelte/icons/user';
	import Bot from 'lucide-svelte/icons/bot';
	import Archive from 'lucide-svelte/icons/archive';
	import Sparkles from 'lucide-svelte/icons/sparkles';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import Circle from 'lucide-svelte/icons/circle';
	import Cpu from 'lucide-svelte/icons/cpu';
	import Pencil from 'lucide-svelte/icons/pencil';
	import Check from 'lucide-svelte/icons/check';
	import X from 'lucide-svelte/icons/x';

	const threadId = $derived($page.params.id || '');

	let input = $state('');
	let showGraph = $state(false);
	let scrollEl: HTMLDivElement;
	let inputEl: HTMLTextAreaElement;
	let editingTitle = $state(false);
	let titleDraft = $state('');

	const activeThread = $derived(
		chat.threads.find((t) => t.thread_id === threadId) || chat.activeThread
	);

	// Load thread + messages on mount / when id changes
	$effect(() => {
		if (threadId && chat.activeThreadId !== threadId) {
			chat.openThread(threadId);
		}
	});

	async function scrollToBottom() {
		await tick();
		if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
	}

	// Scroll on message changes
	$effect(() => {
		void chat.activeMessages.length;
		scrollToBottom();
	});

	async function send() {
		const text = input.trim();
		if (!text || chat.sending) return;
		input = '';
		await chat.sendMessage(text);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			send();
		}
	}

	async function archive() {
		if (!activeThread) return;
		if (!confirm(`Archive "${activeThread.title}"?`)) return;
		await chat.archiveThread(threadId);
		goto('/chat');
	}

	function beginEditTitle() {
		if (!activeThread) return;
		titleDraft = activeThread.title;
		editingTitle = true;
	}

	async function saveTitle() {
		if (!activeThread || !titleDraft.trim()) return;
		await chat.updateThread(threadId, { title: titleDraft.trim() });
		editingTitle = false;
	}

	function timeAgo(ts?: string): string {
		if (!ts) return '';
		try {
			const diff = Date.now() - new Date(ts).getTime();
			const mins = Math.floor(diff / 60000);
			if (mins < 1) return 'just now';
			if (mins < 60) return `${mins}m ago`;
			const hrs = Math.floor(mins / 60);
			if (hrs < 24) return `${hrs}h ago`;
			return `${Math.floor(hrs / 24)}d ago`;
		} catch {
			return ts;
		}
	}

	// Light action summary — keeps the detail page from taking a full
	// dependency on all of the action card components
	function actionSummary(actionResults: any[] | undefined): Array<{icon: string; text: string}> {
		if (!actionResults?.length) return [];
		return actionResults.map((a) => {
			const action = a.action || 'action';
			const data = a.data || {};
			let count = 0;
			if (Array.isArray(data)) count = data.length;
			else if (data.signals) count = data.signals.length;
			else if (data.alerts) count = data.alerts.length;
			else if (data.cases) count = data.cases.length;
			else if (data.nodes) count = data.nodes.length;
			else if (data.count != null) count = data.count;
			return {
				icon: action.includes('search') ? '🔍' :
				      action.includes('create') ? '+' :
				      action.includes('list') ? '≡' :
				      action.includes('get') ? '→' : '•',
				text: count ? `${action} — ${count} result${count !== 1 ? 's' : ''}` : action,
			};
		});
	}

	onMount(() => {
		if (chat.threads.length === 0) chat.loadThreads();
		if (chat.providers.length === 0) chat.loadProviders();
	});
</script>

<div class="flex flex-col h-full bg-background">
	<!-- Header -->
	<div class="flex items-center gap-2 px-4 py-2 border-b border-border bg-card/50 shrink-0">
		<button onclick={() => goto('/chat')}
			class="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground shrink-0"
			title="Back to threads">
			<ArrowLeft class="w-4 h-4" />
		</button>

		<div class="flex-1 min-w-0">
			{#if editingTitle}
				<div class="flex items-center gap-2">
					<input type="text" bind:value={titleDraft}
						onkeydown={(e) => { if (e.key === 'Enter') saveTitle(); else if (e.key === 'Escape') editingTitle = false; }}
						class="flex-1 bg-muted rounded px-2 py-1 text-sm font-semibold text-foreground outline-none focus:ring-2 focus:ring-ring" />
					<button onclick={saveTitle} class="p-1 text-emerald-400 hover:bg-muted rounded"><Check class="w-4 h-4" /></button>
					<button onclick={() => editingTitle = false} class="p-1 text-muted-foreground hover:bg-muted rounded"><X class="w-4 h-4" /></button>
				</div>
			{:else}
				<div class="flex items-center gap-2">
					<h2 class="text-sm font-semibold text-foreground truncate">
						{activeThread?.title || 'Loading…'}
					</h2>
					{#if activeThread}
						<button onclick={beginEditTitle}
							class="p-0.5 text-muted-foreground/50 hover:text-foreground rounded"
							title="Edit title">
							<Pencil class="w-3 h-3" />
						</button>
					{/if}
				</div>
				{#if activeThread?.description}
					<p class="text-[11px] text-muted-foreground line-clamp-1">{activeThread.description}</p>
				{/if}
			{/if}
		</div>

		<!-- Meta chips -->
		{#if activeThread}
			<div class="flex items-center gap-2 text-[10px] text-muted-foreground shrink-0">
				<span class="capitalize text-foreground/80">{activeThread.thread_type}</span>
				{#if activeThread.provider_name}
					<span class="flex items-center gap-1"><Cpu class="w-2.5 h-2.5" /> {activeThread.provider_name}</span>
				{/if}
				{#if activeThread.case_id}
					<span class="flex items-center gap-1 text-amber-400"><Briefcase class="w-2.5 h-2.5" /> case</span>
				{/if}
				{#if activeThread.project_id}
					<span class="flex items-center gap-1 text-cyan-400"><FolderOpen class="w-2.5 h-2.5" /> project</span>
				{/if}
				{#if activeThread.status === 'background'}
					<span class="flex items-center gap-1 text-emerald-400">
						<Circle class="w-2 h-2 fill-emerald-400 animate-pulse" /> background
					</span>
				{/if}
			</div>
		{/if}

		<!-- Graph toggle -->
		<button onclick={() => showGraph = !showGraph}
			class="flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-md border transition-colors
				{showGraph ? 'border-primary/60 text-primary bg-primary/10' : 'border-border text-muted-foreground hover:text-foreground hover:bg-muted'}"
			title="Toggle thread knowledge graph">
			<Network class="w-3.5 h-3.5" />
			{activeThread?.node_count || 0} nodes
		</button>

		<button onclick={archive}
			class="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-destructive"
			title="Archive thread">
			<Archive class="w-4 h-4" />
		</button>
	</div>

	<!-- Main area: messages | graph -->
	<div class="flex-1 flex min-h-0">
		<!-- Messages column -->
		<div class="flex-1 flex flex-col min-w-0">
			<div bind:this={scrollEl} class="flex-1 overflow-y-auto px-6 py-5">
				{#if chat.loading && chat.activeMessages.length === 0}
					<div class="text-center text-sm text-muted-foreground py-10">Loading conversation…</div>
				{:else if chat.activeMessages.length === 0}
					<div class="max-w-md mx-auto text-center py-10 text-muted-foreground">
						<Sparkles class="w-8 h-8 mx-auto mb-3 opacity-30" />
						<p class="text-sm">New thread — send a message to begin</p>
						<p class="text-xs mt-1">Responses produce durable knowledge nodes you can explore in the graph.</p>
					</div>
				{:else}
					<div class="max-w-3xl mx-auto space-y-5">
						{#each chat.activeMessages as msg}
							{@const actions = actionSummary(msg.action_results)}
							<div class="flex gap-3 {msg.role === 'user' ? 'flex-row-reverse' : ''}">
								<div class="w-8 h-8 rounded-full flex items-center justify-center shrink-0
									{msg.role === 'user' ? 'bg-primary/10' : 'bg-muted'}">
									{#if msg.role === 'user'}
										<User class="w-4 h-4 text-primary" />
									{:else}
										<Bot class="w-4 h-4 text-muted-foreground" />
									{/if}
								</div>
								<div class="flex-1 min-w-0 {msg.role === 'user' ? 'text-right' : ''}">
									<div class="inline-block text-sm text-foreground max-w-full text-left whitespace-pre-wrap
										{msg.role === 'user' ? 'bg-primary/10 rounded-2xl rounded-tr-sm px-3.5 py-2' : ''}">
										{msg.content}
									</div>

									<!-- Model + timestamp -->
									{#if msg.role === 'assistant'}
										<div class="flex items-center gap-2 text-[10px] text-muted-foreground/50 mt-1">
											{#if msg.model_used}<span>{msg.model_used}</span>{/if}
											{#if msg.timestamp}<span>· {timeAgo(msg.timestamp)}</span>{/if}
										</div>
									{/if}

									<!-- Nodes produced -->
									{#if msg.nodes_produced?.length}
										<div class="mt-2 flex flex-wrap gap-1 {msg.role === 'user' ? 'justify-end' : ''}">
											{#each msg.nodes_produced as nodeId}
												<button
													onclick={() => (showGraph = true)}
													class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border border-primary/30 bg-primary/5 text-primary hover:bg-primary/15 transition-colors text-[10px]">
													<Network class="w-2.5 h-2.5" />
													<span class="font-mono">{nodeId.slice(0, 16)}</span>
												</button>
											{/each}
										</div>
									{/if}

									<!-- Action summary -->
									{#if actions.length}
										<div class="mt-2 space-y-1 {msg.role === 'user' ? 'flex flex-col items-end' : ''}">
											{#each actions as action}
												<div class="inline-flex items-center gap-1.5 text-[10px] text-muted-foreground border border-border/50 bg-muted/30 rounded px-2 py-0.5">
													<span>{action.icon}</span>
													<span class="font-mono">{action.text}</span>
												</div>
											{/each}
										</div>
									{/if}
								</div>
							</div>
						{/each}

						{#if chat.sending}
							<div class="flex gap-3">
								<div class="w-8 h-8 rounded-full bg-muted flex items-center justify-center shrink-0">
									<Bot class="w-4 h-4 text-muted-foreground" />
								</div>
								<div class="flex items-center gap-1 pt-2">
									<span class="w-1.5 h-1.5 bg-muted-foreground/40 rounded-full animate-pulse"></span>
									<span class="w-1.5 h-1.5 bg-muted-foreground/40 rounded-full animate-pulse" style="animation-delay: 0.15s"></span>
									<span class="w-1.5 h-1.5 bg-muted-foreground/40 rounded-full animate-pulse" style="animation-delay: 0.3s"></span>
								</div>
							</div>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Input -->
			<div class="border-t border-border bg-card/50 px-4 py-3">
				<div class="max-w-3xl mx-auto flex items-end gap-2">
					<textarea
						bind:this={inputEl}
						bind:value={input}
						onkeydown={handleKeydown}
						placeholder="Message this thread… (Enter to send, Shift+Enter for newline)"
						rows="1"
						disabled={chat.sending}
						class="flex-1 bg-muted rounded-lg px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-ring resize-none max-h-40 disabled:opacity-60"></textarea>
					<button onclick={send} disabled={!input.trim() || chat.sending}
						class="p-2.5 rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
						title="Send (Enter)">
						<Send class="w-4 h-4" />
					</button>
				</div>
			</div>
		</div>

		<!-- Graph column (toggled) -->
		{#if showGraph && threadId}
			<div class="w-[480px] border-l border-border bg-card/40 shrink-0 flex flex-col">
				<ThreadGraph {threadId} onClose={() => showGraph = false} />
			</div>
		{/if}
	</div>
</div>
