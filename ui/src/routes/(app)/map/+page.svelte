<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { api } from '$lib/api/mock';
	import { screen } from '$lib/stores/screen.svelte';
	import { onMapCommand } from '$lib/stores/mapCommands';
	import { chat } from '$lib/stores/chat.svelte';
	import MapShell from '$lib/components/map/MapShell.svelte';
	import MapSearch from '$lib/components/map/MapSearch.svelte';
	import MapLayersDropdown from '$lib/components/map/MapLayersDropdown.svelte';
	import MapDetailPanel from '$lib/components/map/MapDetailPanel.svelte';
	import LocationInfoCard from '$lib/components/map/LocationInfoCard.svelte';
	import AddObservationModal from '$lib/components/map/AddObservationModal.svelte';
	import TimelineSlider from '$lib/components/map/TimelineSlider.svelte';
	import ReferenceLayer from '$lib/components/map/ReferenceLayer.svelte';
	import EntityAssetsLayer from '$lib/components/map/EntityAssetsLayer.svelte';
	import DrawingToolbar from '$lib/components/map/DrawingToolbar.svelte';
	import AnnotationLayer from '$lib/components/map/AnnotationLayer.svelte';
	import TrackLayer from '$lib/components/map/TrackLayer.svelte';
	import GeofenceLayer from '$lib/components/map/GeofenceLayer.svelte';
	import KnowledgeEdgeLayer from '$lib/components/map/KnowledgeEdgeLayer.svelte';
	// News-mention heatmap removed June 3 2026 — see commit msg. Aggregated
	// country-centroid mention density was visually busy but not
	// operationally meaningful (hovering a hot dot returned articles that
	// mentioned ANY entity nearby, not articles ABOUT this location). News
	// lives on /feed where ranking + filtering are first-class. Tracks /
	// Aircraft / Vessels remain on the map because they are genuinely
	// spatial (real GPS).
	// import HeatmapLayer from '$lib/components/map/HeatmapLayer.svelte';
	// import HeatmapDetailPanel from '$lib/components/map/HeatmapDetailPanel.svelte';
	import ImportButton from '$lib/components/map/ImportButton.svelte';
	import ScreenshotButton from '$lib/components/map/ScreenshotButton.svelte';
	import TimelinePlayback from '$lib/components/map/TimelinePlayback.svelte';
	import SkyViewPanel from '$lib/components/map/SkyViewPanel.svelte';
	import { LIVE_SOURCES } from '$lib/components/map/live/sources';
	import { aircraftSvg, vesselSvg, seismicSvg } from '$lib/components/map/live/icons';
	import type { MapAdapter } from '$lib/components/map/engine/types';

	// Helper for local reference data queries
	async function fetchOrMockLocal(path: string): Promise<any> {
		try {
			const res = await fetch(`/api${path}`, { headers: { 'Content-Type': 'application/json' } });
			if (res.ok) return await res.json();
		} catch {}
		return {};
	}

	// ─── Map state ──────────────────────────────────────────────────────
	let adapter = $state<MapAdapter | null>(null);
	let loading = $state(false);
	let markerHandles: any[] = [];
	let currentZoom = $state(5);

	// ─── Data ────────────────────────────────────────────────────────────
	let signals = $state<any[]>([]);
	let knowledgeNodes = $state<any[]>([]);
	let projects = $state<any[]>([]);
	let cases = $state<any[]>([]);
	let tracks = $state<any[]>([]);
	let geofences = $state<any[]>([]);
	let annotations = $state<any[]>([]);
	let knowledgeEdges = $state<any[]>([]);
	let deviceNames = $state<Map<string, string>>(new Map());
	let timeFilter = $state({ after: '', before: '' });

	// ─── Context scope ───────────────────────────────────────────────────
	let activeScope = $state<string>(''); // '' = global, 'project:id', 'case:id', 'topic:id', 'contact:id', 'asset:id'
	let activeProject = $derived(projects.find(p => `project:${p.project_id}` === activeScope));
	let projectCases = $derived(
		activeScope.startsWith('project:')
			? cases.filter((c: any) => c.project_id === activeScope.replace('project:', ''))
			: cases
	);

	// Context data for scope
	let watchTopics = $state<any[]>([]);
	let scopeContacts = $state<any[]>([]);
	let scopeAssets = $state<any[]>([]);
	let contextMarkerHandles = $state<any[]>([]);

	async function setScope(scope: string) {
		activeScope = scope;
		// Clear previous context markers + heatmap + popup
		if (activePopup) { activePopup.remove(); activePopup = null; }
		if (adapter) {
			for (const h of contextMarkerHandles) { try { h.remove ? h.remove() : adapter.remove(h); } catch {} }
			contextMarkerHandles = [];
			const rawMap = adapter.getRawMap();
			if (rawMap) {
				try { rawMap.removeLayer('context-heatmap'); } catch {}
				try { rawMap.removeSource('context-heat'); } catch {}
			}
			heatmapAdded = false;
		}

		timelineEvents = [];
		contextEntitiesRaw = [];
		playbackTime = null;
		if (!scope) { loadLiveData(); return; }

		if (scope.startsWith('topic:')) {
			const topicId = scope.replace('topic:', '');
			try {
				const [entData, tlData] = await Promise.all([
					api.watchTopicEntities(topicId),
					api.watchTopicTimeline(topicId),
				]);
				const ents = ((entData as any).entities || []).filter((e: any) => e.latitude != null && e.longitude != null);
				contextEntitiesRaw = ents;
				timelineEvents = (tlData as any).events || [];
				plotContextEntities(ents);
			} catch {}
		} else if (scope.startsWith('case:')) {
			const caseId = scope.replace('case:', '');
			try {
				const [tl, nd] = await Promise.all([api.caseTimeline(caseId), api.caseNodes(caseId)]);
				const geoSignals = ((tl as any).events || []).filter((e: any) => e.latitude != null && e.longitude != null);
				const nodes = ((nd as any).nodes || []).filter((n: any) => n.latitude != null && n.longitude != null);
				plotContextEntities([
					...geoSignals.map((s: any) => ({ ...s, entity_type: 'signal', name: s.title || s.event_type || 'Signal' })),
					...nodes.map((n: any) => ({ ...n, entity_type: 'location', name: n.title })),
				]);
			} catch {}
		} else if (scope.startsWith('project:')) {
			const projectId = scope.replace('project:', '');
			const proj = projects.find(p => p.project_id === projectId);
			if (proj?.center_lat && proj?.center_lon && adapter) {
				adapter.flyTo(proj.center_lat, proj.center_lon, proj.zoom_level || 10);
			}
		} else if (scope.startsWith('contact:')) {
			const contact = scopeContacts.find(c => `contact:${c.contact_id}` === scope);
			if (contact?.metadata?.latitude && contact?.metadata?.longitude && adapter) {
				plotContextEntities([{ ...contact, entity_type: contact.contact_type === 'country' ? 'country' : 'person', name: contact.primary_name }]);
			}
		}
		loadLiveData();
	}

	let activePopup: any = null;
	let heatmapAdded = $state(false);
	let showHeatmap = $state(true);

	// News mention heatmap (Phase 4) — independent of context heatmap.
	// ON by default so /map loads with global news density immediately.
	// News heatmap removed — see comment at imports above. State retained
	// (always false) only so the MapLayersDropdown props don't need a
	// matching change in this commit. Followup: rip the toggle out of the
	// dropdown component entirely.
	let showNewsHeatmap = $state(false);
	let newsHeatmapWindowHours = $state(24);
	// activeHeatmapCell / newsHeatmapMentionCount / HeatmapCell type were
	// removed with the heatmap. Kept the two showNewsHeatmap/window props
	// because the MapLayersDropdown component still accepts them; the
	// toggle is now a no-op until that component is updated separately.
	let timelineEvents = $state<any[]>([]);
	let contextEntitiesRaw = $state<any[]>([]); // store raw entities for time filtering
	let playbackTime = $state<Date | null>(null); // null = show all

	function onPlaybackTimeChange(time: Date | null) {
		playbackTime = time;
		if (!adapter || contextEntitiesRaw.length === 0) return;

		// Clear existing context markers
		for (const h of contextMarkerHandles) { try { h.remove ? h.remove() : adapter.remove(h); } catch {} }
		contextMarkerHandles = [];
		if (activePopup) { activePopup.remove(); activePopup = null; }

		if (!time) {
			// Show all
			plotContextEntities(contextEntitiesRaw);
			return;
		}

		// Filter entities by first_seen <= playbackTime
		const cutoff = time.getTime();
		const visible = contextEntitiesRaw.filter((e: any) => {
			const seen = e.first_seen ? new Date(e.first_seen).getTime() : 0;
			return seen <= cutoff;
		});

		// Remove and re-add heatmap with filtered data
		const rawMap = adapter.getRawMap();
		if (rawMap) {
			try { rawMap.removeLayer('context-heatmap'); } catch {}
			try { rawMap.removeSource('context-heat'); } catch {}
		}

		plotContextEntities(visible);
	}

	// Close popup when side panel opens
	$effect(() => {
		if (selectedItem && activePopup) {
			activePopup.remove();
			activePopup = null;
		}
	});

	// Universal popup for any map item (reference layers, live, signals, entities)
	function showMapPopup(lat: number, lon: number, html: string) {
		if (activePopup) { activePopup.remove(); activePopup = null; }
		selectedItem = null; selectedIsLive = false; selectedIsNode = false;
		const rawMap = adapter?.getRawMap();
		if (!rawMap) return;
		import('maplibre-gl').then(ml => {
			activePopup = new ml.Popup({ closeButton: true, closeOnClick: true, maxWidth: '300px' })
				.setLngLat([lon, lat])
				.setHTML(html)
				.addTo(rawMap);
		});
	}

	function buildRefLayerPopupHtml(name: string, layerName: string, props: any, lat: number, lon: number): string {
		const entries = Object.entries(props || {}).filter(([k]) => k !== 'name' && k !== 'category' && k !== 'layer');
		const facts = entries.slice(0, 6).map(([k, v]) =>
			`<div style="font-size:11px;margin-bottom:3px;display:flex;gap:6px;"><span style="color:#aaa;">${k.replace(/_/g, ' ')}:</span><span style="color:#eee;">${v}</span></div>`
		).join('');
		const category = props?.category || props?.layer || layerName;

		return `<div style="min-width:180px;max-width:260px;font-family:system-ui,sans-serif;">
			<div style="font-size:14px;font-weight:600;color:#fff;margin-bottom:2px;">${name}</div>
			<div style="font-size:10px;color:#888;margin-bottom:6px;">${category}</div>
			${facts ? `<div style="border-top:1px solid #333;padding-top:6px;margin-top:4px;">
				<div style="font-size:9px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:4px;">Details</div>
				${facts}
				${entries.length > 6 ? `<div style="font-size:10px;color:#888;">+${entries.length - 6} more</div>` : ''}
			</div>` : ''}
			<div style="margin-top:8px;display:flex;gap:6px;">
				<a href="/chat?q=tell me about ${name}" style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:6px;background:#3b82f620;color:#3b82f6;font-size:11px;text-decoration:none;">Research</a>
				<a href="/chat?q=tell me about ${name}" style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:6px;background:#ffffff10;color:#aaa;font-size:11px;text-decoration:none;border:1px solid #333;">Ask Enki</a>
			</div>
			<div style="font-size:9px;color:#555;margin-top:6px;">${lat.toFixed(4)}, ${lon.toFixed(4)}</div>
		</div>`;
	}

	function buildLivePopupHtml(label: string, source: string, props: any, lat: number, lon: number): string {
		const entries = Object.entries(props || {}).filter(([k, v]) => v != null && v !== '' && k !== 'key' && k !== 'label');
		const facts = entries.slice(0, 8).map(([k, v]) =>
			`<div style="font-size:11px;margin-bottom:2px;display:flex;gap:6px;"><span style="color:#aaa;">${k.replace(/_/g, ' ')}:</span><span style="color:#eee;">${typeof v === 'number' ? Math.round(v * 10) / 10 : v}</span></div>`
		).join('');

		return `<div style="min-width:180px;max-width:280px;font-family:system-ui,sans-serif;">
			<div style="display:flex;align-items:center;gap:6px;margin-bottom:2px;">
				<div style="font-size:14px;font-weight:600;color:#fff;">${label}</div>
				<span style="font-size:9px;padding:1px 5px;border-radius:6px;background:#3b82f620;color:#60a5fa;">LIVE</span>
			</div>
			<div style="font-size:10px;color:#888;margin-bottom:6px;">${source}</div>
			${facts ? `<div style="border-top:1px solid #333;padding-top:6px;">${facts}</div>` : ''}
			<div style="margin-top:8px;display:flex;gap:6px;">
				<a href="/chat?q=tell me about ${label}" style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:6px;background:#ffffff10;color:#aaa;font-size:11px;text-decoration:none;border:1px solid #333;">Ask Enki</a>
			</div>
			<div style="font-size:9px;color:#555;margin-top:6px;">${lat.toFixed(4)}, ${lon.toFixed(4)}</div>
		</div>`;
	}

	function buildPopupHtml(ent: any): string {
		const typeColors: Record<string, string> = {
			person: '#8b5cf6', organization: '#3b82f6', country: '#10b981',
			region: '#14b8a6', location: '#06b6d4', signal: '#6366f1', asset: '#f97316',
		};
		const color = typeColors[ent.entity_type] || '#6b7280';
		const name = ent.name || ent.title || 'Unknown';
		const et = ent.entity_type || '';
		const mentions = ent.mention_count ? `${ent.mention_count} mentions` : '';
		const aliases = ent.aliases?.length ? `<div style="font-size:10px;color:#888;margin-top:2px;">AKA: ${ent.aliases.join(', ')}</div>` : '';

		let facts = '';
		if (ent.properties?.length) {
			facts = `<div style="margin-top:8px;border-top:1px solid #333;padding-top:6px;">
				<div style="font-size:9px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:4px;">Key Facts</div>
				${ent.properties.map((p: any) => `<div style="font-size:11px;margin-bottom:3px;display:flex;gap:6px;"><span style="color:#aaa;">${p.key.replace(/_/g, ' ')}:</span><span style="color:#eee;">${p.value}</span></div>`).join('')}
			</div>`;
		}

		let articles = '';
		if (ent.article_ids?.length) {
			articles = `<div style="margin-top:8px;border-top:1px solid #333;padding-top:6px;">
				<div style="font-size:9px;text-transform:uppercase;letter-spacing:1px;color:#666;margin-bottom:4px;">Referenced in ${ent.article_ids.length} article${ent.article_ids.length !== 1 ? 's' : ''}</div>
				${ent.article_ids.slice(0, 3).map((id: string) => `<div style="font-size:10px;color:#93c5fd;margin-bottom:2px;">📰 ${id}</div>`).join('')}
				${ent.article_ids.length > 3 ? `<div style="font-size:10px;color:#888;">+${ent.article_ids.length - 3} more</div>` : ''}
			</div>`;
		}

		const promoteLink = ent.promoted_to
			? `<a href="/contacts/${ent.promoted_to}" style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:6px;background:#10b98120;color:#10b981;font-size:11px;text-decoration:none;margin-top:8px;">View Contact</a>`
			: `<a href="/watch/topic_ukraine?tab=entities" style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:6px;background:#3b82f620;color:#3b82f6;font-size:11px;text-decoration:none;margin-top:8px;">Research</a>`;

		return `<div style="min-width:200px;max-width:280px;font-family:system-ui,-apple-system,sans-serif;">
			<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
				<div style="width:10px;height:10px;border-radius:50%;background:${color};flex-shrink:0;"></div>
				<div style="font-size:14px;font-weight:600;color:#fff;">${name}</div>
			</div>
			<div style="display:flex;align-items:center;gap:6px;font-size:10px;">
				<span style="padding:1px 6px;border-radius:8px;background:${color}25;color:${color};font-weight:500;">${et}</span>
				${mentions ? `<span style="color:#888;">${mentions}</span>` : ''}
			</div>
			${aliases}
			${facts}
			${articles}
			<div style="margin-top:8px;display:flex;gap:6px;">
				${promoteLink}
				<a href="/chat?q=tell me about ${name}" style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:6px;background:#ffffff10;color:#aaa;font-size:11px;text-decoration:none;border:1px solid #333;">Ask Enki</a>
			</div>
		</div>`;
	}

	function plotContextEntities(ents: any[]) {
		if (!adapter) return;
		const rawMap = adapter.getRawMap();
		const maplibreLib = rawMap?._maplibre || (globalThis as any).maplibregl;

		const typeColors: Record<string, { color: string; border: string }> = {
			person: { color: '#8b5cf6', border: '#6d28d9' },
			organization: { color: '#3b82f6', border: '#1d4ed8' },
			country: { color: '#10b981', border: '#059669' },
			region: { color: '#14b8a6', border: '#0d9488' },
			location: { color: '#06b6d4', border: '#0891b2' },
			signal: { color: '#6366f1', border: '#4f46e5' },
			asset: { color: '#f97316', border: '#ea580c' },
		};
		const points: { lat: number; lon: number }[] = [];
		const heatPoints: any[] = [];

		// Crash guard: DOM markers are expensive; cap how many pins we create.
		// The heatmap (a cheap GL layer) still receives ALL points, so density
		// stays truthful even when a topic/case returns tens of thousands of entities.
		const MAX_CONTEXT_MARKERS = 2000;
		let markersMade = 0;

		for (const ent of ents) {
			const lat = ent.latitude;
			const lon = ent.longitude;
			if (lat == null || lon == null) continue;
			points.push({ lat, lon });
			heatPoints.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [lon, lat] }, properties: { weight: Math.min(1, (ent.mention_count || 1) / 50) }});

			if (markersMade >= MAX_CONTEXT_MARKERS) continue; // heatmap still shows the rest
			markersMade++;

			const style = typeColors[ent.entity_type] || typeColors.location;
			const size = ent.entity_type === 'country' ? 16 : ent.entity_type === 'region' ? 13 : 10;
			const showLabel = ent.entity_type === 'country' || ent.entity_type === 'region' || (ent.mention_count && ent.mention_count >= 10);
			const label = ent.name || ent.title || '';

			const el = document.createElement('div');
			el.style.cssText = 'display:flex;flex-direction:column;align-items:center;cursor:pointer;';

			const pin = document.createElement('div');
			const isCircle = ent.entity_type === 'country' || ent.entity_type === 'person' || ent.entity_type === 'region';
			pin.style.cssText = `width:${size}px;height:${size}px;border-radius:${isCircle ? '50%' : '3px'};background:${style.color};border:2px solid ${style.border};box-shadow:0 0 8px ${style.color}50;transition:transform 0.15s;`;
			el.appendChild(pin);

			if (showLabel) {
				const lbl = document.createElement('div');
				lbl.textContent = label;
				lbl.style.cssText = 'font-size:10px;font-weight:600;color:white;text-shadow:0 1px 3px rgba(0,0,0,0.8),0 0 6px rgba(0,0,0,0.5);margin-top:2px;white-space:nowrap;pointer-events:none;';
				el.appendChild(lbl);
			}

			el.addEventListener('mouseenter', () => { pin.style.transform = 'scale(1.4)'; });
			el.addEventListener('mouseleave', () => { pin.style.transform = 'scale(1)'; });

			el.addEventListener('click', async (e) => {
				e.stopPropagation();
				// Close any side panel — context entities use popups only
				selectedItem = null; selectedIsLive = false; selectedIsNode = false;
				if (activePopup) { activePopup.remove(); activePopup = null; }
				const ml = await import('maplibre-gl');
				activePopup = new ml.Popup({ closeButton: true, closeOnClick: true, maxWidth: '300px', className: 'entity-popup' })
					.setLngLat([lon, lat])
					.setHTML(buildPopupHtml(ent))
					.addTo(rawMap);
			});

			const ml_marker_module = import('maplibre-gl');
			ml_marker_module.then(ml => {
				const m = new ml.Marker({ element: el }).setLngLat([lon, lat]).addTo(rawMap);
				contextMarkerHandles.push(m);
			});
		}

		// Add heatmap layer
		if (heatPoints.length > 0 && rawMap) {
			// Remove existing heatmap if present
			try { rawMap.removeLayer('context-heatmap'); } catch {}
			try { rawMap.removeSource('context-heat'); } catch {}

			rawMap.addSource('context-heat', {
				type: 'geojson',
				data: { type: 'FeatureCollection', features: heatPoints },
			});
			rawMap.addLayer({
				id: 'context-heatmap',
				type: 'heatmap',
				source: 'context-heat',
				paint: {
					'heatmap-weight': ['get', 'weight'],
					'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 2, 0.5, 8, 2],
					'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 2, 30, 8, 50, 14, 80],
					'heatmap-color': [
						'interpolate', ['linear'], ['heatmap-density'],
						0, 'rgba(0,0,0,0)',
						0.1, 'rgba(6,182,212,0.2)',
						0.3, 'rgba(59,130,246,0.4)',
						0.5, 'rgba(139,92,246,0.5)',
						0.7, 'rgba(249,115,22,0.6)',
						0.9, 'rgba(239,68,68,0.7)',
						1, 'rgba(255,255,255,0.8)',
					],
					'heatmap-opacity': ['interpolate', ['linear'], ['zoom'], 7, 0.8, 14, 0.2],
				},
			});
			heatmapAdded = true;
		}

		// Fit bounds
		if (points.length > 0 && adapter) {
			if (points.length === 1) {
				adapter.flyTo(points[0].lat, points[0].lon, 6);
			} else {
				const north = Math.max(...points.map(p => p.lat));
				const south = Math.min(...points.map(p => p.lat));
				const east = Math.max(...points.map(p => p.lon));
				const west = Math.min(...points.map(p => p.lon));
				adapter.fitBounds({ north, south, east, west }, 60);
			}
		}
	}

	// ─── Selection ───────────────────────────────────────────────────────
	let selectedItem = $state<any>(null);
	let selectedIsLive = $state(false);
	let selectedIsNode = $state(false);
	let selectedLiveSource = $state('');
	let addObservationAt = $state<{ lat: number; lng: number } | null>(null);
	let skyViewAt = $state<{ lat: number; lon: number } | null>(null);

	// ─── Live data ───────────────────────────────────────────────────────
	let liveData = $state<Record<string, any[]>>({});
	let liveHandles: any[] = [];
	let liveTrailHandles: any[] = [];
	let liveInterval: ReturnType<typeof setInterval> | null = null;
	let activeTrailKey = $state('');

	// ─── Generic reference layers (cell towers, WiFi, power plants, etc.) ─
	let refLayerConfigs = $state<any[]>([]);
	let refLayerEnabled = $state<Record<string, boolean>>({});
	let refLayerData = $state<Record<string, any[]>>({});
	let refLayerHandles: any[] = [];

	async function loadRefLayerConfigs() {
		try {
			const data: any = await fetchOrMockLocal('/ref/layers');
			// Only show layers that are enabled in the backend config
			refLayerConfigs = (data?.layers || []).filter((l: any) => l.enabled);
			const enabled: Record<string, boolean> = {};
			for (const l of refLayerConfigs) enabled[l.layer_id] = false;
			refLayerEnabled = enabled;
		} catch {}
	}

	async function loadRefLayerData() {
		if (!adapter) return;
		const bounds = adapter.getBounds();
		const zoom = Math.round(adapter?.getZoom?.() ?? currentZoom);
		if (!bounds) return;

		for (const [layerId, enabled] of Object.entries(refLayerEnabled)) {
			if (!enabled) continue;
			try {
				const data: any = await fetchOrMockLocal(
					`/ref/layers/${layerId}/points?north=${bounds.north}&south=${bounds.south}&east=${bounds.east}&west=${bounds.west}&zoom=${zoom}`
				);
				refLayerData = { ...refLayerData, [layerId]: data?.points || [] };
			} catch {
				refLayerData = { ...refLayerData, [layerId]: [] };
			}
		}
		renderRefLayerMarkers();
	}

	function renderRefLayerMarkers() {
		if (!adapter) return;
		for (const h of refLayerHandles) { try { adapter.remove(h); } catch {} }
		refLayerHandles = [];

		// Crash guard: total DOM markers across ALL enabled reference layers.
		const MAX_REF_MARKERS = 3000;

		for (const [layerId, points] of Object.entries(refLayerData)) {
			if (!refLayerEnabled[layerId]) continue;
			const config = refLayerConfigs.find((l: any) => l.layer_id === layerId);
			if (!config) continue;

			for (const pt of points) {
				if (refLayerHandles.length >= MAX_REF_MARKERS) break;
				if (pt.latitude == null || pt.longitude == null) continue;
				const h = adapter.addMarker({
					lat: pt.latitude, lon: pt.longitude,
					color: config.color || '#6b7280',
					size: 8,
					shape: 'circle',
					tooltip: pt.name || pt.id || layerId,
					onClick: () => {
						showMapPopup(pt.latitude, pt.longitude,
							buildRefLayerPopupHtml(
								pt.name || pt.id || layerId,
								config.display_name || layerId,
								{ ...pt.properties, category: pt.category },
								pt.latitude, pt.longitude
							)
						);
					},
				});
				refLayerHandles.push(h);
			}
		}
	}

	async function toggleRefLayer(layerId: string) {
		const wasEnabled = refLayerEnabled[layerId];
		refLayerEnabled = { ...refLayerEnabled, [layerId]: !wasEnabled };
		if (!wasEnabled) {
			// Turning ON — fetch this specific layer and render
			if (!adapter) return;
			const bounds = adapter.getBounds();
			const zoom = Math.round(adapter?.getZoom?.() ?? currentZoom);
			if (!bounds) return;
			try {
				const data: any = await fetchOrMockLocal(
					`/ref/layers/${layerId}/points?north=${bounds.north}&south=${bounds.south}&east=${bounds.east}&west=${bounds.west}&zoom=${zoom}`
				);
				const pts = data?.points || [];
				refLayerData = { ...refLayerData, [layerId]: pts };
				console.log(`[RefLayer] ${layerId}: ${pts.length} points loaded`);
			} catch (e) {
				console.error(`[RefLayer] ${layerId} fetch failed:`, e);
				refLayerData = { ...refLayerData, [layerId]: [] };
			}
			renderRefLayerMarkers();
		} else {
			refLayerData = { ...refLayerData, [layerId]: [] };
			renderRefLayerMarkers();
		}
	}

	// ─── Layer visibility ────────────────────────────────────────────────
	// All layers start OFF — user turns on what they need via Layers dropdown
	let layers = $state({
		signals: false, tracks: false, geofences: false,
		knowledge: false, annotations: false,
		countries: false, capitals: false, rivers: false, lakes: false,
		entity_assets: false,
	});

	// ─── Knowledge-node filters ──────────────────────────────────────────
	// Drives /knowledge/geo query params. Synced to URL so the view is
	// shareable: ?et=country,city&kw=7d  →  countries + cities, last 7 days.
	type KWindow = 'all' | '24h' | '7d' | '30d';
	const KNOWLEDGE_ENTITY_TYPES = [
		'country', 'region', 'city', 'place', 'facility',
		'person', 'organization', 'company',
	] as const;
	let knowledgeEntityTypes = $state<Set<string>>(new Set());
	let knowledgeTimeWindow = $state<KWindow>('all');
	let knowledgeFiltersOpen = $state(false);

	// Satellite debris toggle. Off by default — adds ~7k debris/rocket-body
	// objects from Cosmos 1408, Fengyun 1C, Iridium-Cosmos collision,
	// Briz-M breakup. Syncs to ?debris=1.
	let showSatelliteDebris = $state(false);

	function kwAfterTimestamp(w: KWindow): string | null {
		const now = Date.now();
		const hours = w === '24h' ? 24 : w === '7d' ? 24 * 7 : w === '30d' ? 24 * 30 : 0;
		if (!hours) return null;
		return new Date(now - hours * 3600_000).toISOString();
	}

	function syncKnowledgeFiltersToUrl() {
		if (typeof window === 'undefined') return;
		const usp = new URLSearchParams(window.location.search);
		if (knowledgeEntityTypes.size > 0) {
			usp.set('et', [...knowledgeEntityTypes].sort().join(','));
		} else {
			usp.delete('et');
		}
		if (knowledgeTimeWindow !== 'all') usp.set('kw', knowledgeTimeWindow);
		else usp.delete('kw');
		const qs = usp.toString();
		window.history.replaceState({}, '', `${window.location.pathname}${qs ? '?' + qs : ''}`);
	}

	function toggleKnowledgeEntityType(t: string) {
		const next = new Set(knowledgeEntityTypes);
		if (next.has(t)) next.delete(t); else next.add(t);
		knowledgeEntityTypes = next;
		syncKnowledgeFiltersToUrl();
		if (layers.knowledge) loadGeoNodes();
	}

	function setKnowledgeTimeWindow(w: KWindow) {
		knowledgeTimeWindow = w;
		syncKnowledgeFiltersToUrl();
		if (layers.knowledge) loadGeoNodes();
	}

	function clearKnowledgeFilters() {
		knowledgeEntityTypes = new Set();
		knowledgeTimeWindow = 'all';
		syncKnowledgeFiltersToUrl();
		if (layers.knowledge) loadGeoNodes();
	}

	function toggleSatelliteDebris() {
		showSatelliteDebris = !showSatelliteDebris;
		// Persist to URL so a shared link captures the choice
		if (typeof window !== 'undefined') {
			const usp = new URLSearchParams(window.location.search);
			if (showSatelliteDebris) usp.set('debris', '1'); else usp.delete('debris');
			const qs = usp.toString();
			window.history.replaceState({}, '', `${window.location.pathname}${qs ? '?' + qs : ''}`);
		}
		// Refresh the layer immediately if satellites are visible
		if (liveSources.satellites?.enabled) loadLiveData();
	}

	// ─── Weather overlay layers (raster tiles on the map) ────────────────
	let weatherOverlays = $state({
		goes_ir: false,      // GOES infrared satellite (Iowa Mesonet — CORS-friendly, no key)
		radar: false,        // NEXRAD composite radar (Iowa Mesonet — CORS-friendly, no key)
		clouds: false,       // GOES visible satellite (Iowa Mesonet)
		temperature: false,  // RainViewer radar (global, no key)
		daynight: false,     // Day/night terminator + sun/moon positions
	});

	// RainViewer dynamic radar path (fetched from their API)
	let rainviewerRadarPath = $state('');

	async function fetchRainviewerPath() {
		try {
			const resp = await fetch('https://api.rainviewer.com/public/weather-maps.json');
			if (resp.ok) {
				const data = await resp.json();
				const radarPast = data?.radar?.past;
				if (radarPast?.length) {
					rainviewerRadarPath = radarPast[radarPast.length - 1].path;
				}
			}
		} catch {}
	}

	function toggleWeatherOverlay(key: string) {
		const map = adapter?.getRawMap?.();
		if (!map) return;

		const newVal = !weatherOverlays[key as keyof typeof weatherOverlays];
		weatherOverlays = { ...weatherOverlays, [key]: newVal };

		const sourceId = `weather-${key}`;
		const layerId = `weather-${key}-layer`;

		if (!newVal) {
			try { if (map.getLayer(layerId)) map.removeLayer(layerId); } catch {}
			try { if (map.getSource(sourceId)) map.removeSource(sourceId); } catch {}
			return;
		}

		let tiles: string[] = [];
		let opacity = 0.6;
		let maxzoom = 12;

		if (key === 'goes_ir') {
			// Iowa Environmental Mesonet — GOES IR 4km composite (US coverage, CORS OK, no key)
			tiles = ['https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/goes-ir-4km-900913/{z}/{x}/{y}.png'];
			opacity = 0.7;
			maxzoom = 8;
		} else if (key === 'radar') {
			// Iowa Environmental Mesonet — NEXRAD base reflectivity composite (US coverage, CORS OK, no key)
			tiles = ['https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913/{z}/{x}/{y}.png'];
			opacity = 0.6;
			maxzoom = 12;
		} else if (key === 'clouds') {
			// GOES visible composite via Iowa Mesonet (Western Hemisphere, no key, CORS OK)
			// More reliable than RainViewer satellite IR which is often unavailable
			tiles = ['https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/goes-vis-1km-900913/{z}/{x}/{y}.png'];
			opacity = 0.6;
			maxzoom = 8;
		} else if (key === 'temperature') {
			// RainViewer global precipitation radar (no key, worldwide)
			if (!rainviewerRadarPath) {
				fetchRainviewerPath().then(() => {
					if (rainviewerRadarPath) {
						weatherOverlays = { ...weatherOverlays, temperature: false };
						toggleWeatherOverlay('temperature');
					}
				});
				return;
			}
			tiles = [`https://tilecache.rainviewer.com${rainviewerRadarPath}/256/{z}/{x}/{y}/2/1_1.png`];
			opacity = 0.6;
			maxzoom = 7;
		}

		// Day/night terminator is handled separately (GeoJSON, not raster tiles)
		if (key === 'daynight') {
			renderDayNight(map, newVal);
			return;
		}

		if (!tiles.length) return;

		try {
			if (!map.getSource(sourceId)) {
				map.addSource(sourceId, { type: 'raster', tiles, tileSize: 256, maxzoom });
			}
			if (!map.getLayer(layerId)) {
				map.addLayer({ id: layerId, type: 'raster', source: sourceId, paint: { 'raster-opacity': opacity } });
			}
		} catch (e) {
			console.warn(`Failed to add weather overlay ${key}:`, e);
		}
	}

	// ─── Sentinel-2 imagery overlays (ESA / Copernicus Data Space) ───────
	// Free & open Sentinel-2 optical imagery via a Sentinel Hub WMS config.
	// Tiles are proxied through /api/satellite/wms so the WMS instance ID stays
	// server-side (configured in Settings → Satellite Imagery); nothing here
	// carries a credential. The group is gated on the connection being set.
	let sentinelConfigured = $state(false);

	// key → { layer: SH layer id, opacity }
	const SENTINEL_LAYERS: Record<string, { layer: string; opacity: number }> = {
		true_color:   { layer: 'TRUE_COLOR',        opacity: 1.0 },
		false_color:  { layer: 'FALSE_COLOR',       opacity: 0.9 },
		urban:        { layer: 'FALSE_COLOR_URBAN', opacity: 0.9 },
		ndvi:         { layer: 'NDVI',              opacity: 0.85 },
		swir:         { layer: 'SWIR',              opacity: 0.9 },
		moisture:     { layer: 'MOISTURE_INDEX',    opacity: 0.85 },
	};

	let sentinelOverlays = $state({
		true_color: false, false_color: false, urban: false,
		ndvi: false, swir: false, moisture: false,
	});

	// Legend for each Sentinel-2 layer. `ramp` layers are continuous indices
	// (gradient bar + low/high labels); `cat` layers are false-colour composites
	// (what each colour means). Drives the on-map legend so values are legible.
	const SENTINEL_LEGENDS: Record<string, { label: string; kind: 'ramp' | 'cat' | 'note';
		gradient?: string; low?: string; high?: string; note?: string;
		items?: { c: string; t: string }[] }> = {
		true_color: { label: 'True Color', kind: 'note', note: 'Natural color — as seen from space' },
		false_color: { label: 'False Color (vegetation)', kind: 'cat', items: [
			{ c: '#e11d48', t: 'Healthy vegetation' }, { c: '#0b1a2b', t: 'Water' }, { c: '#9ca3af', t: 'Urban / bare' } ] },
		urban: { label: 'False Color (urban)', kind: 'cat', items: [
			{ c: '#c026d3', t: 'Built-up' }, { c: '#16a34a', t: 'Vegetation' }, { c: '#f9a8d4', t: 'Bare soil' } ] },
		ndvi: { label: 'NDVI — vegetation health', kind: 'ramp',
			gradient: 'linear-gradient(90deg,#8c6d3f,#d9c34a,#a7d94a,#2e7d32)', low: 'bare / water', high: 'dense · healthy' },
		swir: { label: 'SWIR — fire / geology', kind: 'cat', items: [
			{ c: '#f97316', t: 'Active fire / hot' }, { c: '#16a34a', t: 'Vegetation' }, { c: '#0b1a2b', t: 'Water' }, { c: '#d6bd91', t: 'Bare / urban' } ] },
		moisture: { label: 'Moisture Index', kind: 'ramp',
			gradient: 'linear-gradient(90deg,#8c5a2b,#f5f5f4,#2563eb)', low: 'dry', high: 'wet' },
	};
	const activeSentinelLegends = $derived(
		Object.keys(SENTINEL_LEGENDS).filter(k => sentinelOverlays[k as keyof typeof sentinelOverlays])
	);

	// Legends for the weather raster overlays (same on-map panel as Sentinel).
	const WEATHER_LEGENDS: Record<string, { label: string; kind: 'ramp' | 'cat';
		gradient?: string; low?: string; high?: string; items?: { c: string; t: string }[] }> = {
		goes_ir: { label: 'GOES Infrared — cloud tops', kind: 'cat', items: [
			{ c: '#222222', t: 'Clear' }, { c: '#888888', t: 'Low cloud' }, { c: '#eeeeee', t: 'High cloud' }, { c: '#ff4444', t: 'Storms' } ] },
		radar: { label: 'NEXRAD — precip intensity', kind: 'ramp',
			gradient: 'linear-gradient(90deg,#00ee00,#eeee00,#ff8800,#ff0000,#ff00ff)', low: 'light', high: 'extreme' },
		clouds: { label: 'GOES Visible — clouds', kind: 'cat', items: [
			{ c: '#111111', t: 'Clear' }, { c: '#aaaaaa', t: 'Thin cloud' }, { c: '#ffffff', t: 'Dense cloud' } ] },
		temperature: { label: 'Precipitation (global)', kind: 'ramp',
			gradient: 'linear-gradient(90deg,#0000ff,#00ccff,#00ff00,#ffff00,#ff0000)', low: 'drizzle', high: 'extreme' },
	};
	const activeWeatherLegends = $derived(
		Object.keys(WEATHER_LEGENDS).filter(k => weatherOverlays[k as keyof typeof weatherOverlays])
	);
	// Combined list of active raster-layer legends (Sentinel + weather) for the
	// on-map legend panel.
	const activeLegends = $derived<any[]>([
		...activeSentinelLegends.map(k => SENTINEL_LEGENDS[k]),
		...activeWeatherLegends.map(k => WEATHER_LEGENDS[k]),
	]);

	async function loadSentinelStatus() {
		try {
			const r = await fetch('/api/satellite/status');
			if (r.ok) sentinelConfigured = !!(await r.json()).configured;
		} catch {}
	}

	// Rolling recent window (last ~90 days) so imagery is always fresh;
	// MAXCC caps cloud cover, WMS returns the most-recent clear acquisition.
	function sentinelTimeRange(): string {
		const fmt = (d: Date) => d.toISOString().slice(0, 10);
		return `${fmt(new Date(Date.now() - 90 * 864e5))}/${fmt(new Date())}`;
	}

	function toggleSentinelOverlay(key: string) {
		const map = adapter?.getRawMap?.();
		if (!map) return;

		const cfg = SENTINEL_LAYERS[key];
		if (!cfg) return;

		const newVal = !sentinelOverlays[key as keyof typeof sentinelOverlays];
		sentinelOverlays = { ...sentinelOverlays, [key]: newVal };

		const sourceId = `sentinel-${key}`;
		const layerId = `sentinel-${key}-layer`;

		if (!newVal) {
			try { if (map.getLayer(layerId)) map.removeLayer(layerId); } catch {}
			try { if (map.getSource(sourceId)) map.removeSource(sourceId); } catch {}
			return;
		}

		// Backend WMS proxy — global coverage. {bbox-epsg-3857} is filled
		// per-tile by MapLibre; the server injects the instance ID and caps cloud.
		const tiles = [
			`/api/satellite/wms?layer=${encodeURIComponent(cfg.layer)}` +
			`&bbox={bbox-epsg-3857}&width=512&height=512&time=${sentinelTimeRange()}&maxcc=20`,
		];

		// Insert imagery just above the Esri basemap so nodes/markers stay on top.
		const layersArr = (map.getStyle()?.layers || []) as any[];
		const baseIdx = layersArr.findIndex((l) => l.id === 'satellite-tiles');
		const beforeId = baseIdx >= 0 ? layersArr[baseIdx + 1]?.id : undefined;

		try {
			if (!map.getSource(sourceId)) {
				map.addSource(sourceId, { type: 'raster', tiles, tileSize: 512, maxzoom: 16 });
			}
			if (!map.getLayer(layerId)) {
				map.addLayer(
					{ id: layerId, type: 'raster', source: sourceId, paint: { 'raster-opacity': cfg.opacity } },
					beforeId,
				);
			}
		} catch (e) {
			console.warn(`Failed to add Sentinel overlay ${key}:`, e);
		}
	}

	// ─── Day/night terminator + sun/moon markers ─────────────────────────
	let celestialHandles: any[] = [];
	let celestialInterval: ReturnType<typeof setInterval> | null = null;

	async function renderDayNight(map: any, enabled: boolean) {
		// Clean up old
		for (const h of celestialHandles) { try { adapter?.remove(h); } catch {} }
		celestialHandles = [];
		if (celestialInterval) { clearInterval(celestialInterval); celestialInterval = null; }

		try { if (map.getLayer('night-fill')) map.removeLayer('night-fill'); } catch {}
		try { if (map.getLayer('terminator-line')) map.removeLayer('terminator-line'); } catch {}
		try { if (map.getSource('terminator')) map.removeSource('terminator'); } catch {}

		if (!enabled) return;

		const { sunPosition, moonPosition, terminatorGeoJSON, moonPhase } = await import('$lib/components/map/live/celestial');
		const { sunMarkerSvg, moonMarkerSvg } = await import('$lib/components/map/live/icons');

		function update() {
			const now = new Date();
			const terminator = terminatorGeoJSON(now);
			const sun = sunPosition(now);
			const moon = moonPosition(now);
			const phase = moonPhase(now);

			// Update terminator GeoJSON
			const src = map.getSource('terminator');
			if (src) {
				src.setData({ type: 'FeatureCollection', features: [terminator] });
			} else {
				map.addSource('terminator', {
					type: 'geojson',
					data: { type: 'FeatureCollection', features: [terminator] },
				});
				map.addLayer({
					id: 'night-fill',
					type: 'fill',
					source: 'terminator',
					paint: { 'fill-color': '#000011', 'fill-opacity': 0.35 },
				});
				map.addLayer({
					id: 'terminator-line',
					type: 'line',
					source: 'terminator',
					paint: { 'line-color': '#f59e0b', 'line-width': 1.5, 'line-opacity': 0.6 },
				});
			}

			// Remove old celestial markers
			for (const h of celestialHandles) { try { adapter?.remove(h); } catch {} }
			celestialHandles = [];

			if (!adapter) return;

			// Sun marker
			const sunH = adapter.addMarker({
				lat: sun.latitude, lon: sun.longitude,
				html: `<div title="Sun — subsolar point" style="cursor:pointer">${sunMarkerSvg(28)}</div>`,
				tooltip: `Sun (${sun.latitude.toFixed(1)}°, ${sun.longitude.toFixed(1)}°)`,
				onClick: () => {
					showMapPopup(sun.latitude, sun.longitude, buildLivePopupHtml(
						'Sun — Sub-Solar Point', 'Celestial',
						{ type: 'Sun', description: 'Point on Earth where the Sun is directly overhead' },
						sun.latitude, sun.longitude
					));
				},
			});
			celestialHandles.push(sunH);

			// Moon marker
			const moonH = adapter.addMarker({
				lat: moon.latitude, lon: moon.longitude,
				html: `<div title="Moon — ${phase.name} (${Math.round(phase.illumination * 100)}%)" style="cursor:pointer">${moonMarkerSvg(phase.illumination, 22)}</div>`,
				tooltip: `Moon — ${phase.name} (${Math.round(phase.illumination * 100)}%)`,
				onClick: () => {
					showMapPopup(moon.latitude, moon.longitude, buildLivePopupHtml(
						`Moon — ${phase.name}`, 'Celestial',
						{ type: 'Moon', phase: phase.name, illumination: `${Math.round(phase.illumination * 100)}%` },
						moon.latitude, moon.longitude
					));
				},
			});
			celestialHandles.push(moonH);
		}

		update();
		// Update every 60 seconds (sun moves ~0.25°/min)
		celestialInterval = setInterval(update, 60000);
	}

	let liveSources = $state<Record<string, { enabled: boolean; count: number; status?: 'loading' | 'ok' | 'error'; error?: string }>>({
		adsb: { enabled: false, count: 0 },
		ais: { enabled: false, count: 0 },
		ports: { enabled: false, count: 0 },
		airports: { enabled: false, count: 0 },
		cameras: { enabled: false, count: 0 },
		osm_cameras: { enabled: false, count: 0 },
		seismic: { enabled: false, count: 0 },
		weather: { enabled: false, count: 0 },
		satellites: { enabled: false, count: 0 },
		fireballs: { enabled: false, count: 0 },
		space_weather: { enabled: false, count: 0 },
		gdelt: { enabled: false, count: 0 },
	});

	// ─── Filters ─────────────────────────────────────────────────────────
	let signalTypeFilter = $state<Set<string>>(new Set());
	let severityFilter = $state<Set<string>>(new Set());
	let deviceFilter = $state('');
	let activeCaseIds = $state<Set<string>>(new Set());
	let caseSignals = $state<Map<string, any[]>>(new Map());

	const severities = ['critical', 'high', 'medium', 'low', 'informational'];

	const availableSignalTypes = $derived<string[]>(
		[...new Set(signals.map(s => {
			const t = s.properties?.signal_type || '';
			return t.split('.').slice(0, 3).join('.');
		}).filter(Boolean))].sort()
	);

	const availableDevices = $derived<string[]>(
		[...new Set(signals.map(s => s.properties?.device_id).filter(Boolean))].sort()
	);

	// ─── Zoom-based filtering ────────────────────────────────────────────
	// At low zoom (zoomed out) → show fewer, larger markers (clusters)
	// At high zoom (zoomed in) → show everything
	const zoomMaxMarkers = $derived.by(() => {
		if (currentZoom < 5) return 50;
		if (currentZoom < 8) return 200;
		if (currentZoom < 11) return 500;
		return 5000; // zoomed in enough, show all
	});

	const filteredSignals = $derived.by(() => {
		let result = signals.filter(s => {
			const props = s.properties || {};
			if (signalTypeFilter.size > 0 && !signalTypeFilter.has(props.signal_type?.split('.').slice(0, 3).join('.'))) return false;
			if (severityFilter.size > 0 && !severityFilter.has(props.severity)) return false;
			if (deviceFilter && props.device_id !== deviceFilter) return false;
			return true;
		});
		// Zoom-based limit: prioritize higher severity and newer signals
		if (result.length > zoomMaxMarkers) {
			result = result.slice(0, zoomMaxMarkers);
		}
		return result;
	});

	const filteredKnowledge = $derived.by(() => {
		if (knowledgeEntityTypes.size <= 1) return knowledgeNodes;
		// Multi-select: fetch was unfiltered, narrow client-side.
		return knowledgeNodes.filter(n =>
			knowledgeEntityTypes.has(String(n.properties?.entity_type || '').toLowerCase())
		);
	});
	const activeFilterCount = $derived(signalTypeFilter.size + severityFilter.size + (deviceFilter ? 1 : 0));

	// ─── Domain colors ───────────────────────────────────────────────────
	const domainColors: Record<string, string> = {
		geospatial: '#3b82f6', security: '#ef4444', communications: '#a855f7',
		personal: '#22c55e', financial: '#f59e0b', environmental: '#06b6d4',
		infrastructure: '#f97316', vehicle: '#6366f1', intelligence: '#ec4899',
	};
	const caseColors = ['#f59e0b', '#ec4899', '#8b5cf6', '#14b8a6', '#f97316', '#06b6d4'];

	// Canonical-entity colors for the knowledge-node diamond markers. Stays
	// the same color across the page (legend swatches, popups) so users build
	// a consistent visual vocabulary.
	const entityTypeColors: Record<string, string> = {
		country: '#10b981',         // emerald
		region: '#10b981',
		city: '#34d399',
		place: '#06b6d4',           // cyan
		facility: '#06b6d4',
		address: '#06b6d4',
		person: '#22d3ee',          // sky
		organization: '#f59e0b',    // amber
		company: '#f59e0b',
		government_agency: '#f97316',
		vessel: '#6366f1',          // indigo
		aircraft: '#6366f1',
		vehicle: '#6366f1',
	};
	function colorForEntityType(t: string | null | undefined): string {
		return entityTypeColors[(t || '').toLowerCase()] || '#22c55e';
	}

	// ─── Data loaders ───────────────────────────────────────────────────
	async function loadGeoSignals() {
		loading = true;
		try {
			const params: Record<string, any> = {};
			if (adapter) {
				const b = adapter.getBounds();
				params.west = b.west; params.south = b.south; params.east = b.east; params.north = b.north;
			}
			if (timeFilter.after) params.after = timeFilter.after;
			if (timeFilter.before) params.before = timeFilter.before;
			const data = await api.geoSignals(Object.keys(params).length ? params : undefined);
			signals = data?.features || [];
		} catch { signals = []; }
		loading = false;
	}

	// Bbox-aware knowledge node fetch. Pulls every canonical entity that the
	// pipeline has geocoded inside the current viewport, plus a zoom-aware
	// limit. Bound directly to the knowledge layer toggle + moveend.
	// Filters: knowledgeEntityTypes (multi-select) + knowledgeTimeWindow.
	async function loadGeoNodes() {
		try {
			const params: Record<string, any> = { limit: 1000 };
			if (adapter) {
				const b = adapter.getBounds();
				params.west = b.west; params.south = b.south; params.east = b.east; params.north = b.north;
			}
			// Time window → ISO timestamp on the after= param.
			const after = kwAfterTimestamp(knowledgeTimeWindow);
			if (after) params.after = after;
			// Single-type filter goes to the API. For multi-select we ask for
			// the broadest match and filter client-side (the endpoint accepts
			// one entity_type — fanning out to N requests isn't worth the
			// hop overhead when filteredKnowledge does it cheaply in JS).
			if (knowledgeEntityTypes.size === 1) {
				params.entity_type = [...knowledgeEntityTypes][0];
			}
			const data: any = await api.geoNodes(params);
			knowledgeNodes = data?.features || [];
		} catch { knowledgeNodes = []; }
	}

	async function loadSupportingData() {
		// Load metadata for scope selector + layer dropdowns
		try { const d = await api.projects(); projects = d?.projects || []; } catch {}
		try { const d = await api.cases(); cases = d?.cases || []; } catch {}
		try { const d: any = await api.watchTopics(); watchTopics = d?.topics || []; } catch {}
		try { const d: any = await api.contacts(); scopeContacts = (d?.contacts || []).filter((c: any) => c.status === 'watchlist' || c.contact_type === 'country'); } catch {}
		try { const d: any = await api.assets(); scopeAssets = (d?.assets || []).filter((a: any) => a.status === 'seized' || a.status === 'missing'); } catch {}
	}

	// Load layer-specific data on demand
	async function loadLayerData(layerKey: string) {
		switch (layerKey) {
			case 'signals':
				if (signals.length === 0) await loadGeoSignals();
				break;
			case 'knowledge':
				if (knowledgeNodes.length === 0) await loadGeoNodes();
				break;
			case 'tracks':
				if (tracks.length === 0) {
					try { const d = await api.tracks(); tracks = d?.tracks || []; } catch {}
				}
				break;
			case 'geofences':
				if (geofences.length === 0) {
					try { const d = await api.geofences(); geofences = d?.geofences || []; } catch {}
				}
				break;
			case 'annotations':
				if (annotations.length === 0) {
					try { const d = await api.annotations(); annotations = d?.features || []; } catch {}
				}
				break;
		}
		renderMarkers();
	}

	async function loadLiveData() {
		// Only fetch if at least one live source is enabled
		const anyEnabled = Object.values(liveSources).some(s => s.enabled);
		if (!anyEnabled) return;

		const bounds = adapter ? adapter.getBounds() : null;
		if (!bounds) return;
		// Always read live zoom from adapter — currentZoom state may be stale (initializes at 5)
		const zoom = Math.round(adapter?.getZoom?.() ?? currentZoom);

		for (const [source, state] of Object.entries(liveSources)) {
			if (!state.enabled) continue;
			// Surface a loading state so the Layers menu shows the layer is working.
			liveSources = { ...liveSources, [source]: { ...state, status: 'loading' } };
			try {
				let items: any[] = [];
				if (source === 'cameras' && bounds) {
					const data = await api.cameras({ ...bounds, zoom });
					items = (data?.cameras || []).map((c: any) => ({
						key: c.camera_id, label: c.name,
						latitude: c.latitude, longitude: c.longitude,
						image_url: c.image_url, source_provider: c.provider,
						payload: c,
					}));
				} else if (source === 'ports' && bounds) {
					const data: any = await fetchOrMockLocal(`/ports?north=${bounds.north}&south=${bounds.south}&east=${bounds.east}&west=${bounds.west}&zoom=${zoom}`);
					items = (data?.ports || []).map((p: any) => ({
						key: p.port_id, label: p.name,
						latitude: p.latitude, longitude: p.longitude,
						country: p.country, harbor_size: p.harbor_size,
						payload: p,
					}));
				} else if (source === 'airports' && bounds) {
					const data: any = await fetchOrMockLocal(`/airports?north=${bounds.north}&south=${bounds.south}&east=${bounds.east}&west=${bounds.west}&zoom=${zoom}`);
					items = (data?.airports || []).map((a: any) => ({
						key: a.airport_id, label: a.name || a.airport_id,
						latitude: a.latitude, longitude: a.longitude,
						icao: a.icao, city: a.city, type: a.type,
						elevation_ft: a.elevation_ft, military: a.military,
						scheduled_service: a.scheduled_service,
						airport_id: a.airport_id, name: a.name,
						payload: a,
					}));
				} else if (source === 'osm_cameras' && bounds) {
					// Scale Overpass limit by zoom — sparse at low zoom, dense at high
					const osmLimit = zoom < 10 ? 50 : zoom < 13 ? 200 : 500;
					try {
						const bbox = `${bounds.south},${bounds.west},${bounds.north},${bounds.east}`;
						const query = `[out:json];node["man_made"="surveillance"](${bbox});out ${osmLimit};`;
						const resp = await fetch('https://overpass-api.de/api/interpreter', {
							method: 'POST', body: `data=${encodeURIComponent(query)}`,
							headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
						});
						if (resp.ok) {
							const d = await resp.json();
							items = (d.elements || []).map((e: any) => ({
								key: `osm_${e.id}`,
								label: e.tags?.description || e.tags?.['camera:type'] || 'Camera',
								latitude: e.lat, longitude: e.lon,
								source_provider: 'osm',
								payload: e.tags || {},
							}));
						}
					} catch {}
				} else if (source === 'satellites' && bounds) {
					// Fetch TLEs from local DB, propagate positions client-side
					const { propagateBatch } = await import('$lib/components/map/live/satellite-propagator');
					const debrisParam = showSatelliteDebris ? '&include_debris=true' : '';
					const data: any = await fetchOrMockLocal(`/satellites?north=${bounds.north}&south=${bounds.south}&east=${bounds.east}&west=${bounds.west}&zoom=${zoom}${debrisParam}`);
					const sats = data?.satellites || [];
					const now = new Date();
					// propagateBatch is async — must await, else `positions` is a
					// Promise and every position reads back undefined (0 satellites).
					const positions = await propagateBatch(sats, now);
					items = sats.map((sat: any, i: number) => {
						const pos = positions[i];
						if (!pos) return null;
						return {
							key: `sat_${sat.satellite_id}`,
							label: sat.name,
							latitude: pos.latitude,
							longitude: pos.longitude,
							altitude_km: pos.altitude_km,
							velocity_kms: pos.velocity_kms,
							constellation: sat.constellation,
							payload: sat,
						};
					}).filter(Boolean);
				} else if (source === 'fireballs' && bounds) {
					const data: any = await fetchOrMockLocal(`/fireballs?north=${bounds.north}&south=${bounds.south}&east=${bounds.east}&west=${bounds.west}`);
					items = (data?.fireballs || []).map((f: any) => ({
						key: `fb_${f.fireball_id}`,
						label: f.date ? new Date(f.date).toLocaleDateString() + ' Fireball' : 'Fireball',
						latitude: f.latitude, longitude: f.longitude,
						impact_energy_kt: f.impact_energy_kt,
						payload: f,
					}));
				} else if (source === 'space_weather') {
					// Non-spatial — fetch current events for status display
					const data: any = await fetchOrMockLocal('/space-weather/current');
					items = (data?.events || []).map((e: any) => ({
						key: e.event_id,
						label: `${e.event_type}: ${e.class_type || 'Unknown'}`,
						latitude: 0, longitude: 0, // non-spatial
						payload: e,
					}));
				} else if (source === 'gdelt' && bounds) {
					// Geo-coded news mentions from GDELT GKG (populated by the
					// gdelt_gkg worker every 5 min). bbox-narrowed server-side.
					const url = `/gdelt/mentions?west=${bounds.west}&south=${bounds.south}`
						+ `&east=${bounds.east}&north=${bounds.north}`
						+ `&hours=24&limit=1500`;
					const data: any = await fetchOrMockLocal(url);
					items = (data?.features || []).map((f: any) => ({
						key: `gdelt_${f.properties?.mention_id}`,
						label: f.properties?.name || 'Mention',
						latitude:  f.geometry?.coordinates?.[1],
						longitude: f.geometry?.coordinates?.[0],
						tone: f.properties?.tone,
						payload: f.properties,
					})).filter((m: any) => m.latitude != null && m.longitude != null);
				} else if (bounds && (source === 'adsb' || source === 'ais')) {
					// Viewport-driven: fetch from external API for the visible area
					const data = await api.liveViewport(source, bounds);
					items = data?.items || [];
				} else {
					// Static sources (weather, seismic): use cached data
					const data = await api.liveQuery(source, { limit: 500 });
					items = data?.items || [];
				}
				liveData = { ...liveData, [source]: items };
				liveSources = { ...liveSources, [source]: { ...state, count: items.length, status: 'ok', error: undefined } };
			} catch (e: any) {
				liveData = { ...liveData, [source]: [] };
				liveSources = { ...liveSources, [source]: { ...state, count: 0, status: 'error', error: e?.message || 'fetch failed' } };
			}
		}
		renderLiveMarkers();
	}

	async function loadTrail(source: string, key: string) {
		// Clear old trail
		for (const h of liveTrailHandles) { try { adapter?.remove(h); } catch {} }
		liveTrailHandles = [];

		if (activeTrailKey === key) {
			activeTrailKey = '';
			return;
		}
		activeTrailKey = key;

		try {
			const data = await api.liveTrail(source, key, 4);
			if (data?.track?.geometry?.coordinates?.length >= 2 && adapter) {
				const coords: [number, number][] = data.track.geometry.coordinates.map(
					(c: number[]) => [c[1], c[0]]
				);
				const h = adapter.addPolyline({
					coords,
					color: LIVE_SOURCES[source]?.trailColor || '#60a5fa',
					weight: 3,
					opacity: 0.6,
					dashArray: '8,6',
					tooltip: `${key} trail (${data.count} points)`,
				});
				liveTrailHandles.push(h);
			}
		} catch {}
	}

	function loadAllData() {
		// Only load metadata for dropdowns (projects, cases, devices)
		// Signal data and live feeds load on-demand when layers are toggled ON
		loadSupportingData();
		loadRefLayerConfigs();
	}


	function startLivePolling() {
		if (liveInterval) return;
		loadLiveData();
		liveInterval = setInterval(loadLiveData, 30000);
	}

	function stopLivePolling() {
		if (liveInterval) { clearInterval(liveInterval); liveInterval = null; }
	}

	// ─── Marker rendering ───────────────────────────────────────────────
	function renderMarkers() {
		if (!adapter) return;
		for (const h of markerHandles) { try { adapter.remove(h); } catch {} }
		markerHandles = [];

		// Permanent signal markers
		if (layers.signals) {
			for (const sig of filteredSignals) {
				const lat = sig.properties?.latitude ?? sig.latitude;
				const lon = sig.properties?.longitude ?? sig.longitude;
				if (lat == null || lon == null) continue;

				const sigType = sig.properties?.signal_type ?? '';
				const domain = sigType.split('.')[1] || '';

				const h = adapter.addMarker({
					lat, lon,
					color: domainColors[domain] || '#6b7280',
					size: 10,
					shape: 'circle',
					tooltip: sigType.split('.').pop()?.replace(/_/g, ' '),
					onClick: () => {
						const sl = sigType.split('.').pop()?.replace(/_/g, ' ') || 'Signal';
						showMapPopup(lat, lon, buildLivePopupHtml(
							sl, sig.properties?.device_id || 'Signal',
							{ type: sigType, severity: sig.properties?.severity, timestamp: sig.properties?.timestamp, device: sig.properties?.device_id },
							lat, lon
						));
					},
				});
				markerHandles.push(h);
			}
		}

		// Knowledge markers
		if (layers.knowledge) {
			for (const node of filteredKnowledge) {
				const lat = node.properties?.latitude; const lon = node.properties?.longitude;
				if (lat == null || lon == null) continue;
				const np = node.properties || {};
				// Color by canonical entity_type so the map reads by type at a glance.
				// Falls back to green for non-entity nodes (raw observations).
				const color = colorForEntityType(np.entity_type);
				const h = adapter.addMarker({
					lat, lon, color, size: 18, shape: 'diamond',
					tooltip: np.entity_name || np.title || 'Node',
					onClick: () => {
						// Prefer the structured entity_id; fall back to parsing node_id
						// (some legacy rows may not have master_entity_id set).
						const eid = np.entity_id
							?? (typeof np.node_id === 'string'
								? Number(np.node_id.match(/^entity:(\d+)$/)?.[1])
								: NaN);
						if (Number.isFinite(eid)) {
							goto(`/entity/${eid}`);
							return;
						}
						showMapPopup(lat, lon, buildRefLayerPopupHtml(
							np.title || 'Knowledge Node', np.epistemic_type || 'node',
							{ type: np.epistemic_type, description: np.description, concepts: np.concepts?.join(', ') },
							lat, lon
						));
					},
				});
				markerHandles.push(h);
			}
		}

		// Case overlay markers
		let cIdx = 0;
		for (const [caseId, entries] of caseSignals) {
			if (!activeCaseIds.has(caseId)) continue;
			const color = caseColors[cIdx % caseColors.length];
			const caseObj = cases.find(c => c.case_id === caseId);
			for (const entry of entries) {
				if (entry.latitude == null || entry.longitude == null) continue;
				const h = adapter.addMarker({
					lat: entry.latitude, lon: entry.longitude,
					color, size: 14, shape: 'square',
					tooltip: `${caseObj?.title || caseId}: ${entry.title || 'event'}`,
					onClick: () => {
						showMapPopup(entry.latitude, entry.longitude, buildLivePopupHtml(
							entry.title || 'Case Event', caseObj?.title || caseId,
							entry, entry.latitude, entry.longitude
						));
					},
				});
				markerHandles.push(h);
			}
			cIdx++;
		}
	}

	function renderLiveMarkers() {
		if (!adapter) return;
		for (const h of liveHandles) { try { adapter.remove(h); } catch {} }
		liveHandles = [];

		for (const [source, items] of Object.entries(liveData)) {
			if (!liveSources[source]?.enabled) continue;
			const config = LIVE_SOURCES[source];
			if (!config) continue;

			// Zoom-based limit — cameras are static so allow more; aircraft/vessels update rapidly so limit more
			const isStaticSource = source === 'cameras' || source === 'osm_cameras' || source === 'ports' || source === 'airports' || source === 'seismic' || source === 'weather';
			const maxLive = isStaticSource
				? (currentZoom < 6 ? 500 : currentZoom < 9 ? 2000 : 5000)
				: (currentZoom < 8 ? 100 : currentZoom < 11 ? 300 : 1000);
			const displayItems = items.slice(0, maxLive);

			for (const item of displayItems) {
				const lat = item.latitude; const lon = item.longitude;
				if (lat == null || lon == null) continue;

				const label = (item.label || item.key || '').trim();
				const heading = config.rotationField ? (item[config.rotationField] ?? item.heading) : null;
				const iconSvg = config.iconFn(item);
				const rotation = heading != null ? `transform: rotate(${heading}deg);` : '';

				const h = adapter.addMarker({
					lat, lon,
					// Satellites live above the surface — keep them fully visible over
					// the globe's limb instead of fading out as ground markers do.
					occludedOpacity: source === 'satellites' ? 1 : undefined,
					html: `<div style="cursor:pointer;${rotation}" title="${label}">${iconSvg}</div>`,
					tooltip: label + (item.altitude != null ? ` ${Math.round(item.altitude)}ft` : '') + (item.speed != null ? ` ${Math.round(item.speed)}kt` : ''),
					onClick: () => {
						showMapPopup(lat, lon, buildLivePopupHtml(
							label, source, { ...item.payload, ...item }, lat, lon
						));
					},
				});
				liveHandles.push(h);
			}
		}
	}

	// ─── Callbacks ──────────────────────────────────────────────────────
	function getDeviceName(id: string): string {
		return deviceNames.get(id) || id.replace('mobile_', '').slice(0, 8);
	}

	function toggleLayer(key: string) {
		const newVal = !layers[key as keyof typeof layers];
		layers = { ...layers, [key]: newVal };
		if (newVal) {
			loadLayerData(key);
		} else {
			renderMarkers();
		}
	}

	function toggleLiveSource(source: string) {
		const s = liveSources[source];
		const nowEnabled = !s.enabled;
		liveSources = { ...liveSources, [source]: { ...s, enabled: nowEnabled } };
		if (nowEnabled) {
			loadLiveData(); // just enabled, fetch now
			if (!liveInterval) startLivePolling(); // start polling if not running
		} else {
			renderLiveMarkers(); // disabled, re-render to remove
			// Stop polling if no live sources are enabled
			const anyEnabled = Object.values(liveSources).some(ls => ls.enabled);
			if (!anyEnabled) stopLivePolling();
		}
	}

	function toggleSignalType(type: string) {
		const next = new Set(signalTypeFilter);
		if (next.has(type)) next.delete(type); else next.add(type);
		signalTypeFilter = next;
	}

	function toggleSeverity(sev: string) {
		const next = new Set(severityFilter);
		if (next.has(sev)) next.delete(sev); else next.add(sev);
		severityFilter = next;
	}

	function toggleCase(caseId: string) {
		const next = new Set(activeCaseIds);
		if (next.has(caseId)) {
			next.delete(caseId);
			const m = new Map(caseSignals); m.delete(caseId); caseSignals = m;
		} else {
			next.add(caseId);
			api.caseTimeline(caseId).then(d => {
				const geo = (d?.entries || []).filter((e: any) => e.latitude != null && e.longitude != null);
				caseSignals = new Map(caseSignals).set(caseId, geo);
			}).catch(() => { caseSignals = new Map(caseSignals).set(caseId, []); });
		}
		activeCaseIds = next;
	}

	function clearFilters() {
		signalTypeFilter = new Set();
		severityFilter = new Set();
		deviceFilter = '';
	}

	function handleShowTrail() {
		if (selectedItem && selectedLiveSource) {
			loadTrail(selectedLiveSource, selectedItem.key);
		}
	}

	function handleAddToCase() {
		// Open chat and send as quick message
		chat.open();
		const label = selectedItem?.label || selectedItem?.key || selectedItem?.properties?.signal_id || '';
		chat.sendMessage(`Add ${label} to a case`);
	}

	function handleWatch() {
		chat.open();
		const label = selectedItem?.label || selectedItem?.key || '';
		chat.sendMessage(`Watch ${label} — add to case watchlist`);
	}

	// ─── Map lifecycle ──────────────────────────────────────────────────
	let unsubMapCommands: (() => void) | null = null;
	let cleanupZoomPrevention: (() => void) | null = null;

	// Debounce live data reload on pan/zoom
	let liveReloadTimer: ReturnType<typeof setTimeout> | null = null;
	function debouncedLiveReload() {
		if (liveReloadTimer) clearTimeout(liveReloadTimer);
		liveReloadTimer = setTimeout(() => { loadLiveData(); }, 1500); // 1.5s after stop moving
	}

	function onMapReady(a: MapAdapter) {
		adapter = a;
		currentZoom = a.getZoom?.() ?? currentZoom;

		// Fly to coordinates if passed via URL params (e.g. from graph page)
		const params = $page.url.searchParams;
		const lat = parseFloat(params.get('lat') || '');
		const lng = parseFloat(params.get('lng') || '');
		const zoom = parseInt(params.get('zoom') || '');
		if (!isNaN(lat) && !isNaN(lng)) {
			setTimeout(() => a.flyTo(lat, lng, isNaN(zoom) ? 14 : zoom), 500);
		}
		a.on('moveend', () => {
			currentZoom = a.getZoom?.() ?? currentZoom;
			if (layers.signals) loadGeoSignals();
			if (layers.knowledge) loadGeoNodes();
			renderMarkers();
			renderLiveMarkers();
			// Only reload live data if any live source is enabled
			const anyLiveEnabled = Object.values(liveSources).some(ls => ls.enabled);
			if (anyLiveEnabled) debouncedLiveReload();
			// Reload ref layers if any are enabled
			const anyRefEnabled = Object.values(refLayerEnabled).some(v => v);
			if (anyRefEnabled) {
				if (liveReloadTimer) clearTimeout(liveReloadTimer);
				liveReloadTimer = setTimeout(() => { loadRefLayerData(); }, 1500);
			}
		});
		a.on('zoom', () => {
			currentZoom = a.getZoom?.() ?? currentZoom;
		});
		a.on('contextmenu', (e: any) => {
			e.originalEvent?.preventDefault();
			addObservationAt = { lat: e.lat, lng: e.lon };
			skyViewAt = { lat: e.lat, lon: e.lon };
		});
		loadAllData();
	}

	// Re-render when filters change
	$effect(() => {
		if (adapter && (filteredSignals || filteredKnowledge || caseSignals || activeCaseIds)) {
			renderMarkers();
		}
	});

	onMount(() => {
		// Is the Sentinel-2 imagery connection configured? Gates the overlay group.
		loadSentinelStatus();

		// Prevent browser zoom on the entire map page
		// This catches Ctrl+wheel and trackpad pinch regardless of which element has focus
		function preventBrowserZoom(e: WheelEvent) {
			if (e.ctrlKey || e.metaKey) {
				e.preventDefault();
			}
		}
		document.addEventListener('wheel', preventBrowserZoom, { passive: false });

		// Also prevent keyboard zoom (Ctrl+Plus/Minus)
		function preventKeyboardZoom(e: KeyboardEvent) {
			if ((e.ctrlKey || e.metaKey) && (e.key === '+' || e.key === '-' || e.key === '=' || e.key === '0')) {
				e.preventDefault();
			}
		}
		document.addEventListener('keydown', preventKeyboardZoom);

		cleanupZoomPrevention = () => {
			document.removeEventListener('wheel', preventBrowserZoom);
			document.removeEventListener('keydown', preventKeyboardZoom);
		};

		// Read knowledge filters from URL on first load so a shared link
		// (e.g. /map?et=country,city&kw=7d) lands on the right view.
		const usp = $page.url.searchParams;
		const etParam = usp.get('et');
		if (etParam) {
			knowledgeEntityTypes = new Set(
				etParam.split(',').map(s => s.trim().toLowerCase()).filter(Boolean)
			);
		}
		const kwParam = usp.get('kw');
		if (kwParam && ['all', '24h', '7d', '30d'].includes(kwParam)) {
			knowledgeTimeWindow = kwParam as KWindow;
		}
		// Debris toggle from URL
		if (usp.get('debris') === '1') showSatelliteDebris = true;

		loadAllData();
		unsubMapCommands = onMapCommand((cmd) => {
			if (!adapter) return;
			if (cmd.type === 'navigate') adapter.flyTo(cmd.lat, cmd.lon, cmd.zoom || 12);
			else if (cmd.type === 'highlight') {
				adapter.addPolygon({
					coords: [[cmd.south, cmd.west], [cmd.north, cmd.west], [cmd.north, cmd.east], [cmd.south, cmd.east]],
					color: '#f59e0b', fillColor: '#f59e0b', fillOpacity: 0.12, weight: 2,
				});
				adapter.fitBounds({ west: cmd.west, south: cmd.south, east: cmd.east, north: cmd.north }, 40);
			}
		});
	});

	onDestroy(() => {
		unsubMapCommands?.();
		stopLivePolling();
		cleanupZoomPrevention?.();
	});
