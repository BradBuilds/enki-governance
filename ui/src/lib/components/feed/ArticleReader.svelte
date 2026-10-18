<script lang="ts">
	import { tts } from '$lib/stores/tts.svelte';
	import X from 'lucide-svelte/icons/x';
	import ExternalLink from 'lucide-svelte/icons/external-link';
	import Play from 'lucide-svelte/icons/play';
	import Pause from 'lucide-svelte/icons/pause';
	import Square from 'lucide-svelte/icons/square';
	import Settings2 from 'lucide-svelte/icons/settings-2';
	import Volume2 from 'lucide-svelte/icons/volume-2';

	let { article, onClose }: {
		article: any;
		onClose: () => void;
	} = $props();

	let showVoiceSettings = $state(false);

	// Build readable text. Prefer full_text, fall back to summary+description.
	const bodyText = $derived.by(() => {
		const a = article?.articles?.[0] || article;
		const payload = a?.payload || {};
		return (
			a?.full_text ||
			payload.full_text ||
			payload.content ||
			a?.summary ||
			payload.summary ||
			payload.og_description ||
			payload.description ||
			article?.summary ||
			''
		).trim();
	});

	const paragraphs = $derived.by(() => {
		if (!bodyText) return [];
		// Split on blank lines or \n\n, fallback to single-newline
		const parts = bodyText.split(/\n\s*\n/).map((p: string) => p.trim()).filter(Boolean);
		if (parts.length > 1) return parts;
		// If no paragraph breaks found, split by sentences into reasonable chunks
		const sentences = bodyText.match(/[^.!?]+[.!?]+/g) || [bodyText];
		const chunks: string[] = [];
		let buf = '';
		for (const s of sentences) {
			buf += s + ' ';
			if (buf.length > 300) {
				chunks.push(buf.trim());
				buf = '';
			}
		}
		if (buf.trim()) chunks.push(buf.trim());
		return chunks;
	});

	const headline = $derived(
		article?.headline ||
		article?.articles?.[0]?.headline ||
		article?.articles?.[0]?.payload?.title ||
		article?.title ||
		'Untitled'
	);

	const source = $derived(
		article?.sources?.[0] ||
		article?.source ||
		article?.articles?.[0]?.source ||
		'Unknown source'
	);

	const sourceUrl = $derived(
		article?.articles?.[0]?.source_url ||
		article?.source_url ||
		article?.articles?.[0]?.payload?.link ||
		''
	);

	const publishedAt = $derived(
		article?.articles?.[0]?.timestamp ||
		article?.timestamp ||
		article?.articles?.[0]?.payload?.published ||
		''
	);

	const author = $derived(
		article?.articles?.[0]?.payload?.author ||
		article?.author ||
		''
	);

	const wordCount = $derived(bodyText.split(/\s+/).filter(Boolean).length);
	const readingTimeMin = $derived(Math.max(1, Math.ceil(wordCount / 230)));

	const fullReadText = $derived(headline && bodyText ? `${headline}\n\n${bodyText}` : (bodyText || headline));

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') onClose();
	}

	function playArticle() {
		tts.toggleArticle(headline, bodyText);
	}

	function stopAndClose() {
		tts.stop();
		onClose();
	}

	function timeAgo(ts: string): string {
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

	// Estimate reading progress based on TTS offset
	const readProgress = $derived.by(() => {
		if (tts.state === 'idle' || !fullReadText) return 0;
		if (tts.currentText !== fullReadText) return 0;
		return Math.min(tts.currentOffset / Math.max(fullReadText.length, 1), 1);
	});
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="fixed inset-0 z-50 flex justify-end" onclick={stopAndClose}>
	<div class="absolute inset-0 bg-black/50 backdrop-blur-sm"></div>
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div class="relative w-full max-w-3xl bg-card border-l border-border shadow-2xl overflow-y-auto flex flex-col animate-slide-in"
		onclick={(e) => e.stopPropagation()}>
		<!-- Sticky header with TTS controls -->
		<div class="sticky top-0 z-10 bg-card/95 backdrop-blur border-b border-border">
			<div class="px-5 py-3 flex items-center gap-3">
				<!-- Play/Pause button -->
				{#if tts.isSupported}
					<button
						onclick={playArticle}
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
						<button
							onclick={() => tts.stop()}
							class="flex items-center justify-center w-9 h-9 rounded-full bg-muted text-muted-foreground hover:text-foreground transition-colors shrink-0"
							title="Stop">
							<Square class="w-4 h-4" />
						</button>
					{/if}
				{/if}

				<div class="flex-1 min-w-0">
					<div class="flex items-center gap-2 text-xs text-muted-foreground">
						<span class="font-medium text-foreground/80">{source}</span>
						{#if publishedAt}<span>· {timeAgo(publishedAt)}</span>{/if}
						{#if wordCount > 0}<span>· {readingTimeMin} min read</span>{/if}
					</div>
				</div>

				<button
					onclick={() => showVoiceSettings = !showVoiceSettings}
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
				<button onclick={stopAndClose}
					class="p-2 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground shrink-0"
					title="Close (Esc)">
					<X class="w-4 h-4" />
				</button>
			</div>

			<!-- Progress bar when speaking -->
			{#if tts.state !== 'idle' && tts.currentText === fullReadText}
				<div class="h-0.5 bg-muted">
					<div class="h-full bg-primary transition-all" style="width: {readProgress * 100}%"></div>
				</div>
			{/if}

			<!-- Voice settings panel -->
			{#if showVoiceSettings}
				<div class="border-t border-border/50 px-5 py-3 bg-muted/20 space-y-2">
					<!-- Provider selector -->
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

					<!-- Cloud voice selector -->
					{#if tts.provider !== 'web_speech' && tts.cloudVoices.length > 0}
						<div class="flex items-center gap-2">
							<span class="text-[10px] text-muted-foreground w-14">Voice</span>
							<select value={tts.cloudVoice} onchange={(e) => tts.setCloudVoice((e.target as HTMLSelectElement).value)}
								class="flex-1 bg-card border border-border rounded-md px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-ring">
								{#each tts.cloudVoices as v}
									<option value={v.name}>
										{v.name} {v.tier ? `(${v.tier})` : ''} {v.gender ? `· ${v.gender}` : ''} {v.description ? `· ${v.description}` : ''}
									</option>
								{/each}
							</select>
						</div>
					{:else if tts.provider === 'web_speech' && tts.isSupported}
						<div class="flex items-center gap-2">
							<span class="text-[10px] text-muted-foreground w-14">Voice</span>
							<select value={tts.voiceName} onchange={(e) => tts.setVoice((e.target as HTMLSelectElement).value)}
								class="flex-1 bg-card border border-border rounded-md px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-ring">
								{#each tts.voices as v}
									<option value={v.name}>{v.name} ({v.lang})</option>
								{/each}
							</select>
						</div>
					{/if}

					<div class="flex items-center gap-2">
						<span class="text-[10px] text-muted-foreground w-14">Speed</span>
						<input type="range" min="0.5" max="2.5" step="0.1" value={tts.rate}
							oninput={(e) => tts.setRate(parseFloat((e.target as HTMLInputElement).value))}
							class="flex-1 accent-primary h-1" />
						<span class="text-[10px] text-muted-foreground w-10 text-right font-mono">{tts.rate.toFixed(1)}x</span>
					</div>
					{#if tts.provider === 'web_speech'}
						<div class="flex items-center gap-2">
							<span class="text-[10px] text-muted-foreground w-14">Pitch</span>
							<input type="range" min="0.5" max="2.0" step="0.1" value={tts.pitch}
								oninput={(e) => tts.setPitch(parseFloat((e.target as HTMLInputElement).value))}
								class="flex-1 accent-primary h-1" />
							<span class="text-[10px] text-muted-foreground w-10 text-right font-mono">{tts.pitch.toFixed(1)}</span>
						</div>
					{/if}
					{#if !tts.availableProviders.google && !tts.availableProviders.openai}
						<p class="text-[10px] text-muted-foreground/70 italic pt-1 border-t border-border/30">
							For natural-sounding voices, configure Google Cloud TTS or OpenAI TTS in Settings → AI.
						</p>
					{/if}
				</div>
			{/if}
		</div>

		<!-- Article content -->
		<div class="flex-1 px-8 py-6">
			<article class="max-w-[65ch] mx-auto">
				<!-- Headline -->
				<h1 class="text-2xl font-bold text-foreground leading-tight mb-2">{headline}</h1>

				<!-- Byline -->
				{#if author}
					<p class="text-sm text-muted-foreground mb-4">By <span class="text-foreground">{author}</span></p>
				{/if}

				<!-- Entity chips -->
				{#if article?.countries?.length || article?.people?.length || article?.organizations?.length}
					<div class="flex flex-wrap gap-1.5 mb-5 pb-4 border-b border-border/50">
						{#each (article?.countries || []).slice(0, 4) as c}
							<span class="text-[10px] px-2 py-0.5 rounded-full bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">{c}</span>
						{/each}
						{#each (article?.people || []).slice(0, 4) as p}
							<span class="text-[10px] px-2 py-0.5 rounded-full bg-purple-500/10 text-purple-400 border border-purple-500/20">{p}</span>
						{/each}
						{#each (article?.organizations || []).slice(0, 4) as o}
							<span class="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20">{o}</span>
						{/each}
					</div>
				{/if}

				<!-- Body paragraphs -->
				{#if paragraphs.length > 0}
					<div class="prose-body space-y-4">
						{#each paragraphs as para}
							<p class="text-[15px] leading-relaxed text-foreground/90 whitespace-pre-wrap break-words">{para}</p>
						{/each}
					</div>
				{:else}
					<div class="rounded-lg border border-border bg-muted/30 p-4 text-center">
						<p class="text-sm text-muted-foreground mb-2">No full text captured for this article.</p>
						{#if sourceUrl}
							<a href={sourceUrl} target="_blank" rel="noopener"
								class="inline-flex items-center gap-1 text-xs text-primary hover:underline">
								<ExternalLink class="w-3 h-3" /> Read at source
							</a>
						{/if}
					</div>
				{/if}

				<!-- Articles list (if this is a topic cluster with multiple articles) -->
				{#if article?.articles?.length > 1}
					<div class="mt-8 pt-5 border-t border-border/50">
						<h3 class="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
							Other articles on this topic ({article.articles.length})
						</h3>
						<div class="space-y-1">
							{#each article.articles.slice(1, 10) as a}
								<a href={a.source_url || '#'} target="_blank" rel="noopener"
									class="block px-2 py-1.5 rounded hover:bg-muted/30 transition-colors">
									<div class="flex items-center gap-2 text-xs">
										<span class="text-foreground flex-1 line-clamp-1">{a.headline}</span>
										<span class="text-muted-foreground text-[10px] shrink-0">{a.source}</span>
										<ExternalLink class="w-3 h-3 text-muted-foreground/40 shrink-0" />
									</div>
								</a>
							{/each}
						</div>
					</div>
				{/if}

				<!-- Footer link -->
				{#if sourceUrl}
					<div class="mt-8 pt-5 border-t border-border/50">
						<a href={sourceUrl} target="_blank" rel="noopener"
							class="inline-flex items-center gap-1.5 text-xs text-primary hover:underline">
							<ExternalLink class="w-3 h-3" /> View original at {source}
						</a>
					</div>
				{/if}
			</article>
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
