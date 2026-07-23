<script lang="ts">
	import FileText from 'lucide-svelte/icons/file-text';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';

	interface Props {
		summary: string;
		sections?: { heading: string; body: string }[];
	}

	let { summary, sections = [] }: Props = $props();
	let expandedSections = $state<Set<number>>(new Set());

	function toggle(i: number) {
		const next = new Set(expandedSections);
		if (next.has(i)) next.delete(i); else next.add(i);
		expandedSections = next;
	}
</script>

<div class="rounded-lg border bg-card p-4">
	<div class="flex items-start gap-2 mb-3">
		<FileText size={16} class="text-primary mt-0.5 shrink-0" />
		<p class="text-sm text-foreground leading-relaxed">{summary}</p>
	</div>
	{#if sections.length > 0}
		<div class="space-y-1 mt-3 border-t border-border pt-3">
			{#each sections as section, i}
				<button
					onclick={() => toggle(i)}
					class="flex items-center justify-between w-full text-left px-2 py-1.5 rounded hover:bg-muted/50 transition-colors"
				>
					<span class="text-xs font-medium text-foreground">{section.heading}</span>
					<ChevronDown size={14} class="text-muted-foreground transition-transform {expandedSections.has(i) ? 'rotate-180' : ''}" />
				</button>
				{#if expandedSections.has(i)}
					<div class="px-2 pb-2 text-xs text-muted-foreground leading-relaxed">{section.body}</div>
				{/if}
			{/each}
		</div>
	{/if}
</div>
