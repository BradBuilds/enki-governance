<script lang="ts">
	import { onMount, onDestroy } from 'svelte';

	interface Props {
		topics: any[];
		onSelect?: (topic: any) => void;
	}
	let { topics, onSelect }: Props = $props();

	let mapContainer: HTMLDivElement | null = $state(null);
	let map: any = null;
	let loading = $state(true);
	let located = $state(0);

	type TopicPin = { topic: any; lat: number; lon: number };

	async function loadTopicPins(): Promise<TopicPin[]> {
		// Each topic has an entities endpoint that returns people/places/orgs
		// with lat/lon on place entities. We pick the first located place
		// and use that as the topic pin. Topics with no located place are
		// dropped from the map — the count is surfaced to the user.
		const results: TopicPin[] = [];
		await Promise.all(
			(topics || []).map(async (t) => {
				try {
					const res = await fetch(`/api/watch-topics/${t.topic_id}/entities`);
					if (!res.ok) return;
					const data = await res.json();
					const located = (data.entities || []).find(
						(e: any) => e.latitude != null && e.longitude != null,
					);
					if (located) {
						results.push({ topic: t, lat: located.latitude, lon: located.longitude });
					}
				} catch {
					/* swallow — topic just won't appear on the map */
				}
			}),
		);
		return results;
	}

	onMount(async () => {
		const ml = await import('maplibre-gl');
		if (!mapContainer) return;

		map = new ml.Map({
			container: mapContainer,
			style: {
				version: 8,
				sources: {
					'dark-base': {
						type: 'raster',
						tiles: ['https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png'],
						tileSize: 256,
						attribution: '&copy; CartoDB',
					},
				},
				layers: [{ id: 'dark-base-layer', type: 'raster', source: 'dark-base' }],
			},
			center: [20, 30],
			zoom: 1.8,
			minZoom: 1.2,
			maxZoom: 10,
		});

		map.on('load', async () => {
			const pins = await loadTopicPins();
			located = pins.length;

			for (const pin of pins) {
				const el = document.createElement('div');
				el.className = 'topic-map-pin';
				el.style.cssText = `
					width: 28px; height: 28px; border-radius: 999px;
					background: hsl(var(--primary) / 0.25);
					border: 2px solid hsl(var(--primary));
					display: flex; align-items: center; justify-content: center;
					color: hsl(var(--primary)); font-size: 10px; font-weight: 700;
					cursor: pointer;
				`;
				el.textContent = String(pin.topic.article_count ?? 0);
				el.title = pin.topic.title;
				el.addEventListener('click', () => onSelect?.(pin.topic));

				new ml.Marker({ element: el }).setLngLat([pin.lon, pin.lat]).addTo(map);
			}

			loading = false;
		});
	});

	onDestroy(() => {
		if (map) {
			try {
				map.remove();
			} catch {}
			map = null;
		}
	});
</script>

<div class="w-full h-full relative rounded-xl border border-border bg-card overflow-hidden">
	<div bind:this={mapContainer} class="w-full h-full"></div>

	<div class="absolute top-2 left-2 px-2 py-1 rounded-md bg-card/80 backdrop-blur text-[10px] text-muted-foreground">
		{#if loading}
			Loading topic locations...
		{:else}
			{located} of {topics?.length ?? 0} topics located
		{/if}
	</div>
</div>
