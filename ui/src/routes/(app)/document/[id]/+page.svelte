<script lang="ts">
	import FileText from 'lucide-svelte/icons/file-text';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Check from 'lucide-svelte/icons/check';
	import X from 'lucide-svelte/icons/x';
	import DocumentViewer from '$lib/components/document/DocumentViewer.svelte';
	import { page } from '$app/stores';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
	const doc = $derived(data.doc);
	let pages = $state<any[]>(data.pages ?? []);

	// Deep-link target: ?statement=N tells the DocumentViewer to fetch
	// the span info and render the source-span banner.
	const linkedStatementId = $derived.by(() => {
		const v = $page.url.searchParams.get('statement');
		const n = v ? Number(v) : NaN;
		return Number.isFinite(n) && n > 0 ? n : null;
	});

	// Per-page mention loading (lazy — only fetch when page is expanded)
	let pageMentions = $state<Record<number, any[]>>({});
	let pageImages = $state<Record<number, string>>({});   // page_id -> source image URL
	let expandedPageId = $state<number | null>(null);
	let loadingMentions = $state<Set<number>>(new Set());

	async function togglePage(pageNo: number) {
		const pg = pages.find(p => p.page_number === pageNo);
		if (!pg) return;
		if (expandedPageId === pg.page_id) {
			expandedPageId = null;
			return;
		}
		expandedPageId = pg.page_id;
		if (!pageMentions[pg.page_id]) {
			loadingMentions.add(pg.page_id);
			loadingMentions = new Set(loadingMentions);
			try {
				const r = await fetch(`/api/documents/${doc.document_id}/page/${pageNo}`);
				if (r.ok) {
					const d = await r.json();
					pageMentions[pg.page_id] = d.mentions || [];
					if (d.image_url) pageImages[pg.page_id] = d.image_url;
				}
			} finally {
				loadingMentions.delete(pg.page_id);
				loadingMentions = new Set(loadingMentions);
			}
		}
	}

	// Deep-link: ?page=N (from a timeline/entity extraction) auto-opens that
	// page so you land on the exact source scan the fact came from.
	let _deepLinked = false;
	$effect(() => {
		if (_deepLinked) return;
		const pv = $page.url.searchParams.get('page');
		const n = pv ? Number(pv) : NaN;
		if (Number.isFinite(n) && n > 0 && pages.length) {
			_deepLinked = true;
			togglePage(n);
			queueMicrotask(() => document.getElementById(`page-${n}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
		}
	});

	async function reviewMention(mentionId: number, action: 'accept' | 'reject') {
		const r = await fetch(`/api/documents/${doc.document_id}/mentions/${mentionId}/review`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ action }),
		});
		if (r.ok) {
			const updated = await r.json();
			// Update local state
			for (const pid of Object.keys(pageMentions)) {
				const arr = pageMentions[Number(pid)];
				const m = arr.find(mm => mm.mention_id === mentionId);
				if (m) m.review_status = updated.review_status;
			}
			pageMentions = { ...pageMentions };
		}
	}

	// Render text with mention spans highlighted. Walks the mentions sorted
	// by char_start and splices them in as marked spans.
	function highlightText(text: string, mentions: any[]): Array<{ kind: 'text' | 'mention'; content: string; mention?: any }> {
		if (!text || !mentions || mentions.length === 0) {
			return [{ kind: 'text', content: text || '' }];
		}
		const sorted = [...mentions]
			.filter(m => m.char_start != null && m.char_end != null && m.char_end > m.char_start)
			.sort((a, b) => a.char_start - b.char_start);
		const out: Array<{ kind: 'text' | 'mention'; content: string; mention?: any }> = [];
		let cur = 0;
		for (const m of sorted) {
			if (m.char_start < cur) continue; // overlap, skip
			if (m.char_start > cur) {
				out.push({ kind: 'text', content: text.slice(cur, m.char_start) });
			}
			out.push({ kind: 'mention', content: text.slice(m.char_start, m.char_end), mention: m });
			cur = m.char_end;
		}
		if (cur < text.length) out.push({ kind: 'text', content: text.slice(cur) });
		return out;
	}
</script>

<div class="max-w-5xl mx-auto p-4 space-y-3"
	data-researchable
	data-node-id={`document:${doc?.document_id ?? 0}`}>
	{#if doc}
		<header class="rounded-xl border border-border bg-card px-5 py-4">
			<div class="flex items-baseline gap-2.5 mb-1 flex-wrap">
				<FileText size={18} class="text-primary shrink-0" />
				<h1 class="text-lg font-semibold text-foreground flex-1 min-w-0 break-words">
					{doc.title || `Document ${doc.document_id}`}
				</h1>
				{#if doc.trust_class}
					<span class="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded
						bg-muted text-muted-foreground/80">
						{doc.trust_class}
					</span>
				{/if}
			</div>

			<!-- Facts grid — matches entity / place dossier Profile shape -->
			{#if doc.publisher || doc.author || doc.document_date || doc.page_count || doc.word_count}
				<div class="rounded-md border border-border/60 bg-background/30 p-3 mt-3">
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground mb-2">Facts</div>
					<dl class="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1.5 text-xs">
						{#if doc.publisher}
							<dt class="text-muted-foreground">publisher</dt>
							<dd class="text-foreground">{doc.publisher}</dd>
						{/if}
						{#if doc.author}
							<dt class="text-muted-foreground">author</dt>
							<dd class="text-foreground">{doc.author}</dd>
						{/if}
						{#if doc.document_date}
							<dt class="text-muted-foreground">dated</dt>
							<dd class="text-foreground tabular-nums">{doc.document_date.slice(0, 10)}</dd>
						{/if}
						{#if doc.page_count}
							<dt class="text-muted-foreground">pages</dt>
							<dd class="text-foreground tabular-nums">{doc.page_count}</dd>
						{/if}
						{#if doc.word_count}
							<dt class="text-muted-foreground">words</dt>
							<dd class="text-foreground tabular-nums">{doc.word_count.toLocaleString()}</dd>
						{/if}
						{#if doc.source_type}
							<dt class="text-muted-foreground">source type</dt>
							<dd class="text-foreground">{doc.source_type}</dd>
						{/if}
					</dl>
				</div>
			{/if}
			<div class="mt-3 flex items-center gap-2 flex-wrap">
				{#if doc.source_url}
					<a href={doc.source_url} target="_blank" rel="noopener noreferrer"
						class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground hover:bg-primary/90 text-xs font-medium">
						<ExternalLink size={12} /> Open original PDF
					</a>
				{/if}
				{#if doc.extraction_method}
					<span class="text-[10px] uppercase tracking-wider px-2 py-1 rounded
						{doc.extraction_method === 'ocr_paddle' ? 'bg-amber-500/15 text-amber-300 border border-amber-500/30' :
						 doc.extraction_method === 'pdfplumber' ? 'bg-blue-500/15 text-blue-300 border border-blue-500/30' :
						 'bg-muted text-muted-foreground border border-border'}">
						{doc.extraction_method}
						{#if doc.ocr_quality != null}· q{(doc.ocr_quality * 100).toFixed(0)}{/if}
					</span>
				{/if}
				{#if doc.extraction_quality_class}
					<span class="text-[10px] uppercase tracking-wider px-2 py-1 rounded
						{doc.extraction_quality_class === 'redaction_index' ? 'bg-destructive/15 text-destructive border border-destructive/30' :
						 doc.extraction_quality_class === 'scanned_pure' ? 'bg-amber-500/15 text-amber-300 border border-amber-500/30' :
						 doc.extraction_quality_class === 'born_digital' ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30' :
						 'bg-muted text-muted-foreground border border-border'}">
						{doc.extraction_quality_class.replace(/_/g,' ')}
					</span>
				{/if}
			</div>
		</header>

		<!-- ── Source document (real PDF/file with sha256 integrity badge) ── -->
		<section class="rounded-xl border border-border bg-card p-3 h-[80vh] min-h-[600px]">
			<DocumentViewer
				documentId={doc.document_id}
				title={doc.title}
				sourceUrl={doc.source_url}
				statementId={linkedStatementId} />
		</section>

		<section class="rounded-xl border border-border bg-card px-5 py-4">
			<div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
				<div class="rounded-md bg-muted/30 p-2.5">
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground/70 mb-1">Mentions</div>
					<div class="text-lg font-semibold tabular-nums text-foreground">{doc.counters?.mentions_total ?? 0}</div>
				</div>
				<div class="rounded-md bg-muted/30 p-2.5">
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground/70 mb-1">Unresolved</div>
					<div class="text-lg font-semibold tabular-nums {(doc.counters?.mentions_unresolved ?? 0) > 0 ? 'text-amber-400' : 'text-foreground'}">
						{doc.counters?.mentions_unresolved ?? 0}
					</div>
				</div>
				<div class="rounded-md bg-muted/30 p-2.5">
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground/70 mb-1">Statements</div>
					<div class="text-lg font-semibold tabular-nums text-foreground">{doc.counters?.statements ?? 0}</div>
				</div>
				<div class="rounded-md bg-muted/30 p-2.5">
					<div class="text-[10px] uppercase tracking-wider text-muted-foreground/70 mb-1">Pages loaded</div>
					<div class="text-lg font-semibold tabular-nums text-foreground">{pages.length}</div>
				</div>
			</div>
		</section>

		{#if pages.length > 0}
			<section class="rounded-xl border border-border bg-card px-5 py-4">
				<div class="flex items-baseline justify-between mb-2">
					<h2 class="text-sm font-semibold text-foreground">Pages</h2>
					<span class="text-[10px] text-muted-foreground">click a page to expand text + mentions</span>
				</div>
				<ul class="space-y-2 text-xs">
					{#each pages as p (p.page_id)}
						{@const isExpanded = expandedPageId === p.page_id}
						{@const mentions = pageMentions[p.page_id] || []}
						{@const isLoading = loadingMentions.has(p.page_id)}
						<li id={`page-${p.page_number}`} class="border border-border rounded-lg overflow-hidden scroll-mt-24">
							<button
								onclick={() => togglePage(p.page_number)}
								class="w-full px-3 py-2 flex items-baseline gap-3 hover:bg-muted/30 text-left">
								<span class="text-[10px] uppercase tracking-wider text-muted-foreground/70 w-12 shrink-0">
									Page {p.page_number}
								</span>
								<span class="text-foreground/85 leading-relaxed line-clamp-1 flex-1">
									{p.text?.slice(0, 200) || '(no text extracted on this page)'}
								</span>
								<span class="text-[10px] text-muted-foreground tabular-nums shrink-0">
									{p.mention_count}m
								</span>
								<span class="text-muted-foreground/50 text-[10px]">{isExpanded ? '−' : '+'}</span>
							</button>
							{#if isExpanded}
								<div class="border-t border-border px-3 py-3 bg-muted/10">
									<!-- Source page image — see the extraction in its real context
									     (deposition caption, redaction bars, the exact line). -->
									{#if pageImages[p.page_id]}
										<a href={pageImages[p.page_id]} target="_blank" rel="noopener"
											class="block mb-3 border border-border rounded overflow-hidden bg-black/20">
											<img src={pageImages[p.page_id]} alt={`Page ${p.page_number} source scan`}
												loading="lazy" class="w-full max-h-[720px] object-contain mx-auto" />
											<div class="text-[10px] text-muted-foreground/60 px-2 py-1 border-t border-border/40">
												Source scan · page {p.page_number} · click to open full size
											</div>
										</a>
									{/if}
									{#if isLoading}
										<p class="text-muted-foreground italic">Loading mentions…</p>
									{:else}
										<!-- Full page text with highlighted mentions -->
										<div class="text-[13px] leading-relaxed text-foreground/90 whitespace-pre-wrap mb-3">
											{#each highlightText(p.text || '', mentions) as seg}
												{#if seg.kind === 'mention'}
													{@const m = seg.mention}
													{@const linked_eid = m.candidate_entity_id ?? m.entity_id}
													{@const cls = `inline-block px-1 rounded ${
														m.review_status === 'rejected' ? 'bg-destructive/20 text-destructive line-through' :
														m.review_status === 'accepted' ? 'bg-emerald-500/20 text-emerald-300' :
														linked_eid ? 'bg-primary/15 text-primary hover:bg-primary/25 cursor-pointer' :
														'bg-amber-500/15 text-amber-300'}`}
													{@const tt = m.suggested_type
														? `${m.suggested_type}${linked_eid ? ' · entity ' + linked_eid : ' · unresolved'}${m.review_status ? ' · '+m.review_status : ''}`
														: ''}
													{#if linked_eid}
														<a href={`/entity/${linked_eid}`} class={cls} title={tt}>{seg.content}</a>
													{:else}
														<span class={cls} title={tt}>{seg.content}</span>
													{/if}
												{:else}
													{seg.content}
												{/if}
											{/each}
											{#if p.text_truncated}
												<span class="text-muted-foreground/60 text-[11px]"> … (truncated)</span>
											{/if}
										</div>
										<!-- Mention review chips -->
										{#if mentions.length > 0}
											<div class="flex flex-wrap gap-1.5 pt-2 border-t border-border/40">
												{#each mentions as m}
													{@const eid = m.candidate_entity_id ?? m.entity_id}
													<div class="inline-flex items-center gap-0.5 rounded-md border border-border bg-card px-1.5 py-0.5 text-[11px]">
														{#if eid}
															<a href={`/entity/${eid}`} class="text-primary hover:underline">{m.surface_form}</a>
														{:else}
															<span class="text-foreground">{m.surface_form}</span>
														{/if}
														{#if m.suggested_type && m.suggested_type !== 'unknown'}
															<span class="text-muted-foreground/60 text-[9px] ml-0.5">{m.suggested_type}</span>
														{/if}
														{#if !m.review_status}
															<button onclick={() => reviewMention(m.mention_id, 'accept')}
																class="ml-1 text-emerald-400 hover:bg-emerald-500/20 rounded p-0.5"
																title="accept">
																<Check size={10} />
															</button>
															<button onclick={() => reviewMention(m.mention_id, 'reject')}
																class="text-destructive hover:bg-destructive/20 rounded p-0.5"
																title="reject">
																<X size={10} />
															</button>
														{:else}
															<span class="ml-1 text-[9px] uppercase tracking-wider
																{m.review_status === 'accepted' ? 'text-emerald-400' : 'text-destructive'}">
																{m.review_status}
															</span>
														{/if}
													</div>
												{/each}
											</div>
										{/if}
									{/if}
								</div>
							{/if}
						</li>
					{/each}
				</ul>
			</section>
		{/if}

		<div class="text-xs text-muted-foreground/70 pt-2">
			Document ID <span class="font-mono">{doc.document_id}</span>
		</div>
	{/if}
</div>
