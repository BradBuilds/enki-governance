/**
 * Chat state: multi-thread conversations with per-thread AI providers.
 *
 * Each thread is an independent research context that can:
 *  - Be pinned to a specific AI model
 *  - Be linked to a project or case
 *  - Run in background (monitoring mode)
 *  - Produce knowledge nodes as durable output
 */

export interface ChatMessage {
	rid?: number;
	role: 'user' | 'assistant' | 'system';
	content: string;
	provider_id?: string;
	model_used?: string;
	actions?: any[];
	action_results?: any[];
	nodes_produced?: string[];
	edges_produced?: string[];
	tokens_in?: number;
	tokens_out?: number;
	timestamp?: string;
}

export interface ChatThread {
	thread_id: string;
	title: string;
	description?: string;
	provider_id?: string;
	provider_name?: string;
	provider_type?: string;
	model_name?: string;
	status: 'active' | 'background' | 'archived';
	thread_type: 'research' | 'monitoring' | 'triage' | 'analysis';
	project_id?: string;
	case_id?: string;
	auto_run: boolean;
	message_count: number;
	node_count: number;
	last_message?: string;
	created_at?: string;
	updated_at?: string;
}

export interface AIProvider {
	provider_id: string;
	provider: string;
	model: string;
	display_name: string;
	capabilities: string[];
	is_default: boolean;
	enabled: boolean;
}

export interface ChatContext {
	currentPage: string;
	selectedItem?: { type: string; id: string };
	mapBounds?: { north: number; south: number; east: number; west: number };
	filters?: Record<string, any>;
}

class ChatState {
	// Panel state. Default view is the conversation (quick chat) — the bubble
	// opens straight into a chat, not the thread list. The list/new views are
	// reached on demand via the header's History control.
	isOpen = $state(false);
	view = $state<'list' | 'thread' | 'new'>('thread');

	// Thread data
	threads = $state<ChatThread[]>([]);
	activeThreadId = $state<string | null>(null);
	activeMessages = $state<ChatMessage[]>([]);

	// Providers
	providers = $state<AIProvider[]>([]);

	// Loading states
	loading = $state(false);
	threadsLoading = $state(false);
	sending = $state(false);

	// Context
	context = $state<ChatContext>({ currentPage: '/' });

	// Quick chat (no thread, legacy single-message mode for fast questions)
	quickMessages = $state<ChatMessage[]>([]);

	private WELCOME: ChatMessage = {
		role: 'assistant',
		content: 'Welcome to Enki. Start a research thread, or ask a quick question. I can search your data, manage cases, set alerts, and create detection rules.'
	};

	constructor() {
		if (typeof window !== 'undefined') {
			this.quickMessages = [this.WELCOME];
		}
	}

	// ─── Panel Controls ──────────────────────────────────────────────────

	toggle() {
		this.isOpen = !this.isOpen;
		if (this.isOpen && this.threads.length === 0) {
			this.loadThreads();
			this.loadProviders();
		}
	}
	open() {
		this.isOpen = true;
		if (this.threads.length === 0) {
			this.loadThreads();
			this.loadProviders();
		}
	}
	close() { this.isOpen = false; }

	setContext(ctx: Partial<ChatContext>) {
		const next = { ...this.context, ...ctx };
		if (JSON.stringify(next) !== JSON.stringify(this.context)) {
			this.context = next;
		}
	}

	// ─── Thread Management ───────────────────────────────────────────────

	async loadThreads() {
		this.threadsLoading = true;
		try {
			const res = await fetch('/api/threads?status=active&limit=50');
			if (res.ok) {
				const data = await res.json();
				this.threads = data.threads || [];
			}
		} catch { /* backend unavailable */ }
		this.threadsLoading = false;
	}

