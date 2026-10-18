<script lang="ts">
	import { goto } from '$app/navigation';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Clock from 'lucide-svelte/icons/clock';
	import Navigation from 'lucide-svelte/icons/navigation';
	import Route from 'lucide-svelte/icons/route';

	interface Props {
		trips: any[];
		count?: number;
	}

	let { trips, count = 0 }: Props = $props();

	function formatDuration(hours: number): string {
		if (hours < 1) return `${Math.round(hours * 60)}min`;
		const h = Math.floor(hours);
		const m = Math.round((hours - h) * 60);
		return m > 0 ? `${h}h ${m}m` : `${h}h`;
	}

	function formatDate(iso: string): string {
		try {
			const d = new Date(iso);
			return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
		} catch { return iso?.slice(0, 10) || ''; }
	}

	function formatTime(iso: string): string {
		try {
			return new Date(iso).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
		} catch { return ''; }
	}

	function showOnMap(trip: any) {
		const lat = trip.start_location?.lat;
		const lon = trip.start_location?.lon;
		if (lat && lon) goto(`/map?lat=${lat}&lon=${lon}&z=10`);
	}

	function locationName(loc: any): string {
		if (!loc) return 'Unknown';
		if (loc.city && loc.country) return `${loc.city}, ${loc.country}`;
		if (loc.name) return loc.name;
		if (loc.country) return loc.country;
		return `${loc.lat?.toFixed(3)}, ${loc.lon?.toFixed(3)}`;
	}
</script>

<div class="rounded-lg border bg-card p-3 space-y-3">
	<div class="flex items-center gap-2">
		<Route size={14} class="text-primary" />
		<span class="text-xs font-medium text-muted-foreground">{count || trips.length} trip{(count || trips.length) !== 1 ? 's' : ''} found</span>
	</div>

	{#each trips.slice(0, 5) as trip}
		<div class="rounded-lg border border-border/50 bg-muted/20 p-3">
			<!-- Trip header: start → end -->
			<div class="flex items-center gap-2 mb-2">
				<span class="text-xs font-semibold text-foreground">
					{locationName(trip.start_location)}
				</span>
				{#if locationName(trip.start_location) !== locationName(trip.end_location)}
					<span class="text-muted-foreground">→</span>
					<span class="text-xs font-semibold text-foreground">
						{locationName(trip.end_location)}
					</span>
				{:else}
					<span class="text-[10px] text-muted-foreground">(round trip)</span>
				{/if}
			</div>

			<!-- Date + stats -->
			<div class="flex items-center gap-3 text-[10px] text-muted-foreground mb-2">
				<span>{formatDate(trip.start_time)}</span>
				<span class="flex items-center gap-0.5"><Clock class="w-3 h-3" /> {formatDuration(trip.duration_hours)}</span>
				{#if trip.distance_km}
					<span class="flex items-center gap-0.5"><Navigation class="w-3 h-3" /> {trip.distance_km} km</span>
				{/if}
				<span>{trip.stop_count} stop{trip.stop_count !== 1 ? 's' : ''}</span>
			</div>

			<!-- Stops timeline -->
			{#if trip.stops?.length}
				<div class="space-y-0 ml-1">
					{#each trip.stops as stop, i}
						<div class="flex items-start gap-2 {i < trip.stops.length - 1 ? 'border-l border-border ml-1 pl-3 pb-1.5' : 'ml-1 pl-3'} relative">
							<span class="absolute -left-[3px] top-0.5 w-1.5 h-1.5 rounded-full bg-primary"></span>
							<div class="flex-1 min-w-0">
								<span class="text-[11px] text-foreground">{stop.name || stop.address || `${stop.lat}, ${stop.lon}`}</span>
								<div class="flex items-center gap-2 text-[9px] text-muted-foreground">
									<span>{formatTime(stop.arrived)}</span>
									{#if stop.duration_minutes > 0}
										<span>· {formatDuration(stop.duration_minutes / 60)} dwell</span>
									{/if}
									{#if stop.city}
										<span>· {stop.city}{stop.country_code ? `, ${stop.country_code}` : ''}</span>
									{/if}
								</div>
							</div>
						</div>
					{/each}
				</div>
			{/if}

			<!-- Actions -->
			<div class="flex items-center gap-2 mt-2 pt-2 border-t border-border/30">
				<button
					onclick={() => showOnMap(trip)}
					class="flex items-center gap-1 text-[10px] px-2 py-1 rounded bg-primary/10 text-primary hover:bg-primary/20"
				>
					<MapPin class="w-3 h-3" /> Show on Map
				</button>
			</div>
		</div>
	{/each}

	{#if (count || trips.length) > 5}
		<p class="text-[10px] text-muted-foreground text-center">+{(count || trips.length) - 5} more trips</p>
	{/if}
</div>
