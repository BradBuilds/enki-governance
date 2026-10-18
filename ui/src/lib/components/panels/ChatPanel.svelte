<script lang="ts">
	import { chat, type ChatMessage, type ChatThread } from '$lib/stores/chat.svelte';
	import { dispatchMapCommand } from '$lib/stores/mapCommands';
	import { goto } from '$app/navigation';
	import { accessLog } from '$lib/stores/accessLog.svelte';
	import { space } from '$lib/stores/space.svelte';

	import Send from 'lucide-svelte/icons/send';
	import Bot from 'lucide-svelte/icons/bot';
	import User from 'lucide-svelte/icons/user';
	import X from 'lucide-svelte/icons/x';
	import ArrowLeft from 'lucide-svelte/icons/arrow-left';
	import Trash2 from 'lucide-svelte/icons/trash-2';
	import Plus from 'lucide-svelte/icons/plus';
	import MessageSquare from 'lucide-svelte/icons/message-square';
	import Search from 'lucide-svelte/icons/search';
	import Sparkles from 'lucide-svelte/icons/sparkles';
	import Brain from 'lucide-svelte/icons/brain';
	import Eye from 'lucide-svelte/icons/eye';
	import Zap from 'lucide-svelte/icons/zap';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import Briefcase from 'lucide-svelte/icons/briefcase';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import MoreVertical from 'lucide-svelte/icons/more-vertical';
	import Network from 'lucide-svelte/icons/network';
	import Cloud from 'lucide-svelte/icons/cloud';
	import Lock from 'lucide-svelte/icons/lock';
	import Pin from 'lucide-svelte/icons/pin';
	import PinOff from 'lucide-svelte/icons/pin-off';
	import EyeOff from 'lucide-svelte/icons/eye-off';
	import Unlink from 'lucide-svelte/icons/unlink';
	import Link from 'lucide-svelte/icons/link';
	import FileText from 'lucide-svelte/icons/file-text';

	import SignalCard from '$lib/components/cards/SignalCard.svelte';
	import AlertCard from '$lib/components/cards/AlertCard.svelte';
	import CaseCard from '$lib/components/cards/CaseCard.svelte';
	import StatusCard from '$lib/components/cards/StatusCard.svelte';
	import ActionCard from '$lib/components/cards/ActionCard.svelte';
	import MapPreviewCard from '$lib/components/cards/MapPreviewCard.svelte';
	import AircraftCard from '$lib/components/cards/AircraftCard.svelte';
	import TimelineCard from '$lib/components/cards/TimelineCard.svelte';
	import BriefCard from '$lib/components/cards/BriefCard.svelte';
	import ExportCard from '$lib/components/cards/ExportCard.svelte';
	import { EntityCard, FactCard, EventCard, RefCard, LibraryCard } from '$lib/components/cards2';
	import ConnectorCard from '$lib/components/cards/ConnectorCard.svelte';
	import ObservableCard from '$lib/components/cards/ObservableCard.svelte';
	import ConfirmationCard from '$lib/components/cards/ConfirmationCard.svelte';
	import NodeCardCompact from '$lib/components/cards/NodeCardCompact.svelte';
	import WorkerActivityCard from '$lib/components/cards/WorkerActivityCard.svelte';
	import TripCard from '$lib/components/cards/TripCard.svelte';
	import CorrelationCard from '$lib/components/cards/CorrelationCard.svelte';
	import PhotoGalleryCard from '$lib/components/cards/PhotoGalleryCard.svelte';
	import ThreadGraph from './ThreadGraph.svelte';

	interface Props {
		mode: 'inline' | 'overlay' | 'fullscreen';
	}

	let { mode }: Props = $props();
	let message = $state('');
	let showGraph = $state(false);
	let showMenu = $state(false);
	let chatContainer: HTMLElement;
	let summarizing = $state(false);
	let lastLoggedCount = $state(0);

	// Open the threads/history view (loads threads lazily).
	function openHistory() {
		chat.loadThreads();
		chat.view = 'list';
	}

	// Log access events when new action results arrive
	$effect(() => {
		const msgs = chat.activeMessages;
		if (msgs.length <= lastLoggedCount) return;
		for (let i = lastLoggedCount; i < msgs.length; i++) {
			const msg = msgs[i];
			if (msg.role === 'assistant' && msg.action_results?.length) {
				for (const ar of msg.action_results) {
					if (ar.success && ar.data) {
						accessLog.log({
							action: ar.action,
							resource_type: 'chat_action',
							resource_id: ar.action,
							context: 'chat',
							space: space.activeSpace,
						});
					}
				}
			}
		}
		lastLoggedCount = msgs.length;
	});

	// Inline node quick-action menu
	let nodeMenuId = $state<string | null>(null);
	let nodeMenuPos = $state({ x: 0, y: 0 });

	// Node metadata cache (fetched when graph is loaded or nodes appear)
	let nodeCache = $state<Record<string, { title: string; epistemic_type: string; relevance: string }>>({});

	// New thread form
	let newTitle = $state('');
	let newType = $state<'research' | 'monitoring' | 'triage' | 'analysis'>('research');
	let newProviderId = $state<string | undefined>(undefined);

	function scrollToBottom() {
		if (chatContainer) {
			setTimeout(() => chatContainer.scrollTop = chatContainer.scrollHeight, 50);
		}
	}

	async function send() {
		if (!message.trim() || chat.sending) return;
		const userMsg = message.trim();
		message = '';
		const data = await chat.sendMessage(userMsg);
		scrollToBottom();

		// Handle map commands from action results
		if (data?.action_results) {
			for (const action of data.action_results) {
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
				} else if (aType === 'show_track') {
					dispatchMapCommand({ type: 'show_track', analysis: aData });
					goto('/map');
				} else if (aType === 'navigate_to_page') {
					const targetPage = aData.page || '/dashboard';
					const entityId = aData.entity_id;
					goto(entityId ? `${targetPage}/${entityId}` : targetPage);
				}
			}
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			send();
		}
	}

	// ─── Node helpers ────────────────────────────────────────────────

	async function loadNodeCache(threadId: string) {
		try {
			const res = await fetch(`/api/threads/${threadId}/nodes?include_unlinked=true`);
			if (res.ok) {
				const data = await res.json();
				const cache: Record<string, any> = {};
				for (const n of data.nodes || []) {
					cache[n.node_id] = { title: n.title, epistemic_type: n.epistemic_type, relevance: n.relevance };
				}
				nodeCache = cache;
			}
		} catch { /* */ }
	}

	function showNodeMenu(e: MouseEvent, nodeId: string) {
		e.preventDefault();
		e.stopPropagation();
		nodeMenuId = nodeId;
		nodeMenuPos = { x: e.clientX, y: e.clientY };
	}

	function closeNodeMenu() {
		nodeMenuId = null;
	}

	async function setNodeRelevance(nodeId: string, relevance: string) {
		if (!chat.activeThreadId) return;
		try {
			await fetch(`/api/threads/${chat.activeThreadId}/nodes/${nodeId}/relevance`, {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ relevance }),
			});
			if (nodeCache[nodeId]) nodeCache[nodeId].relevance = relevance;
			nodeCache = { ...nodeCache };
		} catch { /* */ }
		closeNodeMenu();
	}

	async function summarizeThread() {
		if (!chat.activeThreadId || summarizing) return;
		summarizing = true;
		const summaryPrompt = 'Synthesize all established findings in this thread into a concise intelligence summary. Highlight key facts, open questions, and any contradictions between sources. Be specific — cite the evidence behind each point.';
		await chat.sendMessage(summaryPrompt);
		scrollToBottom();
		summarizing = false;
	}

	// Refresh node cache when thread changes
	$effect(() => {
		if (chat.activeThreadId) {
			loadNodeCache(chat.activeThreadId);
		}
	});

	const epistemicChipColors: Record<string, string> = {
		fact: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
		observation: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
		claim: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
		hypothesis: 'bg-purple-500/15 text-purple-400 border-purple-500/30',
		question: 'bg-red-500/15 text-red-400 border-red-500/30',
		evidence: 'bg-cyan-500/15 text-cyan-400 border-cyan-500/30',
	};

	async function createThread() {
		if (!newTitle.trim()) return;
		const threadId = await chat.createThread({
			title: newTitle.trim(),
			thread_type: newType,
			provider_id: newProviderId,
		});
		if (threadId) {
			newTitle = '';
			await chat.openThread(threadId);
		}
	}

	const threadTypeIcons: Record<string, any> = {
		research: Search,
		monitoring: Eye,
		triage: Zap,
		analysis: Brain,
	};

	const threadTypeColors: Record<string, string> = {
		research: 'text-blue-400',
		monitoring: 'text-emerald-400',
		triage: 'text-amber-400',
		analysis: 'text-purple-400',
	};

	const contextLabel = $derived.by(() => {
		if (chat.activeThread) return chat.activeThread.title;
		const p = chat.context.currentPage;
		if (p === '/') return 'Quick Chat';
		if (p.startsWith('/map')) return 'Map';
		if (p.startsWith('/alerts')) return 'Alerts';
		if (p.startsWith('/cases')) return 'Cases';
		return p.slice(1).split('/')[0] || 'Quick Chat';
	});

	const panelClasses: Record<string, string> = {
		inline: 'w-[420px] border-l border-border bg-card flex flex-col shrink-0 h-full',
		overlay: 'fixed right-0 top-0 h-full w-[420px] max-w-[90vw] z-50 bg-card border-l border-border shadow-xl flex flex-col',
		fullscreen: 'fixed inset-0 z-50 bg-background flex flex-col',
	};

	// Which model is answering, and is it local (private) or a cloud/external
	// service (data leaves your hardware). Drives the transparency badge.
	const CLOUD_PROVIDERS = new Set(['openai','anthropic','gemini','google','grok','xai','groq','together','mistral','deepseek','perplexity','fireworks','cohere']);
	const modelInfo = $derived.by(() => {
		const t: any = chat.activeThread;
		let provider: string | undefined = t?.provider_type;
		if (!provider && t?.provider_id) provider = chat.providers.find(p => p.provider_id === t.provider_id)?.provider;
		if (!provider) provider = (chat.providers.find(p => p.is_default) || chat.providers[0])?.provider;
		const model = t?.model_name || (chat.providers.find(p => p.is_default) || chat.providers[0])?.model || '';
		const isCloud = provider
			? CLOUD_PROVIDERS.has(provider.toLowerCase())
			: /\b(gpt|claude|gemini|grok|mistral|deepseek|command|sonar)\b/i.test(model);
		return { provider, model, isCloud };
	});
