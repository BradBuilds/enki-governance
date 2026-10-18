<script lang="ts">
	import Badge from '$lib/components/shared/Badge.svelte';
	import ShieldAlert from 'lucide-svelte/icons/shield-alert';
	import ArrowUpRight from 'lucide-svelte/icons/arrow-up-right';
	import Check from 'lucide-svelte/icons/check';
	import EyeOff from 'lucide-svelte/icons/eye-off';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';

	interface Props {
		alert: any;
		variant?: 'compact' | 'standard' | 'expanded';
		onTriage?: () => void;
		onEscalate?: () => void;
		onSuppress?: () => void;
		onResolve?: () => void;
	}

	let { alert, variant = 'standard', onTriage, onEscalate, onSuppress, onResolve }: Props = $props();

	const severityMap: Record<string, 'critical' | 'high' | 'medium' | 'low' | 'info'> = {
		critical: 'critical', high: 'high', medium: 'medium', low: 'low', informational: 'info'
	};

	const statusMap: Record<string, 'info' | 'medium' | 'high' | 'success'> = {
		new: 'info', triaged: 'medium', escalated: 'high', resolved: 'success', suppressed: 'neutral' as any
	};

	const borderColors: Record<string, string> = {
		critical: 'border-l-[var(--severity-critical)]',
		high: 'border-l-[var(--severity-high)]',
		medium: 'border-l-[var(--severity-medium)]',
		low: 'border-l-[var(--severity-low)]',
		informational: 'border-l-border',
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
</script>

<div class="rounded-lg border border-border bg-card overflow-hidden border-l-4 {borderColors[alert?.severity] ?? 'border-l-border'}">
	{#if variant === 'compact'}
		<!-- Compact: single line -->
		<div class="flex items-center gap-2 px-3 py-2">
			<ShieldAlert size={14} class="text-muted-foreground shrink-0" />
			<span class="text-sm text-foreground truncate flex-1">{alert?.title ?? 'Alert'}</span>
			<Badge text={alert?.severity ?? 'medium'} variant={severityMap[alert?.severity] ?? 'medium'} size="sm" />
			<Badge text={alert?.status ?? 'new'} variant={statusMap[alert?.status] ?? 'info'} size="sm" />
			<span class="text-[10px] text-muted-foreground shrink-0">{timeAgo(alert?.created_at)}</span>
		</div>
	{:else}
		<!-- Standard / Expanded -->
		<div class="px-4 py-3">
			<div class="flex items-start gap-3">
				<div class="w-8 h-8 rounded-lg bg-destructive/10 flex items-center justify-center shrink-0 mt-0.5">
					<ShieldAlert size={16} class="text-destructive" />
				</div>
				<div class="flex-1 min-w-0">
					<div class="flex items-center gap-2 flex-wrap">
						<span class="text-sm font-medium text-foreground">{alert?.title ?? 'Alert'}</span>
						<Badge text={alert?.severity ?? 'medium'} variant={severityMap[alert?.severity] ?? 'medium'} size="sm" />
						<Badge text={alert?.status ?? 'new'} variant={statusMap[alert?.status] ?? 'info'} size="sm" />
					</div>
					<div class="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
						{#if alert?.category}
							<span>{alert.category}</span>
						{/if}
						<span>{timeAgo(alert?.created_at)}</span>
					</div>
				</div>
			</div>

			{#if variant === 'expanded'}
				<div class="mt-3 pt-3 border-t border-border/50 space-y-2 text-xs text-muted-foreground">
					{#if alert?.alert_id}
						<div class="flex gap-2"><span class="font-medium">ID:</span> <span class="font-mono">{alert.alert_id}</span></div>
					{/if}
					{#if alert?.created_at}
						<div class="flex gap-2"><span class="font-medium">Created:</span> {new Date(alert.created_at).toLocaleString()}</div>
					{/if}
				</div>
			{/if}

			{#if variant !== 'compact' && (onTriage || onEscalate || onSuppress || onResolve)}
				<div class="flex items-center gap-2 mt-3 pt-2 border-t border-border/50">
					{#if onTriage && alert?.status === 'new'}
						<button onclick={onTriage} class="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors">
							<AlertTriangle size={12} /> Triage
						</button>
					{/if}
					{#if onEscalate}
						<button onclick={onEscalate} class="flex items-center gap-1 text-xs text-muted-foreground hover:text-primary transition-colors">
							<ArrowUpRight size={12} /> Escalate
						</button>
					{/if}
					{#if onSuppress}
						<button onclick={onSuppress} class="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors">
							<EyeOff size={12} /> Suppress
						</button>
					{/if}
					{#if onResolve}
						<button onclick={onResolve} class="flex items-center gap-1 text-xs text-emerald-400 hover:text-emerald-300 transition-colors">
							<Check size={12} /> Resolve
						</button>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>
