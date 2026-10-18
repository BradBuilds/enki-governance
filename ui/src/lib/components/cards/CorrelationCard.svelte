<script lang="ts">
	import { goto } from '$app/navigation';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import Camera from 'lucide-svelte/icons/camera';
	import Calendar from 'lucide-svelte/icons/calendar';
	import Users from 'lucide-svelte/icons/users';
	import DollarSign from 'lucide-svelte/icons/dollar-sign';
	import MessageCircle from 'lucide-svelte/icons/message-circle';
	import Navigation from 'lucide-svelte/icons/navigation';
	import Layers from 'lucide-svelte/icons/layers';

	interface Props {
		groups: {
			gps?: any[];
			photos?: any[];
			calendar?: any[];
			contacts?: any[];
			financial?: any[];
			communications?: any[];
			other?: any[];
		};
		total: number;
		center?: { lat: number; lon: number };
		radius_m?: number;
		time_range?: { after: string; before: string };
	}

	let { groups, total, center, radius_m, time_range }: Props = $props();

	const sections = $derived([
		{ key: 'gps', label: 'GPS Points', icon: MapPin, color: 'text-blue-400', items: groups.gps || [] },
		{ key: 'photos', label: 'Photos', icon: Camera, color: 'text-purple-400', items: groups.photos || [] },
		{ key: 'calendar', label: 'Calendar', icon: Calendar, color: 'text-emerald-400', items: groups.calendar || [] },
		{ key: 'communications', label: 'Communications', icon: MessageCircle, color: 'text-cyan-400', items: groups.communications || [] },
		{ key: 'contacts', label: 'Contacts', icon: Users, color: 'text-amber-400', items: groups.contacts || [] },
		{ key: 'financial', label: 'Financial', icon: DollarSign, color: 'text-green-400', items: groups.financial || [] },
		{ key: 'other', label: 'Other', icon: Layers, color: 'text-muted-foreground', items: groups.other || [] },
	].filter(s => s.items.length > 0));

	function formatTime(iso: string): string {
		if (!iso) return '';
		try { return new Date(iso).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }); }
		catch { return iso.slice(11, 16); }
	}

	function formatDate(iso: string): string {
		if (!iso) return '';
		try { return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }); }
		catch { return iso.slice(0, 10); }
	}

	function showOnMap() {
		if (center) goto(`/map?lat=${center.lat}&lon=${center.lon}&z=15`);
	}
</script>

<div class="rounded-lg border bg-card p-3 space-y-3">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-2">
			<Layers size={14} class="text-primary" />
			<span class="text-xs font-medium text-muted-foreground">{total} signals correlated</span>
		</div>
		{#if center}
			<button onclick={showOnMap} class="flex items-center gap-1 text-[10px] px-2 py-1 rounded bg-primary/10 text-primary hover:bg-primary/20">
				<Navigation class="w-3 h-3" /> Map
			</button>
		{/if}
	</div>

	<!-- Time range -->
	{#if time_range}
		<div class="text-[10px] text-muted-foreground">
			{formatDate(time_range.after)} {formatTime(time_range.after)} — {formatTime(time_range.before)}
			{#if radius_m} · within {radius_m >= 1000 ? `${(radius_m / 1000).toFixed(1)}km` : `${radius_m}m`}{/if}
		</div>
	{/if}

	<!-- Signal type summary bar -->
	<div class="flex items-center gap-1">
		{#each sections as section}
			<div class="flex items-center gap-1 px-2 py-1 rounded bg-muted/50 text-[10px]">
				<svelte:component this={section.icon} class="w-3 h-3 {section.color}" />
				<span class="text-foreground font-medium">{section.items.length}</span>
			</div>
		{/each}
	</div>

	<!-- Sections -->
	{#each sections as section}
		<div>
			<div class="flex items-center gap-1.5 mb-1">
				<svelte:component this={section.icon} class="w-3.5 h-3.5 {section.color}" />
				<span class="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">{section.label}</span>
				<span class="text-[9px] text-muted-foreground/50">({section.items.length})</span>
			</div>
			<div class="space-y-0.5 ml-5">
				{#each section.items.slice(0, 5) as item}
					<div class="flex items-center justify-between text-[11px] py-0.5">
						<span class="text-foreground truncate max-w-[70%]">
							{item.summary || item.signal_type?.split('.').pop() || 'Signal'}
						</span>
						<span class="text-muted-foreground/60 shrink-0 ml-2">{formatTime(item.timestamp)}</span>
					</div>
				{/each}
				{#if section.items.length > 5}
					<span class="text-[9px] text-muted-foreground">+{section.items.length - 5} more</span>
				{/if}
			</div>
		</div>
	{/each}

	{#if total === 0}
		<div class="text-center py-4 text-muted-foreground text-xs">
			No signals found in this area and time range.
		</div>
	{/if}
</div>
