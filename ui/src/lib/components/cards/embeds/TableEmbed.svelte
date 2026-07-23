<script lang="ts">
	/** Table embed — renders structured data as a sortable table. */
	let { data }: { data: any } = $props();

	const headers = $derived(data?.headers || []);
	const rows = $derived(data?.rows || []);
	const maxRows = $derived(Math.min(rows.length, 10));
</script>

<div class="rounded-lg border border-border overflow-hidden">
	<div class="overflow-x-auto">
		<table class="w-full text-xs">
			{#if headers.length > 0}
				<thead>
					<tr class="bg-muted">
						{#each headers as h}
							<th class="px-3 py-2 text-left text-muted-foreground font-medium">{h}</th>
						{/each}
					</tr>
				</thead>
			{/if}
			<tbody>
				{#each rows.slice(0, maxRows) as row}
					<tr class="border-t border-border/50">
						{#each Array.isArray(row) ? row : Object.values(row) as cell}
							<td class="px-3 py-1.5 text-foreground">{cell}</td>
						{/each}
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	{#if rows.length > maxRows}
		<div class="px-3 py-1.5 text-[10px] text-muted-foreground bg-muted border-t border-border">
			Showing {maxRows} of {rows.length} rows
		</div>
	{/if}
</div>
