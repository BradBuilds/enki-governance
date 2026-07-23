<script lang="ts">
	import { screen } from '$lib/stores/screen.svelte';
	import ArrowUpDown from 'lucide-svelte/icons/arrow-up-down';
	import ArrowUp from 'lucide-svelte/icons/arrow-up';
	import ArrowDown from 'lucide-svelte/icons/arrow-down';
	import ChevronDown from 'lucide-svelte/icons/chevron-down';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import type { Component, Snippet } from 'svelte';

	export interface ColumnDef {
		key: string;
		label: string;
		sortable?: boolean;
		width?: string;
		hideBelow?: 'mobile' | 'tablet';
		render?: (value: any, row: any) => string;
	}

	interface Props {
		columns: ColumnDef[];
		rows: any[];
		rowKey?: string;
		sortKey?: string;
		sortDir?: 'asc' | 'desc';
		onSort?: (key: string) => void;
		onRowClick?: (row: any) => void;
		expandedRow?: string | null;
		expandSlot?: Snippet<[any]>;
	}

	let {
		columns,
		rows,
		rowKey = 'id',
		sortKey = '',
		sortDir = 'asc',
		onSort,
		onRowClick,
		expandedRow = null,
		expandSlot
	}: Props = $props();

	function isVisible(col: ColumnDef): boolean {
		if (!col.hideBelow) return true;
		if (col.hideBelow === 'mobile' && screen.isMobile) return false;
		if (col.hideBelow === 'tablet' && (screen.isMobile || screen.isTablet)) return false;
		return true;
	}

	const visibleColumns = $derived(columns.filter(isVisible));

	function getValue(row: any, key: string): any {
		return key.split('.').reduce((obj, k) => obj?.[k], row);
	}
</script>

<div class="w-full overflow-x-auto">
	<table class="w-full text-sm">
		<thead>
			<tr class="border-b border-border">
				{#if expandSlot}
					<th class="w-8 p-2"></th>
				{/if}
				{#each visibleColumns as col}
					<th
						class="text-left p-2 text-xs font-medium text-muted-foreground uppercase tracking-wider"
						style:width={col.width}
					>
						{#if col.sortable && onSort}
							<button
								onclick={() => onSort(col.key)}
								class="flex items-center gap-1 hover:text-foreground transition-colors"
							>
								{col.label}
								{#if sortKey === col.key}
									{#if sortDir === 'asc'}
										<ArrowUp size={14} />
									{:else}
										<ArrowDown size={14} />
									{/if}
								{:else}
									<ArrowUpDown size={14} class="opacity-30" />
								{/if}
							</button>
						{:else}
							{col.label}
						{/if}
					</th>
				{/each}
			</tr>
		</thead>
		<tbody>
			{#each rows as row (getValue(row, rowKey))}
				{@const isExpanded = expandedRow === getValue(row, rowKey)}
				<tr
					class="border-b border-border/50 hover:bg-muted/50 transition-colors {onRowClick ? 'cursor-pointer' : ''}"
					onclick={() => onRowClick?.(row)}
				>
					{#if expandSlot}
						<td class="p-2 w-8">
							{#if isExpanded}
								<ChevronDown size={16} class="text-muted-foreground" />
							{:else}
								<ChevronRight size={16} class="text-muted-foreground" />
							{/if}
						</td>
					{/if}
					{#each visibleColumns as col}
						<td class="p-2 text-foreground">
							{#if col.render}
								{@html col.render(getValue(row, col.key), row)}
							{:else}
								{getValue(row, col.key) ?? '—'}
							{/if}
						</td>
					{/each}
				</tr>
				{#if isExpanded && expandSlot}
					<tr class="bg-muted/30">
						<td colspan={visibleColumns.length + 1} class="p-4">
							{@render expandSlot(row)}
						</td>
					</tr>
				{/if}
			{/each}
		</tbody>
	</table>
</div>
