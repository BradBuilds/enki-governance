<script lang="ts">
	import { onMount } from 'svelte';
	import CardShell from './CardShell.svelte';
	import User from 'lucide-svelte/icons/user';
	import Building from 'lucide-svelte/icons/building';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Plane from 'lucide-svelte/icons/plane';
	import Car from 'lucide-svelte/icons/car';
	import Smartphone from 'lucide-svelte/icons/smartphone';
	import Ship from 'lucide-svelte/icons/ship';
	import Box from 'lucide-svelte/icons/box';
	import Tag from 'lucide-svelte/icons/tag';

	interface Props {
		entity_id: number;
		size?: 'compact' | 'default' | 'detail';
		entity?: any;  // optional pre-fetched data
	}

	let { entity_id, size = 'default', entity: prefetched }: Props = $props();

	let entity = $state<any>(prefetched ?? null);
	let loading = $state(!prefetched);
	let error = $state<string | null>(null);

	onMount(async () => {
		if (prefetched) return;
		try {
			const res = await fetch(`/api/entities/${entity_id}`);
			if (res.ok) {
				entity = await res.json();
			} else {
				error = `entity ${entity_id} not found`;
			}
		} catch (e: any) {
			error = e?.message || 'fetch failed';
		} finally {
			loading = false;
		}
	});

	const iconFor = (t: string) => {
		switch (t) {
			case 'person': return User;
			case 'organization': case 'government_agency': case 'mass_media': return Building;
			case 'aircraft': return Plane;
			case 'vehicle': return Car;
			case 'vessel': return Ship;
			case 'device': return Smartphone;
			case 'city': case 'country': case 'admin_region': case 'place':
			case 'facility_location': case 'maritime_area': return MapPin;
			default: return Box;
		}
	};

	const title = $derived(entity?.canonical_name || `entity ${entity_id}`);
	const subtitle = $derived(
		entity ? `${entity.canonical_type ?? '—'} · ${entity.entity_kind ?? 'instance'}` : ''
	);
	const icon = $derived(iconFor(entity?.canonical_type ?? 'unknown'));
</script>

<CardShell
	{size}
	{icon}
	{title}
	{subtitle}
	address={entity?.base60_address}
	href={`/entity/${entity_id}`}
	{loading}
	{error}
>
	{#snippet children()}
		{#if entity}
			<div class="grid grid-cols-{size === 'detail' ? 4 : 3} gap-3 text-xs">
				<div>
					<div class="text-muted-foreground">Mentions</div>
					<div class="text-foreground font-medium">{entity.signal_count ?? 0}</div>
				</div>
				<div>
					<div class="text-muted-foreground">Facts</div>
					<div class="text-foreground font-medium">{entity.fact_count ?? 0}</div>
				</div>
				<div>
					<div class="text-muted-foreground">Events</div>
					<div class="text-foreground font-medium">{entity.event_count ?? 0}</div>
				</div>
				{#if size === 'detail' && entity.concept_parent}
					<div>
						<div class="text-muted-foreground">Concept</div>
						<a href={`/entity/${entity.concept_parent.entity_id}`} class="text-primary hover:underline">
							{entity.concept_parent.canonical_name}
						</a>
					</div>
				{/if}
			</div>
			{#if size !== 'compact' && entity.aliases && entity.aliases.length > 0}
				<div class="flex items-center gap-1.5 mt-3 flex-wrap">
					<Tag size={12} class="text-muted-foreground" />
					{#each entity.aliases.slice(0, 4) as alias}
						<span class="text-[10px] bg-muted text-muted-foreground px-1.5 py-0.5 rounded">{alias}</span>
					{/each}
					{#if entity.aliases.length > 4}
						<span class="text-[10px] text-muted-foreground">+{entity.aliases.length - 4} more</span>
					{/if}
				</div>
			{/if}
		{/if}
	{/snippet}
</CardShell>
