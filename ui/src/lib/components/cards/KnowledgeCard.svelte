<script lang="ts">
	import Lightbulb from 'lucide-svelte/icons/lightbulb';
	import Eye from 'lucide-svelte/icons/eye';
	import MessageSquare from 'lucide-svelte/icons/message-square';
	import HelpCircle from 'lucide-svelte/icons/help-circle';
	import FileCheck from 'lucide-svelte/icons/file-check';
	import Scale from 'lucide-svelte/icons/scale';

	interface Props {
		node: any;
		variant?: 'compact' | 'standard';
	}

	let { node, variant = 'standard' }: Props = $props();

	const typeConfig: Record<string, { icon: any; color: string; bg: string }> = {
		fact: { icon: FileCheck, color: 'text-emerald-400', bg: 'bg-emerald-400/10' },
		observation: { icon: Eye, color: 'text-blue-400', bg: 'bg-blue-400/10' },
		claim: { icon: MessageSquare, color: 'text-orange-400', bg: 'bg-orange-400/10' },
		hypothesis: { icon: Lightbulb, color: 'text-purple-400', bg: 'bg-purple-400/10' },
		question: { icon: HelpCircle, color: 'text-yellow-400', bg: 'bg-yellow-400/10' },
		evidence: { icon: Scale, color: 'text-teal-400', bg: 'bg-teal-400/10' },
	};

	const config = $derived(typeConfig[node?.type] ?? typeConfig.observation);
</script>

<div class="rounded-lg border border-border bg-card overflow-hidden">
	<div class="flex items-center gap-2 px-3 py-2">
		<div class="w-6 h-6 rounded flex items-center justify-center shrink-0 {config.bg}">
			<svelte:component this={config.icon} size={14} class={config.color} />
		</div>
		<span class="text-sm text-foreground truncate flex-1">{node?.title ?? 'Node'}</span>
		<span class="text-[10px] text-muted-foreground capitalize">{node?.type ?? ''}</span>
	</div>
	{#if variant === 'standard' && node?.description}
		<div class="px-3 pb-2">
			<p class="text-xs text-muted-foreground line-clamp-2">{node.description}</p>
		</div>
	{/if}
</div>
