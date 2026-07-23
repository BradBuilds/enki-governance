<script lang="ts">
	import { onDestroy } from 'svelte';
	import User from 'lucide-svelte/icons/user';
	import Building2 from 'lucide-svelte/icons/building-2';
	import Globe from 'lucide-svelte/icons/globe';
	import MapPin from 'lucide-svelte/icons/map-pin';
	import FileText from 'lucide-svelte/icons/file-text';
	import ExternalLink from 'lucide-svelte/icons/external-link';

	interface Props {
		entity_id: number;
		lens?: string | null;
		children: any;
		/** ms before hover triggers the popover */
		delay?: number;
	}

	let { entity_id, lens = null, children, delay = 400 }: Props = $props();

	let hover = $state(false);
	let showTimer: ReturnType<typeof setTimeout> | null = null;
	let hideTimer: ReturnType<typeof setTimeout> | null = null;

	let entity = $state<any>(null);
	let loading = $state(false);
	let loaded = $state(false);

	async function ensureLoaded() {
		if (loaded || loading) return;
		loading = true;
		try {
			const q = lens ? `?lens=${encodeURIComponent(lens)}` : '';
			const r = await fetch(`/api/entities/${entity_id}${q}`);
			if (r.ok) entity = await r.json();
			loaded = true;
		} catch {
			loaded = true;
		} finally {
			loading = false;
		}
	}

	function handleEnter() {
		if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
		if (showTimer) clearTimeout(showTimer);
		showTimer = setTimeout(() => { hover = true; ensureLoaded(); }, delay);
	}
	function handleLeave() {
		if (showTimer) { clearTimeout(showTimer); showTimer = null; }
		hideTimer = setTimeout(() => (hover = false), 200);
	}

	onDestroy(() => {
		if (showTimer) clearTimeout(showTimer);
		if (hideTimer) clearTimeout(hideTimer);
	});

	const lensQuery = $derived(lens ? `?lens=${encodeURIComponent(lens)}` : '');
	const TypeIcon = $derived.by(() => {
		const t = (entity?.canonical_type || '').toLowerCase();
		if (t === 'person') return User;
		if (['country', 'city', 'region'].includes(t)) return Globe;
		if (['facility', 'address', 'building', 'compound', 'residence'].includes(t)) return MapPin;
		if (['organization', 'company', 'government_agency'].includes(t)) return Building2;
		return FileText;
	});
</script>

<span class="relative inline-block" onmouseenter={handleEnter} onmouseleave={handleLeave} role="presentation">
	{@render children()}

	{#if hover}
		<div class="absolute z-50 left-0 top-full mt-1 w-80 rounded-md border border-border bg-popover shadow-xl text-xs"
			onmouseenter={handleEnter} onmouseleave={handleLeave} role="presentation">
			<div class="p-3">
				{#if loading}
					<p class="text-muted-foreground italic">Loading…</p>
				{:else if !entity}
					<p class="text-muted-foreground italic">Entity not found.</p>
				{:else}
					<!-- Header -->
					<div class="flex items-center gap-1.5 mb-1">
						<svelte:component this={TypeIcon} size={13} class="text-primary shrink-0" />
						<span class="font-semibold text-foreground truncate">
							{entity.kb_canonical_name || entity.canonical_name}
						</span>
					</div>
					<div class="text-[10px] text-muted-foreground mb-2">
						{entity.canonical_type || 'unknown'} · {entity.fact_count ?? 0} facts · {entity.event_count ?? 0} events
					</div>

					{#if entity.description}
						<p class="text-foreground/85 leading-snug mb-2 line-clamp-3">
							{entity.description}
						</p>
					{:else}
						<p class="text-muted-foreground/60 italic mb-2 text-[11px]">
							No description yet.
						</p>
					{/if}

					{#if entity.aliases && entity.aliases.length > 0 && entity.aliases.length <= 5}
						<div class="text-[10px] text-muted-foreground mb-2">
							a.k.a. <span class="text-foreground">{entity.aliases.join(', ')}</span>
						</div>
					{/if}

					<a href={`/entity/${entity_id}${lensQuery}`}
						class="inline-flex items-center gap-1 text-[11px] text-primary hover:underline mt-1">
						Open full dossier <ExternalLink size={10} />
					</a>
				{/if}
			</div>
		</div>
	{/if}
</span>
