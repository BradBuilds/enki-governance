<script lang="ts">
	/**
	 * NeighborhoodCard — the replacement for the killed /graph hairball.
	 *
	 * Shows a focal entity in the center + up to 20 direct neighbors with
	 * edge labels. Click any neighbor → it becomes the new focal.
	 *
	 * No global force-layout, no D3 spaghetti. Just a focal node with a ring
	 * of clickable neighbors and predicate labels on the rays.
	 */
	import { onMount } from 'svelte';
	import CardShell from './CardShell.svelte';
	import Network from 'lucide-svelte/icons/network';

	interface Neighbor {
		entity_id: number;
		canonical_name: string;
		canonical_type: string;
		predicate: string;
		direction: 'out' | 'in';
		corroborated_by_count: number;
	}

	interface Props {
		entity_id: number;
		size?: 'compact' | 'default' | 'detail';
		max_neighbors?: number;
		onWalk?: (newFocal: number) => void;
	}

	let { entity_id, size = 'default', max_neighbors = 20, onWalk }: Props = $props();

	let focal = $state<any>(null);
	let neighbors = $state<Neighbor[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	async function loadFor(id: number) {
		loading = true;
		error = null;
		try {
			const res = await fetch(`/api/entities/${id}/neighbors?limit=${max_neighbors}`);
			if (res.ok) {
				const data = await res.json();
				focal = data.focal;
				neighbors = (data.neighbors || []) as Neighbor[];
			} else {
				error = `neighbors for ${id} not found`;
			}
		} catch (e: any) {
			error = e?.message || 'fetch failed';
		} finally {
			loading = false;
		}
	}

	onMount(() => loadFor(entity_id));

	function walk(newFocal: number) {
		if (onWalk) {
			onWalk(newFocal);
			return;
		}
		loadFor(newFocal);
	}

	function typeAbbr(t: string): string {
		if (!t) return '?';
		const m: Record<string, string> = {
			person: 'P', organization: 'O', city: 'C', country: 'N',
			aircraft: 'A', vessel: 'V', vehicle: 'V', facility: 'F',
			government_agency: 'G', mass_media: 'M', product: 'X', device: 'D',
		};
		return m[t] || t.slice(0, 1).toUpperCase();
	}
</script>

<CardShell
	{size}
	icon={Network}
	title={focal ? `Neighborhood: ${focal.canonical_name}` : 'Neighborhood'}
	subtitle={focal ? `${neighbors.length} direct connections` : ''}
	address={focal?.base60_address}
	{loading}
	{error}
>
	{#snippet children()}
		{#if focal}
			<!-- Focal in center, neighbors as compact chips below. -->
			<!-- (A radial force-layout SVG is overkill for ≤20 nodes; chip
			      grid stays readable and tappable on mobile.) -->
			<div class="flex flex-col items-center gap-3">
				<div class="px-4 py-2 rounded-full bg-primary/15 border border-primary/30 text-primary text-sm font-semibold">
					{focal.canonical_name}
					<span class="ml-2 text-[10px] text-primary/70 font-mono">{focal.base60_address ?? ''}</span>
				</div>

				{#if neighbors.length === 0}
					<p class="text-xs text-muted-foreground italic">No direct connections yet.</p>
				{:else}
					<div class="grid grid-cols-{size === 'detail' ? 4 : 3} gap-1.5 w-full">
						{#each neighbors as n}
							<button
								onclick={() => walk(n.entity_id)}
								class="group flex flex-col items-start gap-0.5 p-2 rounded-lg border border-border bg-card hover:border-primary/40 hover:bg-muted/30 transition-colors text-left"
								title={`${n.predicate} (${n.direction === 'out' ? 'this → them' : 'them → this'})`}
							>
								<div class="flex items-center gap-1.5 w-full">
									<span class="inline-flex items-center justify-center w-5 h-5 rounded-full bg-muted text-[10px] font-mono text-muted-foreground shrink-0">
										{typeAbbr(n.canonical_type)}
									</span>
									<span class="text-xs text-foreground truncate flex-1 group-hover:text-primary">
										{n.canonical_name}
									</span>
								</div>
								<span class="text-[10px] text-muted-foreground truncate w-full">
									{n.direction === 'out' ? '→' : '←'} {n.predicate}
									{#if n.corroborated_by_count > 1}
										<span class="text-emerald-500">·{n.corroborated_by_count}</span>
									{/if}
								</span>
							</button>
						{/each}
					</div>
				{/if}

				{#if neighbors.length === max_neighbors}
					<p class="text-[10px] text-muted-foreground">
						Showing top {max_neighbors} by corroboration; more exist.
					</p>
				{/if}
			</div>
		{/if}
	{/snippet}
</CardShell>
