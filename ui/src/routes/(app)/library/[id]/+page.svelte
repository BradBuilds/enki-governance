<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/stores';
	import {
		LibraryCard, RefCard, EntityCard, FactCard, EventCard,
	} from '$lib/components/cards2';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import Download from 'lucide-svelte/icons/download';
	import FileText from 'lucide-svelte/icons/file-text';
	import Conversation from '$lib/components/chat/Conversation.svelte';
	import type { ChatMessage } from '$lib/stores/chat.svelte';

	const library_id = $derived(parseInt($page.params.id, 10));

	// Library-scoped chat — the same 62-action assistant, context-pinned to this
	// library, with its own conversation state (isolated from the global bubble).
	let libChat = $state<ChatMessage[]>([]);
	let libChatSending = $state(false);
	async function sendLibChat(text: string) {
		libChat = [...libChat, { role: 'user', content: text, timestamp: new Date().toISOString() }];
		libChatSending = true;
		try {
			const res = await fetch('/api/ai/chat', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					message: text,
					context: { currentPage: `/library/${library_id}`, selectedItem: { type: 'library', id: String(library_id) } },
				}),
			});
			const data = res.ok ? await res.json() : null;
			libChat = [...libChat, {
				role: 'assistant',
				content: data?.response || data?.message || 'Sorry, I hit an error.',
				model_used: data?.model,
				action_results: data?.action_results,
				timestamp: new Date().toISOString(),
			}];
			return data;
		} catch {
			libChat = [...libChat, { role: 'assistant', content: 'Connect an AI provider in Settings to enable chat.', timestamp: new Date().toISOString() }];
		} finally {
			libChatSending = false;
		}
	}

	type Tab = 'dossier' | 'timeline' | 'references' | 'documents' | 'entities' | 'facts' | 'events' | 'signals' | 'map' | 'chat' | 'notes';
	let activeTab = $state<Tab>('dossier');

	// Notes tab state (Phase 6 + federation UI)
	type Note = {
		note_id: number;
		note_sha256: string;
		parent_note_sha: string | null;
		subject_kind: string;
		subject_ref: string | null;
		author_node_id: string;
		author_user_id: string | null;
		author_display_name: string | null;
		body: string;
		body_format: string;
		visibility: string;
		is_deletion_marker: boolean;
		pulled_from_node_id: string | null;
		pulled_from_envelope_sha: string | null;
		created_at: string;
	};
	let notes = $state<Note[]>([]);
	let noteCount = $state(0);
	let notesLoading = $state(false);
	let newNoteBody = $state('');
	let newNoteSubjectKind = $state<'library' | 'entity' | 'document'>('library');
	let newNoteSubjectRef = $state('');
	let postingNote = $state(false);

	async function loadNotes() {
		notesLoading = true;
		try {
			const res = await fetch(`/api/libraries/${library_id}/notes`);
			if (res.ok) {
				const data = await res.json();
				notes = data.notes || [];
				noteCount = data.count || 0;
			}
		} catch (e) { console.error('notes load failed', e); }
		notesLoading = false;
	}

	async function postNote() {
		if (!newNoteBody.trim() || postingNote) return;
		postingNote = true;
		try {
			const res = await fetch(`/api/libraries/${library_id}/notes`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					subject_kind: newNoteSubjectKind,
					subject_ref: newNoteSubjectRef.trim() || null,
					body: newNoteBody,
				}),
			});
			if (res.ok) {
				newNoteBody = '';
				newNoteSubjectRef = '';
				await loadNotes();
			} else {
				alert(`Note post failed: ${res.status} ${await res.text()}`);
			}
		} catch (e) {
			alert(`Note post failed: ${e}`);
		}
		postingNote = false;
	}

	async function deleteNote(sha: string) {
		if (!confirm('Soft-delete this note? The original row stays in the audit trail.')) return;
		try {
			const res = await fetch(`/api/libraries/${library_id}/notes/${sha}/delete`, { method: 'POST' });
			if (res.ok) await loadNotes();
		} catch (e) { console.error('delete failed', e); }
	}

	function fmtAgo(iso: string): string {
		const ms = Date.now() - new Date(iso).getTime();
		const sec = Math.floor(ms / 1000);
		if (sec < 60) return `${sec}s ago`;
		const min = Math.floor(sec / 60);
		if (min < 60) return `${min}m ago`;
		const hr = Math.floor(min / 60);
		if (hr < 24) return `${hr}h ago`;
		return `${Math.floor(hr / 24)}d ago`;
	}

	let library = $state<any>(null);
	let about = $state<any>(null);
	let tabData = $state<{ [k in Tab]?: any }>({});
	let coverage = $state<any>(null);
	$effect(() => {
		fetch(`/api/libraries/${library_id}/coverage`).then(r => r.ok ? r.json() : null).then(c => coverage = c).catch(() => {});
	});
	let loadingTab = $state(false);
	let error = $state<string | null>(null);
	let expandedFactId = $state<number | null>(null);
	let activeRun = $state<any>(null);
	let runBusy = $state(false);

	async function loadActiveRun() {
		try {
			const res = await fetch(`/api/library-runs?limit=5`);
			if (res.ok) {
				const data = await res.json();
				activeRun = (data.runs || []).find((r: any) =>
					r.library_id === library_id && ['queued', 'running', 'paused'].includes(r.run_status)
				) || null;
			}
		} catch {}
	}

	async function startRun() {
		runBusy = true;
		try {
			const res = await fetch(`/api/libraries/${library_id}/run`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ priority: 100, started_by: 'operator' }),
			});
			if (res.ok) await loadActiveRun();
		} finally {
			runBusy = false;
		}
	}

	async function loadLibrary() {
		try {
			const [libRes, aboutRes] = await Promise.all([
				fetch(`/api/libraries/${library_id}`),
				fetch(`/api/libraries/${library_id}/about`),
			]);
			if (libRes.ok) library = await libRes.json();
			else error = 'library not found';
			if (aboutRes.ok) about = await aboutRes.json();
		} catch (e: any) {
			error = e.message;
		}
	}

	async function loadTab(tab: Tab) {
		if (tabData[tab]) return;
		if (tab === 'map' || tab === 'chat') return;
		loadingTab = true;
		try {
			const url = tab === 'dossier'
		  ? `/api/libraries/${library_id}/dossier`
		  : `/api/libraries/${library_id}/${tab === 'facts' ? 'facts' : tab}`;
		const res = await fetch(url);
			if (res.ok) tabData[tab] = await res.json();
		} finally {
			loadingTab = false;
		}
	}

	function timelineDate(iso: string | null): string {
		if (!iso) return '';
		try {
			const d = new Date(iso);
			return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
		} catch { return iso; }
	}
	function timelineYear(iso: string | null): string {
		if (!iso) return '—';
		try { return String(new Date(iso).getFullYear()); } catch { return '—'; }
	}

	$effect(() => { loadTab(activeTab); });

	onMount(() => { loadLibrary(); loadActiveRun(); });

	// Three faces of a topic. Live = its news; Archive = its source docs;
	// Intelligence = the collapsed graph.
	const GROUPS = ['Intelligence', 'Live', 'Archive'] as const;
	type Group = typeof GROUPS[number];
	let activeGroup = $state<Group>('Intelligence');

	const tabs: Array<{ id: Tab; label: string; group: Group; count?: number }> = $derived([
		{ id: 'dossier',    label: 'Overview',  group: 'Intelligence' },
		{ id: 'timeline',   label: 'Timeline',  group: 'Intelligence', count: tabData.timeline?.count },
		{ id: 'entities',   label: 'Entities',  group: 'Intelligence', count: library?.entity_count },
		{ id: 'events',     label: 'Events',    group: 'Intelligence', count: tabData.events?.count },
		{ id: 'map',        label: 'Map',       group: 'Intelligence' },
		{ id: 'chat',       label: 'Chat',      group: 'Intelligence' },
		{ id: 'notes',      label: 'Notes',     group: 'Intelligence', count: noteCount },
		{ id: 'signals',    label: 'News',      group: 'Live', count: tabData.signals?.count },
		{ id: 'references', label: 'Sources',   group: 'Archive', count: library?.reference_count },
		{ id: 'documents',  label: 'Documents', group: 'Archive', count: library?.document_count },
	]);

	function switchGroup(g: Group) {
		activeGroup = g;
		const first = tabs.find(t => t.group === g);
		if (first) activeTab = first.id;
	}

	// Load notes when the Notes tab is opened (lazy — most analysts won't open it)
	$effect(() => { if (activeTab === 'notes') loadNotes(); });

	// Group timeline events by year for the time-spine layout
	interface TLEvent {
		assessment_id: number;
		assessment_tier: string;
		subj?: string;
		subj_type?: string;
		subj_entity_id?: number;
		subj_blurb?: string;
		predicate_edge_type: string;
		obj?: string;
		obj_type?: string;
		obj_entity_id?: number;
		obj_blurb?: string;
		obj_value?: string;
		earliest_event_time?: string;
		latest_event_time?: string;
		display_date?: string;
		display_date_kind?: 'occurred' | 'reported' | 'captured';
		location_name?: string;
		location_blurb?: string;
		primary_location_entity_id?: number;
		corroborated_by_count: number;
		contradicted_by_count: number;
		base60_address?: string;
		sources: any[];
	}

	const timelineByYear = $derived.by(() => {
		const events = (tabData.timeline?.events ?? []) as TLEvent[];
		const groups: Map<string, TLEvent[]> = new Map();
		for (const ev of events) {
			// Prefer extracted/reported display_date; fall back to legacy fields.
			const t = ev.display_date || ev.latest_event_time || ev.earliest_event_time;
			let key = 'undated';
			if (t) {
				try { key = String(new Date(t).getFullYear()); } catch {}
			}
			if (!groups.has(key)) groups.set(key, []);
			groups.get(key)!.push(ev);
		}
		// Sort: real years descending, 'undated' last
		const entries = Array.from(groups.entries()).sort((a, b) => {
			if (a[0] === 'undated') return 1;
			if (b[0] === 'undated') return -1;
			return Number(b[0]) - Number(a[0]);
		});
		return entries;
	});

	function typeIconLabel(t?: string): string {
		const m: Record<string, string> = {
			person: '👤', organization: '🏛', government_agency: '🏛',
			country: '🌍', city: '📍', place: '📍', region: '📍',
			facility: '🏗', vehicle: '🚗', aircraft: '✈', vessel: '🚢',
			mass_media: '📰', product: '📦', date: '📅', concept: '💡',
		};
		return m[(t || '').toLowerCase()] || '·';
	}
