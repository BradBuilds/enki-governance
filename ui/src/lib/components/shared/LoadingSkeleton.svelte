<script lang="ts">
	interface Props {
		lines?: number;
		type?: 'card' | 'table' | 'text';
	}

	let { lines = 3, type = 'text' }: Props = $props();
</script>

{#if type === 'card'}
	<div class="rounded-lg border border-border bg-card p-4 animate-pulse">
		<div class="flex items-center gap-3 mb-3">
			<div class="w-8 h-8 rounded-full bg-muted"></div>
			<div class="flex-1">
				<div class="h-4 w-1/3 rounded bg-muted mb-1.5"></div>
				<div class="h-3 w-1/4 rounded bg-muted"></div>
			</div>
			<div class="h-5 w-14 rounded-full bg-muted"></div>
		</div>
		<div class="h-32 rounded bg-muted mb-3"></div>
		<div class="space-y-2">
			{#each Array(2) as _}
				<div class="flex gap-4">
					<div class="h-3 w-20 rounded bg-muted"></div>
					<div class="h-3 w-32 rounded bg-muted"></div>
				</div>
			{/each}
		</div>
	</div>
{:else if type === 'table'}
	<div class="animate-pulse">
		<div class="h-10 rounded bg-muted mb-2"></div>
		{#each Array(lines) as _}
			<div class="h-12 rounded bg-muted/50 mb-1"></div>
		{/each}
	</div>
{:else}
	<div class="animate-pulse space-y-2.5">
		{#each Array(lines) as _, i}
			<div class="h-4 rounded bg-muted" style:width="{80 - i * 15}%"></div>
		{/each}
	</div>
{/if}