	async createThread(opts: {
		title: string;
		description?: string;
		provider_id?: string;
		thread_type?: string;
		project_id?: string;
		case_id?: string;
	}): Promise<string | null> {
		try {
			const res = await fetch('/api/threads', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					title: opts.title,
					description: opts.description,
					provider_id: opts.provider_id,
					thread_type: opts.thread_type || 'research',
					project_id: opts.project_id,
					case_id: opts.case_id,
				}),
			});
			if (res.ok) {
				const data = await res.json();
				await this.loadThreads();
				return data.thread_id;
			}
		} catch { /* */ }
		return null;
	}

	async openThread(threadId: string) {
		this.activeThreadId = threadId;
		this.view = 'thread';
		this.loading = true;
		try {
			const res = await fetch(`/api/threads/${threadId}`);
			if (res.ok) {
				const data = await res.json();
				this.activeMessages = data.messages || [];
			}
		} catch { /* */ }
		this.loading = false;
	}

	async archiveThread(threadId: string) {
		try {
			await fetch(`/api/threads/${threadId}`, { method: 'DELETE' });
			this.threads = this.threads.filter(t => t.thread_id !== threadId);
			if (this.activeThreadId === threadId) {
				this.activeThreadId = null;
				this.activeMessages = [];
				this.view = 'list';
			}
		} catch { /* */ }
	}

	async updateThread(threadId: string, updates: Partial<ChatThread>) {
		try {
			await fetch(`/api/threads/${threadId}`, {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify(updates),
			});
			await this.loadThreads();
		} catch { /* */ }
	}

	backToList() {
		this.view = 'list';
		this.activeThreadId = null;
		this.activeMessages = [];
	}

	showNewThread() {
		this.view = 'new';
	}

	// ─── Messaging ───────────────────────────────────────────────────────

	async sendMessage(message: string, opts?: { provider_id?: string }) {
		if (!message.trim() || this.sending) return;

		// If in a thread, send to thread
		if (this.activeThreadId) {
			return this.sendThreadMessage(this.activeThreadId, message, opts);
		}

		// Quick chat (no thread) — use legacy endpoint
		return this.sendQuickMessage(message);
	}

	private async sendThreadMessage(threadId: string, message: string, opts?: { provider_id?: string }) {
		this.sending = true;
		this.activeMessages = [...this.activeMessages, {
			role: 'user', content: message, timestamp: new Date().toISOString()
		}];

		try {
			const res = await fetch(`/api/threads/${threadId}/messages`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					message,
					provider_id: opts?.provider_id,
					context: this.context,
				}),
			});

			if (res.ok) {
				const data = await res.json();
				this.activeMessages = [...this.activeMessages, {
					role: 'assistant',
					content: data.response || 'Done.',
					model_used: data.model,
					action_results: data.action_results,
					nodes_produced: data.nodes_produced,
					edges_produced: data.edges_produced,
					timestamp: new Date().toISOString(),
				}];

				// Update thread in list
				const thread = this.threads.find(t => t.thread_id === threadId);
				if (thread) {
					thread.message_count += 2;
					thread.node_count += (data.nodes_produced?.length || 0);
					thread.last_message = message.slice(0, 120);
				}

				return data;
			} else {
				this.activeMessages = [...this.activeMessages, {
					role: 'assistant',
					content: 'Sorry, I encountered an error. Please try again.',
					timestamp: new Date().toISOString(),
				}];
			}
		} catch {
			this.activeMessages = [...this.activeMessages, {
				role: 'assistant',
				content: 'Connect an AI provider in Settings to enable conversation.',
				timestamp: new Date().toISOString(),
			}];
		} finally {
			this.sending = false;
		}
	}

	private async sendQuickMessage(message: string) {
		this.sending = true;
		this.quickMessages = [...this.quickMessages, {
			role: 'user', content: message, timestamp: new Date().toISOString()
		}];

		try {
			const res = await fetch('/api/ai/chat', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ message, context: this.context }),
			});

			if (res.ok) {
				const data = await res.json();
				this.quickMessages = [...this.quickMessages, {
					role: 'assistant',
					content: data.response || data.message || 'Done.',
					action_results: data.action_results,
					timestamp: new Date().toISOString(),
				}];
				return data;
			} else {
				this.quickMessages = [...this.quickMessages, {
					role: 'assistant',
					content: 'Sorry, I encountered an error.',
					timestamp: new Date().toISOString(),
				}];
			}
		} catch {
			this.quickMessages = [...this.quickMessages, {
				role: 'assistant',
				content: 'Connect an AI provider in Settings to enable conversation.',
				timestamp: new Date().toISOString(),
			}];
		} finally {
			this.sending = false;
		}
	}

	// Promote a quick chat into a full thread
	async promoteToThread(title: string, provider_id?: string): Promise<string | null> {
		const threadId = await this.createThread({ title, provider_id });
		if (threadId) {
			await this.openThread(threadId);
		}
		return threadId;
	}

	// ─── Providers ───────────────────────────────────────────────────────

	async loadProviders() {
		try {
			const res = await fetch('/api/providers');
			if (res.ok) {
				const data = await res.json();
				this.providers = data.providers || [];
			}
		} catch { /* */ }
	}

	get defaultProvider(): AIProvider | undefined {
		return this.providers.find(p => p.is_default);
	}

	get activeThread(): ChatThread | undefined {
		return this.threads.find(t => t.thread_id === this.activeThreadId);
	}

	get currentMessages(): ChatMessage[] {
		if (this.activeThreadId) return this.activeMessages;
		return this.quickMessages;
	}

	clearQuickChat() {
		this.quickMessages = [this.WELCOME];
	}
}

export const chat = new ChatState();
