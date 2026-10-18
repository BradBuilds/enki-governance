<script lang="ts">
	import Badge from '$lib/components/shared/Badge.svelte';
	import Briefcase from 'lucide-svelte/icons/briefcase';

	interface Props {
		caseData: any;
		variant?: 'compact' | 'standard' | 'expanded';
		onStatusChange?: (newStatus: string) => void;
	}

	let { caseData, variant = 'standard', onStatusChange }: Props = $props();

	const statusFlow: Record<string, string> = {
		open: 'investigating',
		investigating: 'resolved',
		resolved: 'closed',
	};

	const priorityColors: Record<string, string> = {
		P1: 'bg-[var(--severity-critical)]',
		P2: 'bg-[var(--severity-high)]',
		P3: 'bg-[var(--severity-medium)]',
		P4: 'bg-[var(--severity-low)]',
	};

	const statusVariant: Record<string, 'info' | 'medium' | 'success' | 'neutral'> = {
		open: 'info',
		investigating: 'medium',
		resolved: 'success',
		closed: 'neutral',
	};

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

	const nextStatus = $derived(statusFlow[caseData?.status]);
</script>

<div class="rounded-lg border border-border bg-card overflow-hidden hover:border-primary/20 transition-colors">
	{#if variant === 'compact'}
		<div class="flex items-center gap-2 px-3 py-2">
			<span class="w-2 h-2 rounded-full shrink-0 {priorityColors[caseData?.priority] ?? 'bg-muted'}"></span>
			<span class="text-xs font-mono text-muted-foreground shrink-0">{caseData?.case_id?.slice(0, 10) ?? ''}</span>
			<span class="text-sm text-foreground truncate flex-1">{caseData?.title ?? 'Case'}</span>
			<Badge text={caseData?.status ?? 'open'} variant={statusVariant[caseData?.status] ?? 'info'} size="sm" />
		</div>
	{:else}
		<div class="px-4 py-3">
			<div class="flex items-start gap-3">
				<div class="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center shrink-0 mt-0.5">
					<Briefcase size={16} class="text-primary" />
				</div>
				<div class="flex-1 min-w-0">
					<div class="flex items-center gap-2 flex-wrap">
						<span class="w-2 h-2 rounded-full shrink-0 {priorityColors[caseData?.priority] ?? 'bg-muted'}"></span>
						<span class="text-sm font-medium text-foreground">{caseData?.title ?? 'Case'}</span>
						<Badge text={caseData?.status ?? 'open'} variant={statusVariant[caseData?.status] ?? 'info'} size="sm" />
					</div>
					<div class="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
						{#if caseData?.case_id}
							<span class="font-mono">{caseData.case_id.slice(0, 12)}</span>
						{/if}
						{#if caseData?.owner}
							<span>{caseData.owner}</span>
						{/if}
						<span>{timeAgo(caseData?.updated_at || caseData?.created_at)}</span>
					</div>
					{#if caseData?.tags?.length}
						<div class="flex gap-1 mt-2 flex-wrap">
							{#each caseData.tags as tag}
								<span class="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{tag}</span>
							{/each}
						</div>
					{/if}
				</div>
			</div>

			{#if variant === 'expanded'}
				<div class="mt-3 pt-3 border-t border-border/50 text-xs text-muted-foreground space-y-1">
					{#if caseData?.description}
						<p>{caseData.description}</p>
					{/if}
					<div class="flex gap-4">
						<span>Signals: {caseData?.signal_count ?? 0}</span>
						<span>Observables: {caseData?.observable_count ?? 0}</span>
					</div>
				</div>
			{/if}

			{#if nextStatus && onStatusChange}
				<div class="mt-2 pt-2 border-t border-border/50">
					<button
						onclick={() => onStatusChange(nextStatus)}
						class="text-xs text-primary hover:text-primary/80 transition-colors"
					>
						Move to {nextStatus} →
					</button>
				</div>
			{/if}
		</div>
	{/if}
</div>