</script>

<div class="max-w-5xl mx-auto space-y-4">
	<header class="space-y-3">
		<a href="/library" class="text-xs text-muted-foreground hover:text-foreground">◀ Libraries</a>
		{#if library}
			<div class="rounded-xl border border-border bg-card p-5">
				<div class="flex items-baseline gap-3">
					<h1 class="text-2xl font-bold text-foreground">{library.focal_term || library.canonical_name}</h1>
					{#if library.base60_address}
						<span class="text-xs font-mono text-muted-foreground">{library.base60_address}</span>
					{/if}
				</div>
				<!-- Deep-research About card -->
				{#if about?.description}
					<p class="mt-3 text-sm text-foreground/85 leading-relaxed">{about.description}</p>
					{#if about.source}
						<p class="mt-1 text-[10px] text-muted-foreground">
							via {about.source}
							{#if about.source_url}
								· <a href={about.source_url} target="_blank" rel="noopener" class="hover:text-primary">read more →</a>
							{/if}
						</p>
					{/if}
				{:else}
					<p class="mt-2 text-[11px] text-muted-foreground italic">
						No deep-research summary yet — entity_kb_cache for this topic is empty.
					</p>
				{/if}
				<!-- Coverage / completeness bar -->
				{#if coverage && (coverage.total_refs > 0 || coverage.universe_estimate)}
					{@const denom = coverage.universe_estimate || coverage.total_refs}
					{@const pct = Math.min(100, Math.round((coverage.collapsed / Math.max(denom, 1)) * 100))}
					<div class="mt-3">
						<div class="flex items-baseline justify-between text-[11px] text-muted-foreground mb-1">
							<span>Coverage — <span class="text-foreground font-medium">{coverage.collapsed}</span> collapsed
								of {coverage.total_refs} seeded{#if coverage.universe_estimate} · ~{coverage.universe_estimate} known{/if}</span>
							<span class="tabular-nums">{pct}%{#if coverage.pending} · {coverage.pending} queued{/if}</span>
						</div>
						<div class="h-1.5 rounded-full bg-muted/40 overflow-hidden">
							<div class="h-full bg-primary/70 rounded-full" style="width: {pct}%"></div>
						</div>
					</div>
				{/if}
				<!-- Compact counters -->
				<div class="mt-3 flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
					<span>📄 {library.document_count ?? 0} docs · {library.reference_count ?? 0} refs</span>
					<span>👥 {library.entity_count ?? 0} entities tracked</span>
					<span>★ {tabData.timeline?.count ?? library.fact_count ?? 0} facts</span>
					<span class="flex-1"></span>
					{#if activeRun}
						<a href="/admin/workers"
							class="px-2.5 py-1 text-[11px] rounded-md bg-primary/10 text-primary border border-primary/30 hover:bg-primary/20 flex items-center gap-1.5">
							<span class="w-1.5 h-1.5 rounded-full bg-primary animate-pulse"></span>
							{activeRun.run_status === 'running' ? '▶' : '⏸'} run {activeRun.run_id}
							{#if activeRun.current_phase}· {activeRun.current_phase}{/if}
						</a>
					{:else}
						<button onclick={startRun} disabled={runBusy}
							class="px-3 py-1 text-[11px] rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 font-medium">
							{runBusy ? 'Starting…' : '▶ Start project run'}
						</button>
					{/if}
				</div>
			</div>
		{:else if error}
			<p class="text-sm text-destructive">⚠ {error}</p>
		{:else}
			<p class="text-sm text-muted-foreground">Loading…</p>
		{/if}
	</header>

	<!-- Super-tabs: the three faces of a topic -->
	<nav class="flex items-center gap-2 mb-1">
		{#each GROUPS as g}
			<button
				onclick={() => switchGroup(g)}
				class="px-4 py-1.5 text-sm font-medium rounded-full transition-colors {
					activeGroup === g
						? 'bg-primary/15 text-primary'
						: 'text-muted-foreground hover:text-foreground'
				}"
			>{g}</button>
		{/each}
	</nav>

	<!-- Tab strip (within the active face) -->
	<nav class="flex items-center gap-1 border-b border-border overflow-x-auto scrollbar-thin">
		{#each tabs.filter(t => t.group === activeGroup) as tab}
			<button
				onclick={() => activeTab = tab.id}
				class="px-3 py-2 text-sm whitespace-nowrap transition-colors border-b-2 {
					activeTab === tab.id
						? 'text-primary border-primary'
						: 'text-muted-foreground hover:text-foreground border-transparent'
				}"
			>
				{tab.label}
				{#if tab.count !== undefined && tab.count !== null}
					<span class="ml-1 text-[10px] text-muted-foreground/70">{tab.count}</span>
				{/if}
			</button>
		{/each}
	</nav>

	<!-- Tab content -->
	{#if loadingTab}
		<p class="text-sm text-muted-foreground">Loading {activeTab}…</p>
	{:else if activeTab === 'dossier' && tabData.dossier}
		{@const d = tabData.dossier}
		<div class="space-y-6">
			<!-- TIMELINE -->
			<section class="rounded-xl border border-border bg-card">
				<header class="px-4 py-3 border-b border-border flex items-baseline justify-between">
					<h2 class="text-sm font-semibold text-foreground">Timeline</h2>
					<span class="text-[10px] text-muted-foreground">{d.timeline?.length || 0} dated events</span>
				</header>
				<div class="p-4 font-mono text-[12px] leading-relaxed max-h-[400px] overflow-y-auto">
					{#each d.timeline as ev}
						<div class="flex items-baseline gap-3 py-1 hover:bg-muted/30 rounded px-2 -mx-2">
							<span class="text-primary w-[90px] shrink-0 text-[11px]">{ev.date || '—'}</span>
							<span class="text-muted-foreground w-[70px] shrink-0 text-[11px]">{ev.predicate}</span>
							<a href={`/document/${ev.document_id}`} class="text-foreground hover:text-primary flex-1 line-clamp-2">
								{ev.text}
							</a>
						</div>
					{:else}
						<p class="text-sm text-muted-foreground italic">No dated events yet.</p>
					{/each}
				</div>
			</section>

			<!-- FACTS -->
			<section class="rounded-xl border border-border bg-card">
				<header class="px-4 py-3 border-b border-border flex items-baseline justify-between">
					<h2 class="text-sm font-semibold text-foreground">Key facts</h2>
					<span class="text-[10px] text-muted-foreground">{d.facts?.length || 0} predicate-typed claims</span>
				</header>
				<div class="p-4 space-y-1.5 max-h-[400px] overflow-y-auto">
					{#each d.facts as f}
						<div class="flex items-baseline gap-2 py-1 text-[13px] flex-wrap">
							<span class="inline-block px-2 py-0.5 rounded bg-primary/10 text-primary text-[10px] font-mono shrink-0">{f.predicate}</span>
							{#if f.subj_entity_id}
								<a href={`/entity/${f.subj_entity_id}?lens=library/${library_id}`}
									class="text-foreground hover:text-primary hover:underline">{f.subj_name}</a>
							{:else}
								<span class="text-foreground">{f.subj_name || '?'}</span>
							{/if}
							<span class="text-muted-foreground">{f.predicate}</span>
							{#if f.obj_entity_id}
								<a href={`/entity/${f.obj_entity_id}?lens=library/${library_id}`}
									class="text-foreground hover:text-primary hover:underline">{f.obj_name}</a>
							{:else}
								<span class="text-foreground">{f.obj_name || '?'}</span>
							{/if}
							{#if f.corroborated_by_count > 1}
								<span class="text-[10px] text-emerald-400 tabular-nums shrink-0"
									title={`${f.corroborated_by_count} sources corroborate this claim`}>
									×{f.corroborated_by_count}
								</span>
							{/if}
							{#if f.document_id}
								<a href={`/document/${f.document_id}`}
									class="ml-auto text-[10px] text-muted-foreground/60 hover:text-foreground shrink-0"
									title="open source document">[src]</a>
							{/if}
						</div>
					{:else}
						<p class="text-sm text-muted-foreground italic">No structured facts yet.</p>
					{/each}
				</div>
			</section>

			<!-- NETWORK + LEADS side-by-side -->
			<div class="grid grid-cols-1 md:grid-cols-2 gap-4">
				<section class="rounded-xl border border-border bg-card">
					<header class="px-4 py-3 border-b border-border flex items-baseline justify-between">
						<h2 class="text-sm font-semibold text-foreground">Network</h2>
						<span class="text-[10px] text-muted-foreground">Top co-occurring entities</span>
					</header>
					<div class="p-4 space-y-1 max-h-[400px] overflow-y-auto">
						{#each d.network as n}
							<div class="flex items-baseline gap-2 text-[13px] py-0.5">
								{#if n.entity_id}
									<a href={`/entity/${n.entity_id}?lens=library/${library_id}`}
										class="text-foreground hover:text-primary hover:underline flex-1 truncate"
										title={n.type}>{n.name}</a>
								{:else}
									<span class="text-foreground flex-1 truncate">{n.name}</span>
								{/if}
								<span class="text-[10px] text-muted-foreground tabular-nums">{n.mentions}m</span>
								<span class="text-[10px] text-muted-foreground/60 tabular-nums">{n.in_docs}d</span>
							</div>
						{/each}
					</div>
				</section>

				<section class="rounded-xl border border-border bg-card">
					<header class="px-4 py-3 border-b border-border flex items-baseline justify-between">
						<h2 class="text-sm font-semibold text-foreground">Leads</h2>
						<span class="text-[10px] text-muted-foreground">People worth following · 2+ docs</span>
					</header>
					<div class="p-4 space-y-1 max-h-[400px] overflow-y-auto">
						{#each d.leads as l}
							<div class="flex items-baseline gap-2 text-[13px] py-0.5">
								{#if l.entity_id}
									<a href={`/entity/${l.entity_id}?lens=library/${library_id}`}
										class="text-foreground hover:text-primary hover:underline flex-1 truncate">{l.name}</a>
								{:else}
									<span class="text-foreground flex-1 truncate">{l.name}</span>
								{/if}
								<span class="text-[10px] text-muted-foreground tabular-nums">{l.mentions}m</span>
								<span class="text-[10px] text-muted-foreground/60 tabular-nums">{l.in_docs}d</span>
							</div>
						{:else}
							<p class="text-sm text-muted-foreground italic">No cross-doc leads.</p>
						{/each}
					</div>
				</section>
			</div>

			<!-- SOURCES -->
			<section class="rounded-xl border border-border bg-card">
				<header class="px-4 py-3 border-b border-border flex items-baseline justify-between">
					<h2 class="text-sm font-semibold text-foreground">Sources</h2>
					<span class="text-[10px] text-muted-foreground">{d.sources?.length || 0} docs · ranked by extraction richness</span>
				</header>
				<div class="p-4 space-y-1 max-h-[400px] overflow-y-auto">
					{#each d.sources as src}
						<a href={`/document/${src.document_id}`} class="block py-1 text-[13px] hover:bg-muted/30 rounded px-2 -mx-2">
							<div class="flex items-baseline gap-2">
								<FileText size={12} class="text-primary shrink-0" />
								<span class="text-foreground flex-1 truncate">{src.title}</span>
								<span class="text-[10px] text-muted-foreground tabular-nums">{src.mentions}m</span>
								<span class="text-[10px] text-muted-foreground/60 tabular-nums">{src.statements}s</span>
							</div>
						</a>
					{/each}
				</div>
			</section>
		</div>
	{:else if activeTab === 'references' && tabData.references}
		<div class="space-y-3">
			{#each tabData.references.references as ref}
				<RefCard reference_id={ref.reference_id} reference={ref} size="default" />
			{:else}
				<p class="text-sm text-muted-foreground italic">No references yet.</p>
			{/each}
		</div>
	{:else if activeTab === 'documents' && tabData.documents}
		<div class="space-y-2">
			{#each tabData.documents.documents as doc}
				<a href={`/document/${doc.document_id}`} class="block p-3 rounded-lg border border-border hover:border-primary/40 bg-card transition-colors">
					<div class="flex items-center gap-2">
						<FileText size={14} class="text-primary" />
						<span class="text-sm font-medium text-foreground">{doc.title || `Doc ${doc.document_id}`}</span>
						{#if doc.page_count}
							<span class="text-[10px] text-muted-foreground">{doc.page_count} pages</span>
						{/if}
					</div>
				</a>
			{:else}
				<p class="text-sm text-muted-foreground italic">No documents ingested yet.</p>
			{/each}
		</div>
	{:else if activeTab === 'entities' && tabData.entities}
		<div class="space-y-3">
			{#each tabData.entities.entities as ent}
				<EntityCard entity_id={ent.entity_id} entity={ent} size="default" />
			{:else}
				<p class="text-sm text-muted-foreground italic">No entities resolved yet.</p>
			{/each}
		</div>
	{:else if activeTab === 'facts' && tabData.facts}
		<div class="space-y-3">
			{#each tabData.facts.facts as fact}
				<FactCard assessment_id={fact.assessment_id} assessment={fact} size="default" lens={`library/${library_id}`} />
			{:else}
				<p class="text-sm text-muted-foreground italic">No facts derived yet.</p>
			{/each}
		</div>
	{:else if activeTab === 'events' && tabData.events}
		<div class="space-y-3">
			{#each tabData.events.events as evt}
				<EventCard event_id={evt.event_id} event={evt} size="default" />
			{:else}
				<p class="text-sm text-muted-foreground italic">No events spawned yet.</p>
			{/each}
		</div>
	{:else if activeTab === 'signals' && tabData.signals}
		<div class="space-y-2">
			<p class="text-xs text-muted-foreground">
				RSS articles routed to this library by the library_router worker
				(keyword + entity match against
				<code class="px-1 rounded bg-muted text-foreground">{library?.focal_term ?? 'this topic'}</code>).
			</p>
			{#each tabData.signals.signals as sig}
				<a href={sig.link || '#'} target="_blank" rel="noopener noreferrer"
					class="block p-3 rounded-lg border border-border bg-card hover:border-primary/40 transition-colors">
					<div class="flex items-start gap-3">
						{#if sig.thumbnail_url}
							<img src={sig.thumbnail_url} alt="" loading="lazy"
								class="w-16 h-16 rounded object-cover shrink-0 bg-muted"
								onerror={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }} />
						{/if}
						<div class="flex-1 min-w-0">
							<p class="text-sm font-semibold text-foreground line-clamp-2">{sig.headline}</p>
							<div class="flex items-center gap-2 text-[10px] text-muted-foreground mt-1">
								<span class="font-medium text-foreground/80">{sig.source}</span>
								{#if sig.category}
									<span>· {sig.category}</span>
								{/if}
								<span>· {sig.timestamp ? new Date(sig.timestamp).toLocaleString() : ''}</span>
								<span class="ml-auto px-1.5 py-0.5 rounded border border-primary/30 bg-primary/10 text-primary">
									{sig.match_kind}{sig.keyword_hit ? ` · ${sig.keyword_hit}` : ''}
								</span>
							</div>
						</div>
					</div>
				</a>
			{:else}
				<p class="text-sm text-muted-foreground italic">
					No live news matched yet. The router runs every 60s — narrow topic libraries may rarely match.
				</p>
			{/each}
		</div>
	{:else if activeTab === 'timeline' && tabData.timeline}
		<!-- TIMELINE: linear close-line rows. One fact per line. Click name to research. -->
		{#if timelineByYear.length > 0}
			<div class="font-mono text-[13px] leading-relaxed">
				{#each timelineByYear as [year, evts] (year)}
					<div class="mt-4 mb-1 flex items-baseline gap-2">
						<span class="text-base font-bold {year === 'undated' ? 'text-muted-foreground/60' : 'text-primary'}">
							{year}
						</span>
						<span class="text-[10px] uppercase tracking-wider text-muted-foreground/60">
							{evts.length} fact{evts.length === 1 ? '' : 's'}
						</span>
					</div>
					{#each evts as ev (ev.assessment_id)}
						{@const isExpanded = expandedFactId === ev.assessment_id}
						{@const isContested = ev.contradicted_by_count > 0}
						{@const isConfirmed = ev.corroborated_by_count > 1 || ev.assessment_tier === 'fact'}
						<div class="group flex items-baseline gap-2 py-0.5 px-2 -mx-2 hover:bg-muted/30 rounded">
							<!-- Tier dot -->
							<span class="w-1.5 h-1.5 rounded-full shrink-0 mt-1.5
								{isContested ? 'bg-amber-500' :
								 isConfirmed ? 'bg-emerald-500' :
								 'bg-muted-foreground/40'}"
								title={isContested ? `Contested · ${ev.corroborated_by_count} vs ${ev.contradicted_by_count}` :
									   isConfirmed ? `Confirmed · ${ev.corroborated_by_count} sources` :
									   'Reported · 1 source'}></span>
							<!-- Date col -->
							<span class="text-muted-foreground/70 w-[88px] shrink-0 text-[11px]"
								title={ev.display_date_kind === 'occurred' ? 'extracted from text' :
									   ev.display_date_kind === 'reported' ? 'document publication date' :
									   ev.display_date_kind === 'captured' ? 'when Enki captured this' : ''}>
								{ev.display_date ? timelineDate(ev.display_date) :
								 ev.latest_event_time ? timelineDate(ev.latest_event_time) : ''}
								{#if ev.display_date_kind === 'reported'}
									<span class="text-muted-foreground/40 text-[9px]">·rpt</span>
								{:else if ev.display_date_kind === 'captured'}
									<span class="text-muted-foreground/40 text-[9px]">·cap</span>
								{/if}
							</span>
							<!-- Subject -->
							{#if ev.subj_entity_id}
								<a href={`/entity/${ev.subj_entity_id}?lens=library/${library_id}`}
									class="text-foreground hover:text-primary hover:underline"
									title={ev.subj_blurb || 'click to research'}>
									{ev.subj || '?'}
								</a>
							{:else}
								<span class="text-foreground">{ev.subj || '?'}</span>
							{/if}
							<!-- Predicate -->
							<span class="text-muted-foreground">{ev.predicate_edge_type.replace(/_/g, ' ')}</span>
							<!-- Object -->
							{#if ev.obj_entity_id}
								<a href={`/entity/${ev.obj_entity_id}?lens=library/${library_id}`}
									class="text-foreground hover:text-primary hover:underline"
									title={ev.obj_blurb || 'click to research'}>
									{ev.obj || ev.obj_value || '?'}
								</a>
							{:else}
								<span class="text-foreground">{ev.obj || ev.obj_value || '?'}</span>
							{/if}
							<!-- Location chip -->
							{#if ev.location_name}
								<a href={`/entity/${ev.primary_location_entity_id}`}
									class="text-muted-foreground/70 text-[11px] hover:text-primary">
									· {ev.location_name}
								</a>
							{/if}
							<!-- Sources footnote: click to expand -->
							<button onclick={() => expandedFactId = isExpanded ? null : ev.assessment_id}
								class="ml-auto text-[10px] text-muted-foreground/60 hover:text-foreground shrink-0"
								title="sources">
								[{ev.sources?.length || 0}]
							</button>
						</div>
						{#if isExpanded && ev.sources?.length > 0}
							<div class="ml-[110px] mb-1 mt-0.5 pl-2 border-l border-border/40 text-[11px] font-sans">
								{#if ev.subj_blurb}
									<p class="text-muted-foreground italic mb-1">ℹ {ev.subj}: {ev.subj_blurb}</p>
								{/if}
								{#if ev.obj_blurb && ev.obj_blurb !== ev.subj_blurb}
									<p class="text-muted-foreground italic mb-1">ℹ {ev.obj}: {ev.obj_blurb}</p>
								{/if}
								{#each ev.sources as src, i}
									<div class="text-foreground/80 mb-1">
										<span class="text-muted-foreground/60 mr-1">{i + 1}.</span>
										<span class="italic">"{(src.raw_text || '').slice(0, 220)}"</span>
										<span class="text-muted-foreground/70 ml-1">
											—
											{#if src.document_title}
												<a href={`/document/${src.document_id}`} class="hover:text-primary">{src.document_title}</a>
											{:else if src.signal_title}
												<a href={src.signal_link || '#'} target="_blank" rel="noopener" class="hover:text-primary">{src.signal_source}</a>
											{/if}
										</span>
									</div>
								{/each}
							</div>
						{/if}
					{/each}
				{/each}
			</div>
		{:else}
			<div class="rounded-xl border border-dashed border-border p-6 text-center">
				<p class="text-sm text-muted-foreground">
					No facts yet. Check
					<a href="/governance" class="text-primary hover:underline">Governance → Predicates</a>
					to unlock more.
				</p>
			</div>
		{/if}
	{:else if activeTab === 'map'}
		{#await import('$lib/components/map/FlightMap.svelte') then { default: FlightMap }}
			<FlightMap libraryId={Number(library_id)} />
		{/await}
		<a href={`/map?library=${library_id}`} class="inline-block mt-3 text-sm text-primary hover:underline">Open in full map →</a>
	{:else if activeTab === 'chat'}
		<div class="rounded-xl border border-border bg-card overflow-hidden h-[calc(100vh-16rem)] min-h-[360px]">
			<Conversation
				messages={libChat}
				sending={libChatSending}
				onSend={sendLibChat}
				placeholder="Ask about this library — facts, entities, what's new…"
			/>
		</div>
	{:else if activeTab === 'notes'}
		<div class="space-y-4">
			<!-- Federation context strip — what's this library's identity across nodes -->
			{#if library?.federation_id}
				<div class="rounded-xl border border-border bg-card/50 p-3 text-xs">
					<span class="text-muted-foreground">Federation ID:</span>
					<code class="ml-2 font-mono text-foreground">{library.federation_id}</code>
					{#if library.original_producer_node_id}
						<span class="ml-4 text-muted-foreground">Origin:</span>
						<code class="ml-1 font-mono text-foreground">{library.original_producer_node_id}</code>
					{/if}
					<span class="ml-4 text-muted-foreground">Notes propagate via library manifest v2 to all subscribers.</span>
				</div>
			{/if}

			<!-- Compose form -->
			<div class="rounded-xl border border-border bg-card p-4 space-y-3">
				<div class="text-xs font-medium text-foreground">Add a note</div>
				<div class="grid grid-cols-3 gap-3">
					<div>
						<label for="note-kind" class="text-[10px] text-muted-foreground uppercase tracking-wider">Subject</label>
						<select id="note-kind" bind:value={newNoteSubjectKind}
							class="w-full mt-1 px-2 py-1.5 rounded bg-background border border-border text-xs text-foreground">
							<option value="library">library (this whole library)</option>
							<option value="entity">entity (specific person/place/org)</option>
							<option value="document">document (specific source)</option>
						</select>
					</div>
					<div class="col-span-2">
						<label for="note-ref" class="text-[10px] text-muted-foreground uppercase tracking-wider">
							Subject ref {newNoteSubjectKind === 'library' ? '(optional)' : '(required)'}
						</label>
						<input id="note-ref" type="text" bind:value={newNoteSubjectRef}
							placeholder={newNoteSubjectKind === 'entity'
								? 'canonical_entity_key (sha256)'
								: newNoteSubjectKind === 'document'
								? 'content_sha256'
								: 'leave empty for library-scoped note'}
							class="w-full mt-1 px-2 py-1.5 rounded bg-background border border-border text-xs font-mono text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary" />
					</div>
				</div>
				<textarea bind:value={newNoteBody} rows={3}
					placeholder="Markdown note body — observations, leads, contradictions, links between entities…"
					class="w-full px-3 py-2 rounded bg-background border border-border text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary"
				></textarea>
				<div class="flex items-center justify-between">
					<div class="text-[10px] text-muted-foreground">
						Notes are append-only and content-addressed (sha256). Edits create a new note in the chain.
					</div>
					<button type="button" onclick={postNote}
						disabled={postingNote || !newNoteBody.trim()}
						class="inline-flex items-center px-3 py-1.5 rounded-md text-xs bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40 disabled:cursor-not-allowed">
						{postingNote ? 'Posting…' : 'Post note'}
					</button>
				</div>
			</div>

			<!-- Notes list -->
			<div class="rounded-xl border border-border bg-card overflow-hidden">
				<div class="px-4 py-2 border-b border-border text-[10px] uppercase tracking-wider text-muted-foreground">
					{notes.length} note{notes.length === 1 ? '' : 's'}
				</div>
				{#if notesLoading && notes.length === 0}
					<div class="p-8 text-center text-sm text-muted-foreground">Loading…</div>
				{:else if notes.length === 0}
					<div class="p-8 text-center text-sm text-muted-foreground">
						No notes yet. Be the first to annotate this library.
					</div>
				{:else}
					<ul class="divide-y divide-border">
						{#each notes as n (n.note_sha256)}
							<li class="p-4 space-y-2 hover:bg-background/30 transition-colors">
								<div class="flex items-start justify-between gap-3">
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2 text-xs">
											<span class="text-foreground font-medium">
												{n.author_display_name || n.author_user_id || 'system'}
											</span>
											<span class="text-muted-foreground">·</span>
											<span class="text-muted-foreground">{fmtAgo(n.created_at)}</span>
											{#if n.pulled_from_node_id}
												<span class="text-muted-foreground">·</span>
												<span class="text-[10px] px-1.5 py-0.5 rounded bg-background border border-border text-muted-foreground">
													pulled from {n.pulled_from_node_id}
												</span>
											{/if}
											{#if n.subject_kind !== 'library'}
												<span class="text-muted-foreground">·</span>
												<span class="text-[10px] px-1.5 py-0.5 rounded bg-background border border-border text-muted-foreground">
													{n.subject_kind}: {n.subject_ref?.slice(0, 16) ?? '—'}…
												</span>
											{/if}
											{#if n.parent_note_sha}
												<span class="text-muted-foreground">·</span>
												<span class="text-[10px] text-muted-foreground" title={n.parent_note_sha}>edit</span>
											{/if}
										</div>
										<div class="mt-1.5 text-sm text-foreground whitespace-pre-wrap">{n.body}</div>
										<div class="mt-1.5 font-mono text-[10px] text-muted-foreground" title={n.note_sha256}>
											{n.note_sha256.slice(0, 16)}…
										</div>
									</div>
									{#if !n.pulled_from_node_id && !n.is_deletion_marker}
										<button type="button" onclick={() => deleteNote(n.note_sha256)}
											class="text-[10px] text-muted-foreground hover:text-rose-400 transition-colors">
											delete
										</button>
									{/if}
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		</div>
	{/if}
</div>
