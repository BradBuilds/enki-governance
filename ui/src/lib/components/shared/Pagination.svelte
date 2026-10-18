<script lang="ts">
	import ChevronLeft from 'lucide-svelte/icons/chevron-left';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';

	interface Props {
		page: number;
		pageSize: number;
		total: number;
		onPageChange: (page: number) => void;
	}

	let { page, pageSize, total, onPageChange }: Props = $props();

	const totalPages = $derived(Math.max(1, Math.ceil(total / pageSize)));
	const from = $derived((page - 1) * pageSize + 1);
	const to = $derived(Math.min(page * pageSize, total));
	const hasPrev = $derived(page > 1);
	const hasNext = $derived(page < totalPages);

	const visiblePages = $derived.by(() => {
		const pages: number[] = [];
		const start = Math.max(1, page - 2);
		const end = Math.min(totalPages, page + 2);
		for (let i = start; i <= end; i++) pages.push(i);
		return pages;
	});
</script>

<div class="flex items-center justify-between text-sm">
	<span class="text-muted-foreground">
		Showing {from}–{to} of {total.toLocaleString()}
	</span>
	<div class="flex items-center gap-1">
		<button
			onclick={() => hasPrev && onPageChange(page - 1)}
			disabled={!hasPrev}
			class="p-1.5 rounded hover:bg-muted disabled:opacity-30 disabled:cursor-not-allowed"
		>
			<ChevronLeft size={16} />
		</button>
		{#each visiblePages as p}
			<button
				onclick={() => onPageChange(p)}
				class="min-w-[32px] h-8 rounded text-sm {p === page ? 'bg-primary text-primary-foreground' : 'hover:bg-muted text-muted-foreground'}"
			>
				{p}
			</button>
		{/each}
		<button
			onclick={() => hasNext && onPageChange(page + 1)}
			disabled={!hasNext}
			class="p-1.5 rounded hover:bg-muted disabled:opacity-30 disabled:cursor-not-allowed"
		>
			<ChevronRight size={16} />
		</button>
	</div>
</div>
