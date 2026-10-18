<script lang="ts">
	/**
	 * NodeDetailModal — topic/cluster-level detail view.
	 *
	 * Opens from: feed card click, entity chip "view topic", graph node
	 * click when the node carries a cluster reference. Shows summary +
	 * article list + conflicts (contradictions / credibility gaps) +
	 * entities, in one slide-over.
	 *
	 * For single-article drilling, use ArticleReader.svelte instead.
	 */

	import { tts } from '$lib/stores/tts.svelte';
	import X from 'lucide-svelte/icons/x';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Users from 'lucide-svelte/icons/users';
	import Globe from 'lucide-svelte/icons/globe';
	import Building from 'lucide-svelte/icons/building';
	import Play from 'lucide-svelte/icons/play';
	import BookOpen from 'lucide-svelte/icons/book-open';
	import Zap from 'lucide-svelte/icons/zap';

	type ConflictArticle = {
		signal_id?: string;
		headline?: string;
		source?: string;
		stance?: string;
	};

	type Conflict = {
		type: 'contradiction' | 'credibility_gap' | string;
		trigger: string;
		article_a: ConflictArticle;
		article_b: ConflictArticle;
	};

	type ConflictBlock = {
		count: number;
		conflicts: Conflict[];
		severity: 'high' | 'medium' | 'low' | string;
	};

	type Group = {
		id?: string;
		headline?: string;
		summary?: string;
		thumbnail_url?: string;
		category?: string;
		article_count?: number;
		source_count?: number;
		sources?: string[];
		keywords?: string[];
		countries?: string[];
		people?: string[];
		organizations?: string[];
		severity?: string;
		timestamp?: string;
		velocity?: number;
		articles?: any[];
		conflicts?: ConflictBlock | null;
	};

	let { group, onClose, onOpenArticle }: {
		group: Group;
		onClose: () => void;
		onOpenArticle?: (article: any) => void;
	} = $props();

	const headline = $derived(group?.headline || 'Untitled');
	const summary = $derived(
		(group?.summary || '')
			.replace(/https?:\/\/\S+/g, '')
			.replace(/Article URL:\s*/gi, '')
			.replace(/Comments:\s*https?:\/\/\S*/gi, '')
			.trim()
	);

	const conflicts = $derived(group?.conflicts?.conflicts || []);
	const conflictCount = $derived(group?.conflicts?.count || 0);
	const conflictSeverity = $derived(group?.conflicts?.severity || 'low');

	const articles = $derived(group?.articles || []);

	const severityBadge = $derived(
		conflictSeverity === 'high'
			? { color: 'text-red-400 bg-red-500/10 border-red-500/30', label: 'HIGH SEVERITY' }
			: conflictSeverity === 'medium'
			? { color: 'text-amber-400 bg-amber-500/10 border-amber-500/30', label: 'MEDIUM SEVERITY' }
			: { color: 'text-muted-foreground bg-muted/30 border-border', label: 'LOW SEVERITY' }
	);

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') onClose();
	}

	function playTopic() {
		// Use the existing queue playback: headline + summary in the current voice
		if (tts.state !== 'idle' && tts.currentText.includes(headline)) {
			if (tts.state === 'speaking') tts.pause();
			else tts.resume();
			return;
		}
		tts.speakArticle(headline, summary || '');
	}

	function openArticle(article: any) {
		if (onOpenArticle) onOpenArticle(article);
	}

	function timeAgo(ts: string | undefined): string {
		if (!ts) return '';
		try {
			const diff = Date.now() - new Date(ts).getTime();
			const mins = Math.floor(diff / 60000);
			if (mins < 60) return `${mins}m ago`;
			const hrs = Math.floor(mins / 60);
			if (hrs < 24) return `${hrs}h ago`;
			return `${Math.floor(hrs / 24)}d ago`;
		} catch {
			return ts;
		}
	}

	// Group conflicts by triggering claim-pair so the matrix shows one row per axis
	const conflictRows = $derived.by(() => {
		// One row per conflict; positions = [article_a stance, article_b stance]
		return conflicts.map((c: Conflict, idx: number) => ({
			idx,
			type: c.type,
			trigger: c.trigger,
			positions: [
				{
					stance: c.article_a.stance || (c.type === 'contradiction' ? 'side A' : 'confirmed'),
					headline: c.article_a.headline || '',
					source: c.article_a.source || 'unknown',
					signal_id: c.article_a.signal_id,
				},
				{
					stance: c.article_b.stance || (c.type === 'contradiction' ? 'side B' : 'hedging'),
					headline: c.article_b.headline || '',
					source: c.article_b.source || 'unknown',
					signal_id: c.article_b.signal_id,
				},
			],
		}));
	});
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div class="fixed inset-0 z-50 flex justify-end" onclick={onClose}>
	<div class="absolute inset-0 bg-black/60 backdrop-blur-sm"></div>
	<div class="relative w-full max-w-4xl bg-card border-l border-border shadow-2xl overflow-y-auto flex flex-col animate-slide-in"
		onclick={(e) => e.stopPropagation()}>

		<!-- Header -->
		<div class="sticky top-0 z-10 bg-card/95 backdrop-blur border-b border-border px-6 py-4">
			<div class="flex items-start justify-between gap-3">
				<div class="flex-1 min-w-0">
					<div class="flex items-center gap-2 mb-2 flex-wrap">
						{#if group.category}
							<span class="text-[10px] px-2 py-0.5 rounded-full bg-primary/10 text-primary border border-primary/20">
								{group.category}
							</span>
						{/if}
						{#if conflictCount > 0}
							<span class="text-[10px] px-2 py-0.5 rounded-full flex items-center gap-1 border {severityBadge.color}">
								<AlertTriangle class="w-2.5 h-2.5" />
								{conflictCount} conflict{conflictCount > 1 ? 's' : ''}
							</span>
						{/if}
						{#if group.source_count}
							<span class="text-[10px] text-muted-foreground">
								{group.source_count} source{group.source_count > 1 ? 's' : ''}
							</span>
						{/if}
						{#if group.article_count}
							<span class="text-[10px] text-muted-foreground">· {group.article_count} articles</span>
						{/if}
						{#if (group.velocity ?? 0) > 0}
							<span class="text-[10px] text-amber-400 flex items-center gap-0.5">
								<Zap class="w-2.5 h-2.5" /> {group.velocity}/hr
							</span>
						{/if}
					</div>
					<h1 class="text-xl font-bold text-foreground leading-tight">{headline}</h1>
				</div>
				<div class="flex items-center gap-1 shrink-0">
					<button onclick={playTopic}
						class="flex items-center justify-center w-9 h-9 rounded-full bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
						title="Read headline + summary aloud">
						<Play class="w-4 h-4 ml-0.5" />
					</button>
					<button onclick={onClose}
						class="p-2 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground"
						title="Close (Esc)">
						<X class="w-4 h-4" />
					</button>
				</div>
			</div>
		</div>

		<!-- Body -->
		<div class="flex-1 px-6 py-5 space-y-6">
			<!-- Summary -->
			{#if summary}
				<section>
					<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
						Summary
					</h3>
					<p class="text-sm text-foreground/90 leading-relaxed whitespace-pre-wrap">{summary}</p>
				</section>
			{/if}

			<!-- Conflicts / contradictions -->
			{#if conflictCount > 0}
				<section class="rounded-lg border {severityBadge.color} p-4">
					<div class="flex items-center gap-2 mb-3">
						<AlertTriangle class="w-4 h-4" />
						<h3 class="text-sm font-semibold">
							Conflicts detected ({conflictCount})
						</h3>
						<span class="text-[9px] font-semibold ml-auto opacity-80">{severityBadge.label}</span>
					</div>

					<div class="space-y-4">
						{#each conflictRows as row}
							<div class="rounded-md bg-background/40 border border-border/40 p-3">
								<div class="flex items-center gap-2 mb-2 flex-wrap">
									<span class="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-muted/50 text-muted-foreground font-semibold">
										{row.type.replace('_', ' ')}
									</span>
									<span class="text-[11px] text-foreground/80 font-mono">{row.trigger}</span>
								</div>

								<!-- Position matrix -->
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

			<!-- Entities -->
			{#if (group.countries?.length || group.people?.length || group.organizations?.length)}
				<section>
					<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
						Entities mentioned
					</h3>
					<div class="space-y-2">
						{#if group.countries?.length}
							<div class="flex items-start gap-2">
								<Globe class="w-3.5 h-3.5 text-cyan-400 mt-0.5 shrink-0" />
								<div class="flex flex-wrap gap-1">
									{#each group.countries as c}
										<span class="text-[10px] px-2 py-0.5 rounded-full bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">{c}</span>
									{/each}
								</div>
							</div>
						{/if}
						{#if group.people?.length}
							<div class="flex items-start gap-2">
								<Users class="w-3.5 h-3.5 text-purple-400 mt-0.5 shrink-0" />
								<div class="flex flex-wrap gap-1">
									{#each group.people as p}
										<span class="text-[10px] px-2 py-0.5 rounded-full bg-purple-500/10 text-purple-400 border border-purple-500/20">{p}</span>
									{/each}
								</div>
							</div>
						{/if}
						{#if group.organizations?.length}
							<div class="flex items-start gap-2">
								<Building class="w-3.5 h-3.5 text-amber-400 mt-0.5 shrink-0" />
								<div class="flex flex-wrap gap-1">
									{#each group.organizations as o}
										<span class="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20">{o}</span>
									{/each}
								</div>
							</div>
						{/if}
					</div>
				</section>
			{/if}

			<!-- Article list -->
			<section>
				<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
					Contributing articles ({articles.length})
				</h3>
				{#if articles.length === 0}
					<p class="text-xs text-muted-foreground italic">No article detail available for this cluster.</p>
				{:else}
					<div class="space-y-1.5">
						{#each articles as article}
							<button onclick={() => openArticle(article)}
								class="w-full text-left rounded-md border border-border bg-card hover:border-primary/40 hover:bg-primary/5 transition-colors px-3 py-2 group">
								<div class="flex items-start gap-2">
									<div class="flex-1 min-w-0">
										<div class="flex items-center gap-2 mb-0.5 flex-wrap">
											<span class="text-[10px] font-medium text-foreground/80">{article.source || 'Unknown source'}</span>
											{#if article.timestamp}
												<span class="text-[10px] text-muted-foreground">· {timeAgo(article.timestamp)}</span>
											{/if}
											{#if article.severity && article.severity !== 'informational'}
												<span class="text-[9px] px-1 rounded bg-red-500/10 text-red-400">{article.severity}</span>
											{/if}
										</div>
										<h4 class="text-sm text-foreground line-clamp-2 group-hover:text-primary transition-colors">
											{article.headline || article.title}
										</h4>
									</div>
									<BookOpen class="w-3.5 h-3.5 text-muted-foreground/40 mt-1 shrink-0 group-hover:text-primary transition-colors" />
								</div>
							</button>
						{/each}
					</div>
				{/if}
			</section>

			<!-- Keywords -->
			{#if group.keywords?.length}
				<section>
					<h3 class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider mb-2">
						Keywords
					</h3>
					<div class="flex flex-wrap gap-1">
						{#each group.keywords as kw}
							<span class="text-[10px] px-2 py-0.5 rounded-full bg-muted/60 text-muted-foreground">#{kw}</span>
						{/each}
					</div>
				</section>
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
</style>
