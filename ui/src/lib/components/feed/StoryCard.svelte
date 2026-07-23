<script lang="ts">
	/**
	 * StoryCard — unified feed / article / story detail view.
	 *
	 * Replaces NodeDetailModal + ArticleReader. Renders one coherent
	 * surface regardless of whether the payload is a single article
	 * or a multi-source cluster:
	 *   - single article → just the detail (no "contributing articles")
	 *   - cluster        → summary + conflicts + collapsible article list
	 *
	 * Persistent action row at the bottom always shows Remember,
	 * Research, Open original, plus TTS play in the header.
	 */

	import { tts } from '$lib/stores/tts.svelte';
	import EntityChip from '$lib/components/shared/EntityChip.svelte';
	import { isBookmarked, toggleBookmark } from '$lib/stores/bookmarks.svelte';
	import X from 'lucide-svelte/icons/x';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Users from 'lucide-svelte/icons/users';
	import Globe from 'lucide-svelte/icons/globe';
	import Building from 'lucide-svelte/icons/building';
	import Play from 'lucide-svelte/icons/play';
	import Pause from 'lucide-svelte/icons/pause';
	import Square from 'lucide-svelte/icons/square';
	import Settings2 from 'lucide-svelte/icons/settings-2';
	import Volume2 from 'lucide-svelte/icons/volume-2';
	import Zap from 'lucide-svelte/icons/zap';
	import Star from 'lucide-svelte/icons/star';
	import FlaskConical from 'lucide-svelte/icons/flask-conical';
	import MessageSquare from 'lucide-svelte/icons/message-square';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import ShieldAlert from 'lucide-svelte/icons/shield-alert';
	import ShieldQuestion from 'lucide-svelte/icons/shield-question';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';

	type Story = {
		id?: string;
		node_id?: string;
		headline?: string;
		title?: string;
		summary?: string;
		thumbnail_url?: string;
		category?: string;
		article_count?: number;
		source_count?: number;
		sources?: string[];
		source?: string;
		keywords?: string[];
		countries?: string[];
		people?: string[];
		organizations?: string[];
		severity?: string;
		timestamp?: string;
		velocity?: number;
		articles?: any[];
		conflicts?: any;
		source_url?: string;
		author?: string;
		full_text?: string;
		reality_status?: string | null;
	};

	let { story, onClose, onAsk }: {
		story: Story;
		onClose: () => void;
		onAsk?: (story: Story) => void;
	} = $props();

	let showVoiceSettings = $state(false);
	let expandedArticles = $state(false);
	let researchBusy = $state(false);
	let rememberBusy = $state(false);

	// Research state — polled while running
	let research = $state<any>(null);
	let researchPollTimer: ReturnType<typeof setInterval> | null = null;

	// ─── Derived data ──────────────────────────────────────────────
	const articles = $derived(story?.articles || []);
	const isCluster = $derived(articles.length > 1);
	const primaryArticle = $derived(articles[0] || story);

	const headline = $derived(
		story?.headline || story?.title || primaryArticle?.headline || primaryArticle?.title || 'Untitled'
	);

	const summary = $derived(
		(story?.summary || primaryArticle?.summary || '')
			.replace(/https?:\/\/\S+/g, '')
			.replace(/Article URL:\s*/gi, '')
			.replace(/Comments:\s*https?:\/\/\S*/gi, '')
			.trim()
	);

	const source = $derived(
		story?.sources?.[0] || story?.source || primaryArticle?.source || 'Unknown'
	);

	const sourceUrl = $derived(
		story?.source_url ||
		primaryArticle?.source_url ||
		primaryArticle?.payload?.link ||
		''
	);

	const publishedAt = $derived(
		story?.timestamp || primaryArticle?.timestamp || primaryArticle?.payload?.published || ''
	);

	const author = $derived(
		primaryArticle?.payload?.author || story?.author || ''
	);

	const bodyText = $derived(
		(primaryArticle?.full_text ||
			primaryArticle?.payload?.full_text ||
			primaryArticle?.payload?.content ||
			primaryArticle?.summary ||
			story?.full_text ||
			'').trim()
	);

	const paragraphs = $derived.by(() => {
		if (!bodyText) return [];
		const parts = bodyText.split(/\n\s*\n/).map((p: string) => p.trim()).filter(Boolean);
		if (parts.length > 1) return parts;
		const sentences = bodyText.match(/[^.!?]+[.!?]+/g) || [bodyText];
		const chunks: string[] = [];
		let buf = '';
		for (const s of sentences) {
			buf += s + ' ';
			if (buf.length > 300) { chunks.push(buf.trim()); buf = ''; }
		}
		if (buf.trim()) chunks.push(buf.trim());
		return chunks;
	});

	const wordCount = $derived(bodyText.split(/\s+/).filter(Boolean).length);
	const readingTimeMin = $derived(wordCount > 0 ? Math.max(1, Math.ceil(wordCount / 230)) : 0);

	// Conflicts
	const conflicts = $derived(story?.conflicts?.conflicts || []);
	const conflictCount = $derived(story?.conflicts?.count || 0);
	const conflictSeverity = $derived(story?.conflicts?.severity || 'low');
	const severityBadge = $derived(
		conflictSeverity === 'high'
			? { color: 'text-red-400 bg-red-500/10 border-red-500/30', label: 'HIGH' }
			: conflictSeverity === 'medium'
			? { color: 'text-amber-400 bg-amber-500/10 border-amber-500/30', label: 'MEDIUM' }
			: { color: 'text-muted-foreground bg-muted/30 border-border', label: 'LOW' }
	);

	// Reality status pill (Phase 1 validation)
	const realityBadge = $derived.by(() => {
		const s = story?.reality_status;
		if (s === 'verified') return { icon: ShieldCheck, color: 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10', label: 'verified' };
		if (s === 'ambiguous') return { icon: ShieldQuestion, color: 'text-amber-400 border-amber-500/30 bg-amber-500/10', label: 'ambiguous' };
		if (s === 'unresolved') return { icon: ShieldAlert, color: 'text-orange-400 border-orange-500/30 bg-orange-500/10', label: 'unresolved' };
		if (s === 'stoplisted') return { icon: ShieldAlert, color: 'text-red-400 border-red-500/30 bg-red-500/10', label: 'flagged' };
		return null;
	});

	const fullReadText = $derived(headline && (bodyText || summary) ? `${headline}\n\n${bodyText || summary}` : (bodyText || summary || headline));
	const readProgress = $derived.by(() => {
		if (tts.state === 'idle' || !fullReadText) return 0;
		if (tts.currentText !== fullReadText) return 0;
		return Math.min(tts.currentOffset / Math.max(fullReadText.length, 1), 1);
	});

	const nodeId = $derived(story?.node_id || story?.id || '');
	const remembered = $derived(nodeId ? isBookmarked(nodeId) : false);

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') closeAll();
	}

	function playStory() {
		tts.toggleArticle(headline, bodyText || summary || '');
	}

	function closeAll() {
		tts.stop();
		onClose();
	}

	async function handleRemember() {
		if (!nodeId || rememberBusy) return;
		rememberBusy = true;
		await toggleBookmark(nodeId);
		rememberBusy = false;
	}

	async function loadResearch(poll = false) {
		if (!nodeId) return;
		try {
			const res = await fetch(`/api/stories/${encodeURIComponent(nodeId)}/research`);
			if (res.status === 404) {
				research = null;
				return;
			}
			if (res.ok) research = await res.json();
		} catch (e) {
			console.error('loadResearch failed', e);
		}
		if (poll && research?.status === 'running') {
			if (researchPollTimer) clearInterval(researchPollTimer);
			researchPollTimer = setInterval(async () => {
				await loadResearch(false);
				if (research?.status !== 'running' && researchPollTimer) {
					clearInterval(researchPollTimer);
					researchPollTimer = null;
				}
			}, 2500);
		}
	}

	async function handleResearch() {
		if (researchBusy || !nodeId) return;
		researchBusy = true;
		try {
			const isStale = research?.status === 'complete' &&
				research?.completed_at &&
				(Date.now() - new Date(research.completed_at).getTime()) > 7 * 24 * 3600 * 1000;
			const force = research?.status === 'complete' && !isStale ? false : true;
			const res = await fetch(
				`/api/stories/${encodeURIComponent(nodeId)}/research${force ? '?force=true' : ''}`,
				{ method: 'POST' },
			);
			if (res.ok) {
				research = await res.json();
				if (research?.status === 'running' || research?.status === 'pending') {
					if (researchPollTimer) clearInterval(researchPollTimer);
					researchPollTimer = setInterval(async () => {
						await loadResearch(false);
						if (research?.status !== 'running' && research?.status !== 'pending' && researchPollTimer) {
							clearInterval(researchPollTimer);
							researchPollTimer = null;
						}
					}, 2500);
				}
			}
		} catch (e) {
			console.error('research start failed', e);
		}
		researchBusy = false;
	}

	$effect(() => {
		if (nodeId) loadResearch(true);
		return () => {
			if (researchPollTimer) { clearInterval(researchPollTimer); researchPollTimer = null; }
		};
	});

	const researchDone = $derived(research?.status === 'complete');
	const researchRunning = $derived(research?.status === 'running' || research?.status === 'pending');
	const researchPct = $derived(
		research?.stages_total ? Math.round((research.stages_done / research.stages_total) * 100) : 0,
	);
	const researchAge = $derived.by(() => {
		if (!research?.completed_at) return '';
		const diff = Date.now() - new Date(research.completed_at).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	});
	const researchStageLabel = $derived.by(() => {
		const s = research?.current_stage;
		if (s === 'hydrate') return 'Fetching article text…';
		if (s === 'analyze') return 'Extracting claims with Gemma…';
		if (s === 'discover') return 'Searching GDELT + Google News for corroboration…';
		if (s === 'cross_check') return 'Cross-checking candidate sources…';
		if (s === 'persist') return 'Stitching into the knowledge graph…';
		if (s === 'finalize') return 'Verifying entities…';
		if (s === 'done') return 'Complete';
		return 'Starting…';
	});

	function timeAgo(ts: string | undefined): string {
		if (!ts) return '';
		try {
			const diff = Date.now() - new Date(ts).getTime();
			const mins = Math.floor(diff / 60000);
			if (mins < 60) return `${mins}m ago`;
			const hrs = Math.floor(mins / 60);
			if (hrs < 24) return `${hrs}h ago`;
			return `${Math.floor(hrs / 24)}d ago`;
		} catch { return ts; }
	}

	const conflictRows = $derived.by(() =>
		conflicts.map((c: any, idx: number) => ({
			idx,
			type: c.type,
			trigger: c.trigger,
			positions: [
				{
					stance: c.article_a.stance || (c.type === 'contradiction' ? 'side A' : 'confirmed'),
					headline: c.article_a.headline || '',
					source: c.article_a.source || 'unknown',
				},
				{
					stance: c.article_b.stance || (c.type === 'contradiction' ? 'side B' : 'hedging'),
					headline: c.article_b.headline || '',
					source: c.article_b.source || 'unknown',
				},
			],
		}))
	);
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="fixed inset-0 z-50 flex justify-end" onclick={closeAll}>
	<div class="absolute inset-0 bg-black/60 backdrop-blur-sm"></div>
	<div class="relative w-full max-w-4xl bg-card border-l border-border shadow-2xl overflow-y-auto flex flex-col animate-slide-in"
		onclick={(e) => e.stopPropagation()}>

		<!-- Sticky header -->
		<div class="sticky top-0 z-10 bg-card/95 backdrop-blur border-b border-border">
			<div class="px-5 py-3 flex items-center gap-3">
				{#if tts.isSupported}
					<button
						onclick={playStory}
						disabled={tts.state === 'loading'}
						class="flex items-center justify-center w-10 h-10 rounded-full bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-60 transition-colors shrink-0"
						title={tts.state === 'speaking' && tts.currentText === fullReadText ? 'Pause' :
							tts.state === 'paused' && tts.currentText === fullReadText ? 'Resume' :
							tts.state === 'loading' ? 'Generating audio...' : 'Read aloud'}
					>
						{#if tts.state === 'loading' && tts.currentText === fullReadText}
							<div class="w-5 h-5 border-2 border-primary-foreground/30 border-t-primary-foreground rounded-full animate-spin"></div>
						{:else if tts.state === 'speaking' && tts.currentText === fullReadText}
							<Pause class="w-5 h-5" />
						{:else}
							<Play class="w-5 h-5 ml-0.5" />
						{/if}
					</button>
					{#if tts.state !== 'idle' && tts.currentText === fullReadText}
						<button onclick={() => tts.stop()}
							class="flex items-center justify-center w-9 h-9 rounded-full bg-muted text-muted-foreground hover:text-foreground transition-colors shrink-0"
							title="Stop">
							<Square class="w-4 h-4" />
						</button>
					{/if}
				{/if}

				<div class="flex-1 min-w-0">
					<div class="flex items-center gap-2 text-xs text-muted-foreground flex-wrap">
						<span class="font-medium text-foreground/80">{source}</span>
						{#if publishedAt}<span>· {timeAgo(publishedAt)}</span>{/if}
						{#if readingTimeMin > 0}<span>· {readingTimeMin} min read</span>{/if}
						{#if isCluster}<span>· {story.source_count || articles.length} sources · {story.article_count || articles.length} articles</span>{/if}
					</div>
				</div>

				<button onclick={() => showVoiceSettings = !showVoiceSettings}
					class="p-2 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground shrink-0"
					title="Voice settings">
					<Settings2 class="w-4 h-4" />
				</button>
				{#if sourceUrl}
					<a href={sourceUrl} target="_blank" rel="noopener"
						class="p-2 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground shrink-0"
						title="Open original">
						<ExternalLink class="w-4 h-4" />
					</a>
				{/if}
				<button onclick={closeAll}
					class="p-2 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground shrink-0"
					title="Close (Esc)">
					<X class="w-4 h-4" />
				</button>
			</div>

			<!-- Progress bar while TTS speaking -->
			{#if tts.state !== 'idle' && tts.currentText === fullReadText}
				<div class="h-0.5 bg-muted">
					<div class="h-full bg-primary transition-all" style="width: {readProgress * 100}%"></div>
				</div>
			{/if}

			<!-- Voice settings -->
			{#if showVoiceSettings}
				<div class="border-t border-border/50 px-5 py-3 bg-muted/20 space-y-2">
					<div class="flex items-center gap-2">
						<Volume2 class="w-3.5 h-3.5 text-muted-foreground" />
						<span class="text-[10px] text-muted-foreground w-14">Provider</span>
						<select value={tts.provider} onchange={(e) => tts.setProvider((e.target as HTMLSelectElement).value as any)}
							class="flex-1 bg-card border border-border rounded-md px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-ring">
							<option value="web_speech">Web Speech (browser, free)</option>
							<option value="google" disabled={!tts.availableProviders.google}>
								Google Cloud TTS {tts.availableProviders.google ? '(natural)' : '— not configured'}
							</option>
							<option value="openai" disabled={!tts.availableProviders.openai}>
								OpenAI TTS {tts.availableProviders.openai ? '(natural)' : '— not configured'}
							</option>
						</select>
					</div>
					<div class="flex items-center gap-2">
						<span class="text-[10px] text-muted-foreground w-14">Speed</span>
						<input type="range" min="0.5" max="2.5" step="0.1" value={tts.rate}
							oninput={(e) => tts.setRate(parseFloat((e.target as HTMLInputElement).value))}
							class="flex-1 accent-primary h-1" />
						<span class="text-[10px] text-muted-foreground w-10 text-right font-mono">{tts.rate.toFixed(1)}x</span>
					</div>
				</div>
			{/if}
		</div>

		<!-- Research banner (running / complete / error) -->
		{#if researchRunning}
			<div class="px-5 py-3 border-b border-primary/30 bg-primary/5 flex items-center gap-3">
				<div class="w-4 h-4 border-2 border-primary/30 border-t-primary rounded-full animate-spin shrink-0"></div>
				<div class="flex-1 min-w-0">
					<div class="text-xs font-medium text-primary">{researchStageLabel}</div>
					<div class="mt-1 h-1 bg-primary/10 rounded-full overflow-hidden">
						<div class="h-full bg-primary transition-all duration-500" style="width: {researchPct}%"></div>
					</div>
				</div>
				<div class="text-[10px] text-muted-foreground shrink-0">
					stage {research?.stages_done || 0} / {research?.stages_total || 6}
				</div>
			</div>
		{:else if researchDone}
			{@const corrScore = research?.summary?.corroboration_score ?? 0}
			{@const contested = research?.summary?.contested_count ?? 0}
			{@const factCount = (research?.summary?.atomic_facts || []).length}
			<div class="px-5 py-2 border-b border-emerald-500/20 bg-emerald-500/5">
				<div class="flex items-center gap-3 text-xs">
					<ShieldCheck class="w-3.5 h-3.5 text-emerald-400 shrink-0" />
					<span class="text-emerald-400 font-medium">Researched {researchAge}</span>
					<span class="text-muted-foreground">
						· {(research?.claims || []).length} claims
						· {factCount} atomic facts
						· {contested} contested
						· {(research?.sources_used || []).length} cross-sources
					</span>
				</div>
				{#if (research?.claims || []).length > 0}
					<div class="mt-1.5 flex items-center gap-2 text-[10px]">
						<span class="text-muted-foreground w-24">Corroboration</span>
						<div class="flex-1 h-1 bg-muted/40 rounded-full overflow-hidden">
							<div class="h-full bg-emerald-400 transition-all" style="width: {corrScore * 100}%"></div>
						</div>
						<span class="text-emerald-400 font-mono w-10 text-right">{Math.round(corrScore * 100)}%</span>
					</div>
				{/if}
			</div>
		{:else if research?.status === 'error'}
			<div class="px-5 py-2 border-b border-red-500/30 bg-red-500/5 flex items-center gap-2 text-xs">
				<ShieldAlert class="w-3.5 h-3.5 text-red-400 shrink-0" />
				<span class="text-red-400">Research failed: {research?.error || 'unknown error'}</span>
			</div>
		{/if}

		<!-- Body -->
		<div class="flex-1 px-8 py-6">
			<article
				class="max-w-[70ch] mx-auto"
				data-researchable
				data-node-id={story.node_id || story.id}
			>
				<!-- Badge row -->
				<div class="flex items-center gap-2 mb-3 flex-wrap">
					{#if story.category}
						<span class="text-[10px] px-2 py-0.5 rounded-full bg-primary/10 text-primary border border-primary/20">
							{story.category}
						</span>
					{/if}
					{#if realityBadge}
						<span class="text-[10px] px-2 py-0.5 rounded-full flex items-center gap-1 border {realityBadge.color}">
							<svelte:component this={realityBadge.icon} class="w-2.5 h-2.5" />
							{realityBadge.label}
						</span>
					{/if}
					{#if conflictCount > 0}
						<span class="text-[10px] px-2 py-0.5 rounded-full flex items-center gap-1 border {severityBadge.color}">
							<AlertTriangle class="w-2.5 h-2.5" />
							{conflictCount} conflict{conflictCount > 1 ? 's' : ''}
						</span>
					{/if}
					{#if (story.velocity ?? 0) > 0}
						<span class="text-[10px] text-amber-400 flex items-center gap-0.5">
							<Zap class="w-2.5 h-2.5" /> {story.velocity}/hr
						</span>
					{/if}
				</div>

				<!-- Headline -->
				<h1 class="text-2xl font-bold text-foreground leading-tight mb-2">{headline}</h1>

				<!-- Byline -->
				{#if author}
					<p class="text-sm text-muted-foreground mb-4">By <span class="text-foreground">{author}</span></p>
				{/if}

				<!-- Entity chips -->
				{#if (story?.countries?.length || story?.people?.length || story?.organizations?.length)}
					<div class="flex flex-wrap gap-1.5 mb-5 pb-4 border-b border-border/50">
						{#each (story?.countries || []).slice(0, 6) as c}
							<EntityChip name={c} entityType="country" />
						{/each}
						{#each (story?.people || []).slice(0, 6) as p}
							<EntityChip name={p} entityType="person" />
						{/each}
						{#each (story?.organizations || []).slice(0, 6) as o}
							<EntityChip name={o} entityType="organization" />
						{/each}
					</div>
				{/if}

				<!-- Researched summary (Gemma-produced) -->
				{#if researchDone && research?.summary?.bullets?.length}
					<section class="mb-6">
						<h3 class="text-[10px] font-semibold text-emerald-400 uppercase tracking-wider mb-2 flex items-center gap-1.5">
							<ShieldCheck class="w-3 h-3" /> Researched summary
						</h3>
						<ul class="text-sm text-foreground/90 leading-relaxed space-y-1.5 list-disc ml-5">
							{#each research.summary.bullets as bullet}
								<li>{bullet}</li>
							{/each}
						</ul>
						{#if research?.summary?.open_questions?.length}
							<div class="mt-3 text-[11px] text-muted-foreground">
								<span class="font-medium text-amber-400">Open questions:</span>
								<ul class="list-disc ml-5 mt-1 space-y-0.5">
									{#each research.summary.open_questions as q}
										<li>{q}</li>
									{/each}
								</ul>
							</div>
						{/if}
					</section>
				{:else if summary && (isCluster || !bodyText)}
					<section class="mb-6">
						<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
							Summary
						</h3>
						<p class="text-sm text-foreground/90 leading-relaxed whitespace-pre-wrap">{summary}</p>
					</section>
				{/if}

				<!-- Atomic facts (the "node card" feel — verifiable atoms) -->
				{#if researchDone && (research?.summary?.atomic_facts || []).length}
					<section class="mb-6">
						<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
							Atomic facts ({(research.summary.atomic_facts || []).length})
						</h3>
						<div class="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
							{#each (research.summary.atomic_facts || []) as f}
								{@const kindColor = f.kind === 'date' ? 'text-cyan-400 border-cyan-500/30' :
									f.kind === 'number' || f.kind === 'amount' ? 'text-emerald-400 border-emerald-500/30' :
									f.kind === 'name' ? 'text-purple-400 border-purple-500/30' :
									f.kind === 'location' ? 'text-blue-400 border-blue-500/30' :
									f.kind === 'quote' ? 'text-amber-400 border-amber-500/30' :
									'text-muted-foreground border-border'}
								<div class="rounded border border-border/50 bg-card/40 px-2 py-1.5 text-[11px]">
									<div class="flex items-center gap-1.5 mb-0.5">
										<span class="text-[9px] uppercase tracking-wider px-1 rounded border {kindColor}">
											{f.kind}
										</span>
										{#if f.subject}
											<span class="text-muted-foreground/80 truncate">about {f.subject}</span>
										{/if}
									</div>
									<div class="text-foreground/90 line-clamp-2">{f.fact}</div>
									{#if f.value && f.value !== f.fact}
										<div class="text-foreground/60 text-[10px] mt-0.5 font-mono truncate">{f.value}</div>
									{/if}
								</div>
							{/each}
						</div>
					</section>
				{/if}

				<!-- Researched claims with verdicts -->
				{#if researchDone && research?.claims?.length}
					<section class="mb-6">
						<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
							Main claims ({research.claims.length})
						</h3>
						<div class="space-y-2">
							{#each research.claims as c}
								{@const cf = (research.conflicts || []).find((x: any) => x.claim_id === c.id)}
								{@const verdict = cf?.verdict || 'unsourced'}
								{@const verdictColor = verdict === 'confirmed' ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30' :
									verdict === 'contested' ? 'bg-red-500/15 text-red-400 border-red-500/30' :
									'bg-muted/50 text-muted-foreground border-border'}
								<div class="rounded-md border border-border/60 bg-card/40 p-3">
									<div class="flex items-start gap-2">
										<span class="text-[9px] font-semibold px-1.5 py-0.5 rounded border {verdictColor} uppercase tracking-wider shrink-0">
											{verdict}
										</span>
										<p class="text-sm text-foreground/90 flex-1">{c.claim}</p>
									</div>
									{#if cf && (cf.confirms?.length || cf.contradicts?.length)}
										<div class="mt-2 space-y-1 text-[11px]">
											{#each (cf.confirms || []).slice(0, 3) as s}
												<a href={s.url} target="_blank" rel="noopener" class="block hover:underline">
													<span class="text-emerald-400">✓ {s.outlet}:</span>
													<span class="text-foreground/70">{s.evidence}</span>
												</a>
											{/each}
											{#each (cf.contradicts || []).slice(0, 3) as s}
												<a href={s.url} target="_blank" rel="noopener" class="block hover:underline">
													<span class="text-red-400">✗ {s.outlet}:</span>
													<span class="text-foreground/70">{s.evidence}</span>
												</a>
											{/each}
										</div>
									{/if}
								</div>
							{/each}
						</div>
					</section>
				{/if}

				<!-- Conflicts -->
				{#if conflictCount > 0}
					<section class="mb-6 rounded-lg border {severityBadge.color} p-4">
						<div class="flex items-center gap-2 mb-3">
							<AlertTriangle class="w-4 h-4" />
							<h3 class="text-sm font-semibold">Conflicts detected ({conflictCount})</h3>
							<span class="text-[9px] font-semibold ml-auto opacity-80">{severityBadge.label}</span>
						</div>
						<div class="space-y-3">
							{#each conflictRows as row}
								<div class="rounded-md bg-background/40 border border-border/40 p-3">
									<div class="flex items-center gap-2 mb-2 flex-wrap">
										<span class="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-muted/50 text-muted-foreground font-semibold">
											{row.type.replace('_', ' ')}
										</span>
										<span class="text-[11px] text-foreground/80 font-mono">{row.trigger}</span>
									</div>
									<div class="grid grid-cols-2 gap-2">
										{#each row.positions as pos}
											<div class="rounded border border-border/50 bg-card/60 p-2 text-[11px]">
												<div class="flex items-center gap-1.5 mb-1">
													<span class="text-[9px] px-1.5 py-0.5 rounded font-semibold
														{pos.stance === 'confirmed' ? 'bg-emerald-500/20 text-emerald-400' :
														 pos.stance === 'hedging' ? 'bg-amber-500/20 text-amber-400' :
														 'bg-primary/20 text-primary'}">
														{pos.stance}
													</span>
													<span class="text-[10px] text-muted-foreground truncate">{pos.source}</span>
												</div>
												<p class="text-foreground/90 line-clamp-3">{pos.headline}</p>
											</div>
										{/each}
									</div>
								</div>
							{/each}
						</div>
					</section>
				{/if}

				<!-- Body paragraphs -->
				{#if paragraphs.length > 0}
					<div class="prose-body space-y-4">
						{#each paragraphs as para}
							<p class="text-[15px] leading-relaxed text-foreground/90 whitespace-pre-wrap break-words">{para}</p>
						{/each}
					</div>
				{:else if !summary}
					<div class="rounded-lg border border-border bg-muted/30 p-4 text-center">
						<p class="text-sm text-muted-foreground mb-2">No full text captured.</p>
						{#if sourceUrl}
							<a href={sourceUrl} target="_blank" rel="noopener"
								class="inline-flex items-center gap-1 text-xs text-primary hover:underline">
								<ExternalLink class="w-3 h-3" /> Read at source
							</a>
						{/if}
					</div>
				{/if}

				<!-- Contributing articles — only when cluster -->
				{#if isCluster}
					<section class="mt-8 pt-5 border-t border-border/50">
						<button onclick={() => expandedArticles = !expandedArticles}
							class="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2 hover:text-foreground transition-colors">
							{#if expandedArticles}
								<ChevronDown class="w-3 h-3" />
							{:else}
								<ChevronRight class="w-3 h-3" />
							{/if}
							Contributing articles ({articles.length})
						</button>
						{#if expandedArticles}
							<div class="space-y-1.5">
								{#each articles as a}
									<a href={a.source_url || '#'} target={a.source_url ? '_blank' : '_self'} rel="noopener"
										class="block rounded-md border border-border bg-card hover:border-primary/40 hover:bg-primary/5 transition-colors px-3 py-2 group">
										<div class="flex items-start gap-2">
											<div class="flex-1 min-w-0">
												<div class="flex items-center gap-2 mb-0.5 flex-wrap">
													<span class="text-[10px] font-medium text-foreground/80">{a.source || 'Unknown'}</span>
													{#if a.timestamp}
														<span class="text-[10px] text-muted-foreground">· {timeAgo(a.timestamp)}</span>
													{/if}
												</div>
												<h4 class="text-sm text-foreground line-clamp-2 group-hover:text-primary transition-colors">
													{a.headline || a.title}
												</h4>
											</div>
											<ExternalLink class="w-3.5 h-3.5 text-muted-foreground/40 mt-1 shrink-0 group-hover:text-primary transition-colors" />
										</div>
									</a>
								{/each}
							</div>
						{/if}
					</section>
				{/if}

				<!-- Keywords -->
				{#if story.keywords?.length}
					<section class="mt-6">
						<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
							Keywords
						</h3>
						<div class="flex flex-wrap gap-1">
							{#each story.keywords as kw}
								<span class="text-[10px] px-2 py-0.5 rounded-full bg-muted/60 text-muted-foreground">#{kw}</span>
							{/each}
						</div>
					</section>
				{/if}

				<!-- Phase 4.6 — derivative sections. Render only when the story
				     carries the relevant fields (set by node_metrics worker). -->
				{#if story.frequency_strip?.length || story.spike_factor !== undefined}
					<section class="mt-6 rounded-md border border-border/50 p-3">
						<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
							Frequency
						</h3>
						<div class="flex items-end gap-px h-10">
							{#each (story.frequency_strip || []) as bucket, i}
								<div class="bg-primary/70 rounded-sm flex-1"
									style="height: {Math.max(2, Math.min(100, (bucket / Math.max(1, ...(story.frequency_strip || [1])) * 100)))}%; min-width: 2px"
									title="bucket {i}: {bucket}">
								</div>
							{/each}
						</div>
						{#if story.spike_factor !== undefined}
							<p class="text-xs mt-1 text-muted-foreground">
								spike factor <strong class={story.spike_factor > 2 ? 'text-amber-400' : ''}>
									{story.spike_factor.toFixed(1)}×
								</strong>
								vs 30-day baseline
							</p>
						{/if}
					</section>
				{/if}

				{#if story.collapse_footprint}
					<section class="mt-6 rounded-md border border-border/50 p-3">
						<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
							Collapse footprint
						</h3>
						<dl class="grid grid-cols-2 gap-y-1 text-xs">
							{#if story.collapse_footprint.cluster_size !== undefined}
								<dt class="text-muted-foreground">Cluster size</dt>
								<dd>{story.collapse_footprint.cluster_size}</dd>
							{/if}
							{#if story.collapse_footprint.sibling_articulations !== undefined}
								<dt class="text-muted-foreground">Articulations</dt>
								<dd>{story.collapse_footprint.sibling_articulations}</dd>
							{/if}
							{#if story.collapse_footprint.gdu_node}
								<dt class="text-muted-foreground">GDU node</dt>
								<dd class="font-mono text-[11px]">{story.collapse_footprint.gdu_node}</dd>
							{/if}
							{#if story.collapse_footprint.base60_address}
								<dt class="text-muted-foreground">base60</dt>
								<dd class="font-mono text-[11px]">{story.collapse_footprint.base60_address}</dd>
							{/if}
						</dl>
					</section>
				{/if}

				{#if story.geo_footprint?.places?.length}
					<section class="mt-6 rounded-md border border-border/50 p-3">
						<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
							Geo footprint
						</h3>
						<ul class="space-y-1">
							{#each story.geo_footprint.places.slice(0, 8) as p}
								<li class="flex justify-between text-xs">
									<span>{p.name}</span>
									<span class="text-muted-foreground">{p.weight?.toFixed?.(2) ?? p.weight}</span>
								</li>
							{/each}
						</ul>
					</section>
				{/if}

				{#if story.source_attribution}
					<section class="mt-6 rounded-md border border-border/50 p-3">
						<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
							Source attribution
						</h3>
						<dl class="grid grid-cols-2 gap-y-1 text-xs">
							{#if story.source_attribution.entropy !== undefined}
								<dt class="text-muted-foreground">Source entropy</dt>
								<dd>{story.source_attribution.entropy.toFixed(2)}</dd>
							{/if}
							{#if story.source_attribution.dominant_source_share !== undefined}
								<dt class="text-muted-foreground">Dominant share</dt>
								<dd class={story.source_attribution.dominant_source_share > 0.6 ? 'text-amber-400' : ''}>
									{(story.source_attribution.dominant_source_share * 100).toFixed(0)}%
								</dd>
							{/if}
							{#if story.source_attribution.dominant_source}
								<dt class="text-muted-foreground">Dominant source</dt>
								<dd>{story.source_attribution.dominant_source}</dd>
							{/if}
							{#if story.source_attribution.hype_score !== undefined}
								<dt class="text-muted-foreground">Hype score</dt>
								<dd>{story.source_attribution.hype_score.toFixed(2)}</dd>
							{/if}
						</dl>
					</section>
				{/if}
			</article>
		</div>

		<!-- Sticky action row -->
		<div class="sticky bottom-0 z-10 border-t border-border bg-card/95 backdrop-blur px-5 py-3 flex items-center gap-2 flex-wrap">
			<button
				onclick={handleRemember}
				disabled={rememberBusy || !nodeId}
				class="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border transition-colors disabled:opacity-50"
				class:active-remember={remembered}
				class:idle-remember={!remembered}
				title={remembered ? 'Forget' : 'Remember this'}
			>
				<Star class="w-3.5 h-3.5" fill={remembered ? 'currentColor' : 'none'} />
				{remembered ? 'Remembered' : 'Remember'}
			</button>

			<button
				onclick={handleResearch}
				disabled={researchBusy || researchRunning}
				class="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border transition-colors disabled:opacity-70"
				class:idle-research={!researchDone && !researchRunning}
				class:active-research={researchDone}
				title={researchRunning ? 'Research in progress…' : researchDone ? `Researched ${researchAge} — click to re-run` : 'Run deep research on this'}
			>
				{#if researchBusy || researchRunning}
					<div class="w-3 h-3 border border-current/30 border-t-current rounded-full animate-spin"></div>
					{researchRunning ? 'Researching…' : 'Starting…'}
				{:else if researchDone}
					<ShieldCheck class="w-3.5 h-3.5" />
					Researched · Re-run
				{:else}
					<FlaskConical class="w-3.5 h-3.5" />
					Research
				{/if}
			</button>

			{#if onAsk}
				<button
					onclick={() => onAsk?.(story)}
					class="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border border-border hover:border-primary/40 hover:bg-primary/5 text-muted-foreground hover:text-primary transition-colors"
					title="Ask about this in chat"
				>
					<MessageSquare class="w-3.5 h-3.5" />
					Ask
				</button>
			{/if}

			{#if sourceUrl}
				<a
					href={sourceUrl} target="_blank" rel="noopener"
					class="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md border border-border hover:border-primary/40 hover:bg-primary/5 text-muted-foreground hover:text-primary transition-colors ml-auto"
				>
					<ExternalLink class="w-3.5 h-3.5" />
					Open original at {source}
				</a>
			{/if}
		</div>
	</div>
</div>

<style>
	@keyframes slide-in {
		from { transform: translateX(100%); opacity: 0; }
		to { transform: translateX(0); opacity: 1; }
	}
	:global(.animate-slide-in) {
		animation: slide-in 0.22s ease-out;
	}
	.idle-remember {
		border-color: hsl(var(--border));
		color: hsl(var(--muted-foreground));
	}
	.idle-remember:hover:not(:disabled) {
		color: #fbbf24;
		border-color: #78350f;
		background: rgba(251, 191, 36, 0.05);
	}
	.active-remember {
		color: #fbbf24;
		border-color: #78350f;
		background: rgba(251, 191, 36, 0.08);
	}
	.idle-research {
		border-color: hsl(var(--border));
		color: hsl(var(--muted-foreground));
	}
	.idle-research:hover:not(:disabled) {
		color: hsl(var(--primary));
		border-color: hsl(var(--primary) / 0.4);
		background: hsl(var(--primary) / 0.05);
	}
	.active-research {
		color: #34d399;
		border-color: rgba(52, 211, 153, 0.3);
		background: rgba(52, 211, 153, 0.05);
	}
	.active-research:hover:not(:disabled) {
		background: rgba(52, 211, 153, 0.1);
	}
</style>
