<script lang="ts">
	import Badge from '$lib/components/shared/Badge.svelte';
	import Eye from 'lucide-svelte/icons/eye';
	import FileText from 'lucide-svelte/icons/file-text';
	import User from 'lucide-svelte/icons/user';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import Sparkles from 'lucide-svelte/icons/sparkles';

	interface Props {
		topic: any;
		variant?: 'compact' | 'standard';
		onDeepResearch?: (topicId: string) => void;
	}

	let { topic, variant = 'standard', onDeepResearch }: Props = $props();

	function timeAgo(ts: string): string {
		if (!ts) return '';
		const diff = Date.now() - new Date(ts).getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'now';
		if (mins < 60) return `${mins}m ago`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h ago`;
		return `${Math.floor(hrs / 24)}d ago`;
	}
</script>

<div class="rounded-xl border border-border bg-card overflow-hidden hover:border-primary/30 transition-colors">
	<div class="{variant === 'compact' ? 'px-4 py-3' : 'px-5 py-4'}">
		<div class="flex items-start justify-between gap-3">
			<div class="flex items-center gap-3 min-w-0">
				<div class="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
					<Eye size={20} class="text-primary" />
				</div>
				<div class="min-w-0">
					<a href="/watch/{topic.topic_id}" class="text-sm font-semibold text-foreground hover:text-primary transition-colors truncate block">
						{topic.title}
					</a>
					<p class="text-xs text-muted-foreground mt-0.5">
						{topic.article_count ?? 0} articles · {topic.cluster_count ?? 0} clusters · {topic.entity_count ?? 0} entities
					</p>
				</div>
			</div>
			<div class="flex items-center gap-2 shrink-0">
				<span class="text-[10px] text-muted-foreground">{timeAgo(topic.updated_at)}</span>
				<Badge text={topic.status} variant={topic.status === 'active' ? 'success' : 'neutral'} size="sm" />
			</div>
		</div>

		{#if variant !== 'compact' && topic.ai_summary}
			<div class="mt-3 text-xs text-muted-foreground leading-relaxed">
				<p class="line-clamp-3">{topic.ai_summary}</p>
			</div>
		{/if}

		{#if variant !== 'compact' && topic.keywords?.length}
			<div class="flex flex-wrap gap-1 mt-3">
				{#each topic.keywords.slice(0, 6) as kw}
					<span class="text-[10px] px-1.5 py-0.5 rounded-full bg-primary/10 text-primary">{kw}</span>
				{/each}
			</div>
		{/if}

		{#if variant !== 'compact'}
			<div class="flex items-center gap-2 mt-3 pt-3 border-t border-border/50">
				<a href="/watch/{topic.topic_id}" class="text-xs text-primary hover:underline flex items-center gap-1">
					<FileText class="w-3 h-3" /> Articles
				</a>
				<span class="text-border">·</span>
				<a href="/watch/{topic.topic_id}?tab=entities" class="text-xs text-primary hover:underline flex items-center gap-1">
					<User class="w-3 h-3" /> People
				</a>
				<span class="text-border">·</span>
				<a href="/watch/{topic.topic_id}?tab=map" class="text-xs text-primary hover:underline flex items-center gap-1">
					<MapPin class="w-3 h-3" /> Map
				</a>
				{#if onDeepResearch}
					<span class="text-border">·</span>
					<button onclick={() => onDeepResearch(topic.topic_id)} class="text-xs text-primary hover:underline flex items-center gap-1">
						<Sparkles class="w-3 h-3" /> Deep Research
					</button>
				{/if}
				<span class="flex-1"></span>
				<a href="/watch/{topic.topic_id}" class="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1">
					Open <ChevronRight class="w-3 h-3" />
				</a>
			</div>
		{/if}
	</div>
</div>
