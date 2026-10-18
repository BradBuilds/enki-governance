<script lang="ts">
	import { screen } from '$lib/stores/screen.svelte';
	import type { Snippet } from 'svelte';

	export interface KanbanColumn {
		id: string;
		title: string;
		items: any[];
		badge?: number;
	}

	interface Props {
		columns: KanbanColumn[];
		onMove: (itemId: string, fromCol: string, toCol: string) => void;
		itemKey?: string;
		cardSlot: Snippet<[any]>;
	}

	let { columns, onMove, itemKey = 'id', cardSlot }: Props = $props();

	let mobileTab = $state(columns[0]?.id ?? '');
	let dragItem: { id: string; fromCol: string } | null = null;
	let dragOverCol = $state('');

	function onDragStart(e: DragEvent, itemId: string, colId: string) {
		dragItem = { id: itemId, fromCol: colId };
		if (e.dataTransfer) {
			e.dataTransfer.effectAllowed = 'move';
			e.dataTransfer.setData('text/plain', itemId);
		}
	}

	function onDragOver(e: DragEvent, colId: string) {
		e.preventDefault();
		if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
		dragOverCol = colId;
	}

	function onDragLeave() {
		dragOverCol = '';
	}

	function onDrop(e: DragEvent, colId: string) {
		e.preventDefault();
		dragOverCol = '';
		if (dragItem && dragItem.fromCol !== colId) {
			onMove(dragItem.id, dragItem.fromCol, colId);
		}
		dragItem = null;
	}

	function getItemId(item: any): string {
		return item[itemKey] ?? '';
	}
</script>

{#if screen.isMobile}
	<!-- Mobile: tab bar + single column -->
	<div class="flex gap-1 mb-3 overflow-x-auto pb-1 -mx-1 px-1">
		{#each columns as col}
			<button
				onclick={() => mobileTab = col.id}
				class="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm whitespace-nowrap transition-colors
					{mobileTab === col.id
						? 'bg-primary text-primary-foreground'
						: 'bg-muted text-muted-foreground hover:bg-muted/80'}"
			>
				{col.title}
				{#if col.items.length > 0}
					<span class="text-xs opacity-75">({col.items.length})</span>
				{/if}
			</button>
		{/each}
	</div>
	<div class="space-y-2">
		{#each columns.find(c => c.id === mobileTab)?.items ?? [] as item (getItemId(item))}
			<div class="rounded-lg">
				{@render cardSlot(item)}
			</div>
		{/each}
	</div>
{:else}
	<!-- Desktop/Tablet: side-by-side columns -->
	<div class="flex gap-4 overflow-x-auto min-h-[400px]">
		{#each columns as col}
			<div
				class="flex-1 min-w-[240px] flex flex-col rounded-lg transition-colors
					{dragOverCol === col.id ? 'bg-primary/5 ring-2 ring-primary/30' : 'bg-muted/30'}"
				ondragover={(e) => onDragOver(e, col.id)}
				ondragleave={onDragLeave}
				ondrop={(e) => onDrop(e, col.id)}
			>
				<!-- Column header -->
				<div class="flex items-center justify-between px-3 py-2 border-b border-border/50">
					<h3 class="text-sm font-medium text-foreground">{col.title}</h3>
					<span class="text-xs text-muted-foreground bg-muted rounded-full px-2 py-0.5">
						{col.items.length}
					</span>
				</div>
				<!-- Column items -->
				<div class="flex-1 p-2 space-y-2 overflow-y-auto">
					{#each col.items as item (getItemId(item))}
						<div
							class="cursor-grab active:cursor-grabbing"
							draggable="true"
							ondragstart={(e) => onDragStart(e, getItemId(item), col.id)}
						>
							{@render cardSlot(item)}
						</div>
					{/each}
				</div>
			</div>
		{/each}
	</div>
{/if}
