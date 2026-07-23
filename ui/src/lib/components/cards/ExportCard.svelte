<script lang="ts">
	import Download from 'lucide-svelte/icons/download';

	interface Props {
		downloadUrl: string;
		type?: string;
		format?: string;
	}

	let { downloadUrl, type = 'data', format = 'csv' }: Props = $props();
</script>

<div class="rounded-lg border border-emerald-500/20 bg-emerald-500/5 p-3">
	<div class="flex items-center gap-3">
		<Download size={16} class="text-emerald-400 shrink-0" />
		<div class="flex-1 min-w-0">
			<p class="text-sm text-foreground">Export ready</p>
			<p class="text-xs text-muted-foreground">{type} · {format.toUpperCase()}</p>
		</div>
		<a
			href={downloadUrl}
			download
			class="shrink-0 px-3 py-1.5 text-xs font-medium rounded-md bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors"
		>
			Download
		</a>
	</div>
</div>