</script>

<div class="flex h-[calc(100vh-3.5rem)] lg:h-screen relative" style="overscroll-behavior: none; overflow: hidden;">
	<!-- Map canvas -->
	<div class="flex-1 relative">
		<MapShell height="100%" onReady={onMapReady} />

		<!-- Top bar: scope + search + dropdowns -->
		<div class="absolute top-3 left-3 right-3 z-[1000] flex items-start gap-2">
			<!-- Context scope selector -->
			<select
				value={activeScope}
				onchange={(e) => setScope(e.currentTarget.value)}
				class="px-2.5 py-1.5 rounded-lg bg-card/90 backdrop-blur-sm border border-border shadow-lg text-xs text-foreground min-w-[110px] max-w-[200px]"
			>
				<option value="">Global</option>
				{#if watchTopics.length > 0}
					<optgroup label="Watch Topics">
						{#each watchTopics as t}
							<option value="topic:{t.topic_id}">👁 {t.title}</option>
						{/each}
					</optgroup>
				{/if}
				{#if projects.length > 0}
					<optgroup label="Projects">
						{#each projects as p}
							<option value="project:{p.project_id}">📁 {p.name}</option>
						{/each}
					</optgroup>
				{/if}
				{#if cases.length > 0}
					<optgroup label="Cases">
						{#each cases.filter(c => c.status === 'open' || c.status === 'investigating').slice(0, 10) as c}
							<option value="case:{c.case_id}">📋 {c.title}</option>
						{/each}
					</optgroup>
				{/if}
				{#if scopeContacts.length > 0}
					<optgroup label="Contacts">
						{#each scopeContacts as c}
							<option value="contact:{c.contact_id}">{c.contact_type === 'country' ? '🌍' : '👤'} {c.primary_name}</option>
						{/each}
					</optgroup>
				{/if}
				{#if scopeAssets.length > 0}
					<optgroup label="Assets">
						{#each scopeAssets as a}
							<option value="asset:{a.asset_id}">📦 {a.display_name}</option>
						{/each}
					</optgroup>
				{/if}
			</select>

			<!-- Search -->
			<div class="flex-shrink-0">
				{#if adapter}
					<MapSearch {adapter} />
				{/if}
			</div>

			<!-- Knowledge filter pill — only when knowledge layer is on -->
			{#if layers.knowledge}
				{@const activeCount = knowledgeEntityTypes.size + (knowledgeTimeWindow !== 'all' ? 1 : 0)}
				<div class="relative flex-shrink-0">
					<button
						type="button"
						onclick={() => knowledgeFiltersOpen = !knowledgeFiltersOpen}
						class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border shadow-lg text-xs transition-colors
							{activeCount > 0
								? 'bg-emerald-500/15 border-emerald-500/40 text-emerald-300'
								: 'bg-card/90 backdrop-blur-sm border-border text-muted-foreground hover:text-foreground'}"
						aria-haspopup="true"
						aria-expanded={knowledgeFiltersOpen}
					>
						<span class="w-2 h-2 rounded-full bg-emerald-400"></span>
						Knowledge filters
						{#if activeCount > 0}
							<span class="ml-1 px-1.5 py-px rounded-full text-[9px] bg-emerald-500/30 text-emerald-100">{activeCount}</span>
						{/if}
					</button>

					{#if knowledgeFiltersOpen}
						<div class="absolute right-0 mt-2 w-72 z-[1100] rounded-lg border border-border bg-card/95 backdrop-blur-sm shadow-xl p-3 text-xs space-y-3"
							role="dialog"
							aria-label="Knowledge node filters">
							<!-- Time window -->
							<div>
								<div class="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">Time window</div>
								<div class="flex gap-1">
									{#each [['all', 'All'], ['24h', '24 h'], ['7d', '7 d'], ['30d', '30 d']] as [val, label]}
										<button
											type="button"
											onclick={() => setKnowledgeTimeWindow(val as KWindow)}
											class="flex-1 px-2 py-1 rounded border transition-colors
												{knowledgeTimeWindow === val
													? 'bg-emerald-500/20 border-emerald-500/50 text-emerald-300'
													: 'border-border text-muted-foreground hover:text-foreground hover:bg-muted/30'}">
											{label}
										</button>
									{/each}
								</div>
							</div>

							<!-- Entity types -->
							<div>
								<div class="flex items-center justify-between mb-1.5">
									<div class="text-[10px] uppercase tracking-wider text-muted-foreground">Entity type</div>
									{#if knowledgeEntityTypes.size > 0}
										<button
											type="button"
											onclick={() => { knowledgeEntityTypes = new Set(); syncKnowledgeFiltersToUrl(); if (layers.knowledge) loadGeoNodes(); }}
											class="text-[10px] text-muted-foreground hover:text-foreground">Clear</button>
									{/if}
								</div>
								<div class="flex flex-wrap gap-1">
									{#each KNOWLEDGE_ENTITY_TYPES as t}
										{@const active = knowledgeEntityTypes.has(t)}
										<button
											type="button"
											onclick={() => toggleKnowledgeEntityType(t)}
											class="inline-flex items-center gap-1.5 px-2 py-1 rounded border transition-colors
												{active
													? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-100'
													: 'border-border text-muted-foreground hover:text-foreground hover:bg-muted/30'}">
											<span class="w-2 h-2 rounded-full" style="background:{colorForEntityType(t)}"></span>
											{t}
										</button>
									{/each}
								</div>
							</div>

							<div class="flex items-center justify-between pt-2 border-t border-border/40">
								<span class="text-[10px] text-muted-foreground">{filteredKnowledge.length} in view</span>
								<button
									type="button"
									onclick={clearKnowledgeFilters}
									class="text-[10px] text-muted-foreground hover:text-foreground">Reset</button>
							</div>
						</div>
					{/if}
				</div>
			{/if}

			<!-- Satellite debris pill — only when satellites layer is on -->
			{#if liveSources.satellites?.enabled}
				<button
					type="button"
					onclick={toggleSatelliteDebris}
					class="flex-shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border shadow-lg text-xs transition-colors
						{showSatelliteDebris
							? 'bg-orange-500/15 border-orange-500/40 text-orange-300'
							: 'bg-card/90 backdrop-blur-sm border-border text-muted-foreground hover:text-foreground'}"
					title="Toggle ~7k debris + rocket bodies from Cosmos 1408, Fengyun 1C, Iridium-Cosmos, Briz-M">
					<span class="w-2 h-2 rounded-full bg-orange-400"></span>
					{showSatelliteDebris ? 'Debris on' : 'Show debris'}
				</button>
			{/if}

			<!-- Spacer -->
			<div class="flex-1"></div>

			<!-- Signal count badge -->
			{#if !loading}
				{@const totalVisible = filteredSignals.length + Object.values(liveData).reduce((sum, items) => sum + items.length, 0)}
				{#if totalVisible > 0}
					<div class="px-2.5 py-1 rounded-lg bg-card/90 backdrop-blur-sm border border-border shadow-lg text-xs text-foreground">
						<span class="font-medium">{totalVisible}</span>
						<span class="text-muted-foreground ml-1">in view</span>
					</div>
				{/if}
			{:else}
				<div class="px-2.5 py-1 rounded-lg bg-card/90 backdrop-blur-sm border border-border shadow-lg text-xs text-muted-foreground animate-pulse">
					Loading...
				</div>
			{/if}

			<!-- Layers dropdown (right) -->
			<MapLayersDropdown
				{layers}
				onLayerToggle={toggleLayer}
				signalCount={filteredSignals.length}
				trackCount={tracks.length}
				geofenceCount={geofences.length}
				knowledgeCount={filteredKnowledge.length}
				annotationCount={annotations.length}
				{liveSources}
				onLiveToggle={toggleLiveSource}
				{weatherOverlays}
				onWeatherOverlayToggle={toggleWeatherOverlay}
					{sentinelOverlays}
					onSentinelOverlayToggle={toggleSentinelOverlay}
					{sentinelConfigured}
			{refLayerConfigs}
			{refLayerEnabled}
			onRefLayerToggle={toggleRefLayer}
				{showHeatmap}
				onHeatmapToggle={() => {
					showHeatmap = !showHeatmap;
					const rawMap = adapter?.getRawMap();
					if (rawMap) {
						try { rawMap.setLayoutProperty('context-heatmap', 'visibility', showHeatmap ? 'visible' : 'none'); } catch {}
					}
				}}
				hasContextEntities={contextMarkerHandles.length > 0}
				{showNewsHeatmap}
				onNewsHeatmapToggle={() => { /* news heatmap removed */ }}
				{newsHeatmapWindowHours}
				onNewsHeatmapWindowChange={() => { /* news heatmap removed */ }}
				signalTypes={availableSignalTypes}
				activeSignalTypes={signalTypeFilter}
				onTypeToggle={toggleSignalType}
				{severities}
				activeSeverities={severityFilter}
				onSeverityToggle={toggleSeverity}
				devices={availableDevices}
				activeDevice={deviceFilter}
				onDeviceChange={(d) => deviceFilter = d}
				{getDeviceName}
				onClearFilters={clearFilters}
				{activeFilterCount}
			/>
		</div>

		<!-- MapLibre nav controls render at bottom-right via addControl -->

		<!-- Sentinel-2 legend — appears when an ESA imagery overlay is active -->
		{#if activeLegends.length > 0}
			<div class="absolute bottom-4 left-4 z-[1000] w-56 rounded-lg border border-border bg-card/95 backdrop-blur-sm shadow-xl p-2.5 space-y-2 max-h-[60vh] overflow-y-auto">
				<div class="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground/70">
					<span class="w-1.5 h-1.5 rounded-full bg-sky-400"></span> Legend · what colors mean
				</div>
				{#each activeLegends as lg}
					<div class="space-y-1">
						<p class="text-[11px] font-medium text-foreground">{lg.label}</p>
						{#if lg.kind === 'ramp'}
							<div class="h-2 rounded" style="background:{lg.gradient}"></div>
							<div class="flex justify-between text-[9px] text-muted-foreground">
								<span>{lg.low}</span><span>{lg.high}</span>
							</div>
						{:else if lg.kind === 'cat'}
							<div class="grid grid-cols-2 gap-x-2 gap-y-0.5">
								{#each lg.items ?? [] as it}
									<div class="flex items-center gap-1.5">
										<span class="w-2.5 h-2.5 rounded-sm shrink-0 border border-white/20" style="background:{it.c}"></span>
										<span class="text-[9px] text-muted-foreground truncate">{it.t}</span>
									</div>
								{/each}
							</div>
						{:else}
							<p class="text-[9px] text-muted-foreground">{lg.note}</p>
						{/if}
					</div>
				{/each}
			</div>
		{/if}

		<!-- Bottom toolbar -->
		<div class="absolute bottom-4 left-1/2 -translate-x-1/2 z-[1000] flex items-center gap-2">
			{#if activeScope && timelineEvents.length > 0}
				<TimelinePlayback
					events={timelineEvents}
					onTimeChange={onPlaybackTimeChange}
					onEventClick={(evt) => {
						if (evt.latitude && evt.longitude && adapter) {
							adapter.flyTo(evt.latitude, evt.longitude, 8);
						}
					}}
				/>
			{:else}
				<TimelineSlider onRangeChange={(range) => { timeFilter = range; loadGeoSignals(); }} />
			{/if}
			{#if adapter}
				<DrawingToolbar {adapter} projectId={''} onAnnotationCreated={() => loadSupportingData()} onGeofenceCreated={() => loadSupportingData()} />
				<ImportButton projectId={''} onImported={() => loadSupportingData()} />
				<ScreenshotButton {adapter} />
			{/if}
		</div>

		<!-- Sublayers -->
		{#if adapter && knowledgeEdges.length > 0}
			<KnowledgeEdgeLayer {adapter} nodes={knowledgeNodes} edges={knowledgeEdges} />
		{/if}
		<!-- News-mention heatmap removed June 3 2026.
		     Data was correct (entities geocoded to their actual centroids:
		     Iran at 32.4/53.7, US at 39.8/-98.6, etc.) but the UX
		     overpromised — hovering returned articles that mention ANY
		     entity near here, not articles ABOUT this location. Country
		     mentions also collapse to a single centroid, hiding precision.
		     News goes on /feed; map layers stay focused on genuinely
		     spatial overlays (Tracks, Aircraft, Vessels). -->
		{#if adapter && layers.tracks && tracks.length > 0}
			<TrackLayer map={adapter.getRawMap()} {adapter} {tracks} />
		{/if}
		{#if adapter && layers.geofences && geofences.length > 0}
			<GeofenceLayer map={adapter.getRawMap()} {adapter} {geofences} />
		{/if}
		{#if adapter && layers.annotations}
			<AnnotationLayer {adapter} {annotations} />
		{/if}
		{#if adapter}
			<ReferenceLayer
				{adapter}
				showCountries={layers.countries}
				showCapitals={layers.capitals}
				showRivers={layers.rivers}
				showLakes={layers.lakes}
			/>
		{/if}
		{#if adapter}
			<EntityAssetsLayer
				{adapter}
				visible={layers.entity_assets}
				onEntitySelect={(nodeId) => goto(`/nodes/${nodeId}`)}
			/>
		{/if}
	</div>

	<!-- Detail panel (right side desktop, bottom sheet mobile) -->
	{#if selectedItem && selectedIsNode && !selectedItem.entity_type}
		<LocationInfoCard
			node={selectedItem}
			onClose={() => { selectedItem = null; selectedIsNode = false; }}
			onFlyTo={(lat, lon) => adapter?.flyTo(lat, lon, 14)}
		/>
	{:else if selectedItem}
		<MapDetailPanel
			item={selectedItem}
			isLive={selectedIsLive}
			isNode={selectedIsNode}
			liveSource={selectedLiveSource}
			onClose={() => { selectedItem = null; selectedIsLive = false; selectedIsNode = false; }}
			onAddToCase={handleAddToCase}
			onShowTrail={selectedIsLive ? handleShowTrail : undefined}
			onWatch={selectedIsLive ? handleWatch : undefined}
			onFlyTo={(lat, lon) => adapter?.flyTo(lat, lon, 14)}
		/>
	{/if}
</div>

<!-- Add Observation Modal -->
{#if addObservationAt}
	<AddObservationModal
		latlng={addObservationAt}
		{projects}
		onCreated={() => { addObservationAt = null; loadSupportingData(); }}
		onCancel={() => addObservationAt = null}
	/>
{/if}

<!-- Sky View Panel -->
{#if skyViewAt}
	<SkyViewPanel
		lat={skyViewAt.lat}
		lon={skyViewAt.lon}
		onClose={() => skyViewAt = null}
	/>
{/if}
