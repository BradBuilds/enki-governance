<script lang="ts">
	import CheckCircle from 'lucide-svelte/icons/check-circle';
	import ExternalLink from 'lucide-svelte/icons/external-link';

	let { nodeId, dashboardUrl }: { nodeId: string | null; dashboardUrl?: string } = $props();
</script>

<div class="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-6 mb-6">
	<div class="flex items-center gap-3 mb-3">
		<CheckCircle class="w-5 h-5 text-emerald-400" />
		<h2 class="text-sm font-semibold text-foreground">Your node is alive</h2>
	</div>
	<p class="text-sm text-muted-foreground leading-relaxed mb-4">
		The install completed and your node has checked in with the hub. The install
		companion has retired — your full assistant now lives on your own node, powered
		by local Gemma 4. Open your dashboard to continue.
	</p>
	{#if dashboardUrl}
		<a
			href={dashboardUrl}
			class="inline-flex items-center gap-2 px-3 py-2 rounded-lg bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 transition-colors"
		>
			Open dashboard <ExternalLink class="w-3.5 h-3.5" />
		</a>
	{/if}
	{#if nodeId}
		<p class="text-xs text-muted-foreground mt-3">
			Node ID: <code class="font-mono">{nodeId}</code>
		</p>
	{/if}
</div>