</script>

{#if mode === 'overlay'}
	<button class="fixed inset-0 z-40 bg-black/40" onclick={() => chat.close()} aria-label="Close chat"></button>
{/if}

<div class="{panelClasses[mode]} {mode === 'overlay' ? 'animate-slide-in-right' : ''}">
	<!-- Header -->
	<div class="flex items-center justify-between px-4 py-3 border-b border-border shrink-0 {mode === 'fullscreen' ? 'pt-safe' : ''}">
		<div class="flex items-center gap-2 min-w-0">
			{#if chat.view !== 'thread'}
				<button onclick={() => chat.view = 'thread'} class="p-1 rounded hover:bg-muted text-muted-foreground shrink-0" title="Back to chat">
					<ArrowLeft size={18} />
				</button>
			{/if}
			<Bot size={18} class="text-primary shrink-0" />
			<span class="text-sm font-medium text-foreground truncate">{contextLabel}</span>
			{#if modelInfo.model}
				{#if modelInfo.isCloud}
					<span class="flex items-center gap-1 text-[9px] font-medium px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30 shrink-0"
						title="External model ({modelInfo.provider || 'cloud'}) — your prompts and data are sent to a third-party service.">
						<Cloud size={10} /> External · {modelInfo.model}
					</span>
				{:else}
					<span class="flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 shrink-0"
						title="Local model — runs on your hardware, data stays private.">
						<Lock size={10} /> Local · {modelInfo.model}
					</span>
				{/if}
			{/if}
		</div>
		<div class="flex items-center gap-0.5">
			<!-- History / threads -->
			<button onclick={openHistory} class="p-1.5 rounded hover:bg-muted {chat.view === 'list' ? 'text-primary bg-primary/10' : 'text-muted-foreground'}" title="Chat history & threads">
				<MessageSquare size={16} />
			</button>
			<!-- Overflow menu (the tucked-away power tools) -->
			<div class="relative">
				<button onclick={() => showMenu = !showMenu} class="p-1.5 rounded hover:bg-muted text-muted-foreground" title="More">
					<MoreVertical size={16} />
				</button>
				{#if showMenu}
					<!-- svelte-ignore a11y_no_static_element_interactions -->
					<button class="fixed inset-0 z-40" onclick={() => showMenu = false} aria-label="Close menu"></button>
					<div class="absolute right-0 top-full mt-1 z-50 bg-card border border-border rounded-lg shadow-xl p-1 min-w-[180px]">
						<button onclick={() => { chat.showNewThread(); showMenu = false; }} class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-muted text-left text-foreground transition-colors">
							<Plus size={13} /> New thread
						</button>
						{#if chat.activeThreadId}
							<button onclick={() => { showGraph = !showGraph; showMenu = false; }} class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-muted text-left text-foreground transition-colors">
								<Network size={13} /> {showGraph ? 'Hide' : 'Show'} thread graph
							</button>
							<button onclick={() => { summarizeThread(); showMenu = false; }} disabled={summarizing} class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-muted text-left text-foreground transition-colors disabled:opacity-40">
								<FileText size={13} /> {summarizing ? 'Summarizing…' : 'Summarize thread'}
							</button>
							<div class="border-t border-border/50 my-1"></div>
							<button onclick={() => { chat.archiveThread(chat.activeThreadId!); showMenu = false; }} class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-muted text-left text-red-400 transition-colors">
								<Trash2 size={13} /> Archive thread
							</button>
						{/if}
					</div>
				{/if}
			</div>
			<button onclick={() => chat.close()} class="p-1.5 rounded hover:bg-muted text-muted-foreground" title="Close">
				<X size={16} />
			</button>
		</div>
	</div>

	<!-- ═══ VIEW: Thread List ═══ -->
	{#if chat.view === 'list'}
		<div class="flex-1 overflow-y-auto">
			<!-- Quick chat section -->
			<button
				onclick={() => { chat.view = 'thread'; chat.activeThreadId = null; }}
				class="w-full flex items-center gap-3 px-4 py-3 hover:bg-muted/50 border-b border-border/50 text-left transition-colors"
			>
				<div class="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
					<Sparkles size={16} class="text-primary" />
				</div>
				<div class="flex-1 min-w-0">
					<div class="text-sm font-medium text-foreground">Quick Chat</div>
					<div class="text-[11px] text-muted-foreground truncate">Ask anything — no thread needed</div>
				</div>
				<ChevronRight size={14} class="text-muted-foreground shrink-0" />
			</button>

			<!-- Thread list header -->
			<div class="flex items-center justify-between px-4 py-2 border-b border-border/30">
				<span class="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">Threads</span>
				<button onclick={() => chat.showNewThread()} class="p-1 rounded hover:bg-muted text-muted-foreground" title="New thread">
					<Plus size={14} />
				</button>
			</div>

			{#if chat.threadsLoading}
				<div class="p-4 text-center text-sm text-muted-foreground">Loading...</div>
			{:else if chat.threads.length === 0}
				<div class="p-6 text-center">
					<Network size={32} class="mx-auto text-muted-foreground/30 mb-2" />
					<p class="text-sm text-muted-foreground">No threads yet</p>
					<p class="text-xs text-muted-foreground/60 mt-1">Threads let you research topics with dedicated AI context</p>
					<button
						onclick={() => chat.showNewThread()}
						class="mt-3 px-3 py-1.5 text-xs rounded-lg bg-primary text-primary-foreground hover:bg-primary/90"
					>
						Start a Thread
					</button>
				</div>
			{:else}
				{#each chat.threads as thread}
					{@const TypeIcon = threadTypeIcons[thread.thread_type] || MessageSquare}
					<button
						onclick={() => chat.openThread(thread.thread_id)}
						class="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-muted/50 border-b border-border/20 text-left transition-colors"
					>
						<div class="w-7 h-7 rounded-lg bg-muted flex items-center justify-center shrink-0">
							<svelte:component this={TypeIcon} size={14} class={threadTypeColors[thread.thread_type] || 'text-muted-foreground'} />
						</div>
						<div class="flex-1 min-w-0">
							<div class="text-[13px] font-medium text-foreground truncate">{thread.title}</div>
							<div class="flex items-center gap-2 mt-0.5">
								{#if thread.model_name}
									<span class="text-[10px] text-muted-foreground/70">{thread.model_name}</span>
									<span class="text-muted-foreground/30">·</span>
								{/if}
								<span class="text-[10px] text-muted-foreground/70">{thread.message_count} msgs</span>
								{#if thread.node_count > 0}
									<span class="text-muted-foreground/30">·</span>
									<span class="text-[10px] text-emerald-400/70">{thread.node_count} nodes</span>
								{/if}
							</div>
							{#if thread.last_message}
								<div class="text-[11px] text-muted-foreground/50 truncate mt-0.5">{thread.last_message}</div>
							{/if}
						</div>
						<ChevronRight size={14} class="text-muted-foreground/40 shrink-0" />
					</button>
				{/each}
			{/if}
		</div>

	<!-- ═══ VIEW: New Thread ═══ -->
	{:else if chat.view === 'new'}
		<div class="flex-1 overflow-y-auto p-4 space-y-4">
			<div>
				<label class="text-xs font-medium text-muted-foreground mb-1 block">Thread Name</label>
				<input
					bind:value={newTitle}
					onkeydown={(e) => { if (e.key === 'Enter') createThread(); }}
					placeholder="e.g. Vessel tracking near Crimea"
					class="w-full bg-muted rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary"
				/>
			</div>

			<div>
				<label class="text-xs font-medium text-muted-foreground mb-1.5 block">Thread Type</label>
				<div class="grid grid-cols-2 gap-2">
					{#each (['research', 'analysis', 'monitoring', 'triage'] as const) as type}
						{@const Icon = threadTypeIcons[type]}
						<button
							onclick={() => newType = type}
							class="flex items-center gap-2 px-3 py-2 rounded-lg border text-left text-xs transition-colors
								{newType === type ? 'border-primary bg-primary/10 text-primary' : 'border-border bg-card text-muted-foreground hover:bg-muted'}"
						>
							<svelte:component this={Icon} size={14} />
							<span class="capitalize">{type}</span>
						</button>
					{/each}
				</div>
			</div>

			{#if chat.providers.length > 0}
				<div>
					<label class="text-xs font-medium text-muted-foreground mb-1.5 block">AI Model</label>
					<div class="space-y-1.5">
						<button
							onclick={() => newProviderId = undefined}
							class="w-full flex items-center gap-2 px-3 py-2 rounded-lg border text-left text-xs transition-colors
								{!newProviderId ? 'border-primary bg-primary/10 text-primary' : 'border-border bg-card text-muted-foreground hover:bg-muted'}"
						>
							<Sparkles size={14} />
							<span>Auto (use default)</span>
						</button>
						{#each chat.providers as prov}
							<button
								onclick={() => newProviderId = prov.provider_id}
								class="w-full flex items-center justify-between px-3 py-2 rounded-lg border text-left text-xs transition-colors
									{newProviderId === prov.provider_id ? 'border-primary bg-primary/10 text-primary' : 'border-border bg-card text-muted-foreground hover:bg-muted'}"
							>
								<div class="flex items-center gap-2">
									<Bot size={14} />
									<span>{prov.display_name}</span>
								</div>
								{#if prov.is_default}
									<span class="text-[9px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">default</span>
								{/if}
							</button>
						{/each}
					</div>
				</div>
			{/if}

			<button
				onclick={createThread}
				disabled={!newTitle.trim()}
				class="w-full py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
			>
				Create Thread
			</button>
		</div>

	<!-- ═══ VIEW: Thread / Quick Chat ═══ -->
	{:else}
		<!-- Graph panel (split view when toggled) -->
		{#if showGraph && chat.activeThreadId}
			<div class="h-[45%] min-h-[200px] shrink-0 border-b border-border">
				<ThreadGraph threadId={chat.activeThreadId} onClose={() => showGraph = false} />
			</div>
		{/if}

		<!-- Thread info bar (if in a thread) -->
		{#if chat.activeThread}
			<div class="flex items-center gap-2 px-4 py-1.5 border-b border-border/30 bg-muted/30">
				{#if chat.activeThread.project_id}
					<FolderOpen size={12} class="text-muted-foreground/60" />
					<span class="text-[10px] text-muted-foreground/60">Project linked</span>
				{/if}
				{#if chat.activeThread.case_id}
					<Briefcase size={12} class="text-muted-foreground/60" />
					<span class="text-[10px] text-muted-foreground/60">Case linked</span>
				{/if}
				{#if chat.activeThread.node_count > 0}
					<Network size={12} class="text-emerald-400/60" />
					<span class="text-[10px] text-emerald-400/60">{chat.activeThread.node_count} nodes</span>
				{/if}
				<span class="flex-1"></span>
				<span class="text-[10px] text-muted-foreground/40 capitalize">{chat.activeThread.thread_type}</span>
			</div>
		{/if}

		<!-- Messages -->
		<div bind:this={chatContainer} class="flex-1 overflow-y-auto p-4 space-y-4">
			{#each chat.currentMessages as msg}
				<div class="flex gap-2.5 {msg.role === 'user' ? 'flex-row-reverse' : ''}">
					<div class="w-7 h-7 rounded-full flex items-center justify-center shrink-0 {msg.role === 'user' ? 'bg-primary/10' : 'bg-muted'}">
						{#if msg.role === 'user'}
							<User size={14} class="text-primary" />
						{:else}
							<Bot size={14} class="text-muted-foreground" />
						{/if}
					</div>
					<div class="flex-1 min-w-0 {msg.role === 'user' ? 'text-right' : ''}">
						<div class="inline-block text-sm text-foreground {msg.role === 'user' ? 'bg-primary/10 rounded-2xl rounded-tr-sm px-3 py-2' : ''} max-w-full text-left">
							{msg.content}
						</div>
						{#if msg.model_used}
							{@const msgCloud = /\b(gpt|claude|gemini|grok|mistral|deepseek|command|sonar)\b/i.test(msg.model_used)}
							<div class="text-[9px] mt-0.5 flex items-center gap-1 {msgCloud ? 'text-amber-400/70' : 'text-muted-foreground/40'}">
								{#if msgCloud}<Cloud size={9} /> external:{/if}{msg.model_used}
							</div>
						{/if}
						<!-- Nodes produced — interactive chips -->
						{#if msg.nodes_produced?.length}
							<div class="mt-1.5 flex flex-wrap gap-1 {msg.role === 'user' ? 'justify-end' : ''}">
								{#each msg.nodes_produced as nodeId}
									{@const cached = nodeCache[nodeId]}
									{@const chipColor = cached ? (epistemicChipColors[cached.epistemic_type] || 'bg-muted text-muted-foreground border-border') : 'bg-muted text-muted-foreground border-border'}
									{@const isUnlinked = cached?.relevance === 'unlinked'}
									{@const isPinned = cached?.relevance === 'pinned'}
									{@const isDimmed = cached?.relevance === 'dimmed'}
									<!-- svelte-ignore a11y_no_static_element_interactions -->
									<button
										class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[10px] transition-all
											{chipColor}
											{isUnlinked ? 'opacity-30 line-through' : ''}
											{isDimmed ? 'opacity-50' : ''}
											hover:brightness-110"
										onclick={() => { showGraph = true; }}
										oncontextmenu={(e) => showNodeMenu(e, nodeId)}
										title={cached ? `${cached.epistemic_type}: ${cached.title}\nRight-click for options` : nodeId}
									>
										{#if isPinned}<Pin size={8} class="shrink-0" />{/if}
										<Network size={8} class="shrink-0" />
										<span class="truncate max-w-[120px]">{cached?.title || nodeId.slice(0, 12)}</span>
									</button>
								{/each}
							</div>
						{/if}
						<!-- Action results -->
						{#if msg.action_results?.length}
							<div class="mt-2 space-y-2 text-left">
								{#each msg.action_results as action}
									{@const act = action.action}
									{@const d = action.data}
									{#if act === 'answer_about'}
										<div class="space-y-2">
											<div class="text-sm text-foreground/90 leading-relaxed whitespace-pre-wrap break-words">{d?.answer || ''}</div>
											{#if (d?.sources || []).length}
												<div class="pt-1">
													<div class="text-[10px] font-medium text-muted-foreground uppercase tracking-wide mb-1">Sources</div>
													<ol class="space-y-0.5">
														{#each d.sources.slice(0, 12) as s}
															<li class="text-[11px] flex items-start gap-1.5">
																<span class="text-muted-foreground shrink-0">[{s.marker}]</span>
																{#if s.document_id}
																	<a href="/document/{s.document_id}" class="text-primary hover:underline break-words">{s.title || s.source_url || `document ${s.document_id}`}</a>
																{:else}
																	<span class="text-foreground/80 break-words">{s.title || s.source_url || 'source'}</span>
																{/if}
															</li>
														{/each}
													</ol>
												</div>
											{/if}
											{#if d?.entity_id}
												<a href="/entity/{d.entity_id}" class="inline-block text-[10px] text-muted-foreground/70 hover:text-primary">open {d.canonical_name || 'entity'} profile →</a>
											{/if}
										</div>
									{:else if act === 'search_signals' || act === 'get_signal'}
										{#each (d?.signals || (d ? [d] : [])).slice(0, 5) as sig}
											<SignalCard signal={sig} variant="standard" />
										{/each}
										{#if (d?.count || d?.signals?.length || 0) > 5}
											<a href="/signals" class="text-[10px] text-primary hover:underline">View all {d.count || d.signals.length} signals</a>
										{/if}
									{:else if act === 'list_alerts' || act === 'get_alert'}
										{#each (d?.alerts || (d ? [d] : [])).slice(0, 5) as a}
											<AlertCard alert={a} variant="standard" />
										{/each}
									{:else if act === 'list_cases' || act === 'get_case'}
										{#each (d?.cases || (d ? [d] : [])).slice(0, 5) as c}
											<CaseCard caseData={c} variant="standard" />
										{/each}
									{:else if act === 'get_dashboard'}
										<StatusCard
											title="System Overview"
											status="healthy"
											metrics={[
												{ label: 'Signals', value: d?.signals?.total ?? 0 },
												{ label: 'Alerts', value: d?.alerts?.total ?? 0 },
												{ label: 'Cases', value: d?.cases?.total ?? 0 },
											]}
										/>
									{:else if act === 'navigate_to' || act === 'search_nearby' || act === 'create_geofence' || act === 'highlight_area' || act === 'analyze_device' || act === 'enrich_location' || act === 'nearby_signals'}
										<MapPreviewCard action={act} data={d || {}} />
									{:else if act === 'live_aircraft' && d?.aircraft}
										<AircraftCard aircraft={d.aircraft} count={d.count || d.aircraft.length} />
									{:else if act === 'get_timeline' && d?.events}
										<TimelineCard events={d.events} />
									{:else if act === 'generate_brief'}
										<BriefCard summary={d?.summary || ''} sections={d?.sections || []} />
									{:else if act === 'export_signals' || act === 'export_cases' || act === 'export_contacts'}
										<ExportCard downloadUrl={d?.download_url || '#'} type={d?.type} format={d?.format} />
									{:else if act === 'connectors' || act === 'data_sources'}
										<ConnectorCard connectors={d?.connectors || d?.sources || []} />
									{:else if act === 'show_photos' && d?.photos}
										<PhotoGalleryCard photos={d.photos} count={d.count || d.photos.length} />
									{:else if act === 'cross_correlate' && d?.groups}
										<CorrelationCard groups={d.groups} total={d.total || 0} center={d.center} radius_m={d.radius_m} time_range={d.time_range} />
									{:else if act === 'show_trips' && d?.trips}
										<TripCard trips={d.trips} count={d.count || d.trips.length} />
									{:else if act === 'worker_activity'}
										<WorkerActivityCard events={d?.events || []} limit={d?.count || 10} />
									{:else if act === 'investigate_entity'}
										<ConfirmationCard
											title="Investigation queued: {d?.name || 'entity'}"
											description={d?.message || `Queued for ${d?.entity_type || 'entity'} investigation`}
											variant="info"
										/>
									{:else if act === 'deep_research'}
										<ConfirmationCard
											title="Deep research started: {d?.topic || 'topic'}"
											description={d?.message || 'Running in the background — building a cited report.'}
											variant="info"
											link={d?.run_id ? { href: `/research/${d.run_id}`, label: 'Open run' } : undefined}
										/>
										{#if d?.director_model}
											<span class="mt-1 inline-flex items-center gap-1 text-[9px] font-medium px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30">
												<Cloud size={10} /> External · {d.director_model}
											</span>
										{/if}
									{:else if act === 'research_status' || act === 'list_research'}
										{#if (d?.runs || []).length}
											<div class="space-y-1">
												{#each d.runs.slice(0, 6) as run}
													<a href="/research/{run.run_id}" class="block rounded-lg border border-border bg-card p-2 text-xs hover:bg-muted transition-colors">
														<div class="flex items-center justify-between gap-2">
															<span class="font-medium text-foreground truncate">{run.goal}</span>
															<span class="text-[10px] px-1.5 py-0.5 rounded-full shrink-0 {run.status === 'done' ? 'bg-emerald-500/15 text-emerald-400' : run.status === 'failed' ? 'bg-red-500/15 text-red-400' : 'bg-amber-500/15 text-amber-400'}">{run.status}</span>
														</div>
														<div class="text-[10px] text-muted-foreground mt-0.5">
															iter {run.iteration}/{run.max_iterations} · {run.sources_grabbed} source{run.sources_grabbed === 1 ? '' : 's'} · {run.entities_count} entities · {run.facts_count} facts
														</div>
													</a>
												{/each}
											</div>
										{:else}
											<div class="text-xs text-muted-foreground">No research runs yet.</div>
										{/if}
									{:else if act === 'search_observables' && d?.observables}
										<ObservableCard observables={d.observables} />
									{:else if act === 'search_nodes' || act === 'get_node'}
										<div class="space-y-1">
											{#each (d?.nodes || (d ? [d] : [])).slice(0, 5) as node}
												<a href="/nodes/{encodeURIComponent(node.node_id || '')}" class="block rounded-lg border border-border bg-card p-2 text-xs hover:bg-muted transition-colors">
													<span class="font-medium text-foreground">{node.title || node.node_id}</span>
													{#if node.epistemic_type}<span class="text-muted-foreground ml-1">({node.epistemic_type})</span>{/if}
													{#if node.node_type}<span class="text-muted-foreground ml-1">· {node.node_type}</span>{/if}
												</a>
											{/each}
										</div>
									{:else if act === 'search_contacts' && d?.contacts}
										<div class="space-y-1">
											{#each (d.contacts || []).slice(0, 5) as contact}
												<a href="/contacts/{contact.contact_id}" class="block rounded-lg border border-border bg-card p-2 text-xs hover:bg-muted transition-colors">
													<span class="font-medium text-foreground">{contact.name}</span>
													<span class="text-muted-foreground ml-1">({contact.contact_type})</span>
												</a>
											{/each}
										</div>
									{:else if act === 'search_assets' && d?.assets}
										<div class="space-y-1">
											{#each (d.assets || []).slice(0, 5) as asset}
												<a href="/assets/{asset.asset_id}" class="block rounded-lg border border-border bg-card p-2 text-xs hover:bg-muted transition-colors">
													<span class="font-medium text-foreground">{asset.name}</span>
													<span class="text-muted-foreground ml-1">({asset.category})</span>
													{#if asset.value}<span class="text-muted-foreground ml-1">${asset.value.toLocaleString()}</span>{/if}
												</a>
											{/each}
										</div>
									{:else if act === 'create_case' && d?.case_id}
										<ConfirmationCard title="Case created" link={{ href: `/cases/${d.case_id}`, label: 'Open case' }} />
									{:else if act === 'escalate_alert' && d?.case_id}
										<ConfirmationCard title="Alert escalated to case" variant="warning" link={{ href: `/cases/${d.case_id}`, label: 'Open case' }} />
									{:else if act === 'create_rule'}
										<ConfirmationCard title="Rule created: {d?.name || 'Detection rule'}" link={{ href: '/rules', label: 'View rules' }} />
									{:else if act === 'navigate_to_page'}
										<ConfirmationCard title="Navigated to {d?.label || d?.page || 'page'}" variant="info" />
									{:else if act === 'link_node_case' || act === 'create_connector' || act === 'add_observable'}
										<ConfirmationCard title="{act.replace(/_/g, ' ')}" description={d?.message || 'Action completed'} />
									{:else if act === 'list_rules' || act === 'suggest_rules'}
										<div class="space-y-1">
											{#each (d?.rules || []).slice(0, 5) as rule}
												<div class="rounded-lg border border-border bg-card p-2 text-xs">
													<span class="font-medium text-foreground">{rule.name || rule.rule_name || 'Rule'}</span>
												</div>
											{/each}
										</div>
									{:else if act === 'investigation_status' || act === 'list_investigations'}
										{#if (d?.investigations || []).length}
											<div class="space-y-1">
												{#each d.investigations.slice(0, 6) as inv}
													<div class="rounded-lg border border-border bg-card p-2 text-xs">
														<div class="flex items-center justify-between gap-2">
															<span class="font-medium text-foreground truncate">{inv.name}</span>
															<span class="text-[10px] px-1.5 py-0.5 rounded-full shrink-0 {inv.status === 'complete' ? 'bg-emerald-500/15 text-emerald-400' : inv.status === 'failed' ? 'bg-red-500/15 text-red-400' : 'bg-amber-500/15 text-amber-400'}">{inv.status}</span>
														</div>
														<div class="text-[10px] text-muted-foreground mt-0.5">
															{inv.sources_done} source{inv.sources_done === 1 ? '' : 's'} done · {inv.findings_count} finding{inv.findings_count === 1 ? '' : 's'}{#if inv.entities_discovered} · {inv.entities_discovered} discovered{/if}
														</div>
													</div>
												{/each}
											</div>
										{:else}
											<div class="text-xs text-muted-foreground">No investigations yet.</div>
										{/if}
									{:else if act === 'setup_help' && d?.setup}
										<div class="rounded-lg border border-primary/30 bg-primary/5 p-3 text-xs space-y-2">
											<div class="font-medium text-foreground">{d.setup.feature}</div>
											<div class="text-muted-foreground">{d.setup.needs}</div>
											<ol class="list-decimal ml-4 space-y-0.5 text-muted-foreground">
												{#each d.setup.steps as step}<li>{step}</li>{/each}
											</ol>
											<div class="flex gap-2 pt-1">
												<a href={d.setup.register_url} target="_blank" rel="noopener" class="px-2 py-1 rounded bg-muted text-foreground hover:bg-accent">Register ↗</a>
												<a href={d.setup.settings_link} class="px-2 py-1 rounded bg-primary text-primary-foreground hover:bg-primary/90">Open Settings →</a>
											</div>
										</div>
									{:else if (act === 'entity_lookup' || act === 'library_search' || act === 'fact_search' || act === 'event_search' || act === 'reference_search' || act === 'address_resolve' || act === 'recent_facts') && d?.cards?.length}
										<div class="space-y-2">
											{#each d.cards.slice(0, 6) as card}
												{#if card.type === 'EntityCard'}<EntityCard entity_id={card.data.entity_id} entity={card.data.entity} size="compact" />
												{:else if card.type === 'FactCard'}<FactCard assessment_id={card.data.assessment_id} assessment={card.data.assessment} size="compact" />
												{:else if card.type === 'EventCard'}<EventCard event_id={card.data.event_id} event={card.data.event} size="compact" />
												{:else if card.type === 'RefCard'}<RefCard reference_id={card.data.reference_id} reference={card.data.reference} size="compact" />
												{:else if card.type === 'LibraryCard'}<LibraryCard library_id={card.data.library_id} library={card.data.library} size="compact" />
												{/if}
											{/each}
										</div>
									{:else if act === 'docs_search'}
										{#if (d?.passages || []).length}
											<div class="text-[10px] text-muted-foreground/70">from product docs: {d.passages.slice(0, 3).map((p: any) => p.title).join(' · ')}</div>
										{/if}
									{:else if d}
										<div class="rounded-lg border border-border bg-muted/30 p-2 text-xs">
											<span class="text-muted-foreground font-medium">{act}</span>
											<pre class="mt-1 text-[10px] text-muted-foreground overflow-x-auto max-h-20">{JSON.stringify(d, null, 2).slice(0, 300)}</pre>
										</div>
									{/if}
								{/each}
							</div>
						{/if}
					</div>
				</div>
			{/each}

			{#if chat.sending}
				<div class="flex gap-2.5">
					<div class="w-7 h-7 rounded-full bg-muted flex items-center justify-center shrink-0">
						<Bot size={14} class="text-muted-foreground" />
					</div>
					<div class="flex items-center gap-1 py-2">
						<span class="w-2 h-2 rounded-full bg-muted-foreground/50 animate-bounce" style="animation-delay: 0ms"></span>
						<span class="w-2 h-2 rounded-full bg-muted-foreground/50 animate-bounce" style="animation-delay: 150ms"></span>
						<span class="w-2 h-2 rounded-full bg-muted-foreground/50 animate-bounce" style="animation-delay: 300ms"></span>
					</div>
				</div>
			{/if}
		</div>

		<!-- Input -->
		<div class="border-t border-border p-3 shrink-0 {mode === 'fullscreen' ? 'safe-bottom' : ''}">
			<div class="flex items-end gap-2">
				<textarea
					bind:value={message}
					onkeydown={handleKeydown}
					placeholder={chat.activeThread ? `Message ${chat.activeThread.title}...` : 'Ask Enki anything...'}
					rows="1"
					class="flex-1 resize-none bg-muted rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-1 focus:ring-primary max-h-32"
				></textarea>
				<button
					onclick={send}
					disabled={!message.trim() || chat.sending}
					class="p-2 rounded-lg bg-primary text-primary-foreground shrink-0 hover:bg-primary/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
				>
					<Send size={16} />
				</button>
			</div>
		</div>
	{/if}
</div>

<!-- Floating node context menu -->
{#if nodeMenuId}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<button class="fixed inset-0 z-[100]" onclick={closeNodeMenu} aria-label="Close menu"></button>
	{@const menuNode = nodeMenuId ? nodeCache[nodeMenuId] : null}
	<div
		class="fixed z-[101] bg-card border border-border rounded-lg shadow-xl p-1 min-w-[140px]"
		style="left: {nodeMenuPos.x}px; top: {nodeMenuPos.y}px;"
	>
		{#if menuNode}
			<div class="px-2 py-1 text-[10px] text-muted-foreground border-b border-border/50 mb-1 truncate max-w-[200px]">
				{menuNode.title}
			</div>
		{/if}
		<button
			onclick={() => setNodeRelevance(nodeMenuId!, menuNode?.relevance === 'pinned' ? 'active' : 'pinned')}
			class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-muted text-left transition-colors
				{menuNode?.relevance === 'pinned' ? 'text-amber-400' : 'text-foreground'}"
		>
			{#if menuNode?.relevance === 'pinned'}
				<PinOff size={12} /> Unpin from context
			{:else}
				<Pin size={12} /> Pin to context
			{/if}
		</button>
		<button
			onclick={() => setNodeRelevance(nodeMenuId!, menuNode?.relevance === 'dimmed' ? 'active' : 'dimmed')}
			class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-muted text-left transition-colors text-foreground"
		>
			{#if menuNode?.relevance === 'dimmed'}
				<Eye size={12} /> Restore
			{:else}
				<EyeOff size={12} /> Dim (deprioritize)
			{/if}
		</button>
		<button
			onclick={() => setNodeRelevance(nodeMenuId!, menuNode?.relevance === 'unlinked' ? 'active' : 'unlinked')}
			class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-muted text-left transition-colors
				{menuNode?.relevance === 'unlinked' ? 'text-emerald-400' : 'text-red-400'}"
		>
			{#if menuNode?.relevance === 'unlinked'}
				<Link size={12} /> Relink to thread
			{:else}
				<Unlink size={12} /> Unlink from thread
			{/if}
		</button>
		<div class="border-t border-border/50 mt-1 pt-1">
			<button
				onclick={() => { showGraph = true; closeNodeMenu(); }}
				class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-muted text-left text-foreground transition-colors"
			>
				<Network size={12} /> View in graph
			</button>
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
