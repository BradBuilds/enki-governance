<script lang="ts">
	interface Props {
		title?: string;
		columns: { key: string; label: string }[];
		rows: any[];
		maxRows?: number;
	}

	let { title, columns, rows, maxRows = 5 }: Props = $props();
	let showAll = $state(false);

	const displayRows = $derived(showAll ? rows : rows.slice(0, maxRows));
	const hasMore = $derived(rows.length > maxRows);
</script>

<div class="rounded-lg border border-border bg-card overflow-hidden">
	{#if title}
		<div class="px-3 py-2 border-b border-border/50">
			<span class="text-xs font-medium text-muted-foreground">{title}</span>
		</div>
	{/if}
	<div class="overflow-x-auto">
		<table class="w-full text-xs">
			<thead>
				<tr class="border-b border-border/50">
					{#each columns as col}
						<th class="text-left px-3 py-1.5 text-muted-foreground font-medium">{col.label}</th>
					{/each}
				</tr>
			</thead>
			<tbody>
				{#each displayRows as row}
					<tr class="border-b border-border/30 last:border-0">
						{#each columns as col}
							<td class="px-3 py-1.5 text-foreground">{row[col.key] ?? '—'}</td>
						{/each}
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	{#if hasMore}
		<button
			onclick={() => showAll = !showAll}
			class="w-full px-3 py-1.5 text-xs text-primary hover:bg-muted/50 transition-colors border-t border-border/50"
		>
			{showAll ? 'Show less' : `Show all ${rows.length}`}
		</button>
	{/if}
</div>
