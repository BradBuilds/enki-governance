<script lang="ts">
	import CircleCheck from 'lucide-svelte/icons/circle-check';
	import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
	import CircleX from 'lucide-svelte/icons/circle-x';
	import CircleHelp from 'lucide-svelte/icons/circle-help';

	interface Props {
		title: string;
		status: 'healthy' | 'warning' | 'error' | 'unknown';
		metrics?: { label: string; value: string | number }[];
		variant?: 'compact' | 'standard';
	}

	let { title, status, metrics, variant = 'standard' }: Props = $props();

	const statusConfig: Record<string, { icon: any; color: string; dotColor: string }> = {
		healthy: { icon: CircleCheck, color: 'text-emerald-400', dotColor: 'bg-emerald-400' },
		warning: { icon: AlertTriangle, color: 'text-yellow-400', dotColor: 'bg-yellow-400' },
		error: { icon: CircleX, color: 'text-[var(--severity-critical)]', dotColor: 'bg-[var(--severity-critical)]' },
		unknown: { icon: CircleHelp, color: 'text-muted-foreground', dotColor: 'bg-muted-foreground' },
	};

	const config = $derived(statusConfig[status] ?? statusConfig.unknown);
</script>

{#if variant === 'compact'}
	<div class="flex items-center gap-2 px-3 py-2 rounded-lg border border-border bg-card">
		<span class="w-2 h-2 rounded-full {config.dotColor}"></span>
		<span class="text-sm text-foreground">{title}</span>
		<span class="text-xs text-muted-foreground capitalize">{status}</span>
	</div>
{:else}
	<div class="rounded-lg border border-border bg-card p-4">
		<div class="flex items-center gap-2 mb-2">
			<svelte:component this={config.icon} size={16} class={config.color} />
			<span class="text-sm font-medium text-foreground">{title}</span>
		</div>
		{#if metrics?.length}
			<div class="grid grid-cols-2 gap-2">
				{#each metrics as m}
					<div class="text-xs">
						<span class="text-muted-foreground">{m.label}</span>
						<span class="ml-1 text-foreground font-medium">{m.value}</span>
					</div>
				{/each}
			</div>
		{/if}
	</div>
{/if}
