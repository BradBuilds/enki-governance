<script lang="ts">
	import { onMount } from 'svelte';
	import CardShell from './CardShell.svelte';
	import Calendar from 'lucide-svelte/icons/calendar';
	import MapPin from 'lucide-svelte/icons/map-pin';

	interface Props {
		event_id: number;
		size?: 'compact' | 'default' | 'detail';
		event?: any;
	}

	let { event_id, size = 'default', event: prefetched }: Props = $props();

	let event = $state<any>(prefetched ?? null);
	let loading = $state(!prefetched);
	let error = $state<string | null>(null);

	onMount(async () => {
		if (prefetched) return;
		try {
			const res = await fetch(`/api/events/${event_id}`);
			if (res.ok) {
				event = await res.json();
			} else {
				error = `event ${event_id} not found`;
			}
		} catch (e: any) {
			error = e?.message || 'fetch failed';
		} finally {
			loading = false;
		}
	});

	function formatTime(iso?: string, precision?: string): string {
		if (!iso) return 'time unknown';
		const d = new Date(iso);
		switch (precision) {
			case 'year':  return d.getUTCFullYear().toString();
			case 'month': return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short' });
			case 'day':   return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
			default:      return d.toLocaleString();
		}
	}

	const participants = $derived([
		...(event?.actor_entity_ids || []),
		...(event?.target_entity_ids || []),
	]);

	const title = $derived(
		event?.title || (event?.event_type ? `${event.event_type} event` : `event ${event_id}`)
	);
	const subtitle = $derived(
		event ? `${event.event_type ?? 'event'} · ${participants.length} participant${participants.length === 1 ? '' : 's'}` : ''
	);
</script>

<CardShell
	{size}
	icon={Calendar}
	{title}
	{subtitle}
	address={event?.base60_address}
	href={`/event/${event_id}`}
	{loading}
	{error}
>
	{#snippet children()}
		{#if event}
			<div class="space-y-1.5 text-xs">
				<div class="flex items-center gap-1.5">
					<Calendar size={12} class="text-muted-foreground" />
					<span class="text-foreground">
						{formatTime(event.event_time, event.temporal_precision)}
					</span>
					{#if event.temporal_precision && event.temporal_precision !== 'day'}
						<span class="text-[10px] text-muted-foreground">({event.temporal_precision})</span>
					{/if}
				</div>
				{#if event.location_name || event.latitude}
					<div class="flex items-center gap-1.5">
						<MapPin size={12} class="text-muted-foreground" />
						<span class="text-foreground">
							{event.location_name || `${event.latitude?.toFixed(3)}, ${event.longitude?.toFixed(3)}`}
						</span>
					</div>
				{/if}
				{#if size !== 'compact' && event.confidence !== null && event.confidence !== undefined}
					<div class="flex items-center gap-1.5">
						<span class="text-muted-foreground">confidence:</span>
						<span class="text-foreground">{Number(event.confidence).toFixed(2)}</span>
					</div>
				{/if}
				{#if size === 'detail' && participants.length > 0}
					<div class="mt-2">
						<div class="text-muted-foreground mb-1">Participants ({participants.length})</div>
						<div class="text-[10px] font-mono text-muted-foreground">
							{participants.slice(0, 6).join(', ')}{participants.length > 6 ? ` +${participants.length - 6}` : ''}
						</div>
					</div>
				{/if}
			</div>
		{/if}
	{/snippet}
</CardShell>
