<script lang="ts">
	import { onMount } from 'svelte';
	import CardShell from './CardShell.svelte';
	import Library from 'lucide-svelte/icons/library';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';

	interface Props {
		library_id: number;
		size?: 'compact' | 'default' | 'detail';
		library?: any;
		onRefresh?: () => void;
	}

	let { library_id, size = 'default', library: prefetched, onRefresh }: Props = $props();

	let library = $state<any>(prefetched ?? null);
	let loading = $state(!prefetched);
	let error = $state<string | null>(null);

	onMount(async () => {
		if (prefetched) return;
		try {
			const res = await fetch(`/api/libraries/${library_id}`);
			if (res.ok) {
				library = await res.json();
			} else {
				error = `library ${library_id} not found`;
			}
		} catch (e: any) {
			error = e?.message || 'fetch failed';
		} finally {
			loading = false;
		}
	});

	const subtypeLabel = $derived.by(() => {
		const s = library?.library_subtype || '';
		return s.replace('_library', '');
	});

	const title = $derived(library?.focal_term || library?.canonical_name || `library ${library_id}`);
	const subtitle = $derived(
		library ? `${subtypeLabel} library · ${library.discovery_status ?? 'unknown'}` : ''
	);

	const totalAcquired = $derived((library?.acquired_count ?? 0) + (library?.ingested_count ?? 0));
</script>

<CardShell
	{size}
	icon={Library}
	{title}
	{subtitle}
	address={library?.base60_address}
	href={`/library/${library_id}`}
	{loading}
	{error}
>
	{#snippet actions()}
		{#if onRefresh}
			<button onclick={(e) => { e.preventDefault(); onRefresh?.(); }}
				class="p-1 rounded-md text-muted-foreground hover:text-primary hover:bg-muted transition-colors"
				aria-label="Refresh">
				<RefreshCw size={14} />
			</button>
		{/if}
	{/snippet}

	{#snippet children()}
		{#if library}
			<div class="grid grid-cols-{size === 'detail' ? 6 : 4} gap-3 text-xs">
				<div>
					<div class="text-muted-foreground">Refs</div>
					<div class="text-foreground font-medium">{library.reference_count ?? 0}</div>
				</div>
				<div>
					<div class="text-muted-foreground">Acquired</div>
					<div class="text-emerald-500 font-medium">{totalAcquired}</div>
				</div>
				<div>
					<div class="text-muted-foreground">Pending</div>
					<div class="text-amber-500 font-medium">{library.pending_count ?? 0}</div>
				</div>
				<div>
					<div class="text-muted-foreground">Entities</div>
					<div class="text-foreground font-medium">{library.entity_count ?? 0}</div>
				</div>
				{#if size === 'detail'}
					<div>
						<div class="text-muted-foreground">Facts</div>
						<div class="text-foreground font-medium">{library.fact_count ?? 0}</div>
					</div>
					<div>
						<div class="text-muted-foreground">Docs</div>
						<div class="text-foreground font-medium">{library.document_count ?? 0}</div>
					</div>
				{/if}
			</div>

			{#if size !== 'compact' && library.last_refreshed_at}
				<div class="mt-2 text-[10px] text-muted-foreground">
					Last refresh: {new Date(library.last_refreshed_at).toLocaleString()}
				</div>
			{/if}
		{/if}
	{/snippet}
</CardShell>
