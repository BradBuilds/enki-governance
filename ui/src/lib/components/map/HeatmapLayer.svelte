<script lang="ts">
	/**
	 * MapLibre heatmap layer driven by /api/news-heatmap.
	 *
	 * Phase 4.4 of the collapse rebuild plan. Toggleable via MapLayersDropdown.
	 * Auto-fades at zoom > 6 in favor of individual mention markers (zoom-
	 * driven density rendering — what makes the heatmap feel right at every
	 * scale).
	 */
	import { onMount, onDestroy, untrack } from 'svelte';
	import { getHeaders } from '$lib/api/mock';

	interface Props {
		map: any; // MapLibre map handle
		visible?: boolean;
		windowHours?: number;
		bbox?: [number, number, number, number] | null;
		sources?: string[]; // catalog_ids to filter by
		onCellClick?: (cell: HeatmapCell) => void;
		onMetaUpdate?: (meta: { mentions: number; cells: number }) => void;
	}

	type HeatmapCell = {
		lng: number;
		lat: number;
		count: number;
		story_ids: number[];
	};

	let {
		map,
		visible = true,
		windowHours = 24,
		bbox = null,
		sources = [],
		onCellClick,
		onMetaUpdate
	}: Props = $props();

	const SOURCE_ID = 'enki-news-heatmap';
	const HEATMAP_LAYER = 'enki-news-heatmap-layer';
	const POINT_LAYER = 'enki-news-heatmap-points';

	let pollT: ReturnType<typeof setInterval> | null = null;
	let mounted = false;
	// Hover popup follows the cursor while it's over a heat-point. Holds
	// a MapLibre Popup instance, created lazily so we only depend on the
	// maplibre runtime that the parent already loads.
	let hoverPopup: any = null;

	async function fetchData() {
		if (!map) return;
		const params = new URLSearchParams();
		const since = new Date(Date.now() - windowHours * 3600 * 1000).toISOString();
		params.set('since', since);
		const zoom = Math.round(map.getZoom?.() ?? 2);
		params.set('zoom', String(zoom));
		if (bbox) params.set('bbox', bbox.join(','));
		for (const s of sources) params.append('sources', s);

		try {
			const r = await fetch(`/api/news-heatmap?${params.toString()}`, {
				headers: getHeaders()
			});
			if (!r.ok) return;
			const data = await r.json();
			if (!map.getSource(SOURCE_ID)) return;
			(map.getSource(SOURCE_ID) as any).setData(data);
			onMetaUpdate?.({
				mentions: data?.meta?.mention_count ?? 0,
				cells: data?.meta?.cell_count ?? 0
			});
		} catch (_) {
			// silent — the layer just stays empty
		}
	}

	function ensureLayer() {
		if (!map) return;
		if (!map.getSource(SOURCE_ID)) {
			map.addSource(SOURCE_ID, {
				type: 'geojson',
				data: { type: 'FeatureCollection', features: [] }
			});
		}
		if (!map.getLayer(HEATMAP_LAYER)) {
			map.addLayer({
				id: HEATMAP_LAYER,
				type: 'heatmap',
				source: SOURCE_ID,
				maxzoom: 7,
				paint: {
					// Weight: even a single mention gets noticeable presence.
					// Cap saturates earlier so 20+ mentions = full heat.
					'heatmap-weight': [
						'interpolate', ['linear'], ['get', 'count'],
						1, 0.5,
						20, 1.0
					],
					// Intensity: cranked up so blobs are visible at all zooms.
					'heatmap-intensity': [
						'interpolate', ['linear'], ['zoom'],
						0, 1.4,
						6, 3.0
					],
					// Color: punchier ramp — full alpha at low density so the
					// blue/cyan haze around hot spots is actually visible, not
					// a whisper that fades into the dark globe.
					'heatmap-color': [
						'interpolate', ['linear'], ['heatmap-density'],
						0, 'rgba(0, 0, 0, 0)',
						0.05, 'rgba(60, 130, 255, 0.6)',
						0.2,  'rgba(80, 200, 255, 0.85)',
						0.4,  'rgba(255, 220, 80, 1.0)',
						0.6,  'rgba(255, 150, 50, 1.0)',
						0.8,  'rgba(255, 90, 40, 1.0)',
						1.0,  'rgba(255, 40, 40, 1.0)'
					],
					// Bigger blobs so each hot spot reads from a distance.
					'heatmap-radius': [
						'interpolate', ['linear'], ['zoom'],
						0, 14,
						6, 50
					],
					// Hold opacity high through the whole zoom-out range so
					// the heat is the dominant visual on the globe.
					'heatmap-opacity': [
						'interpolate', ['linear'], ['zoom'],
						0, 1.0,
						5, 0.95,
						6, 0.8,
						7, 0
					]
				}
			});
		}
		if (!map.getLayer(POINT_LAYER)) {
			map.addLayer({
				id: POINT_LAYER,
				type: 'circle',
				source: SOURCE_ID,
				minzoom: 6,
				paint: {
					'circle-radius': [
						'interpolate', ['linear'], ['get', 'count'],
						1, 8,
						100, 28
					],
					'circle-color': [
						'interpolate', ['linear'], ['get', 'count'],
						1,   'rgba(80, 200, 255, 0.9)',
						10,  'rgba(255, 220, 80, 0.95)',
						50,  'rgba(255, 120, 50, 1.0)',
						100, 'rgba(255, 50, 40, 1.0)'
					],
					'circle-stroke-color': '#ffffff',
					'circle-stroke-width': 2,
					'circle-opacity': 0.95
				}
			});
			map.on('click', POINT_LAYER, handleClick);
			map.on('mouseenter', POINT_LAYER, handleMouseEnter);
			map.on('mousemove', POINT_LAYER, handleMouseMove);
			map.on('mouseleave', POINT_LAYER, handleMouseLeave);
		}
	}

	type ArticleStub = {
		headline: string;
		source?: string;
		link?: string;
		timestamp?: string;
	};

	function escapeHTML(s: string): string {
		return String(s)
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;');
	}

	function relativeTime(iso?: string): string {
		if (!iso) return '';
		const ms = Date.now() - new Date(iso).getTime();
		if (!isFinite(ms)) return '';
		const mins = Math.round(ms / 60000);
		if (mins < 60) return `${mins}m`;
		const hrs = Math.round(mins / 60);
		if (hrs < 24) return `${hrs}h`;
		const days = Math.round(hrs / 24);
		return `${days}d`;
	}

	function buildPopupHTML(props: any, articles?: ArticleStub[]): string {
		const count = Number(props.count || 0);
		const noun = count === 1 ? 'mention' : 'mentions';

		// Article list — links open the source article in a new tab. The
		// whole popup is pointer-events:auto so the user can move the cursor
		// over it and click without it vanishing (handled in mouseleave).
		let articleBlock = '';
		if (articles && articles.length > 0) {
			const lis = articles
				.slice(0, 5)
				.map((a) => {
					const safeTitle = escapeHTML(a.headline);
					const safeSource = a.source ? escapeHTML(a.source) : '';
					const safeLink = a.link ? escapeHTML(a.link) : '';
					const time = relativeTime(a.timestamp);
					const titleHTML = safeLink
						? `<a href="${safeLink}" target="_blank" rel="noopener" style="color:#cfe2ff;text-decoration:none">${safeTitle}</a>`
						: `<span style="color:#e5e5e8">${safeTitle}</span>`;
					const metaParts: string[] = [];
					if (safeSource) metaParts.push(safeSource);
					if (time) metaParts.push(time);
					const metaHTML = metaParts.length
						? `<div style="font-size:10px;color:#7a7a85;margin-top:1px">${metaParts.join(' · ')}</div>`
						: '';
					return `
						<li style="margin:8px 0;line-height:1.35;font-size:12px">
							${titleHTML}
							${metaHTML}
						</li>
					`;
				})
				.join('');
			articleBlock = `
				<ul style="margin:8px 0 0;padding:0;list-style:none;max-height:280px;overflow-y:auto">
					${lis}
				</ul>
			`;
		} else {
			// Skeleton while loading — no "click for details" prompt because
			// the popup IS the details.
			articleBlock = `
				<div style="font-size:11px;color:#7a7a85;margin-top:8px;font-style:italic">
					loading stories…
				</div>
			`;
		}

		return `
			<div data-enki-popup="1" style="font-family:inherit;min-width:240px;max-width:340px;pointer-events:auto">
				<div style="font-size:13px;font-weight:600;color:#fff;border-bottom:1px solid #2a2a32;padding-bottom:4px">
					${count.toLocaleString()} ${noun}
				</div>
				${articleBlock}
			</div>
		`;
	}

	// Cache of article stubs keyed by 'lat,lng' so re-hovering the same dot is
	// instant after the first fetch.
	const articleCache = new Map<string, ArticleStub[]>();
	let hoverFetchToken = 0;
	let closeTimer: ReturnType<typeof setTimeout> | null = null;

	async function fetchArticlesForCell(lat: number, lng: number): Promise<ArticleStub[]> {
		const key = `${lat.toFixed(4)},${lng.toFixed(4)}`;
		if (articleCache.has(key)) return articleCache.get(key)!;
		try {
			const params = new URLSearchParams({
				lat: String(lat),
				lon: String(lng),
				radius_km: '25',
				hours: '168',
				limit: '5',
			});
			const r = await fetch(`/api/feed/articles-near?${params}`);
			if (!r.ok) return [];
			const data = await r.json();
			const arr = Array.isArray(data?.articles) ? data.articles : [];
			const articles: ArticleStub[] = arr.map((a: any) => ({
				headline: a.headline || a.title || '(untitled)',
				source: a.source || undefined,
				link: a.link || a.source_url || undefined,
				timestamp: a.timestamp || a.published_at || undefined,
			}));
			articleCache.set(key, articles);
			return articles;
		} catch {
			return [];
		}
	}

	function cancelClose() {
		if (closeTimer) {
			clearTimeout(closeTimer);
			closeTimer = null;
		}
	}

	function attachPopupHoverHandlers() {
		// Once the popup is in the DOM, let the cursor move INTO it without
		// triggering the close. We wire enter/leave on the popup's content
		// div, which carries our data-enki-popup marker.
		if (!hoverPopup) return;
		const el = hoverPopup.getElement?.();
		if (!el) return;
		el.addEventListener('mouseenter', cancelClose);
		el.addEventListener('mouseleave', schedulePopupClose);
	}

	function schedulePopupClose() {
		cancelClose();
		closeTimer = setTimeout(() => {
			if (hoverPopup) hoverPopup.remove();
			if (map) map.getCanvas().style.cursor = '';
		}, 250);
	}

	async function ensurePopup() {
		if (hoverPopup) return hoverPopup;
		// Lazy-import maplibre's Popup so we don't ship it twice if the
		// parent already has it loaded. Falls back gracefully if maplibre-gl
		// isn't available for some reason.
		const maplibre = await import('maplibre-gl').catch(() => null);
		if (!maplibre) return null;
		hoverPopup = new maplibre.Popup({
			closeButton: false,
			closeOnClick: false,
			offset: 12,
			className: 'enki-heatmap-hover-popup',
		});
		return hoverPopup;
	}

	async function handleMouseEnter(e: any) {
		if (!map) return;
		cancelClose(); // user moved back onto a dot before the close fired
		map.getCanvas().style.cursor = 'pointer';
		const f = e?.features?.[0];
		if (!f) return;
		const popup = await ensurePopup();
		if (!popup) return;
		const [lng, lat] = f.geometry.coordinates;
		popup.setLngLat([lng, lat]).setHTML(buildPopupHTML(f.properties)).addTo(map);
		// Wire mouseenter/leave on the popup's DOM so the user can move INTO
		// the popup to read/click articles without it disappearing.
		attachPopupHoverHandlers();

		const myToken = ++hoverFetchToken;
		fetchArticlesForCell(lat, lng).then((articles) => {
			if (myToken !== hoverFetchToken) return;
			if (!hoverPopup) return;
			hoverPopup.setHTML(buildPopupHTML(f.properties, articles));
			// Re-attach handlers since setHTML replaces the inner element.
			attachPopupHoverHandlers();
		});
	}

	function handleMouseMove(e: any) {
		if (!hoverPopup || !map) return;
		const f = e?.features?.[0];
		if (!f) return;
		const [lng, lat] = f.geometry.coordinates;
		hoverPopup.setLngLat([lng, lat]);
	}

	function handleMouseLeave() {
		// Don't close immediately — the cursor may be on its way into the
		// popup. The schedule fires after 250ms; popup's own mouseenter
		// cancels it. The close also resets the cursor.
		schedulePopupClose();
	}

	function setVisibility(v: boolean) {
		if (!map) return;
		const value = v ? 'visible' : 'none';
		if (map.getLayer(HEATMAP_LAYER)) map.setLayoutProperty(HEATMAP_LAYER, 'visibility', value);
		if (map.getLayer(POINT_LAYER)) map.setLayoutProperty(POINT_LAYER, 'visibility', value);
	}

	function handleClick(e: any) {
		const f = e?.features?.[0];
		if (!f) return;
		const props = f.properties || {};
		const story_ids =
			typeof props.story_ids === 'string' ? JSON.parse(props.story_ids) : props.story_ids || [];
		const [lng, lat] = f.geometry.coordinates;
		onCellClick?.({
			lng,
			lat,
			count: Number(props.count || 0),
			story_ids
		});
	}

	function teardown() {
		if (!map) return;
		try {
			cancelClose();
			if (hoverPopup) {
				hoverPopup.remove();
				hoverPopup = null;
			}
			if (map.getLayer(POINT_LAYER)) {
				map.off('click', POINT_LAYER, handleClick);
				map.off('mouseenter', POINT_LAYER, handleMouseEnter);
				map.off('mousemove', POINT_LAYER, handleMouseMove);
				map.off('mouseleave', POINT_LAYER, handleMouseLeave);
				map.removeLayer(POINT_LAYER);
			}
			if (map.getLayer(HEATMAP_LAYER)) map.removeLayer(HEATMAP_LAYER);
			if (map.getSource(SOURCE_ID)) map.removeSource(SOURCE_ID);
		} catch (_) {}
	}

	onMount(() => {
		if (!map) return;
		ensureLayer();
		setVisibility(visible);
		fetchData();
		pollT = setInterval(fetchData, 15_000);
		mounted = true;
	});

	$effect(() => {
		if (!mounted) return;
		setVisibility(visible);
	});

	$effect(() => {
		if (!mounted) return;
		// Refetch when window or sources change
		untrack(() => {});
		void windowHours;
		void sources;
		fetchData();
	});

	onDestroy(() => {
		if (pollT) clearInterval(pollT);
		teardown();
	});
</script>
